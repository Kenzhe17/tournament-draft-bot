import { useState } from "react";
import { api, type PlayerRow } from "../api";

export default function Players() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<PlayerRow[]>([]);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState("");

  const search = async (query: string) => {
    setErr(""); setDetail(null);
    try {
      const r = query
        ? await api.playersSearch(query)
        : await api.playersLeaderboard();
      setItems(r.items);
    } catch (e) { setErr(String(e)); }
  };

  const open = async (uid: number) => {
    setErr("");
    try { setDetail(await api.playerDetail(uid)); }
    catch (e) { setErr(String(e)); }
  };

  return (
    <>
      <h2>Players</h2>
      <div className="row" style={{ marginBottom: 14 }}>
        <input placeholder="Ник или Discord ID…" value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search(q)} style={{ width: 280 }} />
        <button className="btn" onClick={() => search(q)}>Найти</button>
        <button className="btn secondary" onClick={() => { setQ(""); search(""); }}>Лидерборд</button>
      </div>
      {err && <div className="error-box">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>Игрок</th><th>ELO</th><th>W/L</th><th>Игры</th>
            <th>Уровень</th><th>Заработано</th><th>REP</th></tr></thead>
          <tbody>
            {items.map(p => (
              <tr key={p.user_id} style={{ cursor: "pointer" }} onClick={() => open(p.user_id)}>
                <td>{p.name}<div className="mono muted" style={{ fontSize: 11 }}>{p.user_id}</div></td>
                <td className="mono">{p.elo ?? "—"}</td>
                <td className="mono">{p.wins ?? 0}/{(p.wins ?? 0) + 0}W · {p.finals ?? 0} фин.</td>
                <td className="mono">{p.games ?? 0}</td>
                <td className="mono">{p.level ?? "—"}</td>
                <td className="mono">{p.total_earnings ?? 0}</td>
                <td className="mono">{p.reputation ?? 0}</td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={7} className="muted">Нет данных — выполните поиск</td></tr>}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="card">
          <h3>Карточка игрока</h3>
          <button className="btn secondary" onClick={() => setDetail(null)}>Закрыть</button>
          <pre className="mono" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
            {JSON.stringify(detail, null, 1)}
          </pre>
        </div>
      )}
    </>
  );
}
