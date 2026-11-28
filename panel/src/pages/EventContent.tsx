import { useEffect, useRef, useState } from "react";
import {
  api, eventContentApi,
  type ContentEntry, type ContentGameStat,
} from "../api";

const GAMES = [
  { id: "word", label: "Перепутанное слово" },
  { id: "krokodil", label: "Крокодил" },
];
const DIFFS = ["easy", "medium", "hard"];
const PAGE = 50;

interface EditState extends Partial<ContentEntry> {
  isNew?: boolean;
}

export default function EventContent() {
  const [games, setGames] = useState<ContentGameStat[]>([]);
  const [game, setGame] = useState("word");
  const [items, setItems] = useState<ContentEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [qApplied, setQApplied] = useState("");
  const [difficulty, setDifficulty] = useState("");
  const [category, setCategory] = useState("");
  const [showDisabled, setShowDisabled] = useState(true);
  const [categories, setCategories] = useState<string[]>([]);
  const [editing, setEditing] = useState<EditState | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [importReport, setImportReport] = useState<Record<string, unknown> | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const stat = games.find(g => g.game === game);

  const loadGames = () =>
    eventContentApi.games().then(r => setGames(r.games))
      .catch(e => setErr(String(e)));

  const load = () => {
    setErr("");
    eventContentApi.entries(game, {
      q: qApplied, difficulty, category,
      enabled: showDisabled ? undefined : true,
      limit: PAGE, offset,
    }).then(r => { setItems(r.items); setTotal(r.total); })
      .catch(e => setErr(String(e)));
    eventContentApi.categories(game)
      .then(r => setCategories(r.categories)).catch(() => {});
  };

  useEffect(() => { loadGames(); }, []);
  useEffect(() => { setOffset(0); load(); },
    [game, qApplied, difficulty, category, showDisabled, offset]);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setErr(""); setMsg("");
    try { await fn(); setMsg(ok); load(); loadGames(); }
    catch (e) { setErr(String(e)); }
  };

  const save = () => {
    if (!editing) return;
    const body: Record<string, unknown> = {
      category: editing.category ?? "",
      difficulty: editing.difficulty ?? "easy",
      enabled: editing.enabled ?? true,
    };
    if (game === "word") {
      body.word = editing.word;
      if (editing.accepted?.length) body.accepted = editing.accepted;
    } else {
      body.answer = editing.answer;
      body.clue = editing.clue;
      body.accepted = editing.accepted?.length
        ? editing.accepted : [editing.answer];
    }
    if (!editing.isNew && editing.id) {
      // PATCH контентных полей
      run(() => eventContentApi.patch(game, editing.id!, body),
          "Обновлено");
    } else {
      run(() => eventContentApi.upsert(game, body), "Создано");
    }
    setEditing(null);
  };

  const doExport = async () => {
    setErr(""); setMsg("");
    try {
      const data = await eventContentApi.export(game);
      const blob = new Blob([JSON.stringify(data, null, 1)],
        { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${game}_catalog.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("Экспортировано");
    } catch (e) { setErr(String(e)); }
  };

  const doImport = async (dryRun: boolean) => {
    const f = fileRef.current?.files?.[0];
    if (!f) { setErr("Выберите JSON-файл"); return; }
    setErr(""); setMsg(""); setImportReport(null);
    try {
      const text = await f.text();
      const parsed = JSON.parse(text);
      const entries = Array.isArray(parsed) ? parsed
        : (parsed.entries ?? parsed.items);
      if (!Array.isArray(entries)) {
        setErr("Файл должен содержать массив entries");
        return;
      }
      const version = parsed.content_version
        ?? parsed.version ?? "import";
      const r = await eventContentApi.importJson(game, {
        entries, version: String(version), replace: true,
        dry_run: dryRun,
      });
      setImportReport(r);
      if (dryRun) {
        setMsg(r.valid ? "Валидация пройдена" : "Ошибки валидации");
      } else {
        setMsg("Импорт выполнен");
        load(); loadGames();
      }
    } catch (e) { setErr(String(e)); }
  };

  return (
    <>
      <div className="topbar"><h2 style={{ margin: 0 }}>Event Content — каталоги заданий</h2></div>
      {err && <div className="error-box">{err}</div>}
      {msg && <div className="notice">{msg}</div>}

      <div className="card">
        <div className="row" style={{ gap: 16, flexWrap: "wrap" }}>
          {GAMES.map(g => {
            const s = games.find(x => x.game === g.id);
            return (
              <button key={g.id}
                className={`btn ${game === g.id ? "" : "secondary"}`}
                onClick={() => setGame(g.id)}>
                {g.label}
                {s && <span className="muted"> — {s.enabled}/{s.total} активно</span>}
              </button>
            );
          })}
        </div>
        {stat && (
          <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
            версия: {stat.content_version || "—"} ({stat.source || "—"}) ·
            bundled: {stat.bundled_entries} записей v{stat.bundled_version} ·
            {" "}{Object.entries(stat.by_difficulty).map(([d, c]) =>
              `${d}: ${c.enabled}/${c.total}`).join(" · ")}
          </div>
        )}
      </div>

      <div className="card">
        <div className="row" style={{ gap: 8, flexWrap: "wrap",
          alignItems: "center" }}>
          <input placeholder="Поиск…" value={q}
            onChange={e => setQ(e.target.value)}
            onKeyDown={e => e.key === "Enter" && setQApplied(q)}
            style={{ width: 200 }} />
          <button className="btn secondary" style={{ fontSize: 12 }}
            onClick={() => setQApplied(q)}>Найти</button>
          <select value={difficulty}
            onChange={e => setDifficulty(e.target.value)}>
            <option value="">все сложности</option>
            {DIFFS.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
          <select value={category}
            onChange={e => setCategory(e.target.value)}>
            <option value="">все категории</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <label className="muted"><input type="checkbox"
            checked={showDisabled}
            onChange={e => setShowDisabled(e.target.checked)} />
            показать отключённые</label>
          <span className="muted">{total} записей</span>
          <div style={{ flex: 1 }} />
          <button className="btn secondary" style={{ fontSize: 12 }}
            onClick={doExport}>Экспорт JSON</button>
          <button className="btn" style={{ fontSize: 12 }}
            onClick={() => setEditing({ isNew: true,
              enabled: true, difficulty: "easy", category: "" })}>
            + Запись</button>
        </div>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <input ref={fileRef} type="file" accept=".json"
            style={{ fontSize: 12 }} />
          <button className="btn secondary" style={{ fontSize: 12 }}
            onClick={() => doImport(true)}>Проверить импорт</button>
          <button className="btn secondary" style={{ fontSize: 12 }}
            onClick={() => { if (confirm("Заменить каталог импортом?")) doImport(false); }}>
            Импортировать</button>
        </div>
        {importReport && (
          <pre className="muted" style={{ fontSize: 11, maxHeight: 160,
            overflow: "auto", whiteSpace: "pre-wrap" }}>
            {JSON.stringify(importReport, null, 1).slice(0, 4000)}
          </pre>
        )}
      </div>

      <div className="card">
        <table>
          <thead><tr>
            <th>ID</th><th>{game === "word" ? "Слово" : "Ответ"}</th>
            <th>{game === "word" ? "—" : "Подсказка"}</th>
            <th>Категория</th><th>Сложность</th><th>Статус</th><th></th>
          </tr></thead>
          <tbody>
            {items.map(it => (
              <tr key={it.id}>
                <td className="mono" style={{ fontSize: 11 }}>{it.id}</td>
                <td>{game === "word" ? it.word : it.answer}
                  {game === "krokodil" && it.accepted && it.accepted.length > 1 &&
                    <div className="muted" style={{ fontSize: 11 }}>
                      алиасы: {it.accepted.slice(1).join(", ")}</div>}</td>
                <td className="muted" style={{ fontSize: 11,
                  maxWidth: 320 }}>
                  {game === "krokodil" ? it.clue?.slice(0, 90) : ""}</td>
                <td>{it.category}</td>
                <td>{it.difficulty}</td>
                <td>{it.enabled
                  ? <span className="badge ok">on</span>
                  : <span className="badge muted">off</span>}</td>
                <td className="row">
                  <button className="btn secondary" style={{ fontSize: 11,
                    padding: "3px 8px" }}
                    onClick={() => setEditing({ ...it, isNew: false })}>
                    Изм.</button>
                  <button className="btn secondary" style={{ fontSize: 11,
                    padding: "3px 8px" }}
                    onClick={() => run(() => eventContentApi.toggle(game, it.id),
                      it.enabled ? "Отключено" : "Включено")}>
                    {it.enabled ? "Выкл" : "Вкл"}</button>
                  <button className="btn danger" style={{ fontSize: 11,
                    padding: "3px 8px" }}
                    onClick={() => confirm(`Удалить ${it.id}?`) &&
                      run(() => eventContentApi.remove(game, it.id),
                        "Удалено")}>
                    ✕</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn secondary" style={{ fontSize: 12 }}
            disabled={offset <= 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            ← Назад</button>
          <span className="muted">{offset + 1}–{Math.min(offset + PAGE, total)} / {total}</span>
          <button className="btn secondary" style={{ fontSize: 12 }}
            disabled={offset + PAGE >= total}
            onClick={() => setOffset(offset + PAGE)}>
            Вперёд →</button>
        </div>
      </div>

      {editing && (
        <div className="card">
          <h3>{editing.isNew ? "Новая запись" : `Запись: ${editing.id}`}</h3>
          <div className="grid">
            {game === "word" ? (
              <label>Слово<br /><input style={{ width: "100%" }}
                value={editing.word ?? ""}
                onChange={e => setEditing({ ...editing,
                  word: e.target.value })} /></label>
            ) : (
              <>
                <label>Ответ<br /><input style={{ width: "100%" }}
                  value={editing.answer ?? ""}
                  onChange={e => setEditing({ ...editing,
                    answer: e.target.value })} /></label>
                <label>Подсказка (без ответа!)<br />
                  <input style={{ width: "100%" }} value={editing.clue ?? ""}
                    onChange={e => setEditing({ ...editing,
                      clue: e.target.value })} /></label>
                <label>Алиасы (через запятую)<br />
                  <input style={{ width: "100%" }}
                    value={(editing.accepted ?? []).join(", ")}
                    onChange={e => setEditing({ ...editing,
                      accepted: e.target.value.split(",")
                        .map(s => s.trim()).filter(Boolean) })} /></label>
              </>
            )}
            <label>Категория<br /><input style={{ width: "100%" }}
              value={editing.category ?? ""}
              onChange={e => setEditing({ ...editing,
                category: e.target.value })} /></label>
            <label>Сложность<br /><select value={editing.difficulty}
              onChange={e => setEditing({ ...editing,
                difficulty: e.target.value })}>
              {DIFFS.map(d => <option key={d} value={d}>{d}</option>)}
            </select></label>
            <label className="muted"><input type="checkbox"
              checked={!!editing.enabled}
              onChange={e => setEditing({ ...editing,
                enabled: e.target.checked })} /> включена</label>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn" onClick={save}>Сохранить</button>
            <button className="btn secondary"
              onClick={() => setEditing(null)}>Отмена</button>
          </div>
        </div>
      )}
    </>
  );
}
