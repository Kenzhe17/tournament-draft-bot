# r!z3 — Phase 3: мониторинг, fingerprint-замеры, JsonStore — анализ

## 1. Внедрённый мониторинг (`utils/perf_metrics.py`)

- Включён по умолчанию, отключается `ENABLE_PERF_METRICS=false|0|no`.
- Два таска: lag-сэмплер (1с, deque maxlen=300 ≈ 5 мин) и reporter
  (60с → одна JSON-строка `PERF {...}` в лог).
- Метрики: cpu_pct, rss_mb (psutil, fallback resource),
  loop_lag avg/max/p95(≥20 сэмплов)/lag>100ms/lag>500ms,
  asyncio_tasks, gauges (ocr_active по имени корутин,
  ranked_matches из store, pg_pool size/idle/max),
  counters за интервал (сбрасываются): discord_api_calls,
  discord_edits, discord_fetches, discord_429, discord_http_errors,
  avatar_cache_hits/misses, ocr_runs, json_saves, pg_stats_queries;
  timings (скользящее окно ≤1000 сэмплов, avg/max/p95≥20):
  embed_build_ms, json_to_dict_ms, json_dumps_ms,
  json_atomic_write_ms, json_payload_bytes, pg_stats_query_ms,
  ocr_boxes_ms, render_match_found_ms, render_match_result_ms.
- Discord 429/HTTP-ошибки — через обёртку `bot.http.request`
  (только наблюдение, rate-limit discord.py не трогаем).
- Нагрузка: ~1 wakeup/сек + один json.dumps отчёта/мин — пренебрежимо.
- Все публичные функции fail-safe; ошибки gauges → null в отчёте.
- Завершение: `perf_metrics.stop()` в `TournamentBot.close()`.

## 2. Замеры fingerprint (P1.2)

Турнир 32 игрока, 8 match_channels, заполненные picks/teams
(синтетика, локально):

| Турниров | avg/тик | p95 | max | на турнир |
|---|---|---|---|---|
| 1 | 0.08 ms | 0.11 | 0.28 | 0.076 ms |
| 5 | 0.37 ms | 0.55 | 1.00 | 0.074 ms |
| 20 | 1.43 ms | 2.05 | 2.63 | 0.071 ms |

Вывод: fingerprint ~0.07 мс/турнир — на 2–3 порядка дешевле
предотвращаемого rebuild (build_embed + get_many ≈ 5–50 мс +
Discord edit). **Version counter НЕ нужен** — стоимость доказанно
незначительна, а риск пропущенной инвалидации у version counter
(все пути мутации, включая вложенные объекты) выше выгоды.

## 3. JsonStore — сравнение вариантов (P2, не внедрено)

Текущий `store.set()` — **синхронный**: `to_dict()` всех турниров +
`json.dumps` (~50–150 мс на ~1 МБ) + fsync — блокирует event loop.
~179 sync call-site'ов — перевод на async API = массовый рефактор.

| | A. Снимок в loop + запись в thread | B. Writer-task + seq + flush | C. Файлы по турнирам | D. Критические состояния → PG |
|---|---|---|---|---|
| Блокировка loop | to_dict остаётся (~10–30мс), dumps+fsync уходит | почти 0 (to_dict в loop) | ~1–5мс (только изменённый турнир) | 0 для JSON-путей |
| Call-site'ы | без изменений | +flush-точки на критических путях | без изменений (API тот же) | частичная миграция |
| Crash consistency | как сейчас (set ждёт запись) | окно до flush — нужен аудит | per-file атомарность, окно то же | PG-транзакции |
| Lost updates | нет | seq защищает порядок | файл на турнир — нет cross-talk | нет |
| Конкурентность | запись сериализуется очередью | writer один — порядок гарантирован | _save_lock per file | PG row locks |
| Откат | env-флаг | env-флаг | env-флаг single-file mode | сложно |
| Сложность | средняя | средняя+аудит flush | средняя (миграция load) | высокая |
| Railway Volume | tmp+rename работает | то же | много мелких файлов — ок | не зависит |

### Рекомендация: **C (shard-файлы)**, с A как fallback-опцией

Почему C:
- `set(t)` пишет только `to_dict()` ОДНОГО турнира (~КБ) →
  стоимость падает с O(все турниры) до O(1 турнир): ~50–150мс →
  ~1–5мс блокировки. Единственный вариант, убирающий и dumps,
  и fsync большого файла без смены sync API.
- Crash consistency сохраняется: atomic write per file,
  повреждение одного файла не трогает остальные; `.bak` на файл.
- `load()` на старте читает все shard-файлы + legacy single-file
  (миграция при первом save). `_tournaments` уже в памяти —
  `all()/get_by_guild` не меняются.
- Откат: `JSON_STORE_MODE=single` возвращает старый формат.
- Риск: место на volume (N файлов вместо 1) — незначительно;
  `rotate_backup` per file — больше inode-операций, но реже
  по размеру.

Почему не B: окно потери данных между set() и flush сложнее
аудировать на 179 call-site'ах; sync API не даёт await flush —
пришлось бы делать fire-and-forget с довернем кperiodic flush,
что слабее текущей гарантии «set() вернулся = данные на диске».

Почему не D: дублирование состояния JSON↔PG — рассинхрон риски;
оставить как долгосрочное направление для match/OCR-сессий.

### Гарантии при C (чек-лист перед внедрением)

- finalize/cancel: `store.set` → per-file atomic — durable сразу,
  flush-точки НЕ нужны (в отличие от B!). Это ключевое преимущество:
  синхронная семантика сохраняется.
- `.corrupt-*` per shard; unhealthy только для затронутого турнира?
  Нет — сохранить глобальный unhealthy при битом индексе/legacy.
- Backup: `*.bak` рядом с каждым shard.
- Тесты: миграция legacy→shards, corrupt shard → restore из .bak,
  конкурентные set/delete, shutdown.

## 4. Pets — корректность и стоимость

Схема `pets` НЕ содержит guild_id (db.py:699) — фильтрация по
owner_1_id/owner_2_id неизбежна.

| | Старый (G запросов) | Новый (1 scan + Python) |
|---|---|---|
| SQL | G × `owner=ANY(member_ids)` — index probe на КАЖДЫЙ id члена | 1 × `SELECT *` seq scan |
| Стоимость | O(G × members_per_guild) index lookups | O(P) чтение + O(G × P) dict-check |
| Худший случай | 50 гильдий × 500 членов = 25k index probes/5мин | 50 × 10k питомцев = 500k проверок — всё ещё <10мс CPU |
| RAM | только строки своих питомцев | вся таблица pets в памяти (~200B/строка; 100k питомцев ≈ 20МБ — нереалистично) |
| Побочный эффект |, удалённый в guild1, не обработается в guild2 (fresh query) | stale row → `_row_to_pet` + `check_fullness_and_energy` перечитывает по id → no-op. Безопасно. |

Вывод: новый вариант строго дешевле при реалистичных объёмах
(pets — штучный контент, таблица мала). При >100k питомцев
вернуться к `WHERE owner_1_id = ANY($1) OR owner_2_id = ANY($1)`
с глобальным union всех member ids (1 запрос вместо G).

## 5. Reputation — корректность после 60→300с

Механика: `invite.uses`/`vanity.uses` меняются ТОЛЬКО при join.
`record_member_join` сам делает `guild.invites()` после входа и
обновляет `_rep_invite_cache` (reputation.py:236). Sweep-рефреш —
страховка от неудачного post-join refresh + invite create/delete
события тоже обновляют кэш.

Сценарии:
- **Несколько входов подряд**: каждый join → fresh `after` fetch +
  cache update → атрибуция корректна независимо от sweep.
- **Одновременные входы**: `_join_locks[guild_id]` сериализует.
- **Invite удалён**: `on_invite_delete` → refresh кэша.
- **Ошибка Discord API на post-join refresh**: кэш устаревает до
  следующего успешного refresh (≤300с). Следующий join: `before`
  не содержит прошлый инкремент → diff показывает +1 на двух
  инвайтах → `attribute_invite` → "ambiguous" → **reject**.
  Неверного начисления нет — failure mode = потерянная награда,
  не чужая.
- **Рестарт**: кэш пуст до load-refresh; join до первого sweep →
  `before={}` → все инвайты «increased» → ambiguous → reject.
  То же поведение было и при 60с (load-refresh покрывает).
- **Vanity**: проверяется только при attribution=="unknown";
  протухший vanity baseline даёт `after>before` → "vanity" —
  корректно (vanity uses реально выросли).

Риск 300с vs 60с: окно stale-cache ambiguity ×5, но частота
post-join refresh failure мала, а joins сами обновляют кэш.
Добавлен env-override `REP_INVITE_REFRESH_SWEEPS` (default 5)
для отката без редеплоя кода. **Рекомендация: оставить 300с**;
если на staging увидим рост rejected ambiguous — вернуть 1–2.
