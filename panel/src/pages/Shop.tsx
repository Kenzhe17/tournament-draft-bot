import { useEffect, useState } from "react";
import { api, type ShopItem } from "../api";

const CATEGORIES = ["icons", "tags", "services", "roles", "access", "cases"];
const RARITIES = ["basic", "premium", "elite", "special"];
const COSMETIC_TYPES = ["color", "icon", "tag", "frame"];
const ITEM_TYPES = ["cosmetic", "role", "service"];

const EMPTY: Partial<ShopItem> = {
  name: "", description: "", price: 0, category: "icons",
  rarity: "basic", is_sellable: true, is_active: true,
  cosmetic_type: "icon", value: "", item_type: "cosmetic",
  role_id: null, required_level: 0,
};

export default function Shop() {
  const [items, setItems] = useState<ShopItem[]>([]);
  const [showInactive, setShowInactive] = useState(false);
  const [editing, setEditing] = useState<Partial<ShopItem> | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api.shopItems(showInactive)
    .then(r => setItems(r.items)).catch(e => setErr(String(e)));
  useEffect(() => { load(); }, [showInactive]);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setErr(""); setMsg("");
    try { await fn(); setMsg(ok); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const save = () => {
    if (!editing) return;
    const isNew = !items.some(i => i.id === editing.id) && !editing.id;
    const body = { ...editing };
    if (isNew) {
      body.id = (body.name || "item").toLowerCase().replace(/[^a-z0-9]+/g, "_");
    }
    run(() => isNew ? api.shopCreate(body) : api.shopUpdate(editing.id!, body),
        isNew ? "Создано" : "Обновлено");
    setEditing(null);
  };

  return (
    <>
      <div className="topbar"><h2 style={{ margin: 0 }}>Shop — каталог</h2>
        <div className="row">
          <label className="muted"><input type="checkbox" checked={showInactive}
            onChange={e => setShowInactive(e.target.checked)} /> показать скрытые</label>
          <button className="btn" onClick={() => setEditing({ ...EMPTY })}>+ Товар</button>
        </div>
      </div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}
      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Название</th><th>Категория</th><th>Цена</th>
            <th>Редкость</th><th>Статус</th><th></th></tr></thead>
          <tbody>
            {items.map(it => (
              <tr key={it.id}>
                <td className="mono">{it.id}</td>
                <td>{it.name}<div className="muted" style={{ fontSize: 11 }}>{it.description?.slice(0, 60)}</div></td>
                <td>{it.category}</td><td className="mono">{it.price}</td>
                <td>{it.rarity}</td>
                <td>{it.is_active ? <span className="badge ok">active</span> : <span className="badge muted">hidden</span>}</td>
                <td className="row">
                  <button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                    onClick={() => setEditing({ ...it })}>Изм.</button>
                  <button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                    onClick={() => run(() => it.is_active ? api.shopHide(it.id) : api.shopShow(it.id),
                      it.is_active ? "Скрыто" : "Показано")}>{it.is_active ? "Скрыть" : "Показать"}</button>
                  <button className="btn danger" style={{ fontSize: 11, padding: "3px 8px" }}
                    onClick={() => confirm(`Удалить ${it.id}?`) &&
                      run(() => api.shopDelete(it.id), "Удалено")}>✕</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && (
        <div className="card">
          <h3>{items.some(i => i.id === editing.id) ? `Редактирование: ${editing.id}` : "Новый товар"}</h3>
          <div className="grid">
            <label>Название<br /><input style={{ width: "100%" }} value={editing.name ?? ""}
              onChange={e => setEditing({ ...editing, name: e.target.value })} /></label>
            <label>Категория<br /><select value={editing.category}
              onChange={e => setEditing({ ...editing, category: e.target.value })}>
              {CATEGORIES.map(c => <option key={c}>{c}</option>)}</select></label>
            <label>Цена<br /><input type="number" value={editing.price ?? 0}
              onChange={e => setEditing({ ...editing, price: +e.target.value })} /></label>
            <label>Редкость<br /><select value={editing.rarity}
              onChange={e => setEditing({ ...editing, rarity: e.target.value })}>
              {RARITIES.map(r => <option key={r}>{r}</option>)}</select></label>
            <label>Тип<br /><select value={editing.item_type}
              onChange={e => setEditing({ ...editing, item_type: e.target.value })}>
              {ITEM_TYPES.map(t => <option key={t}>{t}</option>)}</select></label>
            <label>Cosmetic type<br /><select value={editing.cosmetic_type}
              onChange={e => setEditing({ ...editing, cosmetic_type: e.target.value })}>
              {COSMETIC_TYPES.map(t => <option key={t}>{t}</option>)}</select></label>
            <label>Значение<br /><input style={{ width: "100%" }} value={editing.value ?? ""}
              onChange={e => setEditing({ ...editing, value: e.target.value })} /></label>
            <label>Описание<br /><input style={{ width: "100%" }} value={editing.description ?? ""}
              onChange={e => setEditing({ ...editing, description: e.target.value })} /></label>
            <label>Мин. уровень<br /><input type="number" value={editing.required_level ?? 0}
              onChange={e => setEditing({ ...editing, required_level: +e.target.value })} /></label>
            {editing.item_type === "role" && (
              <label>Role ID<br /><input type="number" value={editing.role_id ?? ""}
                onChange={e => setEditing({ ...editing, role_id: +e.target.value })} /></label>
            )}
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <label className="muted"><input type="checkbox" checked={!!editing.is_active}
              onChange={e => setEditing({ ...editing, is_active: e.target.checked })} /> активен</label>
            <label className="muted"><input type="checkbox" checked={!!editing.is_sellable}
              onChange={e => setEditing({ ...editing, is_sellable: e.target.checked })} /> продаваемый</label>
            <button className="btn" onClick={save}>Сохранить</button>
            <button className="btn secondary" onClick={() => setEditing(null)}>Отмена</button>
          </div>
        </div>
      )}
    </>
  );
}
