import { useEffect, useState } from "react";
import { api, type AuditRow } from "../api";

export default function Audit() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [action, setAction] = useState("");
  const [err, setErr] = useState("");

  const load = () => api.audit(action ? `&action=${encodeURIComponent(action)}` : "")
    .then(r => setRows(r.items)).catch(e => setErr(String(e)));
  useEffect(() => { load(); }, []);

  return (
    <>
      <div className="topbar"><h2 style={{ margin: 0 }}>Audit Log</h2>
        <div className="row">
          <input placeholder="Фильтр по action…" value={action}
            onChange={e => setAction(e.target.value)}
            onKeyDown={e => e.key === "Enter" && load()} style={{ width: 220 }} />
          <button className="btn secondary" onClick={load}>Фильтр</button>
        </div>
      </div>
      {err && <div className="error-box">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>Время</th><th>Действие</th><th>Актор</th><th>Сущность</th><th>Детали</th></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id}>
                <td className="muted">{String(r.created_at).slice(0, 19)}</td>
                <td className="mono">{r.action}</td>
                <td className="mono">{r.actor_id}</td>
                <td className="muted">{r.entity_type ?? ""} {r.entity_id ?? ""}</td>
                <td className="mono muted" style={{ fontSize: 11, maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {JSON.stringify(r.details)}
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={5} className="muted">Записей нет</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
