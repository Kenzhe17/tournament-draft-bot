# r!z3 — Phase 2: P2 (JsonStore) дизайн, P8, мониторинг

## P2 — JsonStore: вынос сериализации из event loop (ДИЗАЙН, не внедрено)

### Проблема

`store.set(tournament)` синхронно выполняет `to_dict()` +
`json.dumps` всего состояния + atomic write (tmp+fsync+replace)
прямо в event loop. Замерено локально: `json.dumps` состояния
~1MB — 50–150 мс блокировки на каждый вызов; call-site'ов ~179.

### Требования

- Не терять подтверждённые изменения (crash-safety).
- Старый снимок не должен перезаписать новый.
- Сохранить atomic write + `.bak` backup.
- `Tournament`-объекты нельзя безопасно читать из другого потока,
  пока event loop их мутирует → снимок должен делаться В LOOP.

### Предлагаемый механизм

**Снимок в loop + запись в executor:**

1. `store.set(t)` (async-контекст): под существующим локом
   делает `snapshot = json.dumps(to_dict_all(), ...)` — сериализация
   остаётся в loop, но это CPU-bound ~50–150мс. Чтобы убрать и её:
   `to_dict()` всех турниров выполняется в loop быстро (это
   Python-объекты, мутации только в loop — безопасно), а
   `json.dumps` + fsync уходит в `asyncio.to_thread`.
   Однако `to_dict()` сам может быть тяжёлым — мерить отдельно.

2. Очередь записи: один фоновый `_writer_task` с
   `asyncio.Queue`. `set()` кладёт `(seq, payload_bytes)` и
   возвращается. Writer пишет строго по порядку seq — старый
   снимок не перезапишет новый, т.к. payload уже материализован.

3. **Flush-точки (durability)**: перед критическими операциями
   (`finalize`, `cancel`, shutdown бота, запись `message_id` после
   send) — `await store.flush()` ждёт пока очередь опустеет.
   Точек немного и они редки → задержка записи ~0 для UI,
   гарантированная персистентность для критических данных.

4. Crash-семантика: между `set()` и реальной записью есть окно
   (). Данные, изменённые после последнего flush и до crash,
   теряются — как и при любом буферизованном IO. Митигация:
   debounce-окно малое (writer пишет сразу, без sleep — очередь
   и есть буфер), плюс периодический flush раз в N секунд
   (например 5с) ограничивает потерю.

5. Альтернатива проще: `await asyncio.to_thread(_serialize_and_write)`
   на каждый `set()` — убирает блокировку loop, но сохраняет
   синхронную семантику (caller ждёт запись). Минус: fsync в
   executor всё равно занимает поток, при burst-вызовах очередь
   в default executor (лимит ~32 потока) может забиться.
   Рекомендуется вариант с writer-task + flush-точки.

### Риски

- **Высокий**: durability-окно. Нужен аудит всех мест, где
  JSON — единственный источник истины до PG-записи (finalize
  пишет и в JSON и в PG — порядок важен).
- Тесты: crash-consistency (kill между set и flush), порядок
  записей, shutdown flush.
- `store.get_by_guild` и читатели продолжают работать с
  in-memory состоянием — поведение не меняется.

### План внедрения (после согласования)

1. Метрики: логировать `store.set` duration и размер payload.
2. Реализовать writer-task + seq + flush-точки в
   finalize/cancel/shutdown.
3. Тесты crash-consistency на pgserver/JSON.
4. A/B: событие loop-lag до/после на staging.

## P8 — Reputation sweeper: результат

`guild.invites()` + `guild.vanity_invite()` вызывались на каждую
гильдию каждые 60с. Анализ показал: `invite.uses`/`vanity.uses`
меняются только при входе участника, а `record_member_join` сам
обновляет кэш после обработки входа (+ события invite create/delete).
Периодический рефреш — страховка от неудачного обновления кэша.

**Реализовано**: рефреш раз в 5 sweep'ов (`_INVITE_REFRESH_EVERY=5`,
т.е. каждые 5 минут вместо каждой минуты) — сокращение Discord API
вызовов в 5 раз. `process_due_invites`/`process_due_boosts` идут
каждый sweep — задержка выдачи REP не изменилась. Худший случай
при протухшем кэше — `ambiguous` атрибуция → reject, не неверное
начисление.

## Минимальный мониторинг для Railway (предложение)

Без новых зависимостей — через `logging` + опциональный
`/healthz`-подобный лог-канал. Метрики с низкой кардинальностью:

| Метрика | Источник | Интервал |
|---|---|---|
| Event-loop lag | фоновая задача: `loop.time()` дельта | 10с |
| RSS/память | `resource.getrusage` / `psutil` (если есть) | 60с |
| Длительность `store.set` | замер в json_store | на вызов >50мс → warn |
| Длительность `build_embed_for_phase` | замер в bot.py | >200мс → warn |
| Discord edits/мин | счётчик в update_tournament_message | агрегат в лог 60с |
| HTTP 429 | счётчик в discord.http лог-обёртке | на событие |
| Длительность рендера PNG | уже логируется (P4/P6) | на вызов |
| Длительность SQL | asyncpg `command_timeout` + лог медленных (>500мс) | на запрос |
| Активные задачи | `asyncio.all_tasks()` count | 60с |
| OCR `finalizing` sessions | существующий `ocr_recovery` | на sweep |

Реализация: один модуль `utils/perf_metrics.py` с счётчиками и
один `tasks.loop` репортер (60с) — одна строка лога, без Discord
спама. Критические ошибки (PoolAcquireTimeout, repeated 429,
loop lag >1с) — один warn на эпизод с cooldown, не на каждое
событие.
