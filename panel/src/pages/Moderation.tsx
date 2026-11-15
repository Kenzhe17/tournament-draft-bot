import { useEffect, useState } from "react";
import { adminApi, adminApi2, api, type VacRow } from "../api";

export default function Moderation() {
  const [active, setActive] = useState<VacRow[]>([]);
  const [history, setHistory] = useState<VacRow[]>([]);
  const [cases, setCases] = useState<Record<string, unknown>[]>([]);
  const [reports, setReports] = useState<Record<string, unknown>[]>([]);
  const [uid, setUid] = useState("");
  const [form, setForm] = useState({ user_id: "", duration: "7d", reason: "" });
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    adminApi.vacActive().then(r => setActive(r.items)).catch(e => setErr(String(e)));
    adminApi.vacHistory().then(r => setHistory(r.items)).catch(() => {});
    adminApi2.modCases().then(r => setCases(r.items)).catch(() => {});
    adminApi2.modReports().then(r => setReports(r.items)).catch(() => {});
  };
  useEffect(load, []);

  const act = async (action: string, params: Record<string, unknown>, ok: string) => {
    setBusy(true); setErr(""); setMsg("");
    try {
      await api.actions(action, params, `${action}:${JSON.stringify(params)}:${Date.now()}`);
      setMsg(ok);
      setTimeout(load, 4000);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  };

  const issue = () => {
    const user_id = parseInt(form.user_id, 10);
    if (!user_id || !form.duration) { setErr("Укажите user_id и срок"); return; }
    act("vac.issue", { user_id, duration: form.duration, reason: form.reason },
        `VAC выдан пользователю ${user_id} — бот применит блокировку`);
  };

  const revoke = (user_id: number) => {
    if (!confirm(`Снять VAC у ${user_id}?`)) return;
    act("vac.revoke", { user_id }, `Запрос на снятие VAC для ${user_id} отправлен боту`);
  };

  const searchHistory = () => {
    const id = parseInt(uid, 10);
    adminApi.vacHistory(id || undefined).then(r => setHistory(r.items))
      .catch(e => setErr(String(e)));
  };

  return (
    <>
      <h2>Moderation — VAC</h2>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Выдать VAC</h3>
        <div className="row">
          <input placeholder="Discord user ID" value={form.user_id}
            onChange={e => setForm({ ...form, user_id: e.target.value })} style={{ width: 160 }} />
          <input placeholder="Срок (7d / 12h / 30m)" value={form.duration}
            onChange={e => setForm({ ...form, duration: e.target.value })} style={{ width: 140 }} />
          <input placeholder="Причина" value={form.reason} style={{ flex: 1, minWidth: 180 }}
            onChange={e => setForm({ ...form, reason: e.target.value })} />
          <button className="btn" disabled={busy} onClick={issue}>Выдать</button>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>
          Операция выполняется ботом через vac_service (проверка активного VAC,
          лимит vac.max_days, аудит). Статус — в Admin Actions / Audit Log.
        </p>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Действующие VAC ({active.length})</h3>
        <table>
          <thead><tr><th>User</th><th>Выдан</th><th>Истекает</th><th>Уровень</th><th>Причина</th><th></th></tr></thead>
          <tbody>
            {active.map((v, i) => (
              <tr key={i}>
                <td className="mono">{String(v.user_id)}</td>
                <td className="muted">{String(v.issued_at ?? "").slice(0, 19)}</td>
                <td className="mono">{String(v.expires_at ?? "").slice(0, 19)}</td>
                <td className="mono">{String(v.level ?? "—")}</td>
                <td className="muted">{String(v.reason ?? "")}</td>
                <td><button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                  disabled={busy} onClick={() => revoke(Number(v.user_id))}>Снять</button></td>
              </tr>
            ))}
            {active.length === 0 && <tr><td colSpan={6} className="muted">Нет активных VAC</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="topbar"><h3 style={{ margin: 0 }}>История VAC</h3>
          <div className="row">
            <input placeholder="user_id…" value={uid} onChange={e => setUid(e.target.value)} style={{ width: 140 }} />
            <button className="btn secondary" onClick={searchHistory}>Найти</button>
          </div>
        </div>
        <table>
          <thead><tr><th>User</th><th>Выдан</th><th>Срок</th><th>Статус</th><th>Отозван</th></tr></thead>
          <tbody>
            {history.map((v, i) => (
              <tr key={i}>
                <td className="mono">{String(v.user_id)}</td>
                <td className="muted">{String(v.issued_at ?? "").slice(0, 19)}</td>
                <td className="mono">{String(v.duration ?? v.expires_at ?? "")}</td>
                <td>{v.revoked_at ? <span className="badge muted">revoked</span>
                  : <span className="badge ok">active/expired</span>}</td>
                <td className="muted">{String(v.revoked_at ?? "").slice(0, 19)}</td>
              </tr>
            ))}
            {history.length === 0 && <tr><td colSpan={5} className="muted">Нет записей</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Кейсы модерации ({cases.length})</h3>
        <table>
          <thead><tr><th>ID</th><th>Target</th><th>Тип</th><th>Причина</th><th>Статус</th><th>Создан</th></tr></thead>
          <tbody>
            {cases.map((c, i) => (
              <tr key={i}>
                <td className="mono">{String(c.id)}</td>
                <td className="mono">{String(c.target_id)}</td>
                <td>{String(c.kind)}</td>
                <td className="muted">{String(c.reason ?? "").slice(0, 60)}</td>
                <td><span className={`badge ${c.status === "active" ? "warn" : "muted"}`}>{String(c.status)}</span></td>
                <td className="muted">{String(c.created_at ?? "").slice(0, 19)}</td>
              </tr>
            ))}
            {cases.length === 0 && <tr><td colSpan={6} className="muted">Нет кейсов</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Жалобы ({reports.length})</h3>
        <table>
          <thead><tr><th>ID</th><th>Reporter</th><th>Target</th><th>Причина</th><th>Статус</th></tr></thead>
          <tbody>
            {reports.map((r, i) => (
              <tr key={i}>
                <td className="mono">{String(r.id)}</td>
                <td className="mono">{String(r.reporter_id)}</td>
                <td className="mono">{String(r.target_id)}</td>
                <td className="muted">{String(r.reason ?? "").slice(0, 60)}</td>
                <td><span className={`badge ${r.status === "open" ? "warn" : "muted"}`}>{String(r.status)}</span></td>
              </tr>
            ))}
            {reports.length === 0 && <tr><td colSpan={5} className="muted">Нет жалоб</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
