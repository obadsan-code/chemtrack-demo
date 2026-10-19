# chemtrack

Plataforma de operaciones para una planta de manufactura química: bot de Telegram + panel web.
Reemplazó hojas de cálculo y mensajes sueltos con un sistema único que usa el equipo a diario.

## Qué hace
- **Inventario** de materias primas y producto terminado, con lotes y kardex
- **Recetas / fórmulas** y descuento automático de materia prima al producir
- **Pedidos** de clientes con folio, items e IVA
- **Control de tambos** (envases retornables) por cliente
- **Asistencia** de empleados desde Telegram
- **Reportes PDF** semanales y **respaldos automáticos** (SQL + CSV) enviados por Telegram
- **Roles** (admin, supervisor, operadores) con permisos por acción
- **Dashboard** web (React + Express) con inventario, pedidos, asistencia y tambos

## Stack
Python 3.10 · python-telegram-bot 20 · PostgreSQL · reportlab · React 19 · Express 5 · recharts · Railway

## Puesta en marcha
1. `cp .env.example .env` y llena los valores
2. Crea la base: `psql $DATABASE_URL -f db/schema.sql` (opcional: `db/seed_demo.sql`)
3. Bot: `pip install -r requirements.txt && python bot.py`
4. Dashboard: `cd dashboard && npm install && node server.js` (y `npm start` para el front, con `REACT_APP_API_URL`)

Todas las credenciales y los IDs de Telegram se leen de variables de entorno; no hay secretos en el código.
