import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type SettingDef, type SettingValue } from "../api";

const MODULE_LABELS: Record<string, string> = {
  feature: "Feature Flags",
  elo: "ELO (ultimatum/SR)",
  rating: "Rating (CLASSIC ELO)",
  matchmaking: "Matchmaking",
  economy: "Economy",
  reputation: "Reputation (REP)",
  tournament: "Tournament",
  vac: "VAC / Moderation",
};

function ValueEditor({ def, value, onSave, saving }:
  { def: SettingDef; value: unknown; onSave: (v: unknown) => void; saving: boolean }) {
  const [local, setLocal] = useState<unknown>(value);
  useEffect(() => setLocal(value), [value]);

  const input = (() => {
    if (def.type === "bool") {
      return (
        <select value={String(local)} onChange={e => setLocal(e.target.value === "true")}
          disabled={!def.runtime_editable}>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      );
    }
    if (def.type === "json") {
      return (
        <textarea rows={3} style={{ width: "100%", fontFamily: "Consolas, monospace" }}
          value={typeof local === "string" ? local : JSON.stringify(local)}
          disabled={!def.runtime_editable}
          onChange={e => setLocal(e.target.value)} />
      );
    }
    return (
      <input type={def.type === "float" ? "number" : "text"}
        step={def.type === "float" ? "any" : undefined}
        value={local === null || local === undefined ? "" : String(local)}
        disabled={!def.runtime_editable}
        onChange={e => {
          const v = e.target.value;
          if (def.type === "int") setLocal(v === "" ? "" : parseInt(v, 10));
          else if (def.type === "float") setLocal(v === "" ? "" : parseFloat(v));
          else setLocal(v);
        }} />
    );
  })();

  return (
    <div className="row" style={{ justifyContent: "space-between" }}>
      <div style={{ flex: 1, minWidth: 200 }}>{input}</div>
      <button className="btn" disabled={!def.runtime_editable || saving}
        onClick={() => {
          let v = local;
          if (def.type === "json" && typeof v === "string") {
            try { v = JSON.parse(v); } catch { return; }
          }
          onSave(v);
        }}>Сохранить</button>
    </div>
  );
}

export default function Settings({ module }: { module: string | null }) {
  const [defs, setDefs] = useState<SettingDef[]>([]);
  const [values, setValues] = useState<Record<string, SettingValue>>({});
  const [modules, setModules] = useState<string[]>([]);
  const [activeModule, setActiveModule] = useState<string | null>(module);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState("");
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, unknown>[]>([]);

  const load = async (mod: string | null) => {
    setErr("");
    try {
      const [s, v] = await Promise.all([
        api.listSettings(mod ?? undefined),
        api.listValues(),
      ]);
      setDefs(s.settings);
      setModules(s.modules);
      setValues(Object.fromEntries(v.values.map(x => [x.key, x])));
    } catch (e) { setErr(String(e)); }
  };

  useEffect(() => { load(activeModule); }, [activeModule]);
  useEffect(() => { if (module) setActiveModule(module); }, [module]);

  const grouped = useMemo(() => {
    const m = new Map<string, SettingDef[]>();
    for (const d of defs) {
      const arr = m.get(d.module) || [];
      arr.push(d); m.set(d.module, arr);
    }
    return m;
  }, [defs]);

  const save = async (key: string, value: unknown, version?: number) => {
    setSaving(key); setErr(""); setMsg("");
    try {
      await api.setSetting(key, value, version);
      setMsg(`Сохранено: ${key}`);
      await load(activeModule);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr(`Конфликт версий для ${key} — значение изменено извне. Обновите страницу.`);
      } else setErr(String(e));
    } finally { setSaving(""); }
  };

  const showHistory = async (key: string) => {
    setHistoryFor(key);
    try {
      const h = await api.settingHistory(key);
      setHistory(h.history as Record<string, unknown>[]);
    } catch (e) { setErr(String(e)); }
  };

  const rollback = async (key: string, v: number) => {
    if (!confirm(`Откатить ${key} к версии ${v}?`)) return;
    try {
      await api.rollbackSetting(key, v);
      setMsg(`Откачено: ${key} → v${v}`);
      await load(activeModule);
    } catch (e) { setErr(String(e)); }
  };

  return (
    <>
      <h2>Настройки</h2>
      <div className="row" style={{ marginBottom: 14 }}>
        <select value={activeModule ?? ""} onChange={e => setActiveModule(e.target.value || null)}>
          <option value="">Все модули</option>
          {modules.map(m => <option key={m} value={m}>{MODULE_LABELS[m] || m}</option>)}
        </select>
        <button className="btn secondary" onClick={() => load(activeModule)}>Обновить</button>
      </div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}
      {[...grouped.entries()].map(([mod, list]) => (
        <div className="card" key={mod}>
          <h3 style={{ marginTop: 0 }}>{MODULE_LABELS[mod] || mod}</h3>
          <table>
            <thead><tr><th>Ключ</th><th>Значение</th><th>Тип</th><th></th></tr></thead>
            <tbody>
              {list.map(d => {
                const val = values[d.key];
                return (
                  <tr key={d.key}>
                    <td style={{ maxWidth: 280 }}>
                      <div className="mono" style={{ fontSize: 12 }}>{d.key}</div>
                      <div className="muted" style={{ fontSize: 11 }}>{d.description}</div>
                      {d.consumers_pending && <span className="badge warn">consumer pending</span>}
                      {!d.runtime_editable && <span className="badge muted">code-managed</span>}
                    </td>
                    <td style={{ minWidth: 220 }}>
                      <ValueEditor def={d} value={val?.value ?? d.default}
                        saving={saving === d.key}
                        onSave={v => save(d.key, v, val?.version)} />
                    </td>
                    <td className="muted">{d.type}</td>
                    <td>
                      <button className="btn secondary" style={{ fontSize: 11, padding: "4px 8px" }}
                        onClick={() => showHistory(d.key)}>История</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
      {historyFor && (
        <div className="card">
          <h3>История: {historyFor}</h3>
          <button className="btn secondary" onClick={() => setHistoryFor(null)}>Закрыть</button>
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>Версия</th><th>Значение</th><th>Когда</th><th>Кем</th><th></th></tr></thead>
            <tbody>
              {history.map((h, i) => (
                <tr key={i}>
                  <td className="mono">v{String(h.version ?? "?")}</td>
                  <td className="mono" style={{ fontSize: 11 }}>{JSON.stringify(h.value ?? h.new_value ?? "")}</td>
                  <td className="muted">{String(h.created_at ?? "").slice(0, 19)}</td>
                  <td className="muted">{String(h.actor_id ?? "")}</td>
                  <td><button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                    onClick={() => rollback(historyFor, Number(h.version))}>Откат</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
