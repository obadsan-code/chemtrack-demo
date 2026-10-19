import { useState, useEffect } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";

const API = process.env.REACT_APP_API_URL || "http://localhost:4000";

const colors = {
  bg: "#0a0e1a",
  card: "#111827",
  border: "#1e293b",
  teal: "#00d4aa",
  red: "#ff4d6d",
  yellow: "#fbbf24",
  green: "#00d4aa",
  text: "#e2e8f0",
  muted: "#64748b",
};

function StatCard({ title, value, sub, color }) {
  return (
    <div style={{
      background: colors.card,
      border: `1px solid ${colors.border}`,
      borderRadius: 12,
      padding: "20px 24px",
      display: "flex",
      flexDirection: "column",
      gap: 6,
    }}>
      <span style={{ color: colors.muted, fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>{title}</span>
      <span style={{ color: color || colors.teal, fontSize: 32, fontWeight: 700 }}>{value}</span>
      {sub && <span style={{ color: colors.muted, fontSize: 13 }}>{sub}</span>}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 32 }}>
      <h2 style={{ color: colors.teal, fontSize: 14, letterSpacing: 2, textTransform: "uppercase", marginBottom: 16, fontWeight: 600 }}>
        {title}
      </h2>
      {children}
    </div>
  );
}

export default function App() {
  const [mp, setMp] = useState([]);
  const [pt, setPt] = useState([]);
  const [pedidos, setPedidos] = useState([]);
  const [asistencia, setAsistencia] = useState([]);
  const [tambos, setTambos] = useState({ calle: 0, por_lavar: 0, listos: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);

  const fetchData = async () => {
    try {
      const [mpRes, ptRes, pedRes, asisRes, tambRes] = await Promise.all([
        fetch(`${API}/api/mp`),
        fetch(`${API}/api/pt`),
        fetch(`${API}/api/pedidos`),
        fetch(`${API}/api/asistencia`),
        fetch(`${API}/api/tambos`),
      ]);
      setMp(await mpRes.json());
      setPt(await ptRes.json());
      setPedidos(await pedRes.json());
      setAsistencia(await asisRes.json());
      setTambos(await tambRes.json());
      setLastUpdate(new Date().toLocaleTimeString("es-MX"));
      setError(null);
    } catch (e) {
      setError("No se pudo conectar al servidor");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 60000);
    return () => clearInterval(interval);
  }, []);

  const mpBajos = mp.filter(m => parseFloat(m.stock_actual) <= parseFloat(m.stock_minimo) && parseFloat(m.stock_actual) > 0);
  const mpAgotados = mp.filter(m => parseFloat(m.stock_actual) <= 0);

  const getColor = (stock, minimo) => {
    if (parseFloat(stock) <= 0) return colors.red;
    if (parseFloat(stock) <= parseFloat(minimo)) return colors.yellow;
    return colors.green;
  };

  const getEmoji = (stock, minimo) => {
    if (parseFloat(stock) <= 0) return "🔴";
    if (parseFloat(stock) <= parseFloat(minimo)) return "🟡";
    return "🟢";
  };

  return (
    <div style={{
      minHeight: "100vh",
      background: colors.bg,
      color: colors.text,
      fontFamily: "'DM Mono', 'Courier New', monospace",
      padding: "0",
    }}>
      {/* Header */}
      <div style={{
        background: colors.card,
        borderBottom: `1px solid ${colors.border}`,
        padding: "16px 32px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{
            width: 8, height: 8, borderRadius: "50%",
            background: error ? colors.red : colors.teal,
            boxShadow: `0 0 8px ${error ? colors.red : colors.teal}`,
          }} />
          <span style={{ color: colors.teal, fontWeight: 700, fontSize: 16, letterSpacing: 2 }}>CHEMTRACK</span>
          <span style={{ color: colors.muted, fontSize: 12 }}>PANEL DE CONTROL</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {lastUpdate && <span style={{ color: colors.muted, fontSize: 12 }}>Actualizado: {lastUpdate}</span>}
          <button onClick={fetchData} style={{
            background: "transparent",
            border: `1px solid ${colors.teal}`,
            color: colors.teal,
            padding: "6px 14px",
            borderRadius: 6,
            cursor: "pointer",
            fontSize: 12,
          }}>↻ Actualizar</button>
        </div>
      </div>

      <div style={{ padding: "32px", maxWidth: 1400, margin: "0 auto" }}>
        {error && (
          <div style={{
            background: "#1a0a0a", border: `1px solid ${colors.red}`,
            borderRadius: 8, padding: 16, marginBottom: 24, color: colors.red,
          }}>
            ⚠️ {error} — Verifica que el servidor API esté corriendo
          </div>
        )}

        {loading ? (
          <div style={{ textAlign: "center", color: colors.muted, padding: 80 }}>Cargando datos...</div>
        ) : (
          <>
            {/* KPIs */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 16, marginBottom: 32 }}>
              <StatCard title="Materias Primas" value={mp.length} sub={`${mpAgotados.length} agotadas · ${mpBajos.length} bajas`} />
              <StatCard title="Productos Terminados" value={pt.length} sub="en inventario" color={colors.teal} />
              <StatCard title="Pedidos Hoy" value={pedidos.length} sub="registrados" color="#a78bfa" />
              <StatCard title="Asistencia Hoy" value={asistencia.length} sub="movimientos" color="#60a5fa" />
              <StatCard title="Tambos en Calle" value={tambos.calle || 0} sub={`${tambos.por_lavar || 0} por lavar · ${tambos.listos || 0} listos`} color={colors.yellow} />
            </div>

            {/* Inventario MP */}
            <Section title="Inventario Materia Prima">
              <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, overflow: "hidden" }}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${colors.border}` }}>
                        {["", "Materia Prima", "Stock Actual", "Mínimo", "Estado"].map(h => (
                          <th key={h} style={{ padding: "12px 16px", textAlign: "left", color: colors.muted, fontSize: 11, letterSpacing: 1, textTransform: "uppercase" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {mp.map((m, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid ${colors.border}`, transition: "background 0.2s" }}
                          onMouseEnter={e => e.currentTarget.style.background = "#1a2332"}
                          onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                          <td style={{ padding: "10px 16px" }}>{getEmoji(m.stock_actual, m.stock_minimo)}</td>
                          <td style={{ padding: "10px 16px", fontWeight: 500 }}>{m.nombre}</td>
                          <td style={{ padding: "10px 16px", color: getColor(m.stock_actual, m.stock_minimo), fontWeight: 600 }}>
                            {parseFloat(m.stock_actual).toFixed(1)} {m.unidad}
                          </td>
                          <td style={{ padding: "10px 16px", color: colors.muted }}>{parseFloat(m.stock_minimo).toFixed(1)} {m.unidad}</td>
                          <td style={{ padding: "10px 16px" }}>
                            <span style={{
                              padding: "3px 10px", borderRadius: 20, fontSize: 11, fontWeight: 600,
                              background: parseFloat(m.stock_actual) <= 0 ? "#2a0a0f" : parseFloat(m.stock_actual) <= parseFloat(m.stock_minimo) ? "#2a1f0a" : "#0a1f1a",
                              color: getColor(m.stock_actual, m.stock_minimo),
                            }}>
                              {parseFloat(m.stock_actual) <= 0 ? "AGOTADO" : parseFloat(m.stock_actual) <= parseFloat(m.stock_minimo) ? "BAJO" : "OK"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Gráfica MP */}
              <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, padding: 24, marginTop: 16 }}>
                <p style={{ color: colors.muted, fontSize: 12, marginBottom: 16 }}>Stock por materia prima (kg)</p>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={mp.slice(0, 15)} margin={{ top: 0, right: 0, left: -20, bottom: 60 }}>
                    <XAxis dataKey="nombre" tick={{ fill: colors.muted, fontSize: 10 }} angle={-45} textAnchor="end" interval={0} />
                    <YAxis tick={{ fill: colors.muted, fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 8, color: colors.text }} />
                    <Bar dataKey="stock_actual" radius={[4, 4, 0, 0]}>
                      {mp.slice(0, 15).map((m, i) => (
                        <Cell key={i} fill={getColor(m.stock_actual, m.stock_minimo)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Section>

            {/* Producto Terminado */}
            <Section title="Producto Terminado">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
                {pt.map((p, i) => (
                  <div key={i} style={{
                    background: colors.card, border: `1px solid ${colors.border}`,
                    borderRadius: 12, padding: 20,
                    borderLeft: `3px solid ${getColor(p.stock_actual, p.stock_minimo)}`,
                  }}>
                    <div style={{ color: colors.muted, fontSize: 11, marginBottom: 4 }}>{p.codigo}</div>
                    <div style={{ fontWeight: 600, marginBottom: 8 }}>{p.nombre}</div>
                    <div style={{ color: getColor(p.stock_actual, p.stock_minimo), fontSize: 24, fontWeight: 700 }}>
                      {parseFloat(p.stock_actual).toFixed(0)} kg
                    </div>
                    <div style={{ color: colors.muted, fontSize: 12, marginTop: 4 }}>mín: {parseFloat(p.stock_minimo).toFixed(0)} kg</div>
                  </div>
                ))}
              </div>
            </Section>

            {/* Asistencia y Tambos */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
              <Section title="Asistencia Hoy">
                <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, overflow: "hidden" }}>
                  {asistencia.length === 0 ? (
                    <div style={{ padding: 24, color: colors.muted, textAlign: "center" }}>Sin movimientos hoy</div>
                  ) : (
                    asistencia.map((a, i) => (
                      <div key={i} style={{
                        padding: "12px 16px", borderBottom: `1px solid ${colors.border}`,
                        display: "flex", justifyContent: "space-between", alignItems: "center",
                      }}>
                        <span style={{ fontWeight: 500 }}>{a.nombre}</span>
                        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                          <span style={{ color: colors.muted, fontSize: 12 }}>{a.tipo}</span>
                          <span style={{ color: colors.teal, fontSize: 12 }}>{a.hora}</span>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </Section>

              <Section title="Control de Tambos">
                <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
                  {[
                    { label: "En calle", value: tambos.calle || 0, color: colors.yellow },
                    { label: "Por lavar", value: tambos.por_lavar || 0, color: colors.red },
                    { label: "Listos", value: tambos.listos || 0, color: colors.green },
                  ].map((t, i) => (
                    <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ color: colors.muted }}>{t.label}</span>
                      <span style={{ color: t.color, fontSize: 24, fontWeight: 700 }}>{t.value}</span>
                    </div>
                  ))}
                </div>
              </Section>
            </div>

            {/* Últimos Pedidos */}
            <Section title="Últimos Pedidos">
              <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, overflow: "hidden" }}>
                {pedidos.length === 0 ? (
                  <div style={{ padding: 24, color: colors.muted, textAlign: "center" }}>Sin pedidos registrados hoy</div>
                ) : (
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${colors.border}` }}>
                        {["Folio", "Cliente", "Tambos", "Fecha"].map(h => (
                          <th key={h} style={{ padding: "12px 16px", textAlign: "left", color: colors.muted, fontSize: 11, letterSpacing: 1, textTransform: "uppercase" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {pedidos.map((p, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid ${colors.border}` }}>
                          <td style={{ padding: "10px 16px", color: colors.teal, fontWeight: 600 }}>{p.folio}</td>
                          <td style={{ padding: "10px 16px" }}>{p.cliente}</td>
                          <td style={{ padding: "10px 16px", color: colors.yellow }}>{p.tambos_enviados}</td>
                          <td style={{ padding: "10px 16px", color: colors.muted, fontSize: 12 }}>{p.fecha}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </Section>
          </>
        )}
      </div>
    </div>
  );
}
