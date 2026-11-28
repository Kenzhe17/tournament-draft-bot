import { useEffect, useState } from "react";
import { api, type Minigame } from "../api";

export default function Games() {
  const [games, setGames] = useState<Minigame[]>([]);
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState<Minigame | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api.games(all).then(r => setGames(r.games)).catch(e => setErr(String(e)));
  useEffect(() => { load(); }, [all]);

  const save = async () => {
    if (!editing) return;
    setErr(""); setMsg("");
    try {
      await api.gameUpdate(editing.id, {
        name: editing.name, description: editing.description,
        category: editing.category, difficulty: editing.difficulty,
        min_bet: editing.min_bet, max_bet: editing.max_bet,
        multiplier: editing.multiplier, is_pvp: editing.is_pvp,
        is_pve: editing.is_pve, is_active: editing.is_active,
      });
      setMsg(`Обновлено: ${editing.id}`);
      setEditing(null);
      await load();
    } catch (e) { setErr(String(e)); }
  };

  const f = (k: keyof Minigame) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setEditing(ed => ed ? { ...ed, [k]: e.target.type === "checkbox" ? (e.target as HTMLInputElement).checked
      : e.target.type === "number" ? +e.target.value : e.target.value } as Minigame : ed);

  return (
    <>
      <div className="topbar"><h2 style={{ margin: 0 }}>Mini-games</h2>
        <label className="muted"><input type="checkbox" checked={all}
          onChange={e => setAll(e.target.checked)} /> показать неактивные</label>
      </div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Название</th><th>Категория</th><th>Ставки</th>
            <th>Множитель</th><th>Тип</th><th>Статус</th><th></th></tr></thead>
          <tbody>
            {games.map(g => (
              <tr key={g.id}>
                <td className="mono">{g.id}</td><td>{g.name}</td>
                <td>{g.category}</td>
                <td className="mono">{g.min_bet}–{g.max_bet}</td>
                <td className="mono">×{g.multiplier}</td>
                <td className="muted">{g.is_pvp ? "PvP" : ""}{g.is_pve ? " PvE" : ""}</td>
                <td>{g.is_active ? <span className="badge ok">active</span> : <span className="badge muted">off</span>}</td>
                <td><button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                  onClick={() => setEditing({ ...g })}>Изм.</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && (
        <div className="card">
          <h3>Редактирование: {editing.id}</h3>
          <div className="grid">
            <label>Название<br /><input style={{ width: "100%" }} value={editing.name} onChange={f("name")} /></label>
            <label>Категория<br /><input style={{ width: "100%" }} value={editing.category} onChange={f("category")} /></label>
            <label>Сложность<br /><input style={{ width: "100%" }} value={editing.difficulty} onChange={f("difficulty")} /></label>
            <label>Мин. ставка<br /><input type="number" value={editing.min_bet} onChange={f("min_bet")} /></label>
            <label>Макс. ставка<br /><input type="number" value={editing.max_bet} onChange={f("max_bet")} /></label>
            <label>Множитель<br /><input type="number" step="any" value={editing.multiplier} onChange={f("multiplier")} /></label>
            <label>Описание<br /><input style={{ width: "100%" }} value={editing.description} onChange={f("description")} /></label>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <label className="muted"><input type="checkbox" checked={editing.is_pvp} onChange={f("is_pvp")} /> PvP</label>
            <label className="muted"><input type="checkbox" checked={editing.is_pve} onChange={f("is_pve")} /> PvE</label>
            <label className="muted"><input type="checkbox" checked={editing.is_active} onChange={f("is_active")} /> активна</label>
            <button className="btn" onClick={save}>Сохранить</button>
            <button className="btn secondary" onClick={() => setEditing(null)}>Отмена</button>
          </div>
        </div>
      )}
    </>
  );
}
