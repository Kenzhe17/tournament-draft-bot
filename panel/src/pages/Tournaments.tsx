import { useEffect, useState } from "react";
import { adminApi, api, type BotSnapshot, type TournamentSnap, type MMQueueSnap } from "../api";

export default function Tournaments() {
  const [snap, setSnap] = useState<BotSnapshot | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");

  const load = () => adminApi.snapshot().then(setSnap).catch(e => setErr(String(e)));
  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  const cancel = async (t: TournamentSnap) => {
    if (!confirm(`Отменить турнир в канале ${t.channel_id}?\nФаза: ${t.phase}. Действие выполнит бот через свою state-machine.`)) return;
    setBusy(t.id); setErr(""); setMsg("");
    try {
      const r = await api.actions("tournament.cancel",
        { channel_id: t.channel_id }, `tcancel:${t.id}:${Date.now()}`);
      setMsg(`Запрос отмены отправлен боту (action ${(r.action as Record<string, unknown>).id}). Бот удалит турнир и обновит Discord.`);
      setTimeout(load, 5000);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(""); }
  };

  if (!snap) return <div className="muted">Загрузка…</div>;
  if (!snap.available) return <div className="notice">{snap.reason}</div>;

  const tournaments = Object.entries(snap.tournaments || {}).flatMap(([gid, list]) =>
    list.map(t => ({ ...t, guild_id: gid })));
  const queues = Object.entries(snap.matchmaking || {}).flatMap(([gid, list]) =>
    list.map((q: MMQueueSnap) => ({ ...q, guild_id: gid })));

  return (
    <>
      <div className="topbar"><h2 style={{ margin: 0 }}>Tournaments</h2>
        <button className="btn secondary" onClick={load}>Обновить</button></div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}
      <p className="muted">Снимок от бота: {snap.last_seen ? String(snap.last_seen).slice(0, 19) : "—"}.
        Мутации выполняются ботом через Admin Actions (state-machine).</p>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Активные турниры ({tournaments.length})</h3>
        <table>
          <thead><tr><th>ID</th><th>Канал</th><th>Размер</th><th>Фаза</th>
            <th>Регистрация</th><th>Режим</th><th>Игроки</th><th>Тест</th><th></th></tr></thead>
          <tbody>
            {tournaments.map(t => (
              <TournamentRow key={t.id} t={t} busy={busy === t.id} onCancel={cancel} />
            ))}
            {tournaments.length === 0 &&
              <tr><td colSpan={9} className="muted">Активных турниров нет</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Очереди Matchmaking ({queues.length})</h3>
        <table>
          <thead><tr><th>Канал</th><th>Формат</th><th>Состояние</th>
            <th>Игроки</th><th>Таймер</th><th>→ Турнир</th></tr></thead>
          <tbody>
            {queues.map((q, i) => (
              <tr key={i}>
                <td className="mono">{q.channel_id}</td>
                <td>{q.format}</td>
                <td><span className={`badge ${q.state === "open" ? "ok" : "warn"}`}>{q.state}</span></td>
                <td className="mono">{q.players}{q.required ? `/${q.required}` : ""}</td>
                <td className="muted">{q.player_names.slice(0, 6).join(", ")}{q.player_names.length > 6 ? "…" : ""}</td>
                <td className="mono">{q.tournament_channel_id || "—"}</td>
              </tr>
            ))}
            {queues.length === 0 &&
              <tr><td colSpan={6} className="muted">Активных очередей нет</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TournamentRow({ t, busy, onCancel }:
  { t: TournamentSnap & { guild_id: string }; busy: boolean;
    onCancel: (t: TournamentSnap & { guild_id: string }) => void }) {
  const [open, setOpen] = useState(false);
  const p = t.players || {};
  return (
    <>
      <tr>
        <td className="mono" style={{ fontSize: 11 }}>{t.id.slice(0, 20)}…</td>
        <td className="mono">{t.channel_id}</td>
        <td>{t.size}</td>
        <td><span className={`badge ${t.phase === "COMPLETE" ? "muted" : "ok"}`}>{t.phase}</span></td>
        <td>{t.registration}</td>
        <td>{t.formation_mode}</td>
        <td className="mono">{Object.values(p).reduce((a, b) => a + b, 0)}</td>
        <td>{t.is_test ? <span className="badge warn">test</span> : ""}</td>
        <td className="row">
          <button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
            onClick={() => setOpen(!open)}>{open ? "Скрыть" : "Детали"}</button>
          {t.phase !== "COMPLETE" && (
            <button className="btn danger" style={{ fontSize: 11, padding: "3px 8px" }}
              disabled={busy} onClick={() => onCancel(t)}>Отменить</button>
          )}
        </td>
      </tr>
      {open && (
        <tr><td colSpan={9}>
          <div className="mono muted" style={{ fontSize: 12 }}>
            Организатор: {t.organizer_id} · Создан: {String(t.created_at).slice(0, 19)}
            <br />Составы: {Object.entries(p).map(([k, v]) => `${k}: ${v}`).join(" · ")}
          </div>
        </td></tr>
      )}
    </>
  );
}
