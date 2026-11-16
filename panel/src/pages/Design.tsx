import { useEffect, useState } from "react";
import { adminApi, type TemplateRow } from "../api";

export default function Design() {
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [selected, setSelected] = useState<TemplateRow | null>(null);
  const [payloadText, setPayloadText] = useState("");
  const [newKey, setNewKey] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => adminApi.templates().then(r => setTemplates(r.items))
    .catch(e => setErr(String(e)));
  useEffect(() => { load(); }, []);

  const open = (t: TemplateRow) => {
    setSelected(t);
    setPayloadText(JSON.stringify(t.payload, null, 2));
    setNewKey(t.key);
  };

  const save = async () => {
    setErr(""); setMsg("");
    let payload: Record<string, unknown>;
    try { payload = JSON.parse(payloadText); }
    catch { setErr("Невалидный JSON"); return; }
    try {
      const r = await adminApi.putTemplate(newKey.trim(), payload);
      setMsg(`Сохранено: ${newKey} v${r.version}`);
      await load();
    } catch (e) { setErr(String(e)); }
  };

  return (
    <>
      <h2>Design — CV2 шаблоны</h2>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Шаблоны ({templates.length})</h3>
        <table>
          <thead><tr><th>Ключ</th><th>Версия</th><th>Обновлён</th><th>Кем</th><th></th></tr></thead>
          <tbody>
            {templates.map(t => (
              <tr key={`${t.guild_id}:${t.key}`}>
                <td className="mono">{t.key}</td>
                <td className="mono">v{t.version}</td>
                <td className="muted">{String(t.updated_at).slice(0, 19)}</td>
                <td className="mono">{t.updated_by}</td>
                <td><button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                  onClick={() => open(t)}>Открыть</button></td>
              </tr>
            ))}
            {templates.length === 0 &&
              <tr><td colSpan={5} className="muted">Шаблонов нет — создайте ниже</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{selected ? `Редактирование: ${selected.key}` : "Новый шаблон"}</h3>
        <div className="row" style={{ marginBottom: 10 }}>
          <input placeholder="template.key (напр. welcome.card)" value={newKey}
            onChange={e => setNewKey(e.target.value)} style={{ width: 320 }} />
        </div>
        <textarea rows={12} style={{ width: "100%", fontFamily: "Consolas, monospace" }}
          value={payloadText} onChange={e => setPayloadText(e.target.value)}
          placeholder='{"title": "...", "text": "...", "color": 5814783}' />
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn" onClick={save}>Опубликовать</button>
          {selected && <button className="btn secondary" onClick={() => { setSelected(null); setPayloadText(""); setNewKey(""); }}>Новый</button>}
        </div>
        <p className="muted" style={{ fontSize: 12 }}>
          payload — JSON-описание шаблона (заголовок/текст/цвет/кнопки).
          custom_id и обработчики кнопок заданы кодом бота и не редактируются.
          Каждое сохранение — новая версия + запись в Audit Log.
        </p>
      </div>
    </>
  );
}
