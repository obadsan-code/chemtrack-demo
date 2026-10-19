import os
import re
import io
import warnings
from datetime import datetime, time, timedelta
import pytz
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.warnings import PTBUserWarning
import psycopg2
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Silenciar el aviso conocido de PTB sobre el parametro 'days' de run_daily.
# Es solo informativo (recuerda que desde v20 el esquema de dias cambio a estilo cron,
# 0=domingo...6=sabado); nuestro codigo ya usa ese esquema correctamente (sabado=6).
warnings.filterwarnings(
    action="ignore",
    message=r".*cron's weekday scheme.*",
    category=PTBUserWarning,
)
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.enums import TA_CENTER, TA_LEFT

CST = pytz.timezone('America/Mexico_City')

TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Telegram IDs por rol, definidos por variables de entorno (lista separada por comas)
def _ids(nombre):
    return [x.strip() for x in os.environ.get(nombre, "").split(",") if x.strip()]

ADMIN = _ids("ADMIN_IDS")
SUPERVISOR = _ids("SUPERVISOR_IDS")
OPERATOR_A = _ids("OPERATOR_A_IDS")
OPERATOR_B = _ids("OPERATOR_B_IDS")
OPERATOR_C = _ids("OPERATOR_C_IDS")

# IDs que reciben el resumen diario
RESUMEN_IDS = _ids("SUMMARY_IDS") or ADMIN

# Todos los IDs del equipo (para notificaciones de pedidos)
TODOS_IDS = list(dict.fromkeys(ADMIN + SUPERVISOR + OPERATOR_A + OPERATOR_B + OPERATOR_C))

def get_rol(user_id):
    uid = str(user_id)
    if uid in ADMIN:
        return 'admin'
    if uid in SUPERVISOR:
        return 'supervisor'
    if uid in OPERATOR_A:
        return 'operator_a'
    if uid in OPERATOR_B:
        return 'operator_b'
    if uid in OPERATOR_C:
        return 'operator_c'
    return None

def conectar():
    return psycopg2.connect(DATABASE_URL)

def ahora():
    return datetime.now(pytz.utc).astimezone(CST)

def fmt_lote(lote):
    if len(lote) >= 10:
        return f"{lote[:2]}-{lote[2:8]}-{lote[8:]}"
    return lote

def sep():
    return "\u2501" * 22

def obtener_precio(cur, producto_id, cliente_id=None):
    """Devuelve (precio_unitario, moneda) para un producto, priorizando precio
    especifico de cliente sobre el precio general. None, None si no hay precio."""
    if cliente_id:
        cur.execute(
            "SELECT precio_unitario, moneda FROM precios_productos WHERE producto_id = %s AND cliente_id = %s",
            (producto_id, cliente_id)
        )
        row = cur.fetchone()
        if row:
            return float(row[0]), row[1]
    cur.execute(
        "SELECT precio_unitario, moneda FROM precios_productos WHERE producto_id = %s AND cliente_id IS NULL",
        (producto_id,)
    )
    row = cur.fetchone()
    if row:
        return float(row[0]), row[1]
    return None, None

def fmt_moneda(monto, moneda):
    simbolo = "$" if moneda == "USD" else "MX$"
    return f"{simbolo}{monto:,.2f} {moneda}"

# IVA que se suma al total de los pedidos (los montos guardados en la BD son SIN IVA)
IVA_TASA = 0.16

def texto_totales_iva(montos):
    """montos = {moneda: subtotal}. Devuelve lineas Subtotal / IVA 16% / Total por moneda."""
    out = ""
    for moneda, monto in montos.items():
        sub = round(float(monto), 2)
        iva = round(sub * IVA_TASA, 2)
        total = round(sub + iva, 2)
        out += f"   Subtotal: {fmt_moneda(sub, moneda)}\n"
        out += f"   IVA 16%: {fmt_moneda(iva, moneda)}\n"
        out += f"   *Total: {fmt_moneda(total, moneda)}*\n"
    return out

# Umbral: cantidades mayores a esto piden confirmacion (para atrapar "un cero de mas")
LIMITE_CANTIDAD = 2000

def validar_cantidad(texto):
    """Valida una cantidad tecleada.
    Devuelve (ok, valor, mensaje_error, necesita_confirmar).
    - ok=False con mensaje: rechazar y pedir de nuevo.
    - necesita_confirmar=True: la cantidad es valida pero se ve alta, pedir SI.
    """
    try:
        valor = float(texto.replace(",", "").strip())
    except:
        return (False, None, "Escribe un numero valido (solo digitos):", False)
    if valor <= 0:
        return (False, None, "La cantidad debe ser mayor a cero. Escribe de nuevo:", False)
    if valor > LIMITE_CANTIDAD:
        return (True, valor, None, True)
    return (True, valor, None, False)


# ===== ALERTA: materias primas que se van a agotar pronto =====
DIAS_VENTANA_CONSUMO = 14   # sobre cuantos dias medimos el ritmo
DIAS_ALERTA_AGOTAMIENTO = 7  # avisar si quedan estos dias o menos

def calcular_mp_por_agotarse():
    """Calcula que MP se agotara en <= DIAS_ALERTA_AGOTAMIENTO dias
    segun el consumo promedio de los ultimos DIAS_VENTANA_CONSUMO dias.
    Devuelve lista de dicts: {nombre, stock, unidad, consumo_diario, dias_restantes}.
    """
    resultado = []
    try:
        conn = conectar()
        cur = conn.cursor()
        # Consumo de MP en la ventana: sumar (cantidad producida de cada PT x cantidad de MP en su receta / base)
        # 1. Cuanto se produjo de cada PT en la ventana
        cur.execute("""
            SELECT producto_id, COALESCE(SUM(cantidad_inicial), 0) AS producido
            FROM lotes_pt
            WHERE fecha_produccion >= (CURRENT_DATE - %s::int)
            GROUP BY producto_id
        """, (DIAS_VENTANA_CONSUMO,))
        producido_por_pt = {row[0]: float(row[1]) for row in cur.fetchall()}

        # 2. Para cada MP, sumar el consumo derivado de esas producciones segun receta activa
        consumo_mp = {}  # mp_id -> kg consumidos en la ventana
        if producido_por_pt:
            cur.execute("""
                SELECT r.producto_id, ri.materia_prima_id, ri.cantidad, r.cantidad_base
                FROM recetas r
                JOIN receta_items ri ON ri.receta_id = r.id
                WHERE r.activa = TRUE
            """)
            for prod_id, mp_id, cant_receta, base in cur.fetchall():
                producido = producido_por_pt.get(prod_id, 0)
                if producido > 0 and base and float(base) > 0:
                    factor = producido / float(base)
                    consumo = float(cant_receta) * factor
                    consumo_mp[mp_id] = consumo_mp.get(mp_id, 0) + consumo

        # 3. Para cada MP con consumo, calcular dias restantes
        if consumo_mp:
            cur.execute("SELECT id, nombre, stock_actual, unidad FROM materias_primas WHERE activo = TRUE")
            for mp_id, nombre, stock, unidad in cur.fetchall():
                consumo_total = consumo_mp.get(mp_id, 0)
                if consumo_total <= 0:
                    continue
                consumo_diario = consumo_total / DIAS_VENTANA_CONSUMO
                if consumo_diario <= 0:
                    continue
                stock_f = float(stock)
                dias_restantes = stock_f / consumo_diario
                if dias_restantes <= DIAS_ALERTA_AGOTAMIENTO:
                    resultado.append({
                        "nombre": nombre,
                        "stock": stock_f,
                        "unidad": unidad,
                        "consumo_diario": consumo_diario,
                        "dias_restantes": dias_restantes,
                    })
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error calcular_mp_por_agotarse: {e}")
    # ordenar por los mas urgentes primero
    resultado.sort(key=lambda x: x["dias_restantes"])
    return resultado

def formato_alerta_agotamiento(lista):
    """Devuelve el texto de la alerta, o None si no hay nada que avisar."""
    if not lista:
        return None
    msg = f"\u26a0\ufe0f *MATERIAS PRIMAS POR AGOTARSE*\n{sep()}\n"
    msg += "Segun el consumo de los ultimos 14 dias:\n\n"
    for m in lista:
        dias = m["dias_restantes"]
        if dias < 1:
            tiempo = "menos de 1 dia"
        else:
            tiempo = f"~{dias:.0f} dias"
        msg += f"\U0001f534 *{m['nombre']}*\n"
        msg += f"   Quedan {m['stock']:,.0f} {m['unidad']} \u2192 {tiempo}\n"
        msg += f"   (gasta ~{m['consumo_diario']:,.0f} {m['unidad']}/dia)\n\n"
    msg += "_Estimado segun ritmo reciente. Puede variar._"
    return msg


def registrar_audit(cur, usuario_id, usuario_nombre, accion, detalle):
    try:
        cur.execute(
            "INSERT INTO audit_log (usuario_id, usuario_nombre, accion, detalle) VALUES (%s, %s, %s, %s)",
            (str(usuario_id), usuario_nombre, accion, detalle)
        )
    except Exception as e:
        print(f"Error audit log: {e}")

PEDIDO_CLIENTE, PEDIDO_PRODUCTOS, PEDIDO_CONFIRMAR = range(3)
DEVOLUCION_CLIENTE, DEVOLUCION_CANTIDAD = range(3, 5)
LAVADO_CANTIDAD, LAVADO_QUIEN = range(5, 7)
CLOCK_EMPLEADO, CLOCK_TIPO, CLOCK_FOTO = range(7, 10)
ENTRADA_MATERIAL, ENTRADA_CANTIDAD, ENTRADA_PROVEEDOR, ENTRADA_PRESENTACION, ENTRADA_UBICACION, ENTRADA_CONFIRMAR = range(10, 16)
PROD_PRODUCTO, PROD_CANTIDAD, PROD_CONFIRMAR = range(16, 19)
PROD_FECHA = 73

async def enviar_resumen_diario(context):
    try:
        conn = conectar()
        cur = conn.cursor()
        now = ahora()

        cur.execute("SELECT nombre, stock_actual, unidad, stock_minimo FROM materias_primas WHERE activo = true ORDER BY nombre")
        mp = cur.fetchall()

        cur.execute("SELECT nombre, stock_actual, unidad FROM productos_terminados WHERE activo = true ORDER BY nombre")
        pt = cur.fetchall()

        cur.execute("""
            SELECT p.folio, c.nombre, p.tambos_enviados
            FROM pedidos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE (p.fecha_pedido AT TIME ZONE 'America/Mexico_City')::date = (NOW() AT TIME ZONE 'America/Mexico_City')::date
            ORDER BY p.fecha_pedido DESC
        """)
        pedidos = cur.fetchall()

        cur.execute("""
            SELECT e.nombre, a.tipo, a.fecha
            FROM asistencia a
            JOIN empleados e ON e.id = a.empleado_id
            WHERE (a.fecha AT TIME ZONE 'America/Mexico_City')::date = (NOW() AT TIME ZONE 'America/Mexico_City')::date
            ORDER BY a.fecha ASC
        """)
        asistencia = cur.fetchall()

        cur.execute("SELECT COALESCE(SUM(tambos_pendientes), 0) FROM clientes")
        tambos_calle = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'por_lavar'")
        por_lavar = cur.fetchone()[0]

        cur.close()
        conn.close()

        mensaje = f"\U0001f4ca *RESUMEN DIARIO CHEMTRACK*\n"
        mensaje += f"\U0001f4c5 {now.strftime('%d/%m/%Y')} \u2014 {now.strftime('%H:%M')}\n"
        mensaje += f"{sep()}\n\n"

        mp_problemas = [(n, s, u, m) for n, s, u, m in mp if float(s) <= float(m)]
        if mp_problemas:
            mensaje += f"\u26a0\ufe0f *MATERIAS PRIMAS BAJAS/AGOTADAS*\n"
            for nombre, stock, unidad, minimo in mp_problemas:
                if float(stock) <= 0:
                    emoji = "\U0001f534"
                else:
                    emoji = "\U0001f7e1"
                mensaje += f"{emoji} {nombre}: {stock} {unidad}\n"
            mensaje += "\n"

        mensaje += f"\U0001f3ed *PRODUCTO TERMINADO*\n"
        for nombre, stock, unidad in pt:
            mensaje += f"\U0001f7e2 {nombre}: *{stock} {unidad}*\n"
        mensaje += "\n"

        mensaje += f"\U0001f69a *PEDIDOS HOY*\n"
        if pedidos:
            for folio, cliente, tambos in pedidos:
                mensaje += f"\U0001f4cb {folio} \u2014 {cliente} \u2014 {tambos} tambos\n"
        else:
            mensaje += "_Sin pedidos hoy_\n"
        mensaje += "\n"

        mensaje += f"\U0001f553 *ASISTENCIA HOY*\n"
        if asistencia:
            empleados_hoy = {}
            for nombre, tipo, fecha in asistencia:
                if nombre not in empleados_hoy:
                    empleados_hoy[nombre] = []
                empleados_hoy[nombre].append(tipo)
            for nombre, tipos in empleados_hoy.items():
                flecha = " \u2192 "
                mensaje += f"\U0001f464 {nombre}: {flecha.join(tipos)}\n"
        else:
            mensaje += "_Sin registros hoy_\n"
        mensaje += "\n"

        mensaje += f"\U0001f6e2\ufe0f *TAMBOS*\n"
        mensaje += f"En calle: *{tambos_calle}*\n"
        mensaje += f"Por lavar: *{por_lavar}*\n"

        # Proyeccion de agotamiento de MP (ritmo ultimos 14 dias)
        try:
            por_agotarse = calcular_mp_por_agotarse()
            if por_agotarse:
                mensaje += f"\n{sep()}\n"
                mensaje += "\u23f3 *SE VAN A AGOTAR PRONTO*\n"
                for m in por_agotarse:
                    dias = m["dias_restantes"]
                    tiempo = "menos de 1 dia" if dias < 1 else f"~{dias:.0f} dias"
                    mensaje += f"\U0001f534 {m['nombre']}: quedan {m['stock']:,.0f} {m['unidad']} ({tiempo})\n"
        except Exception as e:
            print(f"Error proyeccion en resumen: {e}")

        for chat_id in RESUMEN_IDS:
            await context.bot.send_message(chat_id=int(chat_id), text=mensaje, parse_mode="Markdown")

    except Exception as e:
        print(f"Error en resumen diario: {e}")

async def reporte_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol not in ['admin']:
        await update.message.reply_text("\u274c Sin acceso.")
        return ConversationHandler.END
    teclado = [
        ["Hoy", "3 dias"],
        ["7 dias", "14 dias"],
        ["Mes", "Año"],
        ["Cancelar"]
    ]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f4ca *REPORTE CHEMTRACK*\n{sep()}\nSelecciona el periodo:",
        reply_markup=menu, parse_mode="Markdown"
    )
    return REPORTE_PERIODO

async def reporte_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END

    now = ahora()

    # Definir rango de fechas
    if texto == "Hoy":
        desde = now.replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Hoy {now.strftime('%d/%m/%Y')}"
    elif texto == "3 dias":
        desde = (now - __import__('datetime').timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Ultimos 3 dias"
    elif texto == "7 dias":
        desde = (now - __import__('datetime').timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Ultimos 7 dias"
    elif texto == "14 dias":
        desde = (now - __import__('datetime').timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Ultimos 14 dias"
    elif texto == "Mes":
        desde = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Mes de {now.strftime('%B %Y')}"
    elif texto == "Año":
        desde = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Año {now.strftime('%Y')}"
    else:
        await update.message.reply_text("Selecciona un periodo de la lista.")
        return REPORTE_PERIODO

    await update.message.reply_text(f"\U0001f550 Generando reporte...", reply_markup=ReplyKeyboardRemove())

    try:
        conn = conectar()
        cur = conn.cursor()

        # Inventario PT actual
        cur.execute("SELECT nombre, stock_actual, unidad FROM productos_terminados WHERE activo = true ORDER BY nombre")
        pt = cur.fetchall()

        # Alertas MP
        cur.execute("SELECT nombre, stock_actual, unidad, stock_minimo FROM materias_primas WHERE activo = true AND stock_actual <= stock_minimo ORDER BY nombre")
        mp_alertas = cur.fetchall()

        # Pedidos en el periodo
        cur.execute("""
            SELECT p.folio, c.nombre, p.tambos_enviados, p.fecha_pedido,
                   COALESCE(SUM(pi.cantidad), 0) as kg_total
            FROM pedidos p
            JOIN clientes c ON c.id = p.cliente_id
            LEFT JOIN pedido_items pi ON pi.pedido_id = p.id
            WHERE (p.fecha_pedido AT TIME ZONE 'America/Mexico_City') >= %s
            GROUP BY p.id, p.folio, c.nombre, p.tambos_enviados, p.fecha_pedido
            ORDER BY p.fecha_pedido DESC
        """, (desde,))
        pedidos = cur.fetchall()

        # Produccion en el periodo
        cur.execute("""
            SELECT pt.nombre, l.numero_lote, l.cantidad_inicial, l.fecha_produccion
            FROM lotes_pt l
            JOIN productos_terminados pt ON pt.id = l.producto_id
            WHERE l.fecha_produccion >= %s::date
            ORDER BY l.fecha_produccion DESC
        """, (desde,))
        produccion = cur.fetchall()

        # Tambos en el periodo
        cur.execute("""
            SELECT tipo, SUM(cantidad)
            FROM tambos_movimientos
            WHERE fecha >= %s
            GROUP BY tipo
        """, (desde,))
        tambos_mov = {m[0]: int(m[1]) for m in cur.fetchall()}
        cur.execute("SELECT COALESCE(SUM(tambos_pendientes), 0) FROM clientes")
        tambos_calle = int(cur.fetchone()[0])

        cur.close()
        conn.close()

        # Construir mensaje
        msg = f"\U0001f4ca *REPORTE CHEMTRACK*\n"
        msg += f"\U0001f4c5 {etiqueta}\n"
        msg += f"\U0001f552 Generado: {now.strftime('%d/%m/%Y %H:%M')}\n"
        msg += f"{sep()}\n\n"

        # Inventario PT
        msg += f"\U0001f3ed *INVENTARIO PT ACTUAL*\n"
        for nombre, stock, unidad in pt:
            emoji = "\U0001f534" if float(stock) <= 0 else "\U0001f7e2"
            msg += f"{emoji} {nombre}: *{float(stock):,.0f} {unidad}*\n"
        msg += "\n"

        # Alertas MP
        if mp_alertas:
            msg += f"\u26a0\ufe0f *ALERTAS MATERIA PRIMA*\n"
            for nombre, stock, unidad, minimo in mp_alertas:
                emoji = "\U0001f534" if float(stock) <= 0 else "\U0001f7e1"
                msg += f"{emoji} {nombre}: {float(stock):,.1f} {unidad}\n"
            msg += "\n"

        # Pedidos
        total_pedidos = len(pedidos)
        total_kg = sum(float(p[4]) for p in pedidos)
        total_tambos_env = sum(int(p[2]) for p in pedidos)
        msg += f"\U0001f69a *PEDIDOS* ({total_pedidos} pedido{'s' if total_pedidos != 1 else ''})\n"
        if pedidos:
            msg += f"Total kg despachados: *{total_kg:,.0f} kg*\n"
            msg += f"Total tambos enviados: *{total_tambos_env}*\n"
            msg += "\n"
            for folio, cliente, tambos, fecha, kg in pedidos:
                fecha_cst = fecha.astimezone(CST)
                msg += f"\U0001f4cb {folio} \u2014 {cliente}\n"
                msg += f"   {float(kg):,.0f} kg \u00b7 {tambos} tambos \u00b7 {fecha_cst.strftime('%d/%m %H:%M')}\n"
        else:
            msg += "_Sin pedidos en este periodo_\n"
        msg += "\n"

        # Produccion
        total_prod_kg = sum(float(p[2]) for p in produccion)
        msg += f"\u2699\ufe0f *PRODUCCION* ({len(produccion)} lote{'s' if len(produccion) != 1 else ''})\n"
        if produccion:
            msg += f"Total producido: *{total_prod_kg:,.0f} kg*\n\n"
            for nombre, lote, kg, fecha in produccion:
                msg += f"\U0001f9ea {nombre}: {float(kg):,.0f} kg\n"
                msg += f"   {fmt_lote(lote)} \u00b7 {fecha.strftime('%d/%m/%Y')}\n"
        else:
            msg += "_Sin produccion en este periodo_\n"
        msg += "\n"

        # Tambos
        entradas = tambos_mov.get('entrada', 0)
        salidas = tambos_mov.get('salida', 0)
        devoluciones = tambos_mov.get('devolucion', 0)
        msg += f"\U0001f6e2\ufe0f *TAMBOS*\n"
        msg += f"Entradas: *+{entradas}*\n"
        msg += f"Salidas: *-{salidas}*\n"
        msg += f"Devoluciones: *+{devoluciones}*\n"
        msg += f"En calle ahora: *{tambos_calle}*\n"

        await update.message.reply_text(msg, parse_mode="Markdown")
        await start(update, context)
        return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"\u274c Error generando reporte: {e}")
        await start(update, context)
        return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    rol = get_rol(user_id)

    if rol is None:
        await update.message.reply_text("\u274c No tienes acceso a este sistema.\nContacta al administrador.")
        return

    if rol == 'admin':
        teclado = [
            ["\U0001f4e6 Entrada MP", "\u2699\ufe0f Produccion"],
            ["\U0001f69a Salida Pedido", "\U0001f50d Consultar stock"],
            ["\U0001f3ed Producto terminado", "\U0001f6e2\ufe0f Tambos"],
            ["\u270f\ufe0f Editar inventario", "\U0001f553 Asistencia"],
            ["\U0001f4ca Reportes", "\u2699\ufe0f Admin"],
            ["\U0001f4c4 Reporte PDF"]
        ]
    elif rol == 'supervisor':
        teclado = [
            ["\U0001f50d Consultar stock", "\U0001f3ed Producto terminado"],
            ["\U0001f6e2\ufe0f Tambos", "\U0001f553 Asistencia"]
        ]
    elif rol in ['operator_b', 'operator_c', 'operator_a']:
        teclado = [
            ["\U0001f6e2\ufe0f Tambos", "\U0001f553 Asistencia"]
        ]

    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        "\U0001f9ea *Chemtrack*\n" + sep() + "\nSelecciona una opcion:",
        reply_markup=menu, parse_mode="Markdown"
    )

async def mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nombre = update.message.from_user.first_name
    await update.message.reply_text(
        f"\U0001f194 *Tu Telegram ID*\n{sep()}\n"
        f"\U0001f464 {nombre}\n"
        f"\U0001f522 `{user_id}`\n\n"
        f"Comparte este numero con el administrador para activar tu acceso.",
        parse_mode="Markdown"
    )

async def consultar_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol is None:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT nombre, stock_actual, unidad, stock_minimo FROM materias_primas ORDER BY nombre")
        filas = cur.fetchall()
        cur.close()
        conn.close()
        mensaje = f"\U0001f4ca *INVENTARIO MATERIA PRIMA*\n{sep()}\n"
        for nombre, stock, unidad, minimo in filas:
            if float(stock) <= 0:
                emoji, estado = "\U0001f534", " _AGOTADO_"
            elif float(stock) <= float(minimo):
                emoji, estado = "\U0001f7e1", " _BAJO_"
            else:
                emoji, estado = "\U0001f7e2", ""
            mensaje += f"{emoji} *{nombre}*: {stock} {unidad}{estado}\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

async def consultar_pt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol is None:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.nombre, p.stock_actual, p.unidad, p.stock_minimo,
                   l.numero_lote, l.cantidad_actual, l.presentacion, l.fecha_produccion
            FROM productos_terminados p
            LEFT JOIN lotes_pt l ON l.producto_id = p.id AND l.cantidad_actual > 0
            ORDER BY p.nombre, l.fecha_produccion ASC
        """)
        filas = cur.fetchall()
        cur.close()
        conn.close()
        mensaje = f"\U0001f3ed *PRODUCTO TERMINADO*\n{sep()}\n"
        producto_actual = None
        for nombre, stock, unidad, minimo, lote, cant_lote, presentacion, fecha in filas:
            if nombre != producto_actual:
                if producto_actual is not None:
                    mensaje += "\n"
                producto_actual = nombre
                if float(stock) <= 0:
                    emoji = "\U0001f534"
                elif float(stock) <= float(minimo):
                    emoji = "\U0001f7e1"
                else:
                    emoji = "\U0001f7e2"
                mensaje += f"{emoji} *{nombre}* \u2014 {stock} {unidad}\n"
            if lote:
                mensaje += f"   \U0001f4e6 {fmt_lote(lote)}\n"
                mensaje += f"   {cant_lote} kg \u00b7 {presentacion}\n"
                mensaje += f"   \U0001f4c5 {fecha.strftime('%d/%m/%Y')}\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

def guardar_receta_historico(cur, receta_id, producto_id, producto_nombre, cantidad_base, motivo, usuario_nombre):
    """Registra cambios de receta en tabla historico"""
    try:
        # Obtener snapshot actual de ingredientes
        cur.execute("""
            SELECT string_agg(mp.nombre || ': ' || ri.cantidad || ' ' || mp.unidad, ' | ' ORDER BY mp.nombre)
            FROM receta_items ri
            JOIN materias_primas mp ON mp.id = ri.materia_prima_id
            WHERE ri.receta_id = %s
        """, (receta_id,))
        snapshot = cur.fetchone()[0] or "sin ingredientes"
        cur.execute("""
            INSERT INTO recetas_historico (receta_id, producto_id, producto_nombre, cantidad_base, ingredientes_snapshot, cambio_por, motivo)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (receta_id, producto_id, producto_nombre, cantidad_base, snapshot, usuario_nombre, motivo))
    except Exception as e:
        print(f"Error guardando historico: {e}")

async def consultar_recetas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /recetas \u2014 lista todas las f\u00f3rmulas de producci\u00f3n"""
    rol = get_rol(str(update.message.from_user.id))
    if rol is None:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT r.id, pt.nombre, r.cantidad_base, r.activa,
                   string_agg(mp.nombre || ': ' || ri.cantidad || ' ' || mp.unidad, ' | ' ORDER BY mp.nombre)
            FROM recetas r
            JOIN productos_terminados pt ON pt.id = r.producto_id
            LEFT JOIN receta_items ri ON ri.receta_id = r.id
            LEFT JOIN materias_primas mp ON mp.id = ri.materia_prima_id
            GROUP BY r.id, pt.nombre, r.cantidad_base, r.activa
            ORDER BY pt.nombre
        """)
        recetas = cur.fetchall()
        cur.close()
        conn.close()

        if not recetas:
            await update.message.reply_text("\u274c No hay recetas registradas.")
            return

        mensaje = f"\U0001f9ea *FORMULAS DE PRODUCCION*\n{sep()}\n\n"
        for rec_id, nombre, base, activa, ingredientes in recetas:
            estado = "\u2705" if activa else "\u274c"
            mensaje += f"{estado} *{nombre}*\n"
            mensaje += f"   Base: {base} kg\n"
            if ingredientes:
                mensaje += f"   Ingredientes:\n"
                for ing in ingredientes.split(' | '):
                    mensaje += f"      \u2022 {ing}\n"
            else:
                mensaje += f"      _(sin ingredientes)_\n"
            mensaje += "\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

async def tambos_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol not in ['admin', 'supervisor', 'operator_b', 'operator_c']:
        await update.message.reply_text("\u274c No tienes acceso a este modulo.")
        await start(update, context)
        return
    teclado = [["\U0001f4cb Ver tambos", "\U0001f504 Registrar devolucion"],
               ["\U0001f9fc Registrar lavado", "\U0001f4cb Ver por lavar"],
               ["\U0001f4e6 Entrada tambos"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f6e2\ufe0f *CONTROL DE TAMBOS*\n{sep()}\nSelecciona una opcion:",
        reply_markup=menu, parse_mode="Markdown"
    )

async def ver_tambos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT nombre, tambos_pendientes FROM clientes WHERE tambos_pendientes > 0 ORDER BY nombre")
        filas = cur.fetchall()
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'por_lavar'")
        por_lavar = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'listo'")
        listos = cur.fetchone()[0]
        cur.close()
        conn.close()
        mensaje = f"\U0001f6e2\ufe0f *TAMBOS EN CALLE*\n{sep()}\n"
        total = 0
        if filas:
            for nombre, pendientes in filas:
                mensaje += f"\U0001f465 {nombre}: *{pendientes}* tambo(s)\n"
                total += pendientes
        else:
            mensaje += "_Ningun cliente tiene tambos pendientes_\n"
        mensaje += f"\n*Total en calle: {total}*\n\n\U0001f3ed *EN PLANTA*\n{sep()}\n"
        mensaje += f"\U0001f7e1 Por lavar: *{por_lavar}*\n\U0001f7e2 Listos: *{listos}*"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        await start(update, context)
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        await start(update, context)

async def ver_por_lavar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT t.cantidad, t.fecha_ingreso, c.nombre
            FROM tambos_estado t
            LEFT JOIN clientes c ON c.id = t.cliente_id
            WHERE t.estado = 'por_lavar'
            ORDER BY t.fecha_ingreso ASC
        """)
        filas = cur.fetchall()
        cur.close()
        conn.close()
        if not filas:
            await update.message.reply_text("\u2705 No hay tambos pendientes por lavar.")
            await start(update, context)
            return
        mensaje = f"\U0001f9fc *TAMBOS POR LAVAR*\n{sep()}\n"
        total = 0
        for cantidad, fecha, cliente in filas:
            mensaje += f"\U0001f6e2\ufe0f *{cantidad}* tambos de {cliente}\n"
            mensaje += f"   \U0001f4c5 {fecha.strftime('%d/%m/%Y %H:%M')}\n"
            total += cantidad
        mensaje += f"\n*Total: {total} tambos*"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        await start(update, context)
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        await start(update, context)

# CAMBIO 4: Entrada de tambos nuevos (sin origen obligatorio)
ENTRADA_TAMBOS_CANTIDAD = 20
PEDIDO_NOTA = 21
EDIT_TIPO, EDIT_PRODUCTO, EDIT_ACCION, EDIT_VALOR, EDIT_LOTE_SELECT, EDIT_LOTE_VALOR = range(22, 28)
EDIT_LOTE_ACCION, EDIT_LOTE_NUEVO_NUM = 71, 72
EDIT_LOTE_CONFIRM_BORRAR = 74
REPORTE_PERIODO = 28
REPORTE_PDF_PERIODO = 70
# Admin: agregar producto
ADD_TIPO, ADD_NOMBRE, ADD_CODIGO, ADD_UNIDAD, ADD_PRESENTACION, ADD_STOCK, ADD_MINIMO, ADD_CONFIRMAR = range(29, 37)
# Admin: editar datos de producto
EDITP_TIPO, EDITP_PRODUCTO, EDITP_CAMPO, EDITP_VALOR = range(37, 41)
# Admin: stock minimo
MIN_TIPO, MIN_PRODUCTO, MIN_VALOR = range(41, 44)
# Admin: menu principal
ADMIN_MENU = 44
# Admin: activar/desactivar
TOGGLE_TIPO, TOGGLE_PRODUCTO, TOGGLE_CONFIRMAR = range(45, 48)
# Admin: eliminar producto (MP/PT)
DELPROD_TIPO, DELPROD_SELECT, DELPROD_CONFIRM = 75, 76, 77
# Admin: recetas
REC_PRODUCTO, REC_ACCION, REC_BASE, REC_MP_SELECT, REC_MP_CANTIDAD, REC_ITEM_DEL = range(48, 54)
# Admin: cancelar pedido
CANC_FOLIO, CANC_CONFIRMAR = range(54, 56)
# Pedido: cambiar lote manualmente (swap)
PEDIDO_SWAP_PROD, PEDIDO_SWAP_QUITAR, PEDIDO_SWAP_PONER = range(56, 59)
PEDIDO_SWAP_CANTIDAD = 78
# Admin tanda 3: empleados
EMP_MENU, EMP_ADD_NOMBRE, EMP_ADD_ID, EMP_SELECT, EMP_EDIT_CAMPO, EMP_EDIT_VALOR = range(59, 65)
# Admin tanda 3: clientes
CLI_SELECT, CLI_CAMPO, CLI_VALOR = range(65, 68)
# Admin tanda 3: ajuste tambos
TAMB_ESTADO, TAMB_VALOR = range(68, 70)
# Admin: precios de productos
PRECIOS_ACTUALIZAR, = range(71, 72)

async def entrada_tambos_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"\U0001f4e6 *ENTRADA DE TAMBOS*\n{sep()}\nCuantos tambos entran?",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return ENTRADA_TAMBOS_CANTIDAD

async def entrada_tambos_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cantidad = int(update.message.text.strip())
        if cantidad <= 0:
            await update.message.reply_text("La cantidad debe ser mayor a cero.")
            return ENTRADA_TAMBOS_CANTIDAD
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tambos_movimientos (tipo, cantidad, fecha, notas) VALUES (%s, %s, %s, %s)",
            ('entrada', cantidad, now, 'Entrada manual')
        )
        cur.execute(
            "INSERT INTO tambos_estado (cantidad, estado) VALUES (%s, 'listo')",
            (cantidad,)
        )
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "ENTRADA_TAMBOS", f"Cantidad: {cantidad}")
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(
            f"\u2705 *ENTRADA REGISTRADA*\n{sep()}\n"
            f"\U0001f6e2\ufe0f Tambos: *{cantidad}*\n"
            f"\U0001f4c5 {now.strftime('%d/%m/%Y %H:%M')}\n"
            f"\U0001f7e2 Disponibles para usar",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return ENTRADA_TAMBOS_CANTIDAD

async def devolucion_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT nombre, tambos_pendientes FROM clientes ORDER BY nombre")
        filas = cur.fetchall()
        cur.close()
        conn.close()
        mensaje = f"\U0001f504 *REGISTRAR DEVOLUCION*\n{sep()}\n"
        for nombre, pendientes in filas:
            mensaje += f"\U0001f465 {nombre}: {pendientes} tambos\n"
        mensaje += "\nEscribe el nombre del cliente:"
        await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return DEVOLUCION_CLIENTE
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def devolucion_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cliente_nombre = update.message.text.strip()
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, tambos_pendientes FROM clientes WHERE UPPER(nombre) LIKE UPPER(%s)", (f"%{cliente_nombre}%",))
        cliente = cur.fetchone()
        cur.close()
        conn.close()
        if not cliente:
            await update.message.reply_text(f"\u274c No encontre: {cliente_nombre}\nIntenta de nuevo:")
            return DEVOLUCION_CLIENTE
        context.user_data["devolucion_cliente_id"] = cliente[0]
        context.user_data["devolucion_cliente_nombre"] = cliente[1]
        context.user_data["devolucion_pendientes"] = int(cliente[2])
        await update.message.reply_text(
            f"\U0001f465 *{cliente[1]}*\nTambos en calle: {cliente[2]}\n\nCuantos tambos devuelve?",
            parse_mode="Markdown"
        )
        return DEVOLUCION_CANTIDAD
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def devolucion_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cantidad = int(update.message.text.strip())
        if cantidad <= 0:
            await update.message.reply_text("La cantidad debe ser mayor a cero. Intenta de nuevo:")
            return DEVOLUCION_CANTIDAD
        cliente_id = context.user_data["devolucion_cliente_id"]
        cliente_nombre = context.user_data["devolucion_cliente_nombre"]
        pendientes = context.user_data["devolucion_pendientes"]
        tambos_descontar = min(cantidad, pendientes)
        conn = conectar()
        cur = conn.cursor()
        cur.execute("UPDATE clientes SET tambos_pendientes = GREATEST(tambos_pendientes - %s, 0) WHERE id = %s", (cantidad, cliente_id))
        cur.execute("INSERT INTO tambos_movimientos (cliente_id, tipo, cantidad) VALUES (%s, %s, %s)", (cliente_id, "devolucion", cantidad))
        cur.execute("INSERT INTO tambos_estado (cantidad, estado, cliente_id) VALUES (%s, 'por_lavar', %s)", (cantidad, cliente_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "DEVOLUCION_TAMBOS", f"Cliente: {cliente_nombre} | Cantidad: {cantidad}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *DEVOLUCION REGISTRADA*\n{sep()}\n"
            f"\U0001f465 {cliente_nombre}\n"
            f"\U0001f6e2\ufe0f Recibidos: {cantidad}\n"
            f"\U0001f4cd En calle restantes: {max(pendientes - tambos_descontar, 0)}\n"
            f"\U0001f9fc Estado: Por lavar",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        await start(update, context)
        return ConversationHandler.END

async def lavado_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'por_lavar'")
        total = cur.fetchone()[0]
        cur.close()
        conn.close()
        if total == 0:
            await update.message.reply_text("\u2705 No hay tambos pendientes por lavar.")
            await start(update, context)
            return ConversationHandler.END
        await update.message.reply_text(
            f"\U0001f9fc *REGISTRAR LAVADO*\n{sep()}\nTambos por lavar: *{total}*\n\nCuantos tambos se lavaron?",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return LAVADO_CANTIDAD
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def lavado_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cantidad = int(update.message.text.strip())
        if cantidad <= 0:
            await update.message.reply_text("La cantidad debe ser mayor a cero.")
            return LAVADO_CANTIDAD
        context.user_data["lavado_cantidad"] = cantidad
        await update.message.reply_text("\U0001f464 Quien los lavo?")
        return LAVADO_QUIEN
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return LAVADO_CANTIDAD

async def lavado_quien(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        quien = update.message.text.strip()
        cantidad = context.user_data["lavado_cantidad"]
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, cantidad FROM tambos_estado WHERE estado = 'por_lavar' ORDER BY fecha_ingreso ASC")
        pendientes = cur.fetchall()
        restante = cantidad
        for tambo_id, cant_tambo in pendientes:
            if restante <= 0:
                break
            cant_tambo = int(cant_tambo)
            if cant_tambo <= restante:
                cur.execute("UPDATE tambos_estado SET estado = 'listo', fecha_lavado = %s, lavado_por = %s WHERE id = %s", (now, quien, tambo_id))
                restante -= cant_tambo
            else:
                cur.execute("UPDATE tambos_estado SET cantidad = cantidad - %s WHERE id = %s", (restante, tambo_id))
                cur.execute("INSERT INTO tambos_estado (cantidad, estado, fecha_lavado, lavado_por) VALUES (%s, 'listo', %s, %s)", (restante, now, quien))
                restante = 0
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "LAVADO_TAMBOS", f"Cantidad: {cantidad} | Por: {quien}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *LAVADO REGISTRADO*\n{sep()}\n"
            f"\U0001f9fc Tambos: *{cantidad}*\n"
            f"\U0001f464 Por: {quien}\n"
            f"\U0001f4c5 {now.strftime('%d/%m/%Y %H:%M')}\n"
            f"\U0001f7e2 Listos para usar",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        await start(update, context)
        return ConversationHandler.END

# CAMBIO 5: Reporte semanal de tambos
async def reporte_tambos_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol not in ['admin']:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT tipo, SUM(cantidad)
            FROM tambos_movimientos
            WHERE fecha >= date_trunc('week', NOW() AT TIME ZONE 'America/Mexico_City')
              AND fecha < date_trunc('week', NOW() AT TIME ZONE 'America/Mexico_City') + interval '7 days'
            GROUP BY tipo
        """)
        movs = cur.fetchall()
        cur.execute("SELECT COALESCE(SUM(tambos_pendientes), 0) FROM clientes")
        en_calle = cur.fetchone()[0]
        cur.close()
        conn.close()

        totales = {m[0]: int(m[1]) for m in movs}
        entradas = totales.get('entrada', 0)
        salidas = totales.get('salida', 0)
        devoluciones = totales.get('devolucion', 0)
        neto = entradas + devoluciones - salidas
        now = ahora()

        mensaje = (
            f"\U0001f6e2\ufe0f *TAMBOS \u2014 SEMANA {now.strftime('%V')}*\n{sep()}\n\n"
            f"\U0001f7e2 Entradas (compra): *+{entradas}*\n"
            f"\U0001f534 Salidas (pedidos): *-{salidas}*\n"
            f"\U0001f504 Devoluciones: *+{devoluciones}*\n"
            f"{sep()}\n"
            f"Neto: *{'+' if neto >= 0 else ''}{neto}*\n\n"
            f"\U0001f4cd En calle ahora: *{en_calle}*"
        )
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

async def produccion_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c No tienes acceso a registrar produccion.")
        await start(update, context)
        return ConversationHandler.END
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT pt.id, pt.nombre, pt.stock_actual, r.id, r.cantidad_base, pt.codigo
            FROM productos_terminados pt
            JOIN recetas r ON r.producto_id = pt.id AND r.activa = true
            WHERE pt.activo = true
            ORDER BY pt.nombre
        """)
        productos = cur.fetchall()
        cur.close()
        conn.close()
        if not productos:
            await update.message.reply_text("\u274c No hay productos con formula registrada.")
            await start(update, context)
            return ConversationHandler.END
        context.user_data["prod_productos"] = productos
        teclado = [[p[1]] for p in productos]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            f"\u2699\ufe0f *REGISTRO DE PRODUCCION*\n{sep()}\nSelecciona el producto a fabricar:",
            reply_markup=menu, parse_mode="Markdown"
        )
        return PROD_PRODUCTO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def produccion_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("prod_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("No encontre ese producto. Selecciona de la lista.")
        return PROD_PRODUCTO
    context.user_data["prod_id"] = producto[0]
    context.user_data["prod_nombre"] = producto[1]
    context.user_data["prod_stock"] = float(producto[2])
    context.user_data["prod_receta_id"] = producto[3]
    context.user_data["prod_base"] = float(producto[4])
    context.user_data["prod_codigo"] = producto[5]
    await update.message.reply_text(
        f"\U0001f9ea *{producto[1]}*\n"
        f"Stock actual: {producto[2]} kg\n"
        f"Formula base: {producto[4]} kg\n\n"
        f"Cuantos kg vas a fabricar?",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return PROD_CANTIDAD

async def produccion_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ok, cantidad, err, confirmar = validar_cantidad(update.message.text)
        if not ok:
            await update.message.reply_text(err)
            return PROD_CANTIDAD
        context.user_data["prod_cant_alta"] = confirmar
        receta_id = context.user_data["prod_receta_id"]
        base = context.user_data["prod_base"]
        factor = cantidad / base
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT mp.nombre, mp.stock_actual, mp.unidad, ri.cantidad, mp.id
            FROM receta_items ri
            JOIN materias_primas mp ON mp.id = ri.materia_prima_id
            WHERE ri.receta_id = %s
        """, (receta_id,))
        items = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["prod_cantidad"] = cantidad
        context.user_data["prod_items"] = [(n, float(s), u, float(c), i) for n, s, u, c, i in items]
        context.user_data["prod_factor"] = factor
        prod_nombre = context.user_data["prod_nombre"]
        mensaje = f"\u2699\ufe0f *VISTA PREVIA DE PRODUCCION*\n{sep()}\n"
        mensaje += f"\U0001f3ed Producto: *{prod_nombre}*\n"
        mensaje += f"\u2696\ufe0f Cantidad: *{cantidad} kg*\n"
        if context.user_data.get("prod_cant_alta"):
            mensaje += f"\u26a0\ufe0f *Ojo:* {cantidad:,.0f} kg es mas de lo normal. Verifica que no sobre un cero.\n"
        mensaje += "\n"
        mensaje += f"\U0001f4e6 *Materias primas a consumir:*\n"
        hay_problema = False
        for nombre_mp, stock, unidad, cant_base, mp_id in context.user_data["prod_items"]:
            consumo = round(cant_base * factor, 3)
            if stock < consumo:
                emoji = "\U0001f534"
                hay_problema = True
            else:
                emoji = "\U0001f7e2"
            mensaje += f"   {emoji} {nombre_mp}: *{consumo} kg* (stock: {stock} kg)\n"
        if hay_problema:
            mensaje += f"\n\u26a0\ufe0f *Hay materias primas insuficientes*\n"
        mensaje += f"\n\U0001f7e2 Stock nuevo PT: *{context.user_data['prod_stock'] + cantidad} kg*\n\n"
        mensaje += "\U0001f4c5 Cuando se produjo este lote?"
        teclado = [["Hoy"], ["Escribir otra fecha"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(mensaje, reply_markup=menu, parse_mode="Markdown")
        return PROD_FECHA
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return PROD_CANTIDAD

async def produccion_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("\u274c Produccion cancelada.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    now = ahora()
    if texto == "Hoy":
        context.user_data["prod_fecha"] = now.date()
    elif "Escribir" in texto:
        await update.message.reply_text(
            "Escribe la fecha en formato *DD/MM/AAAA* (ejemplo: 24/06/2026):",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return PROD_FECHA
    else:
        # Intentar interpretar como fecha escrita
        import re as _re
        m = _re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", texto)
        if not m:
            await update.message.reply_text("No entendi la fecha. Escribe DD/MM/AAAA (ej: 24/06/2026) o toca Cancelar:")
            return PROD_FECHA
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            from datetime import date as _date
            context.user_data["prod_fecha"] = _date(yy, mm, dd)
        except Exception:
            await update.message.reply_text("Esa fecha no es valida. Intenta de nuevo (DD/MM/AAAA):")
            return PROD_FECHA
    # Mostrar confirmacion final
    fecha = context.user_data["prod_fecha"]
    prod_nombre = context.user_data["prod_nombre"]
    cantidad = context.user_data["prod_cantidad"]
    prod_codigo = context.user_data["prod_codigo"].upper()
    numero_lote = f"VX{fecha.strftime('%d%m%y')}{prod_codigo}"
    await update.message.reply_text(
        f"\u2699\ufe0f *CONFIRMAR PRODUCCION*\n{sep()}\n"
        f"\U0001f3ed {prod_nombre}\n"
        f"\u2696\ufe0f {cantidad} kg\n"
        f"\U0001f3f7\ufe0f Lote: *{fmt_lote(numero_lote)}*\n"
        f"\U0001f4c5 Fecha: *{fecha.strftime('%d/%m/%Y')}*\n\n"
        f"Escribe *SI* para confirmar o *NO* para cancelar",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return PROD_CONFIRMAR

async def produccion_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text.strip().upper()
    if respuesta != "SI":
        await update.message.reply_text("\u274c Produccion cancelada.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    try:
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        prod_id = context.user_data["prod_id"]
        prod_nombre = context.user_data["prod_nombre"]
        prod_codigo = context.user_data["prod_codigo"].upper()
        cantidad = context.user_data["prod_cantidad"]
        items = context.user_data["prod_items"]
        factor = context.user_data["prod_factor"]
        fecha_prod = context.user_data.get("prod_fecha", now.date())
        fecha_str = fecha_prod.strftime('%d%m%y')
        numero_lote = f"VX{fecha_str}{prod_codigo}"
        cur.execute("SELECT COALESCE(presentacion, 'Tambo 200 kg') FROM productos_terminados WHERE id = %s", (prod_id,))
        presentacion = cur.fetchone()[0]
        for nombre_mp, stock, unidad, cant_base, mp_id in items:
            consumo = round(float(cant_base) * float(factor), 3)
            cur.execute("UPDATE materias_primas SET stock_actual = stock_actual - %s WHERE id = %s", (consumo, mp_id))
        cur.execute("UPDATE productos_terminados SET stock_actual = stock_actual + %s WHERE id = %s", (cantidad, prod_id))
        cur.execute("""
            INSERT INTO lotes_pt (producto_id, numero_lote, presentacion, cantidad_inicial, cantidad_actual, fecha_produccion)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (prod_id, numero_lote, presentacion, cantidad, cantidad, fecha_prod))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "PRODUCCION", f"{prod_nombre} | {cantidad} kg | Lote: {numero_lote}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *PRODUCCION REGISTRADA*\n{sep()}\n"
            f"\U0001f9ea Producto: *{prod_nombre}*\n"
            f"\u2696\ufe0f Cantidad: *{cantidad} kg*\n"
            f"\U0001f3f7\ufe0f Lote: *{fmt_lote(numero_lote)}*\n"
            f"\U0001f4c5 Produccion: {fecha_prod.strftime('%d/%m/%Y')}\n\n"
            f"\U0001f7e2 MP descontada automaticamente\n"
            f"\U0001f7e2 PT actualizado",
            parse_mode="Markdown"
        )
        await start(update, context)

        # Alerta proactiva: revisar si alguna MP quedo por agotarse
        try:
            por_agotarse = calcular_mp_por_agotarse()
            texto_alerta = formato_alerta_agotamiento(por_agotarse)
            if texto_alerta:
                for chat_id in RESUMEN_IDS:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=texto_alerta, parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error alerta post-produccion: {e}")

        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error al confirmar: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# CAMBIO 2: Proveedor opcional en entrada MP
async def entrada_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c No tienes acceso a registrar entradas de MP.")
        await start(update, context)
        return ConversationHandler.END
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, stock_actual, unidad FROM materias_primas WHERE activo = true ORDER BY nombre")
        materiales = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["materiales"] = materiales
        teclado = [[m[1]] for m in materiales]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            f"\U0001f4e6 *ENTRADA DE MATERIA PRIMA*\n{sep()}\nSelecciona el material:",
            reply_markup=menu, parse_mode="Markdown"
        )
        return ENTRADA_MATERIAL
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def entrada_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    materiales = context.user_data.get("materiales", [])
    material = next((m for m in materiales if m[1] == nombre), None)
    if not material:
        await update.message.reply_text("No encontre ese material. Selecciona de la lista.")
        return ENTRADA_MATERIAL
    context.user_data["entrada_id"] = material[0]
    context.user_data["entrada_nombre"] = material[1]
    context.user_data["entrada_stock_actual"] = float(material[2])
    context.user_data["entrada_unidad"] = material[3]
    await update.message.reply_text(
        f"\U0001f9ea *{material[1]}*\nStock actual: {material[2]} {material[3]}\n\nCuanta cantidad entra?",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return ENTRADA_CANTIDAD

async def entrada_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, cantidad, err, confirmar = validar_cantidad(update.message.text)
    if not ok:
        await update.message.reply_text(err)
        return ENTRADA_CANTIDAD
    context.user_data["entrada_cantidad"] = cantidad
    context.user_data["entrada_cant_alta"] = confirmar
    teclado = [["Omitir"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        "\U0001f3ed Nombre del proveedor (o toca *Omitir*):",
        reply_markup=menu, parse_mode="Markdown"
    )
    return ENTRADA_PROVEEDOR

async def entrada_proveedor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    context.user_data["entrada_proveedor"] = "" if texto == "Omitir" else texto
    teclado = [["Saco 25 kg", "Tambo 200 kg"], ["Cuñete 25 kg", "Porron 30 kg"], ["Otro"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text("\U0001f4e6 Selecciona la presentacion:", reply_markup=menu)
    return ENTRADA_PRESENTACION

async def entrada_presentacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["entrada_presentacion"] = update.message.text.strip()
    # CAMBIO 3: Solo Almacen como ubicacion
    teclado = [["Almacen"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text("\U0001f4cd Selecciona la ubicacion:", reply_markup=menu)
    return ENTRADA_UBICACION

async def entrada_ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["entrada_ubicacion"] = update.message.text.strip()
    nombre = context.user_data["entrada_nombre"]
    cantidad = context.user_data["entrada_cantidad"]
    proveedor = context.user_data["entrada_proveedor"]
    presentacion = context.user_data["entrada_presentacion"]
    ubicacion = context.user_data["entrada_ubicacion"]
    stock_actual = context.user_data["entrada_stock_actual"]
    unidad = context.user_data["entrada_unidad"]
    proveedor_txt = proveedor if proveedor else "_Sin proveedor_"
    alerta = ""
    if context.user_data.get("entrada_cant_alta"):
        alerta = f"\u26a0\ufe0f *Ojo:* {cantidad:,.0f} {unidad} es mas de lo normal. Verifica que no sobre un cero.\n\n"
    await update.message.reply_text(
        f"\U0001f4cb *CONFIRMAR ENTRADA*\n{sep()}\n"
        f"\U0001f9ea *{nombre}*\n"
        f"\u2696\ufe0f Cantidad: *{cantidad} {unidad}*\n"
        f"\U0001f3ed Proveedor: {proveedor_txt}\n"
        f"\U0001f4e6 Presentacion: {presentacion}\n"
        f"\U0001f4cd Ubicacion: {ubicacion}\n"
        f"\U0001f4ca Stock actual: {stock_actual} {unidad}\n"
        f"\U0001f7e2 Stock nuevo: *{stock_actual + cantidad} {unidad}*\n\n"
        f"{alerta}"
        f"Escribe *SI* para confirmar o *NO* para cancelar",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return ENTRADA_CONFIRMAR

async def entrada_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text.strip().upper()
    if respuesta != "SI":
        await update.message.reply_text("\u274c Entrada cancelada.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    try:
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        mp_id = context.user_data["entrada_id"]
        cantidad = context.user_data["entrada_cantidad"]
        proveedor = context.user_data["entrada_proveedor"]
        presentacion = context.user_data["entrada_presentacion"]
        ubicacion = context.user_data["entrada_ubicacion"]
        nombre = context.user_data["entrada_nombre"]
        numero_lote = f"E{now.strftime('%d%m%y%H%M')}"
        cur.execute("INSERT INTO entradas_mp (materia_prima_id, cantidad, proveedor, presentacion, ubicacion, numero_lote, registrado_por) VALUES (%s, %s, %s, %s, %s, %s, %s)", (mp_id, cantidad, proveedor, presentacion, ubicacion, numero_lote, "Bot"))
        cur.execute("INSERT INTO lotes_mp (materia_prima_id, numero_lote, proveedor, cantidad_inicial, cantidad_actual, presentacion, ubicacion) VALUES (%s, %s, %s, %s, %s, %s, %s)", (mp_id, numero_lote, proveedor, cantidad, cantidad, presentacion, ubicacion))
        cur.execute("UPDATE materias_primas SET stock_actual = stock_actual + %s WHERE id = %s", (cantidad, mp_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "ENTRADA_MP", f"{nombre} | {cantidad} kg | Lote: {numero_lote} | Proveedor: {proveedor or 'N/A'}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *ENTRADA REGISTRADA*\n{sep()}\n"
            f"\U0001f9ea *{nombre}*\n"
            f"\u2696\ufe0f {cantidad} kg\n"
            f"\U0001f3ed {proveedor if proveedor else 'Sin proveedor'}\n"
            f"\U0001f3f7\ufe0f Lote: {fmt_lote(numero_lote)}\n"
            f"\U0001f4cd {ubicacion}\n\n"
            f"\U0001f7e2 Stock actualizado",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def asistencia_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nombre FROM empleados WHERE activo = true ORDER BY nombre")
        empleados = cur.fetchall()
        cur.execute("""
            SELECT e.nombre, a.tipo, a.fecha
            FROM asistencia a
            JOIN empleados e ON e.id = a.empleado_id
            WHERE DATE(a.fecha AT TIME ZONE 'America/Mexico_City') = DATE(NOW() AT TIME ZONE 'America/Mexico_City')
            ORDER BY a.fecha DESC
        """)
        movimientos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["empleados"] = empleados
        teclado = [[e[1]] for e in empleados]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        mensaje = f"\U0001f553 *ASISTENCIA HOY*\n{sep()}\n"
        if movimientos:
            for nombre, tipo, fecha in movimientos:
                if tipo == "Entrada":
                    emoji = "\U0001f7e2"
                elif tipo == "Salida":
                    emoji = "\U0001f534"
                elif tipo == "Salida comida":
                    emoji = "\U0001f35c"
                else:
                    emoji = "\U0001f504"
                mensaje += f"{emoji} {nombre}: {tipo} {fecha.strftime('%H:%M')}\n"
        else:
            mensaje += "_Sin movimientos hoy_\n"
        mensaje += "\nSelecciona empleado:"
        await update.message.reply_text(mensaje, reply_markup=menu, parse_mode="Markdown")
        return CLOCK_EMPLEADO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def clock_empleado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    empleados = context.user_data.get("empleados", [])
    empleado = next((e for e in empleados if e[1] == nombre), None)
    if not empleado:
        await update.message.reply_text("No encontre ese empleado. Selecciona de la lista.")
        return CLOCK_EMPLEADO
    context.user_data["clock_empleado_id"] = empleado[0]
    context.user_data["clock_empleado_nombre"] = empleado[1]
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT tipo, fecha FROM asistencia WHERE empleado_id = %s AND DATE(fecha AT TIME ZONE 'America/Mexico_City') = DATE(NOW() AT TIME ZONE 'America/Mexico_City') ORDER BY fecha DESC LIMIT 1", (empleado[0],))
        ultimo = cur.fetchone()
        cur.close()
        conn.close()
        if not ultimo:
            siguiente = "Entrada"
        elif ultimo[0] == "Entrada":
            siguiente = "Salida"
        else:
            siguiente = "Entrada"
        context.user_data["clock_tipo"] = siguiente
        teclado = [[siguiente], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        msg = f"\U0001f464 *{empleado[1]}*\n"
        if ultimo:
            msg += f"Ultimo: {ultimo[0]} a las {ultimo[1].strftime('%H:%M')}\n"
        msg += f"\nRegistrar: *{siguiente}*\n\nToca el boton para confirmar:"
        await update.message.reply_text(msg, reply_markup=menu, parse_mode="Markdown")
        return CLOCK_TIPO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def clock_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo = update.message.text.strip()
    if tipo == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    if "Volver" in tipo:
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    context.user_data["clock_tipo"] = tipo
    empleado_id = context.user_data["clock_empleado_id"]
    empleado_nombre = context.user_data["clock_empleado_nombre"]
    now = ahora()
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("INSERT INTO asistencia (empleado_id, tipo, fecha, registrado_por) VALUES (%s, %s, %s, %s)", (empleado_id, tipo, now, "Bot"))
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(
            f"\u2705 *REGISTRADO*\n{sep()}\n"
            f"\U0001f464 {empleado_nombre}\n"
            f"\U0001f553 {tipo}: *{now.strftime('%H:%M')}*\n"
            f"\U0001f4c5 {now.strftime('%d/%m/%Y')}",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("\u274c Operacion cancelada.")
    await start(update, context)
    return ConversationHandler.END

async def manejar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol is None:
        await update.message.reply_text("\u274c No tienes acceso a este sistema.")
        return
    texto = update.message.text
    if "Consultar stock" in texto:
        await consultar_stock(update, context)
    elif "Producto terminado" in texto:
        await consultar_pt(update, context)
    elif "Tambos" in texto:
        await tambos_menu(update, context)
    elif "Ver tambos" in texto:
        await ver_tambos(update, context)
    elif "Ver por lavar" in texto:
        await ver_por_lavar(update, context)
    elif "Entrada tambos" in texto:
        await entrada_tambos_inicio(update, context)

# CAMBIO 6: Pedido puede incluir PT y MP mezclados
async def pedido_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c No tienes acceso a este modulo.")
        await start(update, context)
        return ConversationHandler.END
    await update.message.reply_text(
        f"\U0001f69a *NUEVO PEDIDO*\n{sep()}\nEscribe el nombre del cliente:",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return PEDIDO_CLIENTE

async def pedido_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cliente"] = update.message.text.strip()
    await update.message.reply_text(
        f"\U0001f465 Cliente: *{context.user_data['cliente']}*\n\n"
        "Escribe codigo y cantidad (uno por linea):\nCODIGO CANTIDAD\n\n"
        "Ejemplo:\nPS 600\nCO 600\nTAC 200\n\n"
        "Codigos: PS, CO, TAC, AS, TB, ME, MB\n"
        "(tambien puedes usar el nombre completo)\n\n"
        "Cuando termines escribe *FIN*",
        parse_mode="Markdown"
    )
    return PEDIDO_PRODUCTOS

async def pedido_productos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto.upper() == "FIN":
        if not context.user_data.get("productos"):
            await update.message.reply_text("\u274c No agregaste productos. Escribe NOMBRE CANTIDAD o /cancelar")
            return PEDIDO_PRODUCTOS
        teclado = [["Omitir"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "\U0001f4dd Alguna nota u observacion para bodega?\n(ej: entregar antes de las 2pm, cliente recoge)\n\nEscribela o toca *Omitir*:",
            reply_markup=menu, parse_mode="Markdown"
        )
        return PEDIDO_NOTA
    if "productos" not in context.user_data:
        context.user_data["productos"] = []
    lineas = texto.split("\n")
    for linea in lineas:
        linea = linea.strip()
        if not linea:
            continue
        partes = linea.rsplit(" ", 1)
        if len(partes) == 2:
            try:
                nombre = " ".join(partes[0].strip().upper().split())
                cantidad = float(partes[1].strip())
                context.user_data["productos"].append({"nombre": nombre, "cantidad": cantidad})
            except:
                await update.message.reply_text(f"\u274c No entendi: {linea}\nUsa: NOMBRE CANTIDAD")
                return PEDIDO_PRODUCTOS
    await update.message.reply_text(f"\u2705 {len(context.user_data['productos'])} producto(s).\nEscribe mas o *FIN*", parse_mode="Markdown")
    return PEDIDO_PRODUCTOS

async def pedido_nota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    context.user_data["nota"] = "" if texto == "Omitir" else texto
    return await pedido_calcular(update, context)

async def pedido_calcular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cliente = context.user_data["cliente"]
        productos = context.user_data["productos"]
        resultado_pt = []
        resultado_mp = []
        errores = []

        # Cargar catalogos completos una sola vez
        cur.execute("""
            SELECT id, nombre, stock_actual, COALESCE(kg_por_unidad, 200),
                   COALESCE(presentacion, 'Tambo 200 kg'), COALESCE(UPPER(TRIM(codigo)), '')
            FROM productos_terminados WHERE activo = true
        """)
        pt_catalogo = cur.fetchall()
        cur.execute("""
            SELECT id, nombre, stock_actual, unidad, COALESCE(UPPER(TRIM(codigo)), '')
            FROM materias_primas WHERE activo = true
        """)
        mp_catalogo = cur.fetchall()

        def coincide_nombre(nombre_db, termino):
            nombre_norm = re.sub(r'\s+', ' ', nombre_db).upper()
            return re.search(r'(^|\s)' + re.escape(termino) + r'(\s|$)', nombre_norm) is not None

        for item in productos:
            termino = item['nombre']

            # Buscar en PT: codigo exacto, luego nombre como palabra completa
            producto_pt = next((p for p in pt_catalogo if p[5] == termino), None)
            if not producto_pt:
                producto_pt = next((p for p in pt_catalogo if coincide_nombre(p[1], termino)), None)

            if producto_pt:
                prod_id, prod_nombre, stock_actual, kg_por_unidad, presentacion_prod = producto_pt[0], producto_pt[1], producto_pt[2], producto_pt[3], producto_pt[4]
                if float(stock_actual) < item["cantidad"]:
                    errores.append(f"Stock insuficiente PT {prod_nombre}: {stock_actual} kg")
                    continue
                cur.execute("SELECT id, numero_lote, cantidad_actual, presentacion FROM lotes_pt WHERE producto_id = %s AND cantidad_actual > 0 ORDER BY fecha_produccion ASC", (prod_id,))
                lotes = cur.fetchall()
                lotes_usados = []
                restante = item["cantidad"]
                for lote_id, numero_lote, cant_lote, presentacion in lotes:
                    if restante <= 0:
                        break
                    usar = min(float(cant_lote), restante)
                    lotes_usados.append({"lote_id": lote_id, "numero_lote": numero_lote, "cantidad": usar, "presentacion": presentacion, "kg_por_unidad": float(kg_por_unidad), "es_tambo": "tambo" in str(presentacion_prod).lower()})
                    restante -= usar
                resultado_pt.append({"prod_id": prod_id, "nombre": prod_nombre, "cantidad_total": item["cantidad"], "lotes": lotes_usados, "kg_por_unidad": float(kg_por_unidad), "es_tambo": "tambo" in str(presentacion_prod).lower()})
            else:
                # Buscar en MP: codigo exacto, luego nombre como palabra completa
                mp = next((m for m in mp_catalogo if m[4] == termino), None)
                if not mp:
                    mp = next((m for m in mp_catalogo if coincide_nombre(m[1], termino)), None)
                if mp:
                    mp_id, mp_nombre, mp_stock, mp_unidad = mp[0], mp[1], mp[2], mp[3]
                    if float(mp_stock) < item["cantidad"]:
                        errores.append(f"Stock insuficiente MP {mp_nombre}: {mp_stock} {mp_unidad}")
                        continue
                    resultado_mp.append({"mp_id": mp_id, "nombre": mp_nombre, "cantidad": item["cantidad"], "unidad": mp_unidad})
                else:
                    errores.append(f"No encontre: {item['nombre']}")

        # Calcular precios (solo PT, usando precio de cliente si existe, si no el general)
        cur.execute("SELECT id FROM clientes WHERE UPPER(nombre) = UPPER(%s)", (cliente,))
        cliente_row = cur.fetchone()
        cliente_id_precio = cliente_row[0] if cliente_row else None
        montos_por_moneda = {}
        for item in resultado_pt:
            precio, moneda = obtener_precio(cur, item["prod_id"], cliente_id_precio)
            item["precio_unitario"] = precio
            item["moneda"] = moneda
            if precio is not None:
                importe = item["cantidad_total"] * precio
                item["importe"] = importe
                montos_por_moneda[moneda] = montos_por_moneda.get(moneda, 0) + importe
            else:
                item["importe"] = None

        cur.close()
        conn.close()

        if errores:
            await update.message.reply_text("\u26a0\ufe0f *ATENCION:*\n" + "\n".join([f"\u274c {e}" for e in errores]), parse_mode="Markdown")
            if not resultado_pt and not resultado_mp:
                context.user_data.clear()
                await start(update, context)
                return ConversationHandler.END

        context.user_data["resultado_pt"] = resultado_pt
        context.user_data["resultado_mp"] = resultado_mp
        context.user_data["swap_historial"] = []
        context.user_data["swap_resumen"] = []

        mensaje = f"\U0001f69a *PEDIDO \u2014 {cliente}*\n{sep()}\n"
        total_tambos = 0

        if resultado_pt:
            mensaje += "\n\U0001f3ed *PRODUCTO TERMINADO*\n"
            for item in resultado_pt:
                mensaje += f"\n\U0001f4e6 *{item['nombre']}* \u2014 {item['cantidad_total']:,.0f} kg\n"
                for lote in item["lotes"]:
                    mensaje += f"   {fmt_lote(lote['numero_lote'])} \u2192 {lote['cantidad']:,.0f} kg\n"
                if item["es_tambo"]:
                    u = int(item["cantidad_total"] / item["kg_por_unidad"])
                    total_tambos += u
                    mensaje += f"   \U0001f6e2\ufe0f {u} tambos\n"
                else:
                    u = int(item["cantidad_total"] / item["kg_por_unidad"])
                    mensaje += f"   \U0001f4e6 {u} unidades\n"
                if item.get("importe") is not None:
                    mensaje += f"   \U0001f4b2 {fmt_moneda(item['precio_unitario'], item['moneda'])}/kg \u2192 {fmt_moneda(item['importe'], item['moneda'])}\n"
                else:
                    mensaje += f"   \u26a0\ufe0f Sin precio registrado\n"

        if resultado_mp:
            mensaje += "\n\U0001f9ea *MATERIA PRIMA*\n"
            for item in resultado_mp:
                mensaje += f"\n\U0001f4e6 *{item['nombre']}* \u2014 {item['cantidad']:,.0f} {item['unidad']}\n"

        if montos_por_moneda:
            mensaje += f"\n{sep()}\n\U0001f4b2 *TOTAL PEDIDO*\n"
            mensaje += texto_totales_iva(montos_por_moneda)
            context.user_data["montos_por_moneda"] = montos_por_moneda

        rol = get_rol(str(update.message.from_user.id))
        if rol == 'admin' and resultado_pt:
            mensaje += f"\n{sep()}\n\U0001f6e2\ufe0f *Tambos a entregar: {total_tambos}*\n\n"
            mensaje += "Selecciona una opcion:"
            teclado = [["SI", "CAMBIAR"], ["NO"]]
        else:
            mensaje += f"\n{sep()}\n\U0001f6e2\ufe0f *Tambos a entregar: {total_tambos}*\n\nSelecciona una opcion:"
            teclado = [["SI", "NO"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(mensaje, reply_markup=menu, parse_mode="Markdown")
        return PEDIDO_CONFIRMAR
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def pedido_swap_prod_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el menu para elegir que producto cambiar, deshacer el ultimo cambio, o confirmar."""
    resultado_pt = context.user_data.get("resultado_pt", [])
    historial = context.user_data.get("swap_historial", [])
    teclado = [[item["nombre"]] for item in resultado_pt]
    if historial:
        teclado.append(["\u21a9\ufe0f Deshacer ultimo cambio"])
    teclado.append(["Listo (confirmar pedido)"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        "\U0001f504 *CAMBIAR LOTE*\n"
        "Selecciona el producto cuyo lote quieres cambiar,\n"
        "toca *Deshacer* para revertir el ultimo cambio,\n"
        "o toca *Listo* para confirmar el pedido:",
        reply_markup=menu, parse_mode="Markdown"
    )
    return PEDIDO_SWAP_PROD

async def pedido_swap_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    historial = context.user_data.get("swap_historial", [])
    resumen = context.user_data.get("swap_resumen", [])
    if not historial:
        await update.message.reply_text("No hay cambios que deshacer.")
        return await pedido_swap_prod_menu(update, context)
    context.user_data["resultado_pt"] = historial.pop()
    deshecho = resumen.pop() if resumen else "el ultimo cambio"
    await update.message.reply_text(f"\u21a9\ufe0f *Deshecho:* {deshecho}", parse_mode="Markdown")
    return await pedido_swap_prod_menu(update, context)

async def pedido_swap_prod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if "Listo" in texto or "Confirmar" in texto:
        return await pedido_remostrar(update, context)
    if "Deshacer" in texto:
        return await pedido_swap_deshacer(update, context)
    resultado_pt = context.user_data.get("resultado_pt", [])
    item = next((it for it in resultado_pt if it["nombre"] == texto), None)
    if not item:
        await update.message.reply_text("Selecciona de la lista.")
        return PEDIDO_SWAP_PROD
    context.user_data["swap_prod_id"] = item["prod_id"]
    context.user_data["swap_prod_nombre"] = item["nombre"]
    if not item.get("lotes"):
        await update.message.reply_text("Este producto no tiene lotes asignados.")
        return PEDIDO_SWAP_PROD
    msg = f"\U0001f4e6 *{item['nombre']}*\n{sep()}\n*Lotes en este pedido:*\n"
    teclado = []
    for i, lote in enumerate(item["lotes"]):
        msg += f"   {i+1}. {fmt_lote(lote['numero_lote'])} \u2192 {lote['cantidad']:,.0f} kg\n"
        teclado.append([f"{i+1}. {fmt_lote(lote['numero_lote'])}"])
    teclado.append(["Cancelar"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    msg += "\nSelecciona el lote que quieres *QUITAR*\n(el numero al inicio distingue lotes iguales):"
    await update.message.reply_text(msg, reply_markup=menu, parse_mode="Markdown")
    return PEDIDO_SWAP_QUITAR

async def pedido_swap_quitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        return await pedido_swap_prod_menu(update, context)
    prod_id = context.user_data["swap_prod_id"]
    resultado_pt = context.user_data["resultado_pt"]
    item = next(it for it in resultado_pt if it["prod_id"] == prod_id)
    idx = None
    if "." in texto:
        try:
            idx = int(texto.split(".")[0]) - 1
        except:
            idx = None
    if idx is None or idx < 0 or idx >= len(item["lotes"]):
        await update.message.reply_text("Selecciona un lote de la lista.")
        return PEDIDO_SWAP_QUITAR

    context.user_data["swap_quitar_idx"] = idx
    entrada = item["lotes"][idx]
    cantidad_disponible = entrada["cantidad"]
    context.user_data["swap_cantidad_disponible"] = cantidad_disponible
    context.user_data["swap_lote_quitar_id"] = entrada["lote_id"]
    context.user_data["swap_lote_quitar_numero"] = entrada["numero_lote"]

    teclado = [[f"Todo ({cantidad_disponible:,.0f} kg)"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f4e6 Lote *{fmt_lote(entrada['numero_lote'])}* tiene *{cantidad_disponible:,.0f} kg* en este pedido.\n\n"
        f"\u00bfCuantos kg quieres cambiar? Escribe la cantidad o toca *Todo*:",
        reply_markup=menu, parse_mode="Markdown"
    )
    return PEDIDO_SWAP_CANTIDAD

async def pedido_swap_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        return await pedido_swap_prod_menu(update, context)
    disponible = context.user_data["swap_cantidad_disponible"]
    if texto.startswith("Todo"):
        cantidad = disponible
    else:
        try:
            cantidad = float(texto.replace(",", ""))
        except:
            await update.message.reply_text("Escribe un numero valido, o toca *Todo*:", parse_mode="Markdown")
            return PEDIDO_SWAP_CANTIDAD
        if cantidad <= 0 or cantidad > disponible:
            await update.message.reply_text(
                f"Debe ser mayor a cero y no mas de {disponible:,.0f} kg. Intenta de nuevo:"
            )
            return PEDIDO_SWAP_CANTIDAD
    context.user_data["swap_cantidad"] = cantidad
    context.user_data["swap_restante"] = cantidad
    context.user_data["swap_nuevas_asignaciones"] = []
    return await pedido_swap_mostrar_candidatos(update, context)

async def pedido_swap_mostrar_candidatos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra los lotes disponibles para cubrir lo que falte del swap en curso."""
    prod_id = context.user_data["swap_prod_id"]
    resultado_pt = context.user_data["resultado_pt"]
    item = next(it for it in resultado_pt if it["prod_id"] == prod_id)
    idx = context.user_data["swap_quitar_idx"]
    lote_quitar_id = context.user_data["swap_lote_quitar_id"]
    restante = context.user_data["swap_restante"]

    # cuanto ya esta comprometido en este pedido por lote (sin contar el que se esta quitando)
    usados = {}
    for i, lote in enumerate(item["lotes"]):
        if i == idx:
            continue
        usados[lote["lote_id"]] = usados.get(lote["lote_id"], 0) + lote["cantidad"]
    # sumar lo que ya se asigno en este swap (si se necesito mas de un lote destino)
    for lid, nl, cant in context.user_data.get("swap_nuevas_asignaciones", []):
        usados[lid] = usados.get(lid, 0) + cant

    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, numero_lote, cantidad_actual, fecha_produccion, presentacion
            FROM lotes_pt
            WHERE producto_id = %s AND cantidad_actual > 0
            ORDER BY fecha_produccion ASC
        """, (prod_id,))
        todos = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return await pedido_swap_prod_menu(update, context)

    candidatos = []
    for lid, nl, ca, fp, pres in todos:
        if lid == lote_quitar_id:
            continue
        libre = float(ca) - usados.get(lid, 0)
        if libre > 0:
            candidatos.append((lid, nl, float(ca), fp, pres, libre))

    if not candidatos:
        for k in ["swap_candidatos", "swap_nuevas_asignaciones", "swap_cantidad", "swap_restante",
                  "swap_cantidad_disponible", "swap_lote_quitar_id", "swap_lote_quitar_numero", "swap_quitar_idx"]:
            context.user_data.pop(k, None)
        await update.message.reply_text(
            f"\u26a0\ufe0f No hay otro lote con stock libre para cubrir los {restante:,.0f} kg restantes.\n"
            f"El cambio se cancelo, el pedido queda igual que antes."
        )
        return await pedido_swap_prod_menu(update, context)

    context.user_data["swap_candidatos"] = candidatos
    msg = f"\U0001f504 Faltan *{restante:,.0f} kg* por asignar.\n{sep()}\n*Lotes disponibles:*\n"
    teclado = []
    for lid, nl, ca, fp, pres, libre in candidatos:
        msg += f"   {fmt_lote(nl)}: {libre:,.0f} kg libres ({fp.strftime('%d/%m/%Y')})\n"
        teclado.append([fmt_lote(nl)])
    teclado.append(["Cancelar"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    msg += "\nSelecciona el lote que quieres *PONER*\n(si uno no alcanza, despues eliges otro para completar):"
    await update.message.reply_text(msg, reply_markup=menu, parse_mode="Markdown")
    return PEDIDO_SWAP_PONER

async def pedido_swap_poner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        for k in ["swap_candidatos", "swap_nuevas_asignaciones", "swap_cantidad", "swap_restante",
                  "swap_cantidad_disponible", "swap_lote_quitar_id", "swap_lote_quitar_numero", "swap_quitar_idx"]:
            context.user_data.pop(k, None)
        await update.message.reply_text("Cambio cancelado. El pedido queda igual que antes.")
        return await pedido_swap_prod_menu(update, context)

    candidatos = context.user_data.get("swap_candidatos", [])
    nuevo = next((c for c in candidatos if fmt_lote(c[1]) == texto), None)
    if not nuevo:
        await update.message.reply_text("Selecciona un lote de la lista.")
        return PEDIDO_SWAP_PONER

    lid, nl, ca, fp, pres, libre = nuevo
    restante = context.user_data["swap_restante"]
    tomar = min(libre, restante)
    context.user_data["swap_nuevas_asignaciones"].append((lid, nl, tomar))
    restante -= tomar
    context.user_data["swap_restante"] = restante

    if restante > 0:
        await update.message.reply_text(
            f"\u2705 Asignados {tomar:,.0f} kg de {fmt_lote(nl)}. Faltan {restante:,.0f} kg.",
            parse_mode="Markdown"
        )
        return await pedido_swap_mostrar_candidatos(update, context)

    # Ya se cubrio toda la cantidad solicitada: aplicar el cambio
    prod_id = context.user_data["swap_prod_id"]
    resultado_pt = context.user_data["resultado_pt"]
    item = next(it for it in resultado_pt if it["prod_id"] == prod_id)
    idx = context.user_data["swap_quitar_idx"]
    entrada_original = item["lotes"][idx]
    cantidad_swap = context.user_data["swap_cantidad"]
    lote_quitado_numero = entrada_original["numero_lote"]

    # Guardar snapshot ANTES de aplicar el cambio, para poder deshacer despues
    import copy as _copy
    historial = context.user_data.setdefault("swap_historial", [])
    historial.append(_copy.deepcopy(resultado_pt))

    # Reducir o quitar por completo la entrada original
    if cantidad_swap >= entrada_original["cantidad"]:
        item["lotes"].pop(idx)
    else:
        entrada_original["cantidad"] -= cantidad_swap

    # Agregar las nuevas asignaciones, fusionando con una entrada existente del mismo lote si ya hay una
    for lid2, nl2, cant2 in context.user_data["swap_nuevas_asignaciones"]:
        existente = next((l for l in item["lotes"] if l["lote_id"] == lid2), None)
        if existente:
            existente["cantidad"] += cant2
        else:
            item["lotes"].append({
                "lote_id": lid2,
                "numero_lote": nl2,
                "cantidad": cant2,
                "presentacion": entrada_original.get("presentacion", ""),
                "kg_por_unidad": item["kg_por_unidad"],
                "es_tambo": item["es_tambo"],
            })

    # Guardar resumen legible del cambio para mostrarlo despues
    resumen_txt = context.user_data.setdefault("swap_resumen", [])
    destinos = ", ".join(f"{fmt_lote(nl2)} ({cant2:,.0f}kg)" for _, nl2, cant2 in context.user_data["swap_nuevas_asignaciones"])
    resumen_txt.append(f"{item['nombre']}: {fmt_lote(lote_quitado_numero)} \u2192 {destinos} ({cantidad_swap:,.0f} kg)")

    for k in ["swap_candidatos", "swap_nuevas_asignaciones", "swap_cantidad", "swap_restante",
              "swap_cantidad_disponible", "swap_lote_quitar_id", "swap_lote_quitar_numero", "swap_quitar_idx"]:
        context.user_data.pop(k, None)

    await update.message.reply_text(
        f"\u2705 Cambio hecho: *{fmt_lote(lote_quitado_numero)}* \u2192 *{destinos}* ({cantidad_swap:,.0f} kg)",
        parse_mode="Markdown"
    )
    return await pedido_swap_prod_menu(update, context)

async def pedido_remostrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cliente = context.user_data["cliente"]
    resultado_pt = context.user_data.get("resultado_pt", [])
    resultado_mp = context.user_data.get("resultado_mp", [])
    resumen_swaps = context.user_data.get("swap_resumen", [])
    mensaje = f"\U0001f69a *PEDIDO \u2014 {cliente}*\n{sep()}\n"
    if resumen_swaps:
        mensaje += "\n\U0001f504 *Cambios realizados:*\n"
        for linea in resumen_swaps:
            mensaje += f"   \u2022 {linea}\n"
    total_tambos = 0
    montos_por_moneda = {}
    if resultado_pt:
        mensaje += "\n\U0001f3ed *PRODUCTO TERMINADO*\n"
        for item in resultado_pt:
            mensaje += f"\n\U0001f4e6 *{item['nombre']}* \u2014 {item['cantidad_total']:,.0f} kg\n"
            for lote in item["lotes"]:
                mensaje += f"   {fmt_lote(lote['numero_lote'])} \u2192 {lote['cantidad']:,.0f} kg\n"
            if item["es_tambo"]:
                u = int(item["cantidad_total"] / item["kg_por_unidad"])
                total_tambos += u
                mensaje += f"   \U0001f6e2\ufe0f {u} tambos\n"
            else:
                u = int(item["cantidad_total"] / item["kg_por_unidad"])
                mensaje += f"   \U0001f4e6 {u} unidades\n"
            if item.get("importe") is not None:
                mensaje += f"   \U0001f4b2 {fmt_moneda(item['precio_unitario'], item['moneda'])}/kg \u2192 {fmt_moneda(item['importe'], item['moneda'])}\n"
                montos_por_moneda[item["moneda"]] = montos_por_moneda.get(item["moneda"], 0) + item["importe"]
            else:
                mensaje += f"   \u26a0\ufe0f Sin precio registrado\n"
    if resultado_mp:
        mensaje += "\n\U0001f9ea *MATERIA PRIMA*\n"
        for item in resultado_mp:
            mensaje += f"\n\U0001f4e6 *{item['nombre']}* \u2014 {item['cantidad']:,.0f} {item['unidad']}\n"
    if montos_por_moneda:
        mensaje += f"\n{sep()}\n\U0001f4b2 *TOTAL PEDIDO*\n"
        mensaje += texto_totales_iva(montos_por_moneda)
        context.user_data["montos_por_moneda"] = montos_por_moneda
    mensaje += f"\n{sep()}\n\U0001f6e2\ufe0f *Tambos a entregar: {total_tambos}*\n\n"
    mensaje += "Selecciona una opcion:"
    teclado = [["SI", "CAMBIAR"], ["NO"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(mensaje, reply_markup=menu, parse_mode="Markdown")
    return PEDIDO_CONFIRMAR


async def pedido_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    respuesta = update.message.text.strip().upper()
    if respuesta == "CAMBIAR":
        rol = get_rol(str(update.message.from_user.id))
        if rol != 'admin':
            await update.message.reply_text("\u274c Solo admin puede cambiar lotes.")
            return PEDIDO_CONFIRMAR
        resultado_pt = context.user_data.get("resultado_pt", [])
        if not resultado_pt:
            await update.message.reply_text("No hay productos terminados para cambiar lote.")
            return PEDIDO_CONFIRMAR
        return await pedido_swap_prod_menu(update, context)
    if respuesta != "SI":
        await update.message.reply_text("\u274c Pedido cancelado.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    try:
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        cliente_nombre = context.user_data["cliente"]
        resultado_pt = context.user_data.get("resultado_pt", [])
        resultado_mp = context.user_data.get("resultado_mp", [])

        cur.execute("SELECT id FROM clientes WHERE UPPER(nombre) = UPPER(%s)", (cliente_nombre,))
        cliente = cur.fetchone()
        if not cliente:
            cur.execute("INSERT INTO clientes (nombre) VALUES (%s) RETURNING id", (cliente_nombre,))
            cliente_id = cur.fetchone()[0]
        else:
            cliente_id = cliente[0]

        folio = f"PED{now.strftime('%d%m%y%H%M')}"
        total_tambos = sum(int(i["cantidad_total"] / i["kg_por_unidad"]) for i in resultado_pt if i["es_tambo"])
        monto_usd = sum(i["importe"] for i in resultado_pt if i.get("importe") and i.get("moneda") == "USD")
        monto_mxn = sum(i["importe"] for i in resultado_pt if i.get("importe") and i.get("moneda") == "MXN")

        cur.execute(
            "INSERT INTO pedidos (cliente_id, folio, tambos_enviados, monto_usd, monto_mxn) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (cliente_id, folio, total_tambos, monto_usd, monto_mxn)
        )
        pedido_id = cur.fetchone()[0]

        # Descontar PT
        for item in resultado_pt:
            precio_u = item.get("precio_unitario")
            moneda_item = item.get("moneda")
            for lote in item["lotes"]:
                importe_lote = round(precio_u * lote["cantidad"], 2) if precio_u is not None else None
                cur.execute(
                    "INSERT INTO pedido_items (pedido_id, lote_pt_id, producto_id, cantidad, precio_unitario, moneda, importe) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (pedido_id, lote["lote_id"], item["prod_id"], lote["cantidad"], precio_u, moneda_item, importe_lote)
                )
                cur.execute("UPDATE lotes_pt SET cantidad_actual = cantidad_actual - %s WHERE id = %s", (lote["cantidad"], lote["lote_id"]))
            cur.execute("UPDATE productos_terminados SET stock_actual = stock_actual - %s WHERE id = %s", (item["cantidad_total"], item["prod_id"]))

        # Descontar MP
        for item in resultado_mp:
            cur.execute("UPDATE materias_primas SET stock_actual = stock_actual - %s WHERE id = %s", (item["cantidad"], item["mp_id"]))

        cur.execute("UPDATE clientes SET tambos_pendientes = tambos_pendientes + %s WHERE id = %s", (total_tambos, cliente_id))
        if total_tambos > 0:
            cur.execute("INSERT INTO tambos_movimientos (cliente_id, tipo, cantidad, pedido_id) VALUES (%s, %s, %s, %s)", (cliente_id, "salida", total_tambos, pedido_id))

        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "PEDIDO", f"Folio: {folio} | Cliente: {cliente_nombre} | Tambos: {total_tambos}")
        conn.commit()
        cur.close()
        conn.close()

        nota = context.user_data.get("nota", "")
        registrado_por = update.message.from_user.first_name

        # Construir mensaje de carga para el equipo
        carga = f"\U0001f69a *NUEVO PEDIDO PARA CARGAR*\n{sep()}\n"
        carga += f"\U0001f4cb Folio: *{folio}*\n"
        carga += f"\U0001f465 Cliente: *{cliente_nombre}*\n"
        carga += f"\U0001f553 {now.strftime('%d/%m/%Y %H:%M')}\n"
        carga += f"\U0001f464 Registro: {registrado_por}\n"

        if resultado_pt:
            carga += f"\n\U0001f3ed *PRODUCTO TERMINADO*\n"
            for item in resultado_pt:
                if item["es_tambo"]:
                    u = int(item["cantidad_total"] / item["kg_por_unidad"])
                    unidad_txt = f"{u} tambos"
                else:
                    u = int(item["cantidad_total"] / item["kg_por_unidad"])
                    unidad_txt = f"{u} unidades"
                presentacion_item = item["lotes"][0]["presentacion"] if item["lotes"] else ""
                carga += f"\n\U0001f4e6 *{item['nombre']}* \u2014 {item['cantidad_total']} kg ({unidad_txt})\n"
                if presentacion_item:
                    carga += f"   \U0001f4cf {presentacion_item}\n"
                carga += f"   *Lotes a cargar:*\n"
                for lote in item["lotes"]:
                    carga += f"   \u2610 {fmt_lote(lote['numero_lote'])} \u2192 {lote['cantidad']} kg\n"

        if resultado_mp:
            carga += f"\n\U0001f9ea *MATERIA PRIMA*\n"
            for item in resultado_mp:
                carga += f"\u2610 *{item['nombre']}* \u2014 {item['cantidad']} {item['unidad']}\n"

        carga += f"\n{sep()}\n\U0001f6e2\ufe0f *Total tambos: {total_tambos}*\n"
        montos_iva = {m: v for m, v in (("USD", monto_usd), ("MXN", monto_mxn)) if v}
        if montos_iva:
            carga += "\U0001f4b2 *Importe:*\n" + texto_totales_iva(montos_iva)
        if nota:
            carga += f"\n\u26a0\ufe0f *NOTA:* {nota}\n"

        for chat_id in TODOS_IDS:
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=carga, parse_mode="Markdown")
            except Exception as e:
                print(f"Error notificando a {chat_id}: {e}")

        context.user_data.clear()

        resumen_mp = ""
        if resultado_mp:
            resumen_mp = "\n\U0001f9ea MP descontada del inventario"

        resumen_monto = ""
        if montos_iva:
            resumen_monto += "\n\U0001f4b2 *Importe:*\n" + texto_totales_iva(montos_iva).rstrip("\n")

        await update.message.reply_text(
            f"\u2705 *PEDIDO CONFIRMADO*\n{sep()}\n"
            f"\U0001f4cb Folio: {folio}\n"
            f"\U0001f465 Cliente: {cliente_nombre}\n"
            f"\U0001f6e2\ufe0f Tambos enviados: *{total_tambos}*"
            f"{resumen_monto}\n\n"
            f"\U0001f7e2 Inventario actualizado{resumen_mp}\n"
            f"\U0001f4e2 Equipo notificado",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def ver_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol not in ['admin']:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT usuario_nombre, accion, detalle, fecha
            FROM audit_log
            ORDER BY fecha DESC
            LIMIT 15
        """)
        filas = cur.fetchall()
        cur.close()
        conn.close()
        if not filas:
            await update.message.reply_text("No hay registros en el audit log.")
            return
        mensaje = f"\U0001f4cb *AUDIT LOG \u2014 ultimas 15 acciones*\n{sep()}\n"
        for nombre, accion, detalle, fecha in filas:
            mensaje += f"\n\U0001f464 *{nombre}* \u2014 {accion}\n"
            mensaje += f"   {detalle}\n"
            mensaje += f"   \U0001f4c5 {fecha.strftime('%d/%m/%Y %H:%M')}\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

async def editar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c Solo admin puede editar inventario.")
        await start(update, context)
        return ConversationHandler.END
    teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\u270f\ufe0f *EDITAR INVENTARIO*\n{sep()}\nQue quieres editar?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return EDIT_TIPO

async def editar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    if "Materia" in texto:
        tipo = "mp"
        tabla = "materias_primas"
    elif "Producto" in texto:
        tipo = "pt"
        tabla = "productos_terminados"
    else:
        await update.message.reply_text("Selecciona una opcion de la lista.")
        return EDIT_TIPO
    context.user_data["edit_tipo"] = tipo
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"SELECT id, nombre, stock_actual, unidad, COALESCE(codigo,'') FROM {tabla} WHERE activo = true ORDER BY nombre")
        productos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["edit_productos"] = productos
        teclado = [[f"{p[1]}"] for p in productos]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "\u270f\ufe0f Selecciona el producto a editar:",
            reply_markup=menu, parse_mode="Markdown"
        )
        return EDIT_PRODUCTO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def editar_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("edit_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("No encontre ese producto. Selecciona de la lista.")
        return EDIT_PRODUCTO
    context.user_data["edit_id"] = producto[0]
    context.user_data["edit_nombre"] = producto[1]
    context.user_data["edit_stock"] = float(producto[2])
    context.user_data["edit_unidad"] = producto[3]

    tipo = context.user_data["edit_tipo"]
    lotes_txt = ""
    try:
        conn = conectar()
        cur = conn.cursor()
        if tipo == "pt":
            cur.execute("SELECT id, numero_lote, cantidad_actual, fecha_produccion FROM lotes_pt WHERE producto_id = %s AND cantidad_actual > 0 ORDER BY fecha_produccion ASC", (producto[0],))
        else:
            cur.execute("SELECT id, numero_lote, cantidad_actual, NULL FROM lotes_mp WHERE materia_prima_id = %s AND cantidad_actual > 0 ORDER BY id ASC", (producto[0],))
        lotes = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["edit_lotes"] = lotes
        if lotes:
            lotes_txt = "\n*Lotes:*\n"
            for lid, nl, ca, fp in lotes:
                lotes_txt += f"   {fmt_lote(nl)}: {ca} {producto[3]}\n"
    except Exception:
        context.user_data["edit_lotes"] = []

    teclado = [["\U0001f522 Corregir stock total"], ["\u2795\u2796 Ajustar (+/-)"]]
    if context.user_data.get("edit_lotes"):
        teclado.append(["\U0001f4e6 Editar un lote"])
    teclado.append(["Cancelar"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\u270f\ufe0f *{producto[1]}*\n{sep()}\n"
        f"Stock actual: *{producto[2]} {producto[3]}*\n{lotes_txt}\n"
        f"Que quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return EDIT_ACCION

async def editar_accion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    if "Corregir" in texto:
        context.user_data["edit_modo"] = "set"
        await update.message.reply_text(
            f"Escribe el *stock total correcto* en {context.user_data['edit_unidad']}:",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return EDIT_VALOR
    elif "Ajustar" in texto:
        context.user_data["edit_modo"] = "adjust"
        await update.message.reply_text(
            "Escribe cuanto sumar o restar.\nEjemplo: *50* para sumar, *-30* para restar:",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return EDIT_VALOR
    elif "Editar un lote" in texto:
        lotes = context.user_data.get("edit_lotes", [])
        if not lotes:
            await update.message.reply_text("No hay lotes para editar.")
            return EDIT_ACCION
        teclado = []
        for i, (lid, nl, ca, fp) in enumerate(lotes, 1):
            fecha_txt = f" ({fp.strftime('%d/%m/%y')})" if fp else ""
            teclado.append([f"{i}. {fmt_lote(nl)} - {float(ca):,.0f}kg{fecha_txt}"])
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "Selecciona el lote a editar\n(el numero al inicio distingue lotes iguales):",
            reply_markup=menu
        )
        return EDIT_LOTE_SELECT
    else:
        await update.message.reply_text("Selecciona una opcion de la lista.")
        return EDIT_ACCION

async def editar_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(update.message.text.strip())
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return EDIT_VALOR
    tipo = context.user_data["edit_tipo"]
    modo = context.user_data["edit_modo"]
    edit_id = context.user_data["edit_id"]
    nombre = context.user_data["edit_nombre"]
    unidad = context.user_data["edit_unidad"]
    stock_anterior = context.user_data["edit_stock"]
    tabla = "productos_terminados" if tipo == "pt" else "materias_primas"

    if modo == "set":
        nuevo = valor
    else:
        nuevo = stock_anterior + valor
    if nuevo < 0:
        nuevo = 0

    try:
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"UPDATE {tabla} SET stock_actual = %s WHERE id = %s", (nuevo, edit_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_STOCK", f"{nombre} | {stock_anterior} -> {nuevo} {unidad} ({modo})")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *STOCK ACTUALIZADO*\n{sep()}\n"
            f"\U0001f9ea *{nombre}*\n"
            f"Anterior: {stock_anterior} {unidad}\n"
            f"\U0001f7e2 Nuevo: *{nuevo} {unidad}*\n"
            f"\U0001f4c5 {now.strftime('%d/%m/%Y %H:%M')}",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def editar_lote_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    lotes = context.user_data.get("edit_lotes", [])
    # El boton empieza con el numero de posicion: "1. VX-..."
    idx = None
    if "." in texto:
        try:
            idx = int(texto.split(".")[0]) - 1
        except:
            idx = None
    if idx is None or idx < 0 or idx >= len(lotes):
        await update.message.reply_text("Selecciona un lote de la lista.")
        return EDIT_LOTE_SELECT
    lid, nl, ca, fp = lotes[idx]
    context.user_data["edit_lote_id"] = lid
    context.user_data["edit_lote_numero"] = nl
    context.user_data["edit_lote_actual"] = float(ca)
    fecha_txt = f"\n\U0001f4c5 Fecha: {fp.strftime('%d/%m/%Y')}" if fp else ""
    teclado = [["\U0001f522 Cambiar cantidad"], ["\U0001f516 Cambiar numero de lote"], ["\U0001f5d1\ufe0f Eliminar este lote"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f4e6 Lote *{fmt_lote(nl)}*\n"
        f"Cantidad: {float(ca):,.0f} {context.user_data['edit_unidad']}{fecha_txt}\n\n"
        f"Que quieres cambiar?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return EDIT_LOTE_ACCION

async def editar_lote_accion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.")
        await start(update, context)
        return ConversationHandler.END
    if "cantidad" in texto.lower():
        await update.message.reply_text(
            f"Cantidad actual: {context.user_data['edit_lote_actual']:,.0f} {context.user_data['edit_unidad']}\n\n"
            f"Escribe la *cantidad correcta* para este lote:",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return EDIT_LOTE_VALOR
    elif "numero" in texto.lower():
        await update.message.reply_text(
            f"Numero actual: *{context.user_data['edit_lote_numero']}*\n\n"
            f"Escribe el *nuevo numero de lote* (ejemplo: VX240626TAC):",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return EDIT_LOTE_NUEVO_NUM
    elif "eliminar" in texto.lower():
        numero = context.user_data["edit_lote_numero"]
        cant = context.user_data["edit_lote_actual"]
        unidad = context.user_data["edit_unidad"]
        teclado = [["SI, eliminar"], ["No, cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            f"\U0001f5d1\ufe0f *ELIMINAR LOTE*\n{sep()}\n"
            f"Vas a eliminar por completo:\n"
            f"Lote *{fmt_lote(numero)}* ({cant:,.0f} {unidad})\n\n"
            f"\u26a0\ufe0f Esto lo quita del sistema para siempre y ajusta el stock.\n"
            f"Usalo solo para lotes que ya surtiste o que no existen.\n\n"
            f"Estas seguro?",
            reply_markup=menu, parse_mode="Markdown"
        )
        return EDIT_LOTE_CONFIRM_BORRAR
    else:
        await update.message.reply_text("Selecciona una opcion de la lista.")
        return EDIT_LOTE_ACCION

async def editar_lote_confirmar_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto != "SI, eliminar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado. El lote NO se elimino.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = context.user_data["edit_tipo"]
    edit_id = context.user_data["edit_id"]
    nombre = context.user_data["edit_nombre"]
    unidad = context.user_data["edit_unidad"]
    numero_lote = context.user_data["edit_lote_numero"]
    lote_id = context.user_data["edit_lote_id"]
    try:
        conn = conectar()
        cur = conn.cursor()
        if tipo == "pt":
            # quitar referencias en pedidos y borrar el lote por su ID unico
            cur.execute("DELETE FROM pedido_items WHERE lote_pt_id = %s", (lote_id,))
            cur.execute("DELETE FROM lotes_pt WHERE id = %s", (lote_id,))
            cur.execute("SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_pt WHERE producto_id = %s", (edit_id,))
            nuevo_total = float(cur.fetchone()[0])
            cur.execute("UPDATE productos_terminados SET stock_actual = %s WHERE id = %s", (nuevo_total, edit_id))
        else:
            cur.execute("DELETE FROM lotes_mp WHERE id = %s", (lote_id,))
            cur.execute("SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_mp WHERE materia_prima_id = %s", (edit_id,))
            nuevo_total = float(cur.fetchone()[0])
            cur.execute("UPDATE materias_primas SET stock_actual = %s WHERE id = %s", (nuevo_total, edit_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "ELIMINAR_LOTE", f"{nombre} | Lote {numero_lote} eliminado | Nuevo total: {nuevo_total}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *LOTE ELIMINADO*\n{sep()}\n"
            f"{nombre}\n"
            f"Se elimino el lote {fmt_lote(numero_lote)}\n"
            f"\U0001f7e2 Stock nuevo: *{nuevo_total:,.0f} {unidad}*",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def editar_lote_nuevo_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nuevo_num = " ".join(update.message.text.strip().upper().split())
    if len(nuevo_num) < 3:
        await update.message.reply_text("El numero de lote es muy corto. Escribe de nuevo:")
        return EDIT_LOTE_NUEVO_NUM
    tipo = context.user_data["edit_tipo"]
    lote_id = context.user_data["edit_lote_id"]
    numero_anterior = context.user_data["edit_lote_numero"]
    nombre = context.user_data["edit_nombre"]
    tabla_lotes = "lotes_pt" if tipo == "pt" else "lotes_mp"
    try:
        conn = conectar()
        cur = conn.cursor()
        # Si es PT, intentar actualizar tambien la fecha_produccion si el numero trae fecha (VXddmmyy...)
        if tipo == "pt":
            import re as _re
            m = _re.search(r"(\d{2})(\d{2})(\d{2})", nuevo_num)
            if m:
                dd, mm, yy = m.group(1), m.group(2), m.group(3)
                try:
                    fecha_nueva = f"20{yy}-{mm}-{dd}"
                    cur.execute("UPDATE lotes_pt SET numero_lote = %s, fecha_produccion = %s WHERE id = %s", (nuevo_num, fecha_nueva, lote_id))
                except Exception:
                    cur.execute("UPDATE lotes_pt SET numero_lote = %s WHERE id = %s", (nuevo_num, lote_id))
            else:
                cur.execute("UPDATE lotes_pt SET numero_lote = %s WHERE id = %s", (nuevo_num, lote_id))
        else:
            cur.execute("UPDATE lotes_mp SET numero_lote = %s WHERE id = %s", (nuevo_num, lote_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_LOTE_NUM", f"{nombre} | {numero_anterior} -> {nuevo_num}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *NUMERO DE LOTE ACTUALIZADO*\n{sep()}\n"
            f"{nombre}\n"
            f"Antes: {fmt_lote(numero_anterior)}\n"
            f"\U0001f7e2 Ahora: *{fmt_lote(nuevo_num)}*",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def editar_lote_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    # Si hay una cantidad alta pendiente de confirmar
    if context.user_data.get("edit_lote_pendiente") is not None:
        if texto.upper() == "SI":
            nueva_cant = context.user_data.pop("edit_lote_pendiente")
        else:
            context.user_data.pop("edit_lote_pendiente")
            await update.message.reply_text("Cantidad descartada. Escribe la cantidad correcta:")
            return EDIT_LOTE_VALOR
    else:
        ok, valor, err, confirmar = validar_cantidad(texto)
        if not ok:
            await update.message.reply_text(err)
            return EDIT_LOTE_VALOR
        if confirmar:
            context.user_data["edit_lote_pendiente"] = valor
            await update.message.reply_text(
                f"\u26a0\ufe0f {valor:,.0f} es mas de lo normal para un lote. Verifica que no sobre un cero.\n\n"
                f"Escribe *SI* para confirmar esa cantidad, o escribe la cantidad correcta:",
                parse_mode="Markdown"
            )
            return EDIT_LOTE_VALOR
        nueva_cant = valor
    tipo = context.user_data["edit_tipo"]
    edit_id = context.user_data["edit_id"]
    nombre = context.user_data["edit_nombre"]
    unidad = context.user_data["edit_unidad"]
    numero_lote = context.user_data["edit_lote_numero"]
    lote_id = context.user_data["edit_lote_id"]
    cant_anterior = context.user_data["edit_lote_actual"]
    try:
        now = ahora()
        conn = conectar()
        cur = conn.cursor()
        if tipo == "pt":
            cur.execute("UPDATE lotes_pt SET cantidad_actual = %s WHERE id = %s", (nueva_cant, lote_id))
            cur.execute("SELECT COALESCE(SUM(cantidad_actual), 0) FROM lotes_pt WHERE producto_id = %s", (edit_id,))
            nuevo_total = float(cur.fetchone()[0])
            cur.execute("UPDATE productos_terminados SET stock_actual = %s WHERE id = %s", (nuevo_total, edit_id))
        else:
            cur.execute("UPDATE lotes_mp SET cantidad_actual = %s WHERE id = %s", (nueva_cant, lote_id))
            delta = nueva_cant - cant_anterior
            cur.execute("UPDATE materias_primas SET stock_actual = GREATEST(stock_actual + %s, 0) WHERE id = %s", (delta, edit_id))
            cur.execute("SELECT stock_actual FROM materias_primas WHERE id = %s", (edit_id,))
            nuevo_total = float(cur.fetchone()[0])
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_LOTE", f"{nombre} | Lote {numero_lote}: {cant_anterior} -> {nueva_cant} {unidad} | Total: {nuevo_total}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *LOTE ACTUALIZADO*\n{sep()}\n"
            f"\U0001f9ea *{nombre}*\n"
            f"\U0001f4e6 Lote {fmt_lote(numero_lote)}\n"
            f"Anterior: {cant_anterior} {unidad}\n"
            f"\U0001f7e2 Nuevo: *{nueva_cant} {unidad}*\n"
            f"\U0001f4ca Stock total: *{nuevo_total} {unidad}*\n"
            f"\U0001f4c5 {now.strftime('%d/%m/%Y %H:%M')}",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol not in ['admin']:
        await update.message.reply_text("\u274c Sin acceso.")
        return
    try:
        conn = conectar()
        cur = conn.cursor()
        now = ahora()

        # PT total y valor en kg
        cur.execute("SELECT COALESCE(SUM(stock_actual), 0) FROM productos_terminados WHERE activo = true")
        pt_total = float(cur.fetchone()[0])

        # MP bajas/agotadas
        cur.execute("SELECT COUNT(*) FROM materias_primas WHERE activo = true AND stock_actual <= stock_minimo")
        mp_bajas = cur.fetchone()[0]

        # Pedidos del mes
        cur.execute("""
            SELECT COUNT(*) FROM pedidos
            WHERE DATE(fecha_pedido AT TIME ZONE 'America/Mexico_City') >= date_trunc('month', NOW() AT TIME ZONE 'America/Mexico_City')
        """)
        pedidos_mes = cur.fetchone()[0]

        # Pedidos de la semana
        cur.execute("""
            SELECT COUNT(*) FROM pedidos
            WHERE fecha_pedido >= date_trunc('week', NOW() AT TIME ZONE 'America/Mexico_City')
        """)
        pedidos_semana = cur.fetchone()[0]

        # Tambos en calle
        cur.execute("SELECT COALESCE(SUM(tambos_pendientes), 0) FROM clientes")
        tambos_calle = int(cur.fetchone()[0])

        # Tambos por lavar y listos
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'por_lavar'")
        por_lavar = int(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(cantidad), 0) FROM tambos_estado WHERE estado = 'listo'")
        listos = int(cur.fetchone()[0])

        # Top 3 productos con mas stock
        cur.execute("""
            SELECT nombre, stock_actual FROM productos_terminados
            WHERE activo = true AND stock_actual > 0
            ORDER BY stock_actual DESC LIMIT 3
        """)
        top_pt = cur.fetchall()

        # Clientes con tambos pendientes
        cur.execute("SELECT COUNT(*) FROM clientes WHERE tambos_pendientes > 0")
        clientes_deudores = cur.fetchone()[0]

        cur.close()
        conn.close()

        mensaje = f"\U0001f4ca *DASHBOARD EJECUTIVO*\n"
        mensaje += f"\U0001f4c5 {now.strftime('%d/%m/%Y %H:%M')}\n{sep()}\n\n"

        mensaje += f"\U0001f3ed *INVENTARIO PT*\n"
        mensaje += f"Total en stock: *{pt_total:,.0f} kg*\n"
        if top_pt:
            for nombre, stock in top_pt:
                mensaje += f"   \u2022 {nombre}: {float(stock):,.0f} kg\n"
        mensaje += "\n"

        mensaje += f"\u26a0\ufe0f *ALERTAS MP*\n"
        if mp_bajas > 0:
            mensaje += f"\U0001f534 {mp_bajas} materias primas bajas/agotadas\n"
        else:
            mensaje += f"\U0001f7e2 Todo el inventario MP en nivel\n"
        mensaje += "\n"

        mensaje += f"\U0001f69a *PEDIDOS*\n"
        mensaje += f"Esta semana: *{pedidos_semana}*\n"
        mensaje += f"Este mes: *{pedidos_mes}*\n\n"

        mensaje += f"\U0001f6e2\ufe0f *TAMBOS*\n"
        mensaje += f"En calle: *{tambos_calle}* ({clientes_deudores} clientes)\n"
        mensaje += f"Por lavar: *{por_lavar}*\n"
        mensaje += f"Listos: *{listos}*\n"

        await update.message.reply_text(mensaje, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")

async def admin_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c Solo admin.")
        await start(update, context)
        return ConversationHandler.END
    teclado = [
        ["\u2795 Agregar producto"],
        ["\u270f\ufe0f Editar datos de producto"],
        ["\U0001f4c9 Stock minimo"],
        ["\U0001f513 Activar/Desactivar producto"],
        ["\U0001f5d1\ufe0f Eliminar producto"],
        ["\U0001f9ea Recetas (formulas)"],
        ["\u274c Cancelar pedido"],
        ["\U0001f4b2 Precios"],
        ["\U0001f465 Empleados", "\U0001f3e2 Clientes"],
        ["\U0001f6e2\ufe0f Ajustar tambos", "\U0001f4e5 Exportar inventario"],
        ["Cancelar"]
    ]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\u2699\ufe0f *PANEL ADMIN*\n{sep()}\nQue quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return ADMIN_MENU

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Agregar producto" in texto:
        teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("Que tipo de producto vas a agregar?", reply_markup=menu)
        return ADD_TIPO
    elif "Editar datos" in texto:
        teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("De que tipo es el producto a editar?", reply_markup=menu)
        return EDITP_TIPO
    elif "Stock minimo" in texto:
        teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("De que tipo es el producto?", reply_markup=menu)
        return MIN_TIPO
    elif "Activar/Desactivar" in texto:
        teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("De que tipo es el producto?", reply_markup=menu)
        return TOGGLE_TIPO
    elif "Eliminar producto" in texto:
        teclado = [["\U0001f4e6 Materia Prima", "\U0001f3ed Producto Terminado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "\U0001f5d1\ufe0f *ELIMINAR PRODUCTO*\nDe que tipo es el producto que vas a eliminar?",
            reply_markup=menu, parse_mode="Markdown"
        )
        return DELPROD_TIPO
    elif "Recetas" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("SELECT id, nombre, codigo FROM productos_terminados WHERE activo = true ORDER BY nombre")
            productos = cur.fetchall()
            cur.close()
            conn.close()
            context.user_data["rec_productos"] = productos
            teclado = [[p[1]] for p in productos]
            teclado.append(["Cancelar"])
            menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
            await update.message.reply_text("Selecciona el producto cuya receta vas a ver/editar:", reply_markup=menu)
            return REC_PRODUCTO
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ADMIN_MENU
    elif "Cancelar pedido" in texto:
        await update.message.reply_text(
            "Escribe el *folio* del pedido a cancelar (ej: PED2606261430):",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        return CANC_FOLIO
    elif "Precios" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("""
                SELECT pt.codigo, pt.nombre, pp.precio_unitario, pp.moneda
                FROM precios_productos pp
                JOIN productos_terminados pt ON pt.id = pp.producto_id
                WHERE pp.cliente_id IS NULL
                ORDER BY pt.nombre
            """)
            precios = cur.fetchall()
            cur.close()
            conn.close()
            mensaje = f"\U0001f4b2 *PRECIOS ACTUALES*\n{sep()}\n"
            if precios:
                for codigo, nombre, precio, moneda in precios:
                    mensaje += f"*{codigo}* \u2014 {nombre}: {fmt_moneda(float(precio), moneda)}/kg\n"
            else:
                mensaje += "_Sin precios registrados aun._\n"
            mensaje += f"\n{sep()}\nPara actualizar un precio escribe:\n`CODIGO PRECIO [MONEDA]`\nEjemplo: `PS 1.75 USD`\n\nO escribe *Cancelar* para salir."
            await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
            return PRECIOS_ACTUALIZAR
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ADMIN_MENU
    elif "Empleados" in texto:
        teclado = [["\u2795 Agregar empleado"], ["\u270f\ufe0f Editar empleado"], ["Cancelar"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("\U0001f465 *EMPLEADOS*\nQue quieres hacer?", reply_markup=menu, parse_mode="Markdown")
        return EMP_MENU
    elif "Clientes" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("SELECT id, nombre, COALESCE(telefono,''), tambos_pendientes FROM clientes ORDER BY nombre")
            clientes = cur.fetchall()
            cur.close()
            conn.close()
            if not clientes:
                await update.message.reply_text("No hay clientes registrados.")
                return ADMIN_MENU
            context.user_data["cli_lista"] = clientes
            teclado = [[c[1]] for c in clientes]
            teclado.append(["Cancelar"])
            menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
            await update.message.reply_text("\U0001f3e2 Selecciona el cliente:", reply_markup=menu)
            return CLI_SELECT
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ADMIN_MENU
    elif "Ajustar tambos" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(tambos_pendientes),0) FROM clientes")
            en_calle = int(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(cantidad),0) FROM tambos_estado WHERE estado='por_lavar'")
            por_lavar = int(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(cantidad),0) FROM tambos_estado WHERE estado='listo'")
            listos = int(cur.fetchone()[0])
            cur.close()
            conn.close()
            await update.message.reply_text(
                f"\U0001f6e2\ufe0f *AJUSTE DE TAMBOS*\n{sep()}\n"
                f"Estado actual:\n"
                f"   Por lavar: {por_lavar}\n"
                f"   Listos: {listos}\n"
                f"   En calle: {en_calle}\n\n"
                f"_Nota: 'en calle' se ajusta por cliente, no aqui._",
                parse_mode="Markdown"
            )
            teclado = [["Por lavar", "Listos"], ["Cancelar"]]
            menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
            await update.message.reply_text("Cual estado quieres corregir?", reply_markup=menu)
            return TAMB_ESTADO
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ADMIN_MENU
    elif "Exportar inventario" in texto:
        return await exportar_inventario(update, context)
    else:
        await update.message.reply_text("Selecciona una opcion de la lista.")
        return ADMIN_MENU

# ========== PRECIOS ==========
async def precios_actualizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto.lower() in ("cancelar", "salir"):
        await update.message.reply_text("Listo.", reply_markup=ReplyKeyboardRemove())
        await admin_inicio(update, context)
        return ADMIN_MENU
    partes = texto.split()
    if len(partes) < 2:
        await update.message.reply_text(
            "Formato invalido. Escribe: `CODIGO PRECIO [MONEDA]` (ej: `PS 1.75 USD`) o *Cancelar*.",
            parse_mode="Markdown"
        )
        return PRECIOS_ACTUALIZAR
    codigo = partes[0].upper()
    try:
        precio = float(partes[1])
    except ValueError:
        await update.message.reply_text("El precio debe ser un numero. Intenta de nuevo o escribe *Cancelar*.", parse_mode="Markdown")
        return PRECIOS_ACTUALIZAR
    moneda = partes[2].upper() if len(partes) >= 3 else "USD"
    if moneda not in ("USD", "MXN"):
        await update.message.reply_text("Moneda invalida. Usa USD o MXN.")
        return PRECIOS_ACTUALIZAR
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nombre FROM productos_terminados WHERE UPPER(TRIM(codigo)) = %s", (codigo,))
        prod = cur.fetchone()
        if not prod:
            await update.message.reply_text(f"❌ No encontre un producto con codigo *{codigo}*. Intenta de nuevo o escribe *Cancelar*.", parse_mode="Markdown")
            cur.close()
            conn.close()
            return PRECIOS_ACTUALIZAR
        prod_id, prod_nombre = prod
        cur.execute("""
            INSERT INTO precios_productos (producto_id, cliente_id, precio_unitario, moneda)
            VALUES (%s, NULL, %s, %s)
            ON CONFLICT (producto_id) WHERE cliente_id IS NULL
            DO UPDATE SET precio_unitario = EXCLUDED.precio_unitario, moneda = EXCLUDED.moneda, updated_at = now()
        """, (prod_id, precio, moneda))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "PRECIO", f"{prod_nombre} ({codigo}) -> {fmt_moneda(precio, moneda)}/kg")
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(
            f"✅ Precio actualizado: *{prod_nombre}* ({codigo}) → {fmt_moneda(precio, moneda)}/kg\n\n"
            f"Escribe otro `CODIGO PRECIO [MONEDA]` o *Cancelar* para salir.",
            parse_mode="Markdown"
        )
        return PRECIOS_ACTUALIZAR
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return PRECIOS_ACTUALIZAR

# ========== AGREGAR PRODUCTO ==========
async def add_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Materia" in texto:
        context.user_data["add_tipo"] = "mp"
    elif "Producto" in texto:
        context.user_data["add_tipo"] = "pt"
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return ADD_TIPO
    await update.message.reply_text("Escribe el *nombre* del producto:", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ADD_NOMBRE

async def add_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_nombre"] = update.message.text.strip()
    await update.message.reply_text("Escribe el *codigo* corto (ej: ALK, PS):", parse_mode="Markdown")
    return ADD_CODIGO

async def add_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codigo = " ".join(update.message.text.strip().upper().split())
    # Verificar que no exista
    try:
        conn = conectar()
        cur = conn.cursor()
        tabla = "materias_primas" if context.user_data["add_tipo"] == "mp" else "productos_terminados"
        cur.execute(f"SELECT nombre FROM {tabla} WHERE UPPER(TRIM(codigo)) = %s", (codigo,))
        existe = cur.fetchone()
        cur.close()
        conn.close()
        if existe:
            await update.message.reply_text(f"\u274c El codigo {codigo} ya existe ({existe[0]}). Escribe otro:")
            return ADD_CODIGO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END
    context.user_data["add_codigo"] = codigo
    teclado = [["kg", "litros"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text("Selecciona la *unidad*:", reply_markup=menu, parse_mode="Markdown")
    return ADD_UNIDAD

async def add_unidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_unidad"] = update.message.text.strip()
    if context.user_data["add_tipo"] == "pt":
        teclado = [["Tambo 200 kg", "Cunete 25 kg"], ["Cunete 40 kg", "Saco 25 kg"], ["Otro"]]
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("Selecciona la *presentacion*:", reply_markup=menu, parse_mode="Markdown")
        return ADD_PRESENTACION
    else:
        context.user_data["add_presentacion"] = ""
        await update.message.reply_text("Escribe el *stock actual* (cantidad):", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return ADD_STOCK

async def add_presentacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_presentacion"] = update.message.text.strip()
    await update.message.reply_text("Escribe el *stock actual* (cantidad):", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ADD_STOCK

async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add_stock"] = float(update.message.text.strip())
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return ADD_STOCK
    await update.message.reply_text("Escribe el *stock minimo* (para alertas, o 0 si no quieres):", parse_mode="Markdown")
    return ADD_MINIMO

async def add_minimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["add_minimo"] = float(update.message.text.strip())
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return ADD_MINIMO
    d = context.user_data
    tipo_txt = "Materia Prima" if d["add_tipo"] == "mp" else "Producto Terminado"
    pres_txt = f"\n\U0001f4cf Presentacion: {d['add_presentacion']}" if d.get("add_presentacion") else ""
    await update.message.reply_text(
        f"\U0001f4cb *CONFIRMAR NUEVO PRODUCTO*\n{sep()}\n"
        f"Tipo: {tipo_txt}\n"
        f"\U0001f9ea Nombre: *{d['add_nombre']}*\n"
        f"\U0001f516 Codigo: *{d['add_codigo']}*\n"
        f"\u2696\ufe0f Unidad: {d['add_unidad']}{pres_txt}\n"
        f"\U0001f4ca Stock: {d['add_stock']} {d['add_unidad']}\n"
        f"\U0001f4c9 Minimo: {d['add_minimo']} {d['add_unidad']}\n\n"
        f"Escribe *SI* para confirmar o *NO* para cancelar",
        parse_mode="Markdown"
    )
    return ADD_CONFIRMAR

async def add_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().upper() != "SI":
        await update.message.reply_text("\u274c Cancelado.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    d = context.user_data
    try:
        conn = conectar()
        cur = conn.cursor()
        if d["add_tipo"] == "mp":
            cur.execute("""
                INSERT INTO materias_primas (nombre, codigo, stock_actual, stock_minimo, unidad, activo)
                VALUES (%s, %s, %s, %s, %s, true)
            """, (d["add_nombre"], d["add_codigo"], d["add_stock"], d["add_minimo"], d["add_unidad"]))
        else:
            cur.execute("""
                INSERT INTO productos_terminados (codigo, nombre, categoria, unidad, stock_actual, stock_minimo, metodo_salida, presentacion, kg_por_unidad, activo)
                VALUES (%s, %s, 'Auxiliar', %s, %s, %s, 'FIFO', %s, %s, true)
            """, (d["add_codigo"], d["add_nombre"], d["add_unidad"], d["add_stock"], d["add_minimo"], d["add_presentacion"] or "Tambo 200 kg", 200))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "ADD_PRODUCTO", f"{d['add_tipo'].upper()} {d['add_nombre']} ({d['add_codigo']})")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *PRODUCTO AGREGADO*\n{sep()}\n{d['add_nombre']} ({d['add_codigo']})\nYa puedes usarlo en el bot.",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== EDITAR DATOS DE PRODUCTO ==========
async def editp_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = "mp" if "Materia" in texto else "pt" if "Producto" in texto else None
    if not tipo:
        await update.message.reply_text("Selecciona de la lista.")
        return EDITP_TIPO
    context.user_data["editp_tipo"] = tipo
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"SELECT id, nombre, COALESCE(codigo,'') FROM {tabla} WHERE activo = true ORDER BY nombre")
        productos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["editp_productos"] = productos
        teclado = [[p[1]] for p in productos]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("Selecciona el producto:", reply_markup=menu)
        return EDITP_PRODUCTO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def editp_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("editp_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("Selecciona de la lista.")
        return EDITP_PRODUCTO
    context.user_data["editp_id"] = producto[0]
    context.user_data["editp_nombre"] = producto[1]
    teclado = [["Nombre", "Codigo"]]
    if context.user_data["editp_tipo"] == "pt":
        teclado.append(["Presentacion"])
    teclado.append(["Cancelar"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\u270f\ufe0f *{producto[1]}* (codigo: {producto[2]})\nQue dato quieres cambiar?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return EDITP_CAMPO

async def editp_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    campo = update.message.text.strip()
    if campo == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    mapa = {"Nombre": "nombre", "Codigo": "codigo", "Presentacion": "presentacion"}
    if campo not in mapa:
        await update.message.reply_text("Selecciona de la lista.")
        return EDITP_CAMPO
    context.user_data["editp_campo"] = mapa[campo]
    await update.message.reply_text(f"Escribe el nuevo valor para *{campo}*:", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return EDITP_VALOR

async def editp_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valor = update.message.text.strip()
    campo = context.user_data["editp_campo"]
    if campo == "codigo":
        valor = " ".join(valor.upper().split())
    tipo = context.user_data["editp_tipo"]
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    edit_id = context.user_data["editp_id"]
    nombre_ant = context.user_data["editp_nombre"]
    try:
        conn = conectar()
        cur = conn.cursor()
        # Si es codigo, validar que no exista
        if campo == "codigo":
            cur.execute(f"SELECT nombre FROM {tabla} WHERE UPPER(TRIM(codigo)) = %s AND id <> %s", (valor, edit_id))
            if cur.fetchone():
                await update.message.reply_text(f"\u274c El codigo {valor} ya esta en uso. Intenta otro:")
                cur.close()
                conn.close()
                return EDITP_VALOR
        cur.execute(f"UPDATE {tabla} SET {campo} = %s WHERE id = %s", (valor, edit_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_PRODUCTO", f"{nombre_ant} | {campo} -> {valor}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *ACTUALIZADO*\n{sep()}\n{nombre_ant}\n{campo}: *{valor}*",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== STOCK MINIMO ==========
async def min_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = "mp" if "Materia" in texto else "pt" if "Producto" in texto else None
    if not tipo:
        await update.message.reply_text("Selecciona de la lista.")
        return MIN_TIPO
    context.user_data["min_tipo"] = tipo
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"SELECT id, nombre, stock_minimo, unidad FROM {tabla} WHERE activo = true ORDER BY nombre")
        productos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["min_productos"] = productos
        teclado = [[p[1]] for p in productos]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("Selecciona el producto:", reply_markup=menu)
        return MIN_PRODUCTO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def min_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("min_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("Selecciona de la lista.")
        return MIN_PRODUCTO
    context.user_data["min_id"] = producto[0]
    context.user_data["min_nombre"] = producto[1]
    context.user_data["min_unidad"] = producto[3]
    await update.message.reply_text(
        f"\U0001f4c9 *{producto[1]}*\nMinimo actual: {producto[2]} {producto[3]}\n\nEscribe el nuevo stock minimo:",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return MIN_VALOR

async def min_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nuevo = float(update.message.text.strip())
        if nuevo < 0:
            await update.message.reply_text("No puede ser negativo. Intenta de nuevo:")
            return MIN_VALOR
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return MIN_VALOR
    tipo = context.user_data["min_tipo"]
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    min_id = context.user_data["min_id"]
    nombre = context.user_data["min_nombre"]
    unidad = context.user_data["min_unidad"]
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"UPDATE {tabla} SET stock_minimo = %s WHERE id = %s", (nuevo, min_id))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "STOCK_MINIMO", f"{nombre} | minimo -> {nuevo} {unidad}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *STOCK MINIMO ACTUALIZADO*\n{sep()}\n{nombre}\nNuevo minimo: *{nuevo} {unidad}*",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END


# ========== ACTIVAR / DESACTIVAR PRODUCTO ==========
async def toggle_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = "mp" if "Materia" in texto else "pt" if "Producto" in texto else None
    if not tipo:
        await update.message.reply_text("Selecciona de la lista.")
        return TOGGLE_TIPO
    context.user_data["toggle_tipo"] = tipo
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"SELECT id, nombre, COALESCE(codigo,''), activo FROM {tabla} ORDER BY nombre")
        productos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["toggle_productos"] = productos
        teclado = []
        for p in productos:
            estado = "\U0001f7e2" if p[3] else "\U0001f534"
            teclado.append([f"{estado} {p[1]}"])
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "\U0001f7e2 = activo | \U0001f534 = inactivo\nSelecciona el producto para cambiar su estado:",
            reply_markup=menu
        )
        return TOGGLE_PRODUCTO
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def toggle_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    # quitar el emoji del inicio
    nombre = texto.replace("\U0001f7e2", "").replace("\U0001f534", "").strip()
    productos = context.user_data.get("toggle_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("Selecciona de la lista.")
        return TOGGLE_PRODUCTO
    tipo = context.user_data["toggle_tipo"]
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    nuevo_estado = not producto[3]
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"UPDATE {tabla} SET activo = %s WHERE id = %s", (nuevo_estado, producto[0]))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "TOGGLE_PRODUCTO", f"{producto[1]} -> {'ACTIVO' if nuevo_estado else 'INACTIVO'}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        estado_txt = "\U0001f7e2 ACTIVADO" if nuevo_estado else "\U0001f534 DESACTIVADO"
        await update.message.reply_text(
            f"\u2705 *{producto[1]}*\nAhora esta: *{estado_txt}*",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== ELIMINAR PRODUCTO (MP / PT) ==========
async def delprod_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = "mp" if "Materia" in texto else "pt" if "Producto" in texto else None
    if not tipo:
        await update.message.reply_text("Selecciona de la lista.")
        return DELPROD_TIPO
    context.user_data["delprod_tipo"] = tipo
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"SELECT id, nombre, COALESCE(codigo,'') FROM {tabla} ORDER BY nombre")
        productos = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["delprod_lista"] = productos
        teclado = [[p[1]] for p in productos]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text(
            "Selecciona el producto que vas a eliminar:",
            reply_markup=menu
        )
        return DELPROD_SELECT
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def delprod_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("delprod_lista", [])
    producto = next((p for p in productos if p[1] == texto), None)
    if not producto:
        await update.message.reply_text("Selecciona de la lista.")
        return DELPROD_SELECT
    tipo = context.user_data["delprod_tipo"]
    context.user_data["delprod_id"] = producto[0]
    context.user_data["delprod_nombre"] = producto[1]

    # Proteccion: si es MP y esta en una receta activa, bloquear
    if tipo == "mp":
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT pt.nombre
                FROM receta_items ri
                JOIN recetas r ON r.id = ri.receta_id
                JOIN productos_terminados pt ON pt.id = r.producto_id
                WHERE ri.materia_prima_id = %s AND r.activa = TRUE
            """, (producto[0],))
            en_recetas = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ConversationHandler.END
        if en_recetas:
            lista = ", ".join(en_recetas)
            await update.message.reply_text(
                f"\U0001f6ab *No se puede eliminar*\n{sep()}\n"
                f"*{producto[1]}* esta en la receta de: {lista}\n\n"
                f"Primero quitala de esas recetas (Admin > Recetas), luego podras eliminarla.",
                reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
            )
            context.user_data.clear()
            await start(update, context)
            return ConversationHandler.END

    # Ofrecer desactivar o eliminar de verdad
    teclado = [["\U0001f534 Solo desactivar"], ["\U0001f5d1\ufe0f Eliminar de verdad"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f4e6 *{producto[1]}*\n{sep()}\n"
        f"*Desactivar:* deja de aparecer pero se guarda el historial (reversible).\n\n"
        f"*Eliminar de verdad:* desaparece por completo, para siempre.\n\n"
        f"Que quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return DELPROD_CONFIRM

async def delprod_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        context.user_data.clear()
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    tipo = context.user_data["delprod_tipo"]
    prod_id = context.user_data["delprod_id"]
    nombre = context.user_data["delprod_nombre"]
    tabla = "materias_primas" if tipo == "mp" else "productos_terminados"
    try:
        conn = conectar()
        cur = conn.cursor()
        if "desactivar" in texto.lower():
            cur.execute(f"UPDATE {tabla} SET activo = FALSE WHERE id = %s", (prod_id,))
            accion_txt = "\U0001f534 DESACTIVADO (se puede reactivar despues)"
            registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
                "DESACTIVAR_PRODUCTO", f"{nombre}")
        elif "eliminar" in texto.lower():
            # Borrar lotes y referencias primero, luego el producto
            if tipo == "mp":
                cur.execute("DELETE FROM lotes_mp WHERE materia_prima_id = %s", (prod_id,))
                cur.execute("DELETE FROM materias_primas WHERE id = %s", (prod_id,))
            else:
                cur.execute("DELETE FROM pedido_items WHERE producto_id = %s", (prod_id,))
                cur.execute("DELETE FROM lotes_pt WHERE producto_id = %s", (prod_id,))
                # borrar recetas del producto y sus items
                cur.execute("SELECT id FROM recetas WHERE producto_id = %s", (prod_id,))
                for (rid,) in cur.fetchall():
                    cur.execute("DELETE FROM receta_items WHERE receta_id = %s", (rid,))
                cur.execute("DELETE FROM recetas WHERE producto_id = %s", (prod_id,))
                cur.execute("DELETE FROM productos_terminados WHERE id = %s", (prod_id,))
            accion_txt = "\U0001f5d1\ufe0f ELIMINADO por completo"
            registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
                "ELIMINAR_PRODUCTO", f"{nombre} ({tipo})")
        else:
            await update.message.reply_text("Selecciona una opcion de la lista.")
            return DELPROD_CONFIRM
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *{nombre}*\n{accion_txt}",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== RECETAS ==========
async def rec_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    productos = context.user_data.get("rec_productos", [])
    producto = next((p for p in productos if p[1] == nombre), None)
    if not producto:
        await update.message.reply_text("Selecciona de la lista.")
        return REC_PRODUCTO
    context.user_data["rec_prod_id"] = producto[0]
    context.user_data["rec_prod_nombre"] = producto[1]
    # Mostrar receta actual
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, cantidad_base FROM recetas WHERE producto_id = %s AND activa = true", (producto[0],))
        receta = cur.fetchone()
        receta_txt = ""
        if receta:
            context.user_data["rec_receta_id"] = receta[0]
            context.user_data["rec_base"] = float(receta[1])
            cur.execute("""
                SELECT mp.nombre, ri.cantidad, mp.unidad
                FROM receta_items ri JOIN materias_primas mp ON mp.id = ri.materia_prima_id
                WHERE ri.receta_id = %s ORDER BY mp.nombre
            """, (receta[0],))
            items = cur.fetchall()
            receta_txt = f"\n*Receta actual* (base {float(receta[1]):,.0f} kg):\n"
            for n, c, u in items:
                receta_txt += f"   \u2022 {n}: {float(c):,.2f} {u}\n"
        else:
            context.user_data["rec_receta_id"] = None
            receta_txt = "\n_Este producto no tiene receta aun._\n"
        cur.close()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

    teclado = [["\u2795 Agregar ingrediente"]]
    if context.user_data.get("rec_receta_id"):
        teclado.append(["\u2796 Quitar ingrediente"])
        teclado.append(["\U0001f522 Cambiar cantidad base"])
    teclado.append(["Cancelar"])
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f9ea *{producto[1]}*{receta_txt}\nQue quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return REC_ACCION

async def rec_accion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Cambiar cantidad base" in texto:
        await update.message.reply_text(
            f"Base actual: {context.user_data.get('rec_base', 0):,.0f} kg\nEscribe la nueva cantidad base:",
            reply_markup=ReplyKeyboardRemove()
        )
        return REC_BASE
    elif "Agregar ingrediente" in texto:
        # Si no hay receta, crearla primero (pedir base)
        if not context.user_data.get("rec_receta_id"):
            await update.message.reply_text(
                "Primero define la *cantidad base* de la receta (ej: 1000 kg de producto):",
                reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
            )
            context.user_data["rec_crear"] = True
            return REC_BASE
        return await rec_listar_mp(update, context)
    elif "Quitar ingrediente" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("""
                SELECT mp.id, mp.nombre, ri.cantidad
                FROM receta_items ri JOIN materias_primas mp ON mp.id = ri.materia_prima_id
                WHERE ri.receta_id = %s ORDER BY mp.nombre
            """, (context.user_data["rec_receta_id"],))
            items = cur.fetchall()
            cur.close()
            conn.close()
            if not items:
                await update.message.reply_text("La receta no tiene ingredientes.")
                return REC_ACCION
            context.user_data["rec_items_del"] = items
            teclado = [[i[1]] for i in items]
            teclado.append(["Cancelar"])
            menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
            await update.message.reply_text("Cual ingrediente quieres quitar?", reply_markup=menu)
            return REC_ITEM_DEL
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return REC_ACCION
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return REC_ACCION

async def rec_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        base = float(update.message.text.strip())
        if base <= 0:
            await update.message.reply_text("Debe ser mayor a cero.")
            return REC_BASE
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return REC_BASE
    prod_id = context.user_data["rec_prod_id"]
    try:
        conn = conectar()
        cur = conn.cursor()
        if context.user_data.get("rec_crear"):
            # Crear receta nueva
            cur.execute("INSERT INTO recetas (producto_id, cantidad_base, activa) VALUES (%s, %s, true) RETURNING id", (prod_id, base))
            context.user_data["rec_receta_id"] = cur.fetchone()[0]
            context.user_data["rec_crear"] = False
            registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
                "RECETA_CREAR", f"{context.user_data['rec_prod_nombre']} | base {base} kg")
            conn.commit()
            cur.close()
            conn.close()
            context.user_data["rec_base"] = base
            return await rec_listar_mp(update, context)
        else:
            # Actualizar base existente
            receta_id = context.user_data["rec_receta_id"]
            prod_id = context.user_data["rec_prod_id"]
            prod_nombre = context.user_data["rec_prod_nombre"]
            cur.execute("UPDATE recetas SET cantidad_base = %s WHERE id = %s", (base, receta_id))
            guardar_receta_historico(cur, receta_id, prod_id, prod_nombre, base,
                f"Cambio de base: {base} kg", update.message.from_user.first_name)
            registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
                "RECETA_BASE", f"{prod_nombre} | base -> {base} kg")
            conn.commit()
            cur.close()
            conn.close()
            context.user_data.clear()
            await update.message.reply_text(f"\u2705 Cantidad base actualizada a {base:,.0f} kg", reply_markup=ReplyKeyboardRemove())
            await start(update, context)
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def rec_listar_mp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, unidad FROM materias_primas WHERE activo = true ORDER BY nombre")
        mps = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["rec_mps"] = mps
        teclado = [[m[1]] for m in mps]
        teclado.append(["Cancelar"])
        menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
        await update.message.reply_text("Selecciona la materia prima a agregar:", reply_markup=menu)
        return REC_MP_SELECT
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def rec_mp_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    mps = context.user_data.get("rec_mps", [])
    mp = next((m for m in mps if m[1] == nombre), None)
    if not mp:
        await update.message.reply_text("Selecciona de la lista.")
        return REC_MP_SELECT
    context.user_data["rec_mp_id"] = mp[0]
    context.user_data["rec_mp_nombre"] = mp[1]
    context.user_data["rec_mp_unidad"] = mp[2]
    base = context.user_data.get("rec_base", 0)
    await update.message.reply_text(
        f"\U0001f9ea {mp[1]}\nPor cada {base:,.0f} kg de producto, cuantos {mp[2]} de {mp[1]} se usan?",
        reply_markup=ReplyKeyboardRemove()
    )
    return REC_MP_CANTIDAD

async def rec_mp_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cantidad = float(update.message.text.strip())
        if cantidad <= 0:
            await update.message.reply_text("Debe ser mayor a cero.")
            return REC_MP_CANTIDAD
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return REC_MP_CANTIDAD
    receta_id = context.user_data["rec_receta_id"]
    mp_id = context.user_data["rec_mp_id"]
    mp_nombre = context.user_data["rec_mp_nombre"]
    prod_id = context.user_data["rec_prod_id"]
    prod_nombre = context.user_data["rec_prod_nombre"]
    try:
        conn = conectar()
        cur = conn.cursor()
        # Si ya existe el ingrediente, actualizar; si no, insertar
        cur.execute("SELECT id FROM receta_items WHERE receta_id = %s AND materia_prima_id = %s", (receta_id, mp_id))
        existe = cur.fetchone()
        if existe:
            cur.execute("UPDATE receta_items SET cantidad = %s WHERE id = %s", (cantidad, existe[0]))
        else:
            cur.execute("INSERT INTO receta_items (receta_id, materia_prima_id, cantidad) VALUES (%s, %s, %s)", (receta_id, mp_id, cantidad))
        # Registrar cambio en historico
        cur.execute("SELECT cantidad_base FROM recetas WHERE id = %s", (receta_id,))
        base = cur.fetchone()[0]
        guardar_receta_historico(cur, receta_id, prod_id, prod_nombre, base,
            f"Ingrediente {mp_nombre}: {cantidad}", update.message.from_user.first_name)
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "RECETA_ITEM", f"{prod_nombre} | {mp_nombre} {cantidad}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *INGREDIENTE AGREGADO*\n{sep()}\n{mp_nombre}: {cantidad}\nYa quedo en la receta.",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def rec_item_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre = update.message.text.strip()
    if nombre == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    items = context.user_data.get("rec_items_del", [])
    item = next((i for i in items if i[1] == nombre), None)
    if not item:
        await update.message.reply_text("Selecciona de la lista.")
        return REC_ITEM_DEL
    try:
        conn = conectar()
        cur = conn.cursor()
        receta_id = context.user_data["rec_receta_id"]
        prod_id = context.user_data["rec_prod_id"]
        prod_nombre = context.user_data["rec_prod_nombre"]
        cur.execute("DELETE FROM receta_items WHERE receta_id = %s AND materia_prima_id = %s", (receta_id, item[0]))
        # Registrar cambio en historico
        cur.execute("SELECT cantidad_base FROM recetas WHERE id = %s", (receta_id,))
        base = cur.fetchone()[0]
        guardar_receta_historico(cur, receta_id, prod_id, prod_nombre, base,
            f"Eliminado ingrediente: {item[1]}", update.message.from_user.first_name)
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "RECETA_ITEM_DEL", f"{prod_nombre} | -{item[1]}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(f"\u2705 Quitado: {item[1]}", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== CANCELAR PEDIDO ==========
async def canc_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folio = update.message.text.strip().upper()
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.folio, c.nombre, p.tambos_enviados, p.fecha_pedido
            FROM pedidos p JOIN clientes c ON c.id = p.cliente_id
            WHERE UPPER(p.folio) = %s
        """, (folio,))
        pedido = cur.fetchone()
        if not pedido:
            cur.close()
            conn.close()
            await update.message.reply_text(f"\u274c No encontre el folio {folio}. Verifica e intenta de nuevo, o /cancelar:")
            return CANC_FOLIO
        # Mostrar detalle
        cur.execute("""
            SELECT pt.nombre, pi.cantidad, l.numero_lote
            FROM pedido_items pi
            JOIN productos_terminados pt ON pt.id = pi.producto_id
            LEFT JOIN lotes_pt l ON l.id = pi.lote_pt_id
            WHERE pi.pedido_id = %s
        """, (pedido[0],))
        items = cur.fetchall()
        cur.close()
        conn.close()
        context.user_data["canc_pedido_id"] = pedido[0]
        context.user_data["canc_folio"] = pedido[1]
        context.user_data["canc_cliente"] = pedido[2]
        context.user_data["canc_tambos"] = int(pedido[3])
        msg = f"\u26a0\ufe0f *CANCELAR PEDIDO*\n{sep()}\n"
        msg += f"\U0001f4cb Folio: {pedido[1]}\n"
        msg += f"\U0001f465 Cliente: {pedido[2]}\n"
        msg += f"\U0001f4c5 {pedido[4].astimezone(CST).strftime('%d/%m/%Y %H:%M')}\n\n"
        msg += "*Se devolvera al inventario:*\n"
        for n, c, lote in items:
            msg += f"   \u2022 {n}: {float(c):,.0f} kg ({fmt_lote(lote) if lote else 's/lote'})\n"
        msg += f"\n\U0001f6e2\ufe0f Tambos a restar al cliente: {pedido[3]}\n\n"
        msg += "Escribe *SI* para confirmar la cancelacion o *NO* para abortar"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return CANC_CONFIRMAR
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        return ConversationHandler.END

async def canc_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().upper() != "SI":
        await update.message.reply_text("\u274c Cancelacion abortada. El pedido sigue activo.")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END
    pedido_id = context.user_data["canc_pedido_id"]
    folio = context.user_data["canc_folio"]
    cliente = context.user_data["canc_cliente"]
    tambos = context.user_data["canc_tambos"]
    try:
        conn = conectar()
        cur = conn.cursor()
        # Devolver cada item a su lote y al stock del producto
        cur.execute("SELECT producto_id, lote_pt_id, cantidad FROM pedido_items WHERE pedido_id = %s", (pedido_id,))
        items = cur.fetchall()
        for prod_id, lote_id, cantidad in items:
            if lote_id:
                cur.execute("UPDATE lotes_pt SET cantidad_actual = cantidad_actual + %s WHERE id = %s", (cantidad, lote_id))
            cur.execute("UPDATE productos_terminados SET stock_actual = stock_actual + %s WHERE id = %s", (cantidad, prod_id))
        # Restar tambos al cliente
        if tambos > 0:
            cur.execute("""
                UPDATE clientes SET tambos_pendientes = GREATEST(tambos_pendientes - %s, 0)
                WHERE id = (SELECT cliente_id FROM pedidos WHERE id = %s)
            """, (tambos, pedido_id))
            cur.execute("""
                INSERT INTO tambos_movimientos (cliente_id, tipo, cantidad, notas)
                SELECT cliente_id, 'cancelacion', %s, %s FROM pedidos WHERE id = %s
            """, (tambos, f"Pedido cancelado: {folio}", pedido_id))
        # Desvincular movimientos de tambos de este pedido antes de borrarlo
        # (evita violar la FK tambos_movimientos.pedido_id -> pedidos.id; el
        # historial de movimientos se conserva, solo se le quita la referencia)
        cur.execute("UPDATE tambos_movimientos SET pedido_id = NULL WHERE pedido_id = %s", (pedido_id,))
        # Borrar items y pedido
        cur.execute("DELETE FROM pedido_items WHERE pedido_id = %s", (pedido_id,))
        cur.execute("DELETE FROM pedidos WHERE id = %s", (pedido_id,))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "CANCELAR_PEDIDO", f"Folio {folio} | Cliente {cliente} | {tambos} tambos devueltos")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *PEDIDO CANCELADO*\n{sep()}\n"
            f"\U0001f4cb Folio: {folio}\n"
            f"\U0001f7e2 Inventario devuelto\n"
            f"\U0001f6e2\ufe0f Tambos restados al cliente: {tambos}",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END


# ========== EMPLEADOS ==========
async def emp_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Agregar" in texto:
        await update.message.reply_text("Escribe el *nombre* del empleado:", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return EMP_ADD_NOMBRE
    elif "Editar" in texto:
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("SELECT id, nombre, activo, COALESCE(telegram_id,0) FROM empleados ORDER BY nombre")
            emps = cur.fetchall()
            cur.close()
            conn.close()
            context.user_data["emp_lista"] = emps
            teclado = []
            for e in emps:
                estado = "\U0001f7e2" if e[2] else "\U0001f534"
                teclado.append([f"{estado} {e[1]}"])
            teclado.append(["Cancelar"])
            menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
            await update.message.reply_text("Selecciona el empleado:", reply_markup=menu)
            return EMP_SELECT
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return EMP_MENU
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return EMP_MENU

async def emp_add_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["emp_nombre"] = update.message.text.strip()
    await update.message.reply_text(
        "Escribe el *Telegram ID* del empleado (numero), o escribe *0* si aun no lo tienes:",
        parse_mode="Markdown"
    )
    return EMP_ADD_ID

async def emp_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tid = int(update.message.text.strip())
    except:
        await update.message.reply_text("Escribe un numero valido (o 0):")
        return EMP_ADD_ID
    nombre = context.user_data["emp_nombre"]
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute("INSERT INTO empleados (nombre, activo, telegram_id) VALUES (%s, true, %s)", (nombre, str(tid) if tid > 0 else None))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "ADD_EMPLEADO", f"{nombre} | tid: {tid}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 *EMPLEADO AGREGADO*\n{sep()}\n{nombre}\n"
            + (f"Telegram ID: {tid}" if tid > 0 else "_Sin Telegram ID por ahora_"),
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

async def emp_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    nombre = texto.replace("\U0001f7e2", "").replace("\U0001f534", "").strip()
    emps = context.user_data.get("emp_lista", [])
    emp = next((e for e in emps if e[1] == nombre), None)
    if not emp:
        await update.message.reply_text("Selecciona de la lista.")
        return EMP_SELECT
    context.user_data["emp_id"] = emp[0]
    context.user_data["emp_sel_nombre"] = emp[1]
    context.user_data["emp_activo"] = emp[2]
    estado = "activo \U0001f7e2" if emp[2] else "inactivo \U0001f534"
    teclado = [["Renombrar", "Cambiar Telegram ID"], ["Activar/Desactivar"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f464 *{emp[1]}* ({estado})\nQue quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return EMP_EDIT_CAMPO

async def emp_edit_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Activar/Desactivar" in texto:
        nuevo = not context.user_data["emp_activo"]
        try:
            conn = conectar()
            cur = conn.cursor()
            cur.execute("UPDATE empleados SET activo = %s WHERE id = %s", (nuevo, context.user_data["emp_id"]))
            registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
                "EDIT_EMPLEADO", f"{context.user_data['emp_sel_nombre']} -> {'ACTIVO' if nuevo else 'INACTIVO'}")
            conn.commit()
            cur.close()
            conn.close()
            context.user_data.clear()
            estado_msg = "ACTIVO \U0001f7e2" if nuevo else "INACTIVO \U0001f534"
            await update.message.reply_text(
                f"\u2705 Empleado ahora *{estado_msg}*",
                reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
            )
            await start(update, context)
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"\u274c Error: {e}")
            return ConversationHandler.END
    elif "Renombrar" in texto:
        context.user_data["emp_campo"] = "nombre"
        await update.message.reply_text("Escribe el nuevo nombre:", reply_markup=ReplyKeyboardRemove())
        return EMP_EDIT_VALOR
    elif "Cambiar Telegram ID" in texto:
        context.user_data["emp_campo"] = "telegram_id"
        await update.message.reply_text("Escribe el nuevo Telegram ID (numero):", reply_markup=ReplyKeyboardRemove())
        return EMP_EDIT_VALOR
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return EMP_EDIT_CAMPO

async def emp_edit_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valor = update.message.text.strip()
    campo = context.user_data["emp_campo"]
    if campo == "telegram_id":
        try:
            valor = str(int(valor))
        except:
            await update.message.reply_text("Escribe un numero valido:")
            return EMP_EDIT_VALOR
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"UPDATE empleados SET {campo} = %s WHERE id = %s", (valor, context.user_data["emp_id"]))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_EMPLEADO", f"{context.user_data['emp_sel_nombre']} | {campo} -> {valor}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(f"\u2705 Actualizado: {campo} = {valor}")
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== CLIENTES ==========
async def cli_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    clientes = context.user_data.get("cli_lista", [])
    cli = next((c for c in clientes if c[1] == texto), None)
    if not cli:
        await update.message.reply_text("Selecciona de la lista.")
        return CLI_SELECT
    context.user_data["cli_id"] = cli[0]
    context.user_data["cli_nombre"] = cli[1]
    teclado = [["Renombrar", "Telefono"], ["Corregir tambos en calle"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f3e2 *{cli[1]}*\n\U0001f4de {cli[2] or 'sin telefono'}\n\U0001f6e2\ufe0f Tambos en calle: {cli[3]}\n\nQue quieres hacer?",
        reply_markup=menu, parse_mode="Markdown"
    )
    return CLI_CAMPO

async def cli_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Renombrar" in texto:
        context.user_data["cli_campo"] = "nombre"
        await update.message.reply_text("Escribe el nuevo nombre:", reply_markup=ReplyKeyboardRemove())
    elif "Telefono" in texto:
        context.user_data["cli_campo"] = "telefono"
        await update.message.reply_text("Escribe el telefono:", reply_markup=ReplyKeyboardRemove())
    elif "Corregir tambos" in texto:
        context.user_data["cli_campo"] = "tambos_pendientes"
        await update.message.reply_text("Escribe la cantidad correcta de tambos en calle:", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return CLI_CAMPO
    return CLI_VALOR

async def cli_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valor = update.message.text.strip()
    campo = context.user_data["cli_campo"]
    if campo == "tambos_pendientes":
        try:
            valor = int(valor)
            if valor < 0:
                await update.message.reply_text("No puede ser negativo.")
                return CLI_VALOR
        except:
            await update.message.reply_text("Escribe un numero valido:")
            return CLI_VALOR
    try:
        conn = conectar()
        cur = conn.cursor()
        cur.execute(f"UPDATE clientes SET {campo} = %s WHERE id = %s", (valor, context.user_data["cli_id"]))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "EDIT_CLIENTE", f"{context.user_data['cli_nombre']} | {campo} -> {valor}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(f"\u2705 Actualizado: {campo} = {valor}")
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== AJUSTE DE TAMBOS ==========
async def tamb_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if "Por lavar" in texto:
        context.user_data["tamb_estado"] = "por_lavar"
    elif "Listos" in texto:
        context.user_data["tamb_estado"] = "listo"
    else:
        await update.message.reply_text("Selecciona de la lista.")
        return TAMB_ESTADO
    await update.message.reply_text(
        f"Escribe la cantidad *total correcta* de tambos {texto.lower()}:",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return TAMB_VALOR

async def tamb_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nuevo = int(update.message.text.strip())
        if nuevo < 0:
            await update.message.reply_text("No puede ser negativo.")
            return TAMB_VALOR
    except:
        await update.message.reply_text("Escribe un numero valido:")
        return TAMB_VALOR
    estado = context.user_data["tamb_estado"]
    try:
        conn = conectar()
        cur = conn.cursor()
        # Reemplazar: borrar los registros de ese estado y crear uno con el total correcto
        cur.execute("DELETE FROM tambos_estado WHERE estado = %s", (estado,))
        if nuevo > 0:
            cur.execute("INSERT INTO tambos_estado (cantidad, estado) VALUES (%s, %s)", (nuevo, estado))
        registrar_audit(cur, update.message.from_user.id, update.message.from_user.first_name,
            "AJUSTE_TAMBOS", f"{estado} -> {nuevo}")
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"\u2705 Tambos *{estado.replace('_',' ')}* ajustados a *{nuevo}*",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        context.user_data.clear()
        await start(update, context)
        return ConversationHandler.END

# ========== EXPORTAR INVENTARIO ==========
async def exportar_inventario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("\U0001f4e5 Generando archivo...", reply_markup=ReplyKeyboardRemove())
    try:
        import csv, io
        conn = conectar()
        cur = conn.cursor()
        now = ahora()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["INVENTARIO CHEMTRACK", now.strftime('%d/%m/%Y %H:%M')])
        writer.writerow([])
        writer.writerow(["PRODUCTO TERMINADO"])
        writer.writerow(["Nombre", "Codigo", "Stock", "Unidad", "Minimo", "Lotes"])
        cur.execute("""
            SELECT pt.nombre, COALESCE(pt.codigo,''), pt.stock_actual, pt.unidad, pt.stock_minimo,
                   COUNT(l.id)
            FROM productos_terminados pt
            LEFT JOIN lotes_pt l ON l.producto_id = pt.id AND l.cantidad_actual > 0
            WHERE pt.activo = true
            GROUP BY pt.id, pt.nombre, pt.codigo, pt.stock_actual, pt.unidad, pt.stock_minimo
            ORDER BY pt.nombre
        """)
        for row in cur.fetchall():
            writer.writerow([row[0], row[1], float(row[2]), row[3], float(row[4]), row[5]])

        writer.writerow([])
        writer.writerow(["MATERIA PRIMA"])
        writer.writerow(["Nombre", "Codigo", "Stock", "Unidad", "Minimo"])
        cur.execute("SELECT nombre, COALESCE(codigo,''), stock_actual, unidad, stock_minimo FROM materias_primas WHERE activo = true ORDER BY nombre")
        for row in cur.fetchall():
            writer.writerow([row[0], row[1], float(row[2]), row[3], float(row[4])])

        # Detalle de lotes PT
        writer.writerow([])
        writer.writerow(["DETALLE DE LOTES PT"])
        writer.writerow(["Producto", "Lote", "Cantidad", "Fecha"])
        cur.execute("""
            SELECT pt.nombre, l.numero_lote, l.cantidad_actual, l.fecha_produccion
            FROM lotes_pt l JOIN productos_terminados pt ON pt.id = l.producto_id
            WHERE l.cantidad_actual > 0
            ORDER BY pt.nombre, l.fecha_produccion
        """)
        for row in cur.fetchall():
            writer.writerow([row[0], row[1], float(row[2]), row[3].strftime('%d/%m/%Y')])

        cur.close()
        conn.close()

        data = output.getvalue().encode('utf-8-sig')
        bio = io.BytesIO(data)
        bio.name = f"inventario_{now.strftime('%d%m%y_%H%M')}.csv"
        await update.message.reply_document(document=bio, filename=bio.name,
            caption=f"\U0001f4ca Inventario al {now.strftime('%d/%m/%Y %H:%M')}\nAbrelo en Excel.")
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")
        await start(update, context)
        return ConversationHandler.END


def _rango_periodo(texto, now):
    """Devuelve (desde, etiqueta) segun el periodo pedido."""
    if texto == "Semana":
        desde = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = "Ultimos 7 dias"
    elif texto == "2 Semanas":
        desde = (now - timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = "Ultimos 14 dias"
    elif texto == "Mes":
        desde = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Mes de {now.strftime('%m/%Y')}"
    elif texto == "Año":
        desde = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta = f"Año {now.strftime('%Y')}"
    else:
        desde = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta = "Ultimos 7 dias"
    return desde, etiqueta

def generar_pdf_reporte(desde, etiqueta, now):
    """Construye el PDF y devuelve un BytesIO listo para enviar."""
    conn = conectar()
    cur = conn.cursor()

    # Inventario PT
    cur.execute("SELECT nombre, stock_actual, unidad FROM productos_terminados WHERE activo = true ORDER BY nombre")
    pt = cur.fetchall()
    # Inventario MP
    cur.execute("SELECT nombre, stock_actual, unidad, stock_minimo FROM materias_primas WHERE activo = true ORDER BY nombre")
    mp = cur.fetchall()
    # Pedidos del periodo
    cur.execute("""
        SELECT p.folio, c.nombre, p.tambos_enviados, p.fecha_pedido,
               COALESCE(SUM(pi.cantidad), 0)
        FROM pedidos p
        JOIN clientes c ON c.id = p.cliente_id
        LEFT JOIN pedido_items pi ON pi.pedido_id = p.id
        WHERE (p.fecha_pedido AT TIME ZONE 'America/Mexico_City') >= %s
        GROUP BY p.id, p.folio, c.nombre, p.tambos_enviados, p.fecha_pedido
        ORDER BY p.fecha_pedido DESC
    """, (desde,))
    pedidos = cur.fetchall()
    # Produccion del periodo
    cur.execute("""
        SELECT pt.nombre, l.numero_lote, l.cantidad_inicial, l.fecha_produccion
        FROM lotes_pt l JOIN productos_terminados pt ON pt.id = l.producto_id
        WHERE l.fecha_produccion >= %s::date
        ORDER BY l.fecha_produccion DESC
    """, (desde,))
    produccion = cur.fetchall()
    # Tambos
    cur.execute("""
        SELECT tipo, SUM(cantidad) FROM tambos_movimientos
        WHERE fecha >= %s GROUP BY tipo
    """, (desde,))
    tambos_mov = {m[0]: int(m[1]) for m in cur.fetchall()}
    cur.execute("SELECT COALESCE(SUM(tambos_pendientes), 0) FROM clientes")
    tambos_calle = int(cur.fetchone()[0])
    cur.close()
    conn.close()

    # ---- Construir PDF ----
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            topMargin=1.5*cm, bottomMargin=1.5*cm,
                            leftMargin=1.5*cm, rightMargin=1.5*cm)
    styles = getSampleStyleSheet()
    azul = colors.HexColor("#1f4e79")
    gris = colors.HexColor("#666666")

    st_titulo = ParagraphStyle('titulo', parent=styles['Title'], fontSize=20, textColor=azul, spaceAfter=4)
    st_sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=10, textColor=gris, alignment=TA_CENTER, spaceAfter=2)
    st_seccion = ParagraphStyle('seccion', parent=styles['Heading2'], fontSize=13, textColor=azul, spaceBefore=14, spaceAfter=6)
    st_normal = ParagraphStyle('norm', parent=styles['Normal'], fontSize=9)

    elementos = []
    elementos.append(Paragraph("CHEMTRACK", st_titulo))
    elementos.append(Paragraph("Reporte de operaciones", st_sub))
    elementos.append(Paragraph(f"Periodo: {etiqueta}  |  Generado: {now.strftime('%d/%m/%Y %H:%M')}", st_sub))
    elementos.append(Spacer(1, 0.3*cm))

    def tabla(data, anchos, header_bg=azul):
        t = Table(data, colWidths=anchos, repeatRows=1)
        estilo = [
            ('BACKGROUND', (0,0), (-1,0), header_bg),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#f2f6fa")]),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ]
        t.setStyle(TableStyle(estilo))
        return t

    # --- Resumen ejecutivo ---
    total_pedidos = len(pedidos)
    total_kg_ped = sum(float(p[4]) for p in pedidos)
    total_tambos_env = sum(int(p[2]) for p in pedidos)
    total_prod_kg = sum(float(p[2]) for p in produccion)
    elementos.append(Paragraph("Resumen ejecutivo", st_seccion))
    resumen_data = [
        ["Indicador", "Valor"],
        ["Pedidos despachados", str(total_pedidos)],
        ["Kg despachados", f"{total_kg_ped:,.0f} kg"],
        ["Tambos enviados", str(total_tambos_env)],
        ["Lotes producidos", str(len(produccion))],
        ["Kg producidos", f"{total_prod_kg:,.0f} kg"],
        ["Tambos en calle (hoy)", str(tambos_calle)],
    ]
    elementos.append(tabla(resumen_data, [9*cm, 7*cm]))

    # --- Inventario PT ---
    elementos.append(Paragraph("Inventario \u2014 Producto Terminado", st_seccion))
    pt_data = [["Producto", "Stock", "Unidad"]]
    for nombre, stock, unidad in pt:
        pt_data.append([nombre, f"{float(stock):,.0f}", unidad])
    elementos.append(tabla(pt_data, [9*cm, 4*cm, 3*cm]))

    # --- Inventario MP (con alertas marcadas) ---
    elementos.append(Paragraph("Inventario \u2014 Materia Prima", st_seccion))
    mp_data = [["Materia prima", "Stock", "Unidad", "Estado"]]
    for nombre, stock, unidad, minimo in mp:
        if float(stock) <= 0:
            estado = "AGOTADO"
        elif float(stock) <= float(minimo):
            estado = "BAJO"
        else:
            estado = "OK"
        mp_data.append([nombre, f"{float(stock):,.1f}", unidad, estado])
    t_mp = tabla(mp_data, [8*cm, 3.5*cm, 2.5*cm, 2*cm])
    # Colorear filas de alerta
    extra = []
    for i, row in enumerate(mp_data[1:], start=1):
        if row[3] == "AGOTADO":
            extra.append(('TEXTCOLOR', (3,i), (3,i), colors.HexColor("#c00000")))
            extra.append(('FONTNAME', (3,i), (3,i), 'Helvetica-Bold'))
        elif row[3] == "BAJO":
            extra.append(('TEXTCOLOR', (3,i), (3,i), colors.HexColor("#bf8f00")))
            extra.append(('FONTNAME', (3,i), (3,i), 'Helvetica-Bold'))
    if extra:
        t_mp.setStyle(TableStyle(extra))
    elementos.append(t_mp)

    # --- Pedidos ---
    elementos.append(Paragraph("Pedidos del periodo", st_seccion))
    if pedidos:
        ped_data = [["Folio", "Cliente", "Kg", "Tambos", "Fecha"]]
        for folio, cliente, tambos, fecha, kg in pedidos:
            fc = fecha.astimezone(CST)
            ped_data.append([folio, cliente, f"{float(kg):,.0f}", str(tambos), fc.strftime('%d/%m %H:%M')])
        elementos.append(tabla(ped_data, [3.5*cm, 5*cm, 2.5*cm, 2*cm, 3*cm]))
    else:
        elementos.append(Paragraph("Sin pedidos en este periodo.", st_normal))

    # --- Produccion ---
    elementos.append(Paragraph("Produccion del periodo", st_seccion))
    if produccion:
        prod_data = [["Producto", "Lote", "Kg", "Fecha"]]
        for nombre, lote, kg, fecha in produccion:
            prod_data.append([nombre, fmt_lote(lote), f"{float(kg):,.0f}", fecha.strftime('%d/%m/%Y')])
        elementos.append(tabla(prod_data, [6*cm, 4.5*cm, 2.5*cm, 3*cm]))
    else:
        elementos.append(Paragraph("Sin produccion en este periodo.", st_normal))

    # --- Tambos ---
    elementos.append(Paragraph("Movimiento de tambos", st_seccion))
    tambos_data = [
        ["Concepto", "Cantidad"],
        ["Entradas", f"+{tambos_mov.get('entrada', 0)}"],
        ["Salidas", f"-{tambos_mov.get('salida', 0)}"],
        ["Devoluciones", f"+{tambos_mov.get('devolucion', 0)}"],
        ["En calle (hoy)", str(tambos_calle)],
    ]
    elementos.append(tabla(tambos_data, [9*cm, 7*cm]))

    elementos.append(Spacer(1, 0.6*cm))
    pie = ParagraphStyle('pie', parent=styles['Normal'], fontSize=7, textColor=gris, alignment=TA_CENTER)
    elementos.append(Paragraph("Chemtrack \u2014 Reporte generado automaticamente por chemtrack", pie))

    doc.build(elementos)
    buf.seek(0)
    return buf

async def enviar_reporte_semanal(context):
    """Job automatico: cada viernes 7pm manda PDF semanal a admins."""
    try:
        now = ahora()
        desde, etiqueta = _rango_periodo("Semana", now)
        pdf = generar_pdf_reporte(desde, etiqueta, now)
        nombre_archivo = f"reporte_semanal_{now.strftime('%d%m%y')}.pdf"
        data = pdf.read()
        for chat_id in RESUMEN_IDS:
            try:
                bio = io.BytesIO(data)
                bio.name = nombre_archivo
                await context.bot.send_document(
                    chat_id=chat_id, document=bio, filename=nombre_archivo,
                    caption=f"\U0001f4c4 *Reporte semanal Chemtrack*\n{etiqueta}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Error enviando PDF a {chat_id}: {e}")
    except Exception as e:
        print(f"Error generando reporte semanal: {e}")

async def reporte_pdf_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c Sin acceso.")
        return ConversationHandler.END
    teclado = [["Semana", "2 Semanas"], ["Mes", "Año"], ["Cancelar"]]
    menu = ReplyKeyboardMarkup(teclado, resize_keyboard=True)
    await update.message.reply_text(
        f"\U0001f4c4 *REPORTE PDF*\n{sep()}\nSelecciona el periodo a generar:",
        reply_markup=menu, parse_mode="Markdown"
    )
    return REPORTE_PDF_PERIODO

async def reporte_pdf_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto == "Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=ReplyKeyboardRemove())
        await start(update, context)
        return ConversationHandler.END
    if texto not in ["Semana", "2 Semanas", "Mes", "Año"]:
        await update.message.reply_text("Selecciona un periodo de la lista.")
        return REPORTE_PDF_PERIODO
    await update.message.reply_text("\U0001f550 Generando PDF...", reply_markup=ReplyKeyboardRemove())
    try:
        now = ahora()
        desde, etiqueta = _rango_periodo(texto, now)
        pdf = generar_pdf_reporte(desde, etiqueta, now)
        nombre_archivo = f"reporte_{now.strftime('%d%m%y_%H%M')}.pdf"
        await update.message.reply_document(
            document=pdf, filename=nombre_archivo,
            caption=f"\U0001f4c4 *Reporte Chemtrack*\n{etiqueta}",
            parse_mode="Markdown"
        )
        await start(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"\u274c Error generando PDF: {e}")
        await start(update, context)
        return ConversationHandler.END


# ========== RESPALDO AUTOMATICO ==========
# Chat privado que recibe los respaldos
BACKUP_ID = os.environ.get("BACKUP_CHAT_ID")

# Todas las tablas de la base de datos
BACKUP_TABLAS = [
    'productos_terminados', 'materias_primas', 'lotes_pt', 'lotes_mp',
    'recetas', 'receta_items', 'tambos_movimientos', 'tambos_estado',
    'clientes', 'empleados', 'pedidos', 'pedido_items', 'asistencia', 'audit_log'
]

def _sql_valor(v):
    """Convierte un valor de Python a su representacion en SQL."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    from decimal import Decimal
    if isinstance(v, Decimal):
        return str(v)
    # fechas, timestamps, texto -> con comillas y escapando comillas simples
    s = str(v).replace("'", "''")
    return f"'{s}'"

def generar_respaldo_sql():
    """Genera un dump SQL completo (INSERTs) de todas las tablas. Devuelve bytes."""
    conn = conectar()
    cur = conn.cursor()
    now = ahora()
    lineas = []
    lineas.append(f"-- RESPALDO CHEMTRACK")
    lineas.append(f"-- Generado: {now.strftime('%d/%m/%Y %H:%M:%S')} CST")
    lineas.append(f"-- Este archivo permite restaurar los datos de la base.")
    lineas.append("")
    lineas.append("BEGIN;")
    lineas.append("")

    for tabla in BACKUP_TABLAS:
        try:
            # Obtener columnas
            cur.execute(f"SELECT * FROM {tabla} LIMIT 0")
            columnas = [desc[0] for desc in cur.description]
            # Obtener filas
            cur.execute(f"SELECT * FROM {tabla}")
            filas = cur.fetchall()
            lineas.append(f"-- ===== Tabla: {tabla} ({len(filas)} registros) =====")
            if filas:
                cols_txt = ", ".join(columnas)
                for fila in filas:
                    vals = ", ".join(_sql_valor(v) for v in fila)
                    lineas.append(f"INSERT INTO {tabla} ({cols_txt}) VALUES ({vals});")
            else:
                lineas.append(f"-- (sin registros)")
            lineas.append("")
        except Exception as e:
            lineas.append(f"-- ERROR al respaldar {tabla}: {e}")
            lineas.append("")

    lineas.append("COMMIT;")
    lineas.append("")
    cur.close()
    conn.close()
    contenido = "\n".join(lineas)
    return contenido.encode('utf-8')

def generar_respaldo_csv():
    """Genera un CSV legible con el inventario y datos clave. Devuelve bytes."""
    import csv
    conn = conectar()
    cur = conn.cursor()
    now = ahora()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["RESPALDO CHEMTRACK", now.strftime('%d/%m/%Y %H:%M')])
    writer.writerow([])

    # PT
    writer.writerow(["PRODUCTO TERMINADO"])
    writer.writerow(["Nombre", "Codigo", "Stock", "Unidad", "Minimo"])
    cur.execute("SELECT nombre, COALESCE(codigo,''), stock_actual, unidad, stock_minimo FROM productos_terminados ORDER BY nombre")
    for r in cur.fetchall():
        writer.writerow([r[0], r[1], float(r[2]), r[3], float(r[4])])
    writer.writerow([])

    # MP
    writer.writerow(["MATERIA PRIMA"])
    writer.writerow(["Nombre", "Codigo", "Stock", "Unidad", "Minimo"])
    cur.execute("SELECT nombre, COALESCE(codigo,''), stock_actual, unidad, stock_minimo FROM materias_primas ORDER BY nombre")
    for r in cur.fetchall():
        writer.writerow([r[0], r[1], float(r[2]), r[3], float(r[4])])
    writer.writerow([])

    # Lotes PT
    writer.writerow(["LOTES PT (con existencia)"])
    writer.writerow(["Producto", "Lote", "Cantidad", "Fecha"])
    cur.execute("""
        SELECT pt.nombre, l.numero_lote, l.cantidad_actual, l.fecha_produccion
        FROM lotes_pt l JOIN productos_terminados pt ON pt.id = l.producto_id
        WHERE l.cantidad_actual > 0 ORDER BY pt.nombre, l.fecha_produccion
    """)
    for r in cur.fetchall():
        writer.writerow([r[0], r[1], float(r[2]), r[3].strftime('%d/%m/%Y')])
    writer.writerow([])

    # Clientes
    writer.writerow(["CLIENTES"])
    writer.writerow(["Nombre", "Telefono", "Tambos en calle"])
    cur.execute("SELECT nombre, COALESCE(telefono,''), tambos_pendientes FROM clientes ORDER BY nombre")
    for r in cur.fetchall():
        writer.writerow([r[0], r[1], r[2]])
    writer.writerow([])

    # Empleados
    writer.writerow(["EMPLEADOS"])
    writer.writerow(["Nombre", "Activo"])
    cur.execute("SELECT nombre, activo FROM empleados ORDER BY nombre")
    for r in cur.fetchall():
        writer.writerow([r[0], "Si" if r[1] else "No"])

    cur.close()
    conn.close()
    return output.getvalue().encode('utf-8-sig')

async def enviar_respaldo_automatico(context):
    """Job: cada 3 dias manda SQL + CSV al administrador por privado."""
    try:
        now = ahora()
        fecha = now.strftime('%d%m%y')
        # SQL
        sql_bytes = generar_respaldo_sql()
        bio_sql = io.BytesIO(sql_bytes)
        bio_sql.name = f"respaldo_chemtrack_{fecha}.sql"
        # CSV
        csv_bytes = generar_respaldo_csv()
        bio_csv = io.BytesIO(csv_bytes)
        bio_csv.name = f"inventario_chemtrack_{fecha}.csv"

        await context.bot.send_document(
            chat_id=BACKUP_ID, document=bio_sql, filename=bio_sql.name,
            caption=f"\U0001f4be *Respaldo automatico chemtrack*\n{now.strftime('%d/%m/%Y %H:%M')}\n\nArchivo SQL: restaura toda la base si algo falla.\nGuardalo en un lugar seguro.",
            parse_mode="Markdown"
        )
        await context.bot.send_document(
            chat_id=BACKUP_ID, document=bio_csv, filename=bio_csv.name,
            caption="\U0001f4ca Version CSV (se abre en Excel) para consulta rapida."
        )
    except Exception as e:
        try:
            await context.bot.send_message(chat_id=BACKUP_ID, text=f"\u26a0\ufe0f Error generando respaldo automatico: {e}")
        except Exception:
            print(f"Error respaldo automatico: {e}")

async def alerta_agotamiento_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando manual /agotamiento (solo admin) para ver proyeccion de MP."""
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c Sin acceso.")
        return
    por_agotarse = calcular_mp_por_agotarse()
    texto = formato_alerta_agotamiento(por_agotarse)
    if texto:
        await update.message.reply_text(texto, parse_mode="Markdown")
    else:
        await update.message.reply_text("\u2705 Ninguna materia prima esta por agotarse en los proximos 7 dias.")

async def respaldo_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando manual /respaldo (solo admin) para generar respaldo al instante."""
    rol = get_rol(str(update.message.from_user.id))
    if rol != 'admin':
        await update.message.reply_text("\u274c Sin acceso.")
        return
    await update.message.reply_text("\U0001f4be Generando respaldo...")
    try:
        now = ahora()
        fecha = now.strftime('%d%m%y')
        sql_bytes = generar_respaldo_sql()
        bio_sql = io.BytesIO(sql_bytes)
        bio_sql.name = f"respaldo_chemtrack_{fecha}.sql"
        csv_bytes = generar_respaldo_csv()
        bio_csv = io.BytesIO(csv_bytes)
        bio_csv.name = f"inventario_chemtrack_{fecha}.csv"
        await update.message.reply_document(
            document=bio_sql, filename=bio_sql.name,
            caption=f"\U0001f4be *Respaldo chemtrack*\n{now.strftime('%d/%m/%Y %H:%M')}\n\nArchivo SQL: restaura toda la base si algo falla.",
            parse_mode="Markdown"
        )
        await update.message.reply_document(
            document=bio_csv, filename=bio_csv.name,
            caption="\U0001f4ca Version CSV (Excel) para consulta rapida."
        )
    except Exception as e:
        await update.message.reply_text(f"\u274c Error: {e}")


def main():
    app = Application.builder().token(TOKEN).build()

    app.job_queue.run_daily(
        enviar_resumen_diario,
        time=time(hour=1, minute=0, tzinfo=pytz.utc),
        name="resumen_diario"
    )
    # Reporte semanal PDF: viernes 7pm CST = sabado 01:00 UTC
    # En PTB v20 los dias 0-6 son domingo-sabado, por eso sabado = 6
    app.job_queue.run_daily(
        enviar_reporte_semanal,
        time=time(hour=1, minute=0, tzinfo=pytz.utc),
        days=(6,),
        name="reporte_semanal_pdf"
    )
    # Respaldo automatico cada 3 dias (a las 3am CST = 9:00 UTC, sin operacion)
    from datetime import time as _t
    app.job_queue.run_repeating(
        enviar_respaldo_automatico,
        interval=timedelta(days=3),
        first=_t(hour=9, minute=0, tzinfo=pytz.utc),
        name="respaldo_automatico"
    )

    conv_produccion = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Produccion"), produccion_inicio)],
        states={
            PROD_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, produccion_producto)],
            PROD_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, produccion_cantidad)],
            PROD_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, produccion_fecha)],
            PROD_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, produccion_confirmar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_entrada = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Entrada MP"), entrada_inicio)],
        states={
            ENTRADA_MATERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_material)],
            ENTRADA_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_cantidad)],
            ENTRADA_PROVEEDOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_proveedor)],
            ENTRADA_PRESENTACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_presentacion)],
            ENTRADA_UBICACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_ubicacion)],
            ENTRADA_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_confirmar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_pedido = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Salida Pedido"), pedido_inicio)],
        states={
            PEDIDO_CLIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_cliente)],
            PEDIDO_PRODUCTOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_productos)],
            PEDIDO_NOTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_nota)],
            PEDIDO_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_confirmar)],
            PEDIDO_SWAP_PROD: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_swap_prod)],
            PEDIDO_SWAP_QUITAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_swap_quitar)],
            PEDIDO_SWAP_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_swap_cantidad)],
            PEDIDO_SWAP_PONER: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_swap_poner)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_devolucion = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Registrar devolucion"), devolucion_inicio)],
        states={
            DEVOLUCION_CLIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, devolucion_cliente)],
            DEVOLUCION_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, devolucion_cantidad)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_lavado = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Registrar lavado"), lavado_inicio)],
        states={
            LAVADO_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lavado_cantidad)],
            LAVADO_QUIEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, lavado_quien)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_asistencia = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Asistencia"), asistencia_menu)],
        states={
            CLOCK_EMPLEADO: [MessageHandler(filters.TEXT & ~filters.COMMAND, clock_empleado)],
            CLOCK_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, clock_tipo)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_entrada_tambos = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Entrada tambos"), entrada_tambos_inicio)],
        states={
            ENTRADA_TAMBOS_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, entrada_tambos_cantidad)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_editar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Editar inventario"), editar_inicio)],
        states={
            EDIT_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_tipo)],
            EDIT_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_producto)],
            EDIT_ACCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_accion)],
            EDIT_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_valor)],
            EDIT_LOTE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_lote_select)],
            EDIT_LOTE_ACCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_lote_accion)],
            EDIT_LOTE_CONFIRM_BORRAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_lote_confirmar_borrar)],
            EDIT_LOTE_NUEVO_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_lote_nuevo_num)],
            EDIT_LOTE_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editar_lote_valor)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )

    conv_reporte = ConversationHandler(
        entry_points=[
            CommandHandler("reporte", reporte_ahora),
            MessageHandler(filters.Regex("Reportes"), reporte_ahora)
        ],
        states={
            REPORTE_PERIODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, reporte_periodo)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    conv_admin = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^\u2699\ufe0f Admin$"), admin_inicio)],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu)],
            ADD_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_tipo)],
            ADD_NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_nombre)],
            ADD_CODIGO: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_codigo)],
            ADD_UNIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_unidad)],
            ADD_PRESENTACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_presentacion)],
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock)],
            ADD_MINIMO: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_minimo)],
            ADD_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirmar)],
            EDITP_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editp_tipo)],
            EDITP_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editp_producto)],
            EDITP_CAMPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, editp_campo)],
            EDITP_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, editp_valor)],
            MIN_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_tipo)],
            MIN_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_producto)],
            MIN_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_valor)],
            TOGGLE_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, toggle_tipo)],
            TOGGLE_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, toggle_producto)],
            DELPROD_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, delprod_tipo)],
            DELPROD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delprod_select)],
            DELPROD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delprod_confirm)],
            REC_PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_producto)],
            REC_ACCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_accion)],
            REC_BASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_base)],
            REC_MP_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_mp_select)],
            REC_MP_CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_mp_cantidad)],
            REC_ITEM_DEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_item_del)],
            CANC_FOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, canc_folio)],
            CANC_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, canc_confirmar)],
            EMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_menu)],
            EMP_ADD_NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_add_nombre)],
            EMP_ADD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_add_id)],
            EMP_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_select)],
            EMP_EDIT_CAMPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_edit_campo)],
            EMP_EDIT_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_edit_valor)],
            CLI_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cli_select)],
            CLI_CAMPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cli_campo)],
            CLI_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, cli_valor)],
            TAMB_ESTADO: [MessageHandler(filters.TEXT & ~filters.COMMAND, tamb_estado)],
            TAMB_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, tamb_valor)],
            PRECIOS_ACTUALIZAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, precios_actualizar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("inv", consultar_stock))
    app.add_handler(CommandHandler("id", mi_id))
    app.add_handler(CommandHandler("tambos_semana", reporte_tambos_semana))
    app.add_handler(CommandHandler("audit", ver_audit))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("respaldo", respaldo_ahora))
    app.add_handler(CommandHandler("agotamiento", alerta_agotamiento_ahora))
    app.add_handler(CommandHandler("recetas", consultar_recetas))
    conv_reporte_pdf = ConversationHandler(
        entry_points=[
            CommandHandler("reportepdf", reporte_pdf_inicio),
            MessageHandler(filters.Regex("^\U0001f4c4 Reporte PDF$"), reporte_pdf_inicio)
        ],
        states={
            REPORTE_PDF_PERIODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, reporte_pdf_periodo)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )
    app.add_handler(conv_reporte)
    app.add_handler(conv_reporte_pdf)
    app.add_handler(conv_admin)
    app.add_handler(conv_produccion)
    app.add_handler(conv_entrada)
    app.add_handler(conv_pedido)
    app.add_handler(conv_devolucion)
    app.add_handler(conv_lavado)
    app.add_handler(conv_asistencia)
    app.add_handler(conv_entrada_tambos)
    app.add_handler(conv_editar)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_menu))
    print("Bot corriendo...")
    app.run_polling()

if __name__ == "__main__":
    main()