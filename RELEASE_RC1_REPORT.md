# r!z3 CLASSIC — Release Candidate 1 Report

**Дата:** 2026-11-26
**RC branch:** `release/rc1-stabilization`
**RC SHA:** `c7e4de24921ae8d281724daf7ef3dd2640482688`
**Base:** `origin/feat/classic-riz3-cv2` @ `13cc6aa`
**Diff vs base:** 52 файла, +8940/-345

## Статус

| Проверка | Результат |
|---|---|
| pytest (local, без PG) | **1573 passed, 6 skipped** |
| compileall (bot, cogs, services, storage, views, models, utils, ultimatum, control_center) | чисто |
| Skipped | 6 тестов `test_economy_bridge_pg.py` — требуют `TEST_DATABASE_URL` |
| Миграции БД | Только аддитивные `CREATE TABLE IF NOT EXISTS` + advisory lock; деструктивных DDL нет; все `DELETE FROM` — в тестовых фикстурах |
| Git-конфликты при сборке RC | 0 (9 веток, clean merge) |
| Discord E2E | **НЕ ВЫПОЛНЕН** — требует staging-окружения |
| pg_dump / backup | **НЕ ВЫПОЛНЕН** — требует доступа к Railway/PG |

## Включённые PR / ветки

| PR | Ветка | Содержание | Статус в RC |
|---|---|---|---|
| #33 | fix/journal-resilience | Журнал: threads API, recovery worker, 429 retry_after, идемпотентность | merged |
| #37 | audit/p0-final-winner | P0 confirm_final_winner — валидация до побочных эффектов | merged |
| #34 | fix/access-hardening | Access: fail-closed panel, pinned channel, retry_after | merged |
| #39 | fix/owner-only-admin-cmds | rep adjust/history/invites_debug + banner — только владелец (+ regression tests) | merged |
| #41 | fix/classic-tournament-create-10062 | **НОВЫЙ P0**: defer/ACK, идемпотентность, без дублей | merged |
| #42 | fix/classic-room-card-cleanup | Убрана строка «Правила» из карточки комнаты | merged |
| #32 | feat/cashcup-alert | /alert — roll-call пинги, санитизация, dedup; get_pool fail-fast | merged |
| #35 | feat/cashcup-isolated-test | Cash Cup: публичная сетка/карточки + изолированный тест-ког + banner DDL lock | merged |
| #40 | fix/classic-tournament-nickname-cleanup | Очистка ников в турнирах (BR/Ultimatum/карточки/лидерборд) | merged |

## Исключённые PR (с обоснованием)

| PR | Решение | Причина |
|---|---|---|
| #36 feat/cashcup-public | **закрыть после merge #35** | Все общие файлы байт-в-байт идентичны #35; #35 — надмножество (+тест-ког, GF-регрессии, banner lock) |
| #38 fix/banner-ddl-lock | **закрыть после merge #35** | Advisory lock в `storage/banner_store.py` идентичен куску в #35 |
| #20 feat/banner-remove-voice-cap | **закрыть без merge** | Лимит 6 ч/сутки уже снят в базе (`MAX_VOICE_SECONDS` отсутствует, `new = prev + elapsed`, `get_voice_day_map` без cap) |
| #18 feat/classic-club-reconstruction | **аудит, не мержить** | 62 файла, +3246, behind=57; уникальное: server_layout migration, club_hub, welcome rework — требует разделения на малые PR |
| #12 feat/classic-riz3-cv2 -> main | **не мержить** | Release-PR в main — решение владельца после GO |
| #4-#9, #11 -> main | **legacy, решение владельца** | #6/#9/#11 obsolete (format_vote/rules_vote/message_refresher отсутствуют в CLASSIC); #4/#5/#7/#8 — старый контур (OCR/PNG/MM), отдельное архитектурное решение |

## Ключевые технические решения

### /tournament create (PR #41)
- `defer(ephemeral=True, thinking=True)` сразу после синхронной валидации — до архивации, PG и публикации. Устраняет Discord 10062.
- Валидация `size`/`formation` перенесена до архивации — невалидный ввод не архивирует предыдущий турнир.
- «Турнир создан» через `followup` только после `channel.send` + `store.set`.
- `_CREATE_IN_FLIGHT` — защита от параллельного создания в одном канале.
- При сбое после публикации карточка удаляется; при ошибке до `store.set` состояние не подтверждается.
- Метрики длительности archive/create в логах.

### Cash Cup консолидация
- #35 содержит весь код #36 + тест-ког `cogs/cashcup_test` (таблицы `cashcup_test_*`, `CASHCUP_TEST_ENABLED` default off — только staging).
- Публичная сетка/карточки в PG (`cashcup_publication_store`), ping dedup через `pinged` флаг, backfill без пингов.
- Призовые — только ручные отметки, бот не выплачивает.

### Owner-only perms (PR #39 + tests)
- `is_bot_owner()` — строгая проверка `BOT_OWNER_ID`, fail-closed, server-side.
- `/reputation stats` остаётся публичной.
- `tests/test_owner_only_admin_cmds.py` — 19 тестов: owner/admin/user × 6 команд.

### Nickname cleanup (PR #40)
- `clean_tournament_nickname` — до добавления косметики; сохраняет кириллицу/казахские символы; fallback `Игрок-XXXX` по user_id; суффиксы при коллизиях через `_unique_player_name`; Discord-ник не редактируется; FF-ники/UID Cash Cup не затронуты.

## Control Center
- Уже в base: FastAPI `control_center/` + `dashboard/` (Next.js), отдельный Railway-сервис `SERVICE_ROLE=panel`.
- Auth: Discord OAuth2 -> HMAC session cookie (HttpOnly, SameSite=Lax, CSRF).
- Секретов в Git нет; env-валидация при старте. Настройки -> `admin_config_store` (PG).
- Не проверено: сборка frontend, реальный деплой.

## Env-переменные (НЕ добавлять в prod без разрешения)
- `CASHCUP_TEST_ENABLED` — только staging (hopeful-purpose), никогда production.
- `CASHCUP_BRACKET_CHANNEL_ID`, `CASHCUP_MATCH_CHANNEL_ID` — только с разрешения владельца.
- `ACCESS_ROTATION_CHANNEL_ID=1573700340417699890` — должен быть подтверждён в prod.

## Rollback-план
- Откат кода: revert к `13cc6aa` (или предыдущий production SHA) — безопасен: схема БД только добавляет таблицы, старый код их игнорирует.
- Данные: новые таблицы (`cashcup_publications`, `cashcup_test_*`, journal) можно оставить — старый код не читает их.
- Опасных миграций (DROP/ALTER/DELETE в проде) в RC нет.

## Release Gate (AGENT 13)

**Статус: CONDITIONAL GO для кода / NO-GO для production до закрытия блокеров.**

| Gate | Статус |
|---|---|
| P0-ошибки исправлены | YES: 10062 (#41), final_winner (#37), journal (#33), perms (#34/#39) |
| pytest | YES: 1573 passed; PG-тесты ждут TEST_DATABASE_URL |
| Discord E2E | **BLOCKED** — нет доступа к тестовому серверу/токену |
| Резервные копии | **BLOCKED** — нужен Railway/PG доступ |
| Миграции на копии БД | Плюс: аддитивны; не прогнаны на реальной PG |
| Rollback | Проверен: безопасный revert, схема обратно-совместима |

### Блокеры перед GO (owner actions)
1. Discord E2E на staging: регистрация, драфт 8/16/32, RANDOM/DRAFT/Balanced-SR, BR, Ultimatum, Cash Cup, /alert, магазин, REP, рестарт-recovery.
2. PG-backed pytest с TEST_DATABASE_URL.
3. pg_dump + бэкап persistent data + проверка restore.
4. Merge PR #41, #42, #39, #40, #32, #33, #34, #35, #37 -> feat/classic-riz3-cv2 (или деплой release/rc1-stabilization) — требуется разрешение.
5. Закрыть #36, #38, #20 после подтверждения.
6. Мониторинг после релиза: 10062/429, PG-отказы, дубли карточек.

**Deploy: production не трогал, переменные не менял, merge в cv2/main не выполнял.**
