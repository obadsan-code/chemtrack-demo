-- Esquema de chemtrack (sin datos)
CREATE TABLE IF NOT EXISTS asistencia (
    id SERIAL PRIMARY KEY,
    empleado_id integer,
    tipo character varying(20) NOT NULL,
    fecha timestamp without time zone DEFAULT now(),
    registrado_por character varying(100)
);

CREATE TABLE IF NOT EXISTS clientes (
    id SERIAL PRIMARY KEY,
    nombre character varying(150) NOT NULL,
    telefono character varying(20),
    tambos_pendientes integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT now(),
    tambos_favor integer DEFAULT 0
);

CREATE TABLE IF NOT EXISTS empleados (
    id SERIAL PRIMARY KEY,
    nombre character varying(100) NOT NULL,
    activo boolean DEFAULT true,
    telegram_id bigint,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entradas_mp (
    id SERIAL PRIMARY KEY,
    materia_prima_id integer,
    cantidad numeric(12,3) NOT NULL,
    proveedor character varying(100),
    presentacion character varying(50),
    ubicacion character varying(50),
    numero_lote character varying(50),
    fecha_ingreso date DEFAULT CURRENT_DATE,
    registrado_por character varying(100),
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kardex (
    id SERIAL PRIMARY KEY,
    tipo_registro character varying(10),
    referencia_id integer,
    tipo_movimiento character varying(20),
    cantidad numeric(12,3),
    saldo numeric(12,3),
    notas text,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lotes_mp (
    id SERIAL PRIMARY KEY,
    materia_prima_id integer,
    numero_lote character varying(50),
    proveedor character varying(100),
    cantidad_inicial numeric(12,3),
    cantidad_actual numeric(12,3),
    ubicacion character varying(50),
    fecha_ingreso date DEFAULT CURRENT_DATE,
    created_at timestamp without time zone DEFAULT now(),
    presentacion character varying(50)
);

CREATE TABLE IF NOT EXISTS lotes_pt (
    id SERIAL PRIMARY KEY,
    producto_id integer,
    numero_lote character varying(20) NOT NULL,
    presentacion character varying(50),
    cantidad_inicial numeric(12,3),
    cantidad_actual numeric(12,3),
    fecha_produccion date,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS materias_primas (
    id SERIAL PRIMARY KEY,
    codigo character varying(20) NOT NULL,
    nombre character varying(100) NOT NULL,
    categoria character varying(50),
    unidad character varying(10) DEFAULT 'kg'::character varying,
    stock_actual numeric(12,3) DEFAULT 0,
    stock_minimo numeric(12,3) DEFAULT 0,
    activo boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pedido_items (
    id SERIAL PRIMARY KEY,
    pedido_id integer,
    lote_pt_id integer,
    producto_id integer,
    cantidad numeric(12,3),
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pedidos (
    id SERIAL PRIMARY KEY,
    cliente_id integer,
    folio character varying(20),
    estado character varying(20) DEFAULT 'entregado'::character varying,
    tambos_enviados integer DEFAULT 0,
    tambos_devueltos integer DEFAULT 0,
    fecha_pedido timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS productos_terminados (
    id SERIAL PRIMARY KEY,
    codigo character varying(20) NOT NULL,
    nombre character varying(100) NOT NULL,
    categoria character varying(50),
    unidad character varying(10) DEFAULT 'kg'::character varying,
    stock_actual numeric(12,3) DEFAULT 0,
    stock_minimo numeric(12,3) DEFAULT 0,
    metodo_salida character varying(10) DEFAULT 'FIFO'::character varying,
    activo boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT now(),
    presentacion character varying(50),
    kg_por_unidad numeric(10,3)
);

CREATE TABLE IF NOT EXISTS receta_items (
    id SERIAL PRIMARY KEY,
    receta_id integer,
    materia_prima_id integer,
    cantidad numeric(12,4) NOT NULL
);

CREATE TABLE IF NOT EXISTS recetas (
    id SERIAL PRIMARY KEY,
    producto_id integer,
    cantidad_base numeric(12,3) NOT NULL,
    activa boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tambos_estado (
    id SERIAL PRIMARY KEY,
    cantidad integer NOT NULL,
    estado character varying(20) DEFAULT 'por_lavar'::character varying,
    cliente_id integer,
    pedido_id integer,
    fecha_ingreso timestamp without time zone DEFAULT now(),
    fecha_lavado timestamp without time zone,
    lavado_por character varying(100),
    notas text
);

CREATE TABLE IF NOT EXISTS tambos_movimientos (
    id SERIAL PRIMARY KEY,
    cliente_id integer,
    tipo character varying(10) NOT NULL,
    cantidad integer NOT NULL,
    pedido_id integer,
    fecha timestamp without time zone DEFAULT now(),
    notas text
);

ALTER TABLE ONLY public.asistencia
    ADD CONSTRAINT asistencia_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.clientes
    ADD CONSTRAINT clientes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.empleados
    ADD CONSTRAINT empleados_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.entradas_mp
    ADD CONSTRAINT entradas_mp_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.kardex
    ADD CONSTRAINT kardex_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.lotes_mp
    ADD CONSTRAINT lotes_mp_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.lotes_pt
    ADD CONSTRAINT lotes_pt_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.materias_primas
    ADD CONSTRAINT materias_primas_codigo_key UNIQUE (codigo);
ALTER TABLE ONLY public.materias_primas
    ADD CONSTRAINT materias_primas_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pedido_items
    ADD CONSTRAINT pedido_items_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.pedidos
    ADD CONSTRAINT pedidos_folio_key UNIQUE (folio);
ALTER TABLE ONLY public.pedidos
    ADD CONSTRAINT pedidos_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.productos_terminados
    ADD CONSTRAINT productos_terminados_codigo_key UNIQUE (codigo);
ALTER TABLE ONLY public.productos_terminados
    ADD CONSTRAINT productos_terminados_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.receta_items
    ADD CONSTRAINT receta_items_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.recetas
    ADD CONSTRAINT recetas_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tambos_estado
    ADD CONSTRAINT tambos_estado_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tambos_movimientos
    ADD CONSTRAINT tambos_movimientos_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.asistencia
    ADD CONSTRAINT asistencia_empleado_id_fkey FOREIGN KEY (empleado_id) REFERENCES public.empleados(id);
ALTER TABLE ONLY public.entradas_mp
    ADD CONSTRAINT entradas_mp_materia_prima_id_fkey FOREIGN KEY (materia_prima_id) REFERENCES public.materias_primas(id);
ALTER TABLE ONLY public.lotes_mp
    ADD CONSTRAINT lotes_mp_materia_prima_id_fkey FOREIGN KEY (materia_prima_id) REFERENCES public.materias_primas(id);
ALTER TABLE ONLY public.lotes_pt
    ADD CONSTRAINT lotes_pt_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos_terminados(id);
ALTER TABLE ONLY public.pedido_items
    ADD CONSTRAINT pedido_items_lote_pt_id_fkey FOREIGN KEY (lote_pt_id) REFERENCES public.lotes_pt(id);
ALTER TABLE ONLY public.pedido_items
    ADD CONSTRAINT pedido_items_pedido_id_fkey FOREIGN KEY (pedido_id) REFERENCES public.pedidos(id);
ALTER TABLE ONLY public.pedido_items
    ADD CONSTRAINT pedido_items_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos_terminados(id);
ALTER TABLE ONLY public.pedidos
    ADD CONSTRAINT pedidos_cliente_id_fkey FOREIGN KEY (cliente_id) REFERENCES public.clientes(id);
ALTER TABLE ONLY public.receta_items
    ADD CONSTRAINT receta_items_materia_prima_id_fkey FOREIGN KEY (materia_prima_id) REFERENCES public.materias_primas(id);
ALTER TABLE ONLY public.receta_items
    ADD CONSTRAINT receta_items_receta_id_fkey FOREIGN KEY (receta_id) REFERENCES public.recetas(id);
ALTER TABLE ONLY public.recetas
    ADD CONSTRAINT recetas_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos_terminados(id);
ALTER TABLE ONLY public.tambos_estado
    ADD CONSTRAINT tambos_estado_cliente_id_fkey FOREIGN KEY (cliente_id) REFERENCES public.clientes(id);
ALTER TABLE ONLY public.tambos_estado
    ADD CONSTRAINT tambos_estado_pedido_id_fkey FOREIGN KEY (pedido_id) REFERENCES public.pedidos(id);
ALTER TABLE ONLY public.tambos_movimientos
    ADD CONSTRAINT tambos_movimientos_cliente_id_fkey FOREIGN KEY (cliente_id) REFERENCES public.clientes(id);
ALTER TABLE ONLY public.tambos_movimientos
    ADD CONSTRAINT tambos_movimientos_pedido_id_fkey FOREIGN KEY (pedido_id) REFERENCES public.pedidos(id);
