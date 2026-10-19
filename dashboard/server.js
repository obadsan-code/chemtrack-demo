const express = require("express");
const { Pool } = require("pg");
const cors = require("cors");

const app = express();
app.use(cors());
app.use(express.json());

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
});

// Materia Prima
app.get("/api/mp", async (req, res) => {
  try {
    const r = await pool.query("SELECT nombre, stock_actual, unidad, stock_minimo FROM materias_primas WHERE activo = true ORDER BY nombre");
    res.json(r.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Producto Terminado
app.get("/api/pt", async (req, res) => {
  try {
    const r = await pool.query("SELECT nombre, codigo, stock_actual, unidad, stock_minimo FROM productos_terminados WHERE activo = true ORDER BY nombre");
    res.json(r.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Pedidos recientes
app.get("/api/pedidos", async (req, res) => {
  try {
    const r = await pool.query(`
      SELECT p.folio, c.nombre as cliente, p.tambos_enviados, 
             TO_CHAR(p.fecha_pedido, 'DD/MM/YYYY HH24:MI') as fecha
      FROM pedidos p
      JOIN clientes c ON c.id = p.cliente_id
      ORDER BY p.fecha_pedido DESC
      LIMIT 20
    `);
    res.json(r.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Asistencia hoy
app.get("/api/asistencia", async (req, res) => {
  try {
    const r = await pool.query(`
      SELECT e.nombre, a.tipo, TO_CHAR(a.fecha, 'HH24:MI') as hora
      FROM asistencia a
      JOIN empleados e ON e.id = a.empleado_id
      WHERE DATE(a.fecha) = CURRENT_DATE
      ORDER BY a.fecha DESC
    `);
    res.json(r.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Tambos
app.get("/api/tambos", async (req, res) => {
  try {
    const calle = await pool.query("SELECT COALESCE(SUM(tambos_pendientes), 0) as total FROM clientes");
    const por_lavar = await pool.query("SELECT COALESCE(SUM(cantidad), 0) as total FROM tambos_estado WHERE estado = 'por_lavar'");
    const listos = await pool.query("SELECT COALESCE(SUM(cantidad), 0) as total FROM tambos_estado WHERE estado = 'listo'");
    res.json({
      calle: parseInt(calle.rows[0].total),
      por_lavar: parseInt(por_lavar.rows[0].total),
      listos: parseInt(listos.rows[0].total),
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

const PORT = process.env.PORT || 4000;
app.listen(PORT, () => console.log(`API corriendo en puerto ${PORT}`));
