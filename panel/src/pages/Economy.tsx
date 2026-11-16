import { useState } from "react";
import { adminApi, api, type EconomyPlayer, type GrantRow } from "../api";

export default function Economy() {
  const [uid, setUid] = useState("");
  const [player, setPlayer] = useState<EconomyPlayer | null>(null);
  const [grants, setGrants] = useState<GrantRow[]>([]);
  const [form, setForm] = useState({ amount: "", reason: "" });
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const lookup = async () => {
    const id = parseInt(uid, 10);
    if (!id) { setErr("Введите Discord user ID"); return; }
    setErr(""); setPlayer(null);
    try {
      setPlayer(await adminApi.economyPlayer(id));
      const g = await adminApi.economyGrants();
      setGrants(g.items.filter(x => x.user_id === id));
    } catch (e) { setErr(String(e)); }
  };

  const adjust = async () => {
    const id = parseInt(uid, 10);
    const amount = parseInt(form.amount, 10);
    if (!id || !amount) { setErr("Укажите user_id и сумму"); return; }
    const big = Math.abs(amount) >= 10000;
    if (big && !confirm(`Крупная операция: ${amount > 0 ? "+" : ""}${amount} монет для ${id}. Подтвердить?`)) return;
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await api.actions("economy.adjust",
        { user_id: id, amount, reason: form.reason },
        `econ:${id}:${amount}:${Date.now()}`);
      const act = r.action as Record<string, unknown>;
      setMsg(`Операция поставлена в очередь (action ${act.id}). Бот выполнит через транзакционный store и запишет аудит.`);
      setForm({ amount: "", reason: "" });
      setTimeout(lookup, 5000);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <>
      <h2>Economy — балансы</h2>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}

      <div className="card">
        <div className="row">
          <input placeholder="Discord user ID" value={uid}
            onChange={e => setUid(e.target.value)} style={{ width: 180 }}
            onKeyDown={e => e.key === "Enter" && lookup()} />
          <button className="btn" onClick={lookup}>Найти</button>
        </div>
        {player && (
          <div style={{ marginTop: 14 }}>
            <p>Баланс <strong className="mono">{player.balance ?? "—"}</strong> монет
              {player.balance !== null && player.balance >= 100000 &&
                <span className="badge warn"> у лимита cap</span>}</p>
            <div className="row">
              <input placeholder="Сумма (+ начислить / − списать)" value={form.amount}
                onChange={e => setForm({ ...form, amount: e.target.value })} style={{ width: 220 }} />
              <input placeholder="Причина (audit)" value={form.reason} style={{ flex: 1, minWidth: 160 }}
                onChange={e => setForm({ ...form, reason: e.target.value })} />
              <button className="btn" disabled={busy} onClick={adjust}>Выполнить</button>
            </div>
            <p className="muted" style={{ fontSize: 12 }}>
              Списание ограничено текущим балансом; начисление — лимитом
              economy.max_balance. Идемпотентность — по action id в очереди.
            </p>
          </div>
        )}
      </div>

      {player && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Последние начисления ({grants.length})</h3>
          <table>
            <thead><tr><th>Match ID</th><th>Сумма</th><th>Время</th></tr></thead>
            <tbody>
              {grants.map((g, i) => (
                <tr key={i}><td className="mono" style={{ fontSize: 11 }}>{g.match_id}</td>
                  <td className="mono">+{g.amount}</td>
                  <td className="muted">{String(g.granted_at).slice(0, 19)}</td></tr>
              ))}
              {grants.length === 0 && <tr><td colSpan={3} className="muted">Нет записей</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
