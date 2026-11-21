import { useEffect, useState } from "react";
import { api, type Dashboard as Dash, type Health } from "../api";

export default function Dashboard() {
  const [dash, setDash] = useState<Dash | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.dashboard().then(setDash).catch(e => setErr(String(e)));
    api.health().then(setHealth).catch(() => setHealth(null));
    const t = setInterval(() => {
      api.dashboard().then(setDash).catch(() => {});
      api.health().then(setHealth).catch(() => {});
    }, 15000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="error-box">{err}</div>;
  if (!dash) return <div className="muted">Загрузка…</div>;

  const services = (dash.services || []) as { name: string; status: string; extra?: Record<string, unknown> }[];
  const counts = (dash.counts || {}) as Record<string, number>;

  return (
    <>
      <h2>Dashboard</h2>
      <div className="grid">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Сервис панели</h3>
          <p>API: <span className={`badge ${health?.status === "ok" ? "ok" : "err"}`}>
            {health?.status ?? "…"}</span></p>
          <p className="muted">БД: {health?.database ?? "…"}</p>
        </div>
        {services.map(s => (
          <div className="card" key={s.name}>
            <h3 style={{ marginTop: 0 }}>{s.name}</h3>
            <span className={`badge ${s.status === "up" || s.status === "ok" ? "ok" : s.status === "degraded" ? "warn" : "err"}`}>
              {s.status}
            </span>
            {s.extra && Object.keys(s.extra).length > 0 && (
              <pre className="mono muted" style={{ fontSize: 11, marginTop: 8, whiteSpace: "pre-wrap" }}>
                {JSON.stringify(s.extra, null, 1)}
              </pre>
            )}
          </div>
        ))}
      </div>
      {Object.keys(counts).length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Данные PostgreSQL</h3>
          <table><tbody>
            {Object.entries(counts).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v}</td></tr>
            ))}
          </tbody></table>
        </div>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        Статус бота обновляется через heartbeat (admin_service_status).
        Активные турниры и очереди MM живут в Discord-командах.
      </p>
    </>
  );
}
