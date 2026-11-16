import { useEffect, useState } from "react";
import Settings from "./Settings";
import { adminApi, type BotSnapshot } from "../api";

export default function DiscordSettings() {
  const [snap, setSnap] = useState<BotSnapshot | null>(null);
  useEffect(() => { adminApi.snapshot().then(setSnap).catch(() => {}); }, []);

  const guild = snap?.guilds ? Object.values(snap.guilds)[0] : null;

  return (
    <>
      <h2>Discord Settings</h2>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Ресурсы сервера (от бота)</h3>
        {!guild ? (
          <p className="muted">Снимок каналов/ролей ещё не опубликован ботом
            (обновляется каждые ~60 сек). ID каналов можно вводить вручную.</p>
        ) : (
          <div className="grid">
            <div>
              <h4>Каналы ({guild.channels.length})</h4>
              <div style={{ maxHeight: 300, overflow: "auto" }}>
                {guild.channels.map(c => (
                  <div key={c.id} className="mono" style={{ fontSize: 12, padding: "2px 0" }}>
                    #{c.name} <span className="muted">{c.id}</span>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <h4>Роли ({guild.roles.length})</h4>
              <div style={{ maxHeight: 300, overflow: "auto" }}>
                {guild.roles.map(r => (
                  <div key={r.id} className="mono" style={{ fontSize: 12, padding: "2px 0" }}>
                    {r.name} <span className="muted">{r.id}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
        <p className="muted" style={{ fontSize: 12 }}>
          Значения применяются через runtime-настройки ниже; часть ключей
          ещё помечена consumer pending (см. бейджи).
        </p>
      </div>
      <Settings module="discord" />
    </>
  );
}
