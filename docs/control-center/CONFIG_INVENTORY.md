# r!z3 Control Center — инвентаризация конфигурации

Дата аудита: 2026-11-15
Базовый коммит: `63efeabe53d843843dbc0580a2348e2437bdd9d5`
(`feat/moderation-suite-cv2` = `feat/classic-riz3-cv2@7b74d97` + интеграция MODERATION)

Документ — источник истины для Этапа 2 (Configuration Engine). Не приступать
к массовому рефакторингу параметров, не сверившись с этим файлом.

---

## 1. Ландшафт хранения данных

Проект использует **три разных источника истины** — это ключевой риск
для Control Center:

| Хранилище | Что хранит | Доступ |
|---|---|---|
| **PostgreSQL** (`storage/db.py`, asyncpg pool) | `player_stats`, `user_balance`, `user_bank`, `bets`, `bet_settlements`, `betting_stats`, `match_odds`, `bonus_cooldowns`, `questions`, `question_history`, `cases`, `case_history`, `whoami_cooldowns`, `minigames`, `shop_items` (PG-таблица), `user_inventory` (PG), `moderation_*`, `vac_*`, `bot_settings`, `tournament_audit*`, rep-таблицы, economy bridge | Только async (`get_pool()`) |
| **JSON-файлы** (`DATA_DIR`, atomic write) | `tournaments.json` / shards (`json_store.py`), `settings.json`, `shop_items.json`, `player_inventory.json`, pet/wardrobe/marriage/nickname-операции и др. | Синхронно, in-memory кэш + atomic write |
| **Env / config.py** | Каналы, роли, REP-суммы, веса MM-балансировки, эмодзи, секреты | Только при старте процесса |
| **Redis** (опционально, `storage/redis_client.py`) | Кэши | — |

**Критично:** турнирное состояние живёт в JSON-файлах, а не в PostgreSQL.
Economy-часть балансов имеет dual-mode (JSON ↔ PG через `enable_db()`).
Панель НЕ должна писать в JSON-хранилища бота напрямую — для игрового
состояния нужен Admin Actions Engine, исполняемый самим ботом.

## 2. Источники конфигурации

### 2.1 `config.py` — env-константы (code-managed по умолчанию)

Все значения ниже читаются **один раз при импорте модуля**. Изменение
в панели требует либо runtime-слоя настроек, либо деплоя.

| Группа | Ключи | Примечание |
|---|---|---|
| Секреты/инфра | `DISCORD_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `BOT_OWNER_ID`, `EXTRA_ADMIN_USER_IDS`, `ALLOWED_GUILD_IDS`, `GUILD_ID` | **Никогда не в панели** |
| Discord-ресурсы | `ORG_ROLE_ID`, `MANAGER_ROLE_ID`, `ORG_1/2/3_ROLE_ID`, `ORG_ROLE_MAP`, `TOURNAMENT_CHANNELS`, `MATCH_FOUND_CHANNEL_ID`, `TOURNAMENT_AUDIT_CHANNEL_ID`, `MOD_LOG_WEBHOOK_URL`(секрет), `MOD_LOG_CHANNEL_ID`, `NOTIFICATION_ROLE_ID`, `NOTIFICATION_CHANNEL_ID`, `MM_MATCH_CHANNEL_POOL`, `BR_*`, `ULT_*`, `WELCOME_CHANNEL_ID`, `PET_HUNGER_CHANNEL_ID`, `ROOM_DATA_CHANNEL_ID`, `ROOM_OPEN_NOTIFY_CHANNEL_ID`, `GAMES_CHANNEL_ID`, `INFO_CHANNEL_ID`, `REP_LOG_CHANNEL_ID`, `LOG_CHANNEL_ID`(utils/logging) | Хорошие кандидаты на panel-editable, но константы читаются при импорте — нужна миграция на сервис настроек |
| Welcome/модерация | `WELCOME_BANNER_URL`, `ORGANIZER_THUMBNAIL_URL`, `MUTE_ROLE_ID`, `WARN_GIF_URL`, `UNWARN_GIF_URL`, `GIF_TRIGGER_USER_IDS` | — |
| Economy limits | `MAX_BALANCE=100000`, `MIN_BET=10`, `MAX_BET=1000` | Хардкод, не env |
| Лимиты турнира | `MAX_CAPTAINS=4`, `MAX_PLAYERS_PER_CIRCLE=4`, `CIRCLES=(2,3,4)` | Хардкод |
| Matchmaking | `MM_BALANCE_WEIGHTS{sum,top,dist,spread}`, `MM_BALANCE_SUM_TOLERANCE`, `MM_BALANCE_EPSILON` | env-переопределяемые |
| Ready Check | `RANKED_READY_TIMEOUT_SECONDS=90` | env |
| Reputation 2.0 | `REP_MANUAL_AMOUNT=1`, `REP_MANUAL_COOLDOWN_HOURS=24`, `REP_INVITE_AMOUNT=5`, `REP_INVITE_VERIFICATION_HOURS=72`, `REP_BOOST_AMOUNT=10`, `REP_BOOST_VERIFICATION_DAYS=7`, `REP_SERVER_TAG_AMOUNT=5`, `REP_ONE_*` флаги, `REP_LOG_CHANNEL_NAME`, `REP_INVITE_REFRESH_SWEEPS=5` | env; частично читается в `cogs/reputation.py` |
| Эмодзи | `RANK_EMOJIS`, `GAME_EMOJIS`, `ICON_EMOJIS`, `STANDARD_EMOJIS`, `DECORATIVE_EMOJI_KEYS` (~100 `EMOJI_*` env) | Огромный блок; кандидаты на панель частично |
| Прочее | `FREEFIRE_API_KEY`, `FREEFIRE_API_BASE`, `ENABLE_PERF_METRICS`, `JSON_STORE_MODE`, `PG_POOL_*`, `GUIDE_NAV_TEXT`, `VAC_MAX_DAYS=365` | Инфра/диагностика — code-managed |
| Economy bridge | `ECONOMY_BRIDGE_*` (token, port, host, rate limit, writes flag) | Отдельный HTTP-сервис, секреты |

### 2.2 `settings.json` (DATA_DIR, через `store.get_settings/set_settings`)

Уже существующий runtime-механизм, но **JSON-файл, без версионирования,
без аудита, без PG**. Известные ключи:

| Ключ | Тип | Где используется |
|---|---|---|
| `welcome_enabled` | bool | `cogs/bot_settings.py` (`/off`), welcome-ивент |
| `coin_drop_enabled` | bool | то же; `cogs/coin_drop.py` |

### 2.3 PostgreSQL-таблицы, уже являющиеся хранилищем настроек

| Таблица | Содержимое | Статус |
|---|---|---|
| `moderation_settings` | `moderator_role_id`, `head_role_id`, `log_channel_id`, `reports_channel_id` per guild | Создаётся `storage/moderation_store.py`, без JSON-fallback (fail-closed) |
| `bot_settings` (guild_id, key, value) | Каналы репутации и др. (`storage/reputation_store.py` ~L920) | Разрозненные ключи, нет реестра |
| `minigames` | Каталог мини-игр; **`sync_games_to_db()` перезаписывает поля из хардкода при каждом старте** (`bot.py` setup_hook) | Конфликт с панелью — требует миграции (см. §5) |
| `shop_items` (PG) + `shop_items.json` | **Двойное хранилище**: PG-таблица создаётся в `db.py`, но фактический магазин — `storage/shop_store.py` → JSON-файл | Риск рассинхрона; инвентарь `user_inventory` тоже dual |

### 2.4 Хардкод, не вынесенный в конфиг

| Константа | Файл | Примечание |
|---|---|---|
| `ELO_CONFIG` (skill_rating_scale, min/max_match_elo, kill/death_weight, performance_modifiers, rank_protection) | `utils/elo_config.py` | Единая точка ELO — идеально для панели; читается `ultimatum/elo*.py`, `utils/rating_calculator.py` |
| `ALL_GAMES` / `GameConfig` (min_bet, max_bet, multiplier, status, описания) | `storage/games_config.py` | ~6 игр в меню + 40+ файлов `games/` и `cogs/games/` — реальные правила игр в коде |
| `VAC_LADDER` (6 ступеней: 3мин→6ч), `VIOLATION_DECAY=24ч`, `MAX_VAC_LEVEL=6` | `storage/vac_store.py` | Кандидат на panel-editable |
| `MATCH_START_RETRY_COOLDOWN=120.0` | `cogs/mm.py:67` | MM-таймаут |
| Циклы `betting_timer_loop`, `draft_timer_loop`, `pet_hunger_check_loop`, `birthday_loop` | `bot.py` | Интервалы внутри циклов — требуется аудит каждого |

## 3. Реестр параметров по модулям

Формат: `параметр | источник | runtime? | scope | риск/примечание`.
`runtime?`: `env` = требует редеплоя, `json` = меняется через файл,
`pg` = меняется в БД, `code` = только код, `panel` = цель Control Center.

### 3.1 Турниры (`cogs/tournament.py` ~3578 строк, `models/tournament.py`, `storage/json_store.py`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| Активные турниры, команды, матчи, стадии | `tournaments.json`/shards | json | Состояние, не настройка; панель читает через сервис, пишет через Admin Actions |
| `MAX_CAPTAINS=4`, `MAX_PLAYERS_PER_CIRCLE=4`, `CIRCLES` | config.py хардкод | code | Влияет на валидность регистрации — применять к новым турнирам |
| Каналы/роли турниров | env `TOURNAMENT_CHANNELS`, `ORG_ROLE_MAP`, `MATCH_FOUND_CHANNEL_ID`, `TOURNAMENT_AUDIT_CHANNEL_ID` | env | Кандидаты panel-editable; используются при создании/публикации |
| Таймеры стадий, ready timeout | частично env (`RANKED_READY_TIMEOUT_SECONDS`), частично в моделях | смешанно | Требует точечного аудита `models/tournament.py` |
| Форматы: CLASSIC, BR (`cogs/br.py`), Ultimatum (`cogs/ultimatum.py`), CASH CUP (`feat/cashcup-*` ветки, не в этой базе) | отдельные cogs | — | BR/Ultimatum — отдельные подформаты со своими сторами (`br_store`, `ultimatum/store.py` — PG) |

### 3.2 Matchmaking (`cogs/mm.py` ~1656 строк, `storage/mm_search_store.py`, `utils/smart_balance.py`, `utils/balanced_sr.py`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| Веса балансировки | `MM_BALANCE_WEIGHTS` env | env→panel | Применять только к новым матчам; снимок весов в матче желателен |
| `MM_BALANCE_SUM_TOLERANCE=1.0`, `MM_BALANCE_EPSILON=0.001` | env | env→panel | То же |
| `MM_MATCH_CHANNEL_POOL` | env, дефолт 2 канала | env→panel | Занятые слоты — состояние, не конфиг |
| `MATCH_START_RETRY_COOLDOWN=120s` | хардкод mm.py | code→panel | — |
| Очереди MM | `mm_search_store` (PG) + `Tournament.mm_search_channel_id` | pg/json | Состояние очередей — Admin Actions для stop/retry |
| Голосования за формат | `format_vote_store`, `rules_vote_store` | pg | Состояние голосований |

### 3.3 ELO / Skill Rating (`utils/elo_config.py`, `utils/rating_calculator.py`, `cogs/elo.py`, `ultimatum/elo*.py`, `player_stats.elo`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| Вся таблица `ELO_CONFIG` | хардкод dict | code→panel | **Целевой первый модуль**: все ключи безопасно runtime-editable; читать через сервис с кэшем |
| Формула вероятности победы | `skill_rating_scale` в той же таблице | code→panel | Win Rate в формуле отсутствует — сохранить инвариант |
| Границы изменения ELO | `min_match_elo=20`, `max_match_elo=30` | code→panel | — |
| `rank_protection` | enabled/top_limit/bonus | code→panel | — |
| Завершённые матчи | `match_history_store`, `player_stats` | pg | Новый коэффициент не должен пересчитывать историю — применять только к новым расчётам |

### 3.4 Экономика (`storage/user_balance_store.py`, `user_bank_store.py`, `storage/economy.py`, `cogs/bank.py`, `services/economy_bridge.py`, `bonus_cooldowns`, daily rewards в `player_stats`/`bonus_cooldowns`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| `MAX_BALANCE=100000`, `MIN_BET=10`, `MAX_BET=1000` | config.py хардкод | code→panel | Глобальные лимиты ставок/баланса |
| Начисления за матчи/турниры, daily rewards, XP/level (`xp_to_next_level`) | разбросано по cogs/services + `player_stats` | смешанно | Требует инвентаризации сумм в `cogs/tournament.py`, `cogs/mm.py`, `services/*` |
| Транзакции | `user_balance_store`, `bet_settlements` (идемпотентность по `settlement_key`) | pg | **Все денежные операции панели — только через эти сервисы**, никогда прямой UPDATE |
| Economy bridge | отдельный HTTP-сервис, `ECONOMY_BRIDGE_*` env | env | Уже сервисная архитектура — образец для Admin Actions |
| `daily_tournament_coins`, `daily_coins_date` | колонки player_stats | pg | Состояние, не конфиг |

### 3.5 Магазин (`storage/shop_items.py`, `shop_store.py`, `models/shop_item.py`, PG `shop_items`/`user_inventory`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| Каталог товаров | `initialize_shop_items()` → `shop_store.clear_all()` **при каждом импорте и setup_hook** | code (деструктивно!) | **Обязательный фикс**: заменить wipe на идемпотентную миграцию (upsert по `id`, не удалять panel-созданные товары). Покупки в `user_inventory` сохраняются |
| Поля товара | `ShopItem`: name, description, price, cosmetic_type, rarity, value, category, item_type, is_sellable | json→panel | Категории: `icons`, `tags`, `services`, roles/доступы |
| PG-таблица `shop_items` | создаётся, но не является источником | — | Устранить дублирование при миграции магазина в PG |

### 3.6 Игроки (`player_stats_store.py`, `rep_store.py`, `reputation_store.py`, `match_history_store.py`, `pet_store.py`, `wardrobe_store.py`, `marriage_store.py`, `nickname_ops_store.py`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| ELO/SR/K/D/XP/level/balance/REP/inventory | PG `player_stats`, `user_balance`, `user_inventory` | pg | Карточка игрока — read-only агрегат + безопасные правки через сервисы |
| Репутация 2.0 | `REP_*` env + `reputation_store` (PG, `bot_settings` для каналов) | env→panel | Amounts/cooldowns/verifications — кандидаты |
| `rep_log_channel_id` и пр. | `bot_settings` PG + env fallback | pg | Уже частично runtime |

### 3.7 Модерация (`cogs/moderation.py`, `cogs/mod.py`, `cogs/vac.py`, `storage/moderation_store.py`, `vac_store.py`, `vac_service.py`, `moderation_service.py`)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| `moderation_settings` (роли, каналы логов/репортов) | PG per-guild | pg | **Уже runtime-редактируемо** — выставить в панель |
| VAC-лестница, decay, `VAC_MAX_DAYS` | `VAC_LADDER` хардкод + env | code/env→panel | — |
| `GIF_TRIGGER_USER_IDS`, `MUTE_ROLE_ID`, GIF-URL | env | env→panel | — |
| VAC-баны, cases, appeals, reports | PG-таблицы | pg | Состояние — Admin Actions (ban/unban/resolve) |
| Права | `is_org_check` включает организаторов — **для панели нужна отдельная RBAC**, не переиспользовать напрямую | — | §7 ТЗ |

### 3.8 OCR / результаты матчей

OCR-обработка скриншотов результатов — в `cogs/tournament.py`/`services/` (worker, `perf/ocr-speed` ветки). Пороги уверенности, форматы, retry-политики — **требуется точечный аудит** (модули `ocr`/`screenshot` в cogs и services; в этой базе часть логики в `test_screenshot_rule_embed.py`-окружении). До аудита — code-managed.

### 3.9 Мини-игры (`storage/games_config.py`, `storage/minigame_store.py`, `storage/minigame_init.py`, `games/*.py` ~40 файлов, `cogs/games/*`, `cogs/math_quiz.py` и др.)

| Параметр | Сейчас | runtime? | Примечание |
|---|---|---|---|
| Каталог `ALL_GAMES` (6 записей: guess_number, coin_flip, dice_roll, math_quiz, roulette, high_low) | хардкод → `sync_games_to_db` **перезаписывает PG при старте** | code | Тот же дефект, что у магазина: sync должен быть upsert без затирания panel-правок, либо каталог полностью в PG |
| min_bet/max_bet/multiplier/is_active per game | GameConfig + PG `minigames` | смешанно | — |
| Правила 40+ игр (шансы, выплаты, кулдауны) | `games/*.py` хардкод | code | Code-managed, в панели — только вкл/выкл и лимиты где безопасно |

### 3.10 Discord / server settings

См. §2.1 — все ID каналов/ролей. Рекомендация: перевести чтение на
`settings_service.get(guild_id, "discord.channel.*")` с env-fallback
на переходный период; при старте резолвить и кэшировать.

### 3.11 Feature flags

| Флаг | Сейчас | runtime? |
|---|---|---|
| `welcome_enabled`, `coin_drop_enabled` | `settings.json` через `/off` | json → мигрировать в PG-движок |
| Турниры/MM/магазин/OCR/игры on-off | отсутствуют как единый механизм | **новое** — `feature.*` в реестре |

## 4. Фоновые задачи и таймеры (bot.py)

| Задача | Что делает | Конфиг-зависимость |
|---|---|---|
| `betting_timer_loop` | таймеры ставок на матчи | интервалы в коде |
| `draft_timer_loop` | авто-рандом пиков драфта | интервалы в коде |
| `pet_hunger_check_loop` | голод питомцев (`PET_HUNGER_CHANNEL_ID`) | env-канал |
| `birthday_loop` | поздравления 00:00 Astana | `player_stats.birthday` |
| `audit_svc.flush_pending` / `republish_unpublished` | журнал аудита турниров → `TOURNAMENT_AUDIT_CHANNEL_ID` | env-канал |
| `_refresh_match_cards` (on_ready) | CV2-карточки матчей | состояние |
| `economy_bridge` HTTP-сервис | внешний доступ к economy API | env, секретный token |

## 5. Выявленные дефекты, блокирующие требования ТЗ

1. **`initialize_shop_items()` → `shop_store.clear_all()`** при импорте
   модуля и в `setup_hook` — затирает любые изменения каталога при каждом
   запуске. Покупки (`user_inventory`) при этом не удаляются, но
   panel-созданные товары исчезнут. → Идемпотентный upsert.
2. **`sync_games_to_db()`** перезаписывает поля `minigames` из хардкода
   при каждом `setup_hook`. → Тот же подход: upsert только полей,
   которыми владеет код, или полный переезд каталога в PG.
3. **Двойное хранилище магазина**: PG `shop_items`/`user_inventory` vs
   JSON `shop_items.json`/`player_inventory.json` (`_use_db` флаг).
   → Выбрать PG как источник, JSON как fallback/миграция.
4. **`settings.json`** — нет версионирования/аудита/PG. → Мигрировать
   ключи в `admin_config_*` движок, оставить чтение через сервис.
5. **Env-константы читаются при импорте** — даже после появления
   движка нужен рефакторинг точек чтения (config.py → services).
6. **`is_admin()`/`is_org_check()`** включают организаторов — для панели
   нужна отдельная RBAC (§7 ТЗ), не переиспользовать напрямую.
7. **Турниры в JSON**, не в PG — панель читает снимки через бот-сервис;
   мутации только через Admin Actions Engine.

## 6. Стратегия миграции (предложение)

- **Этап 2**: `admin_config_*` таблицы + `SettingsService` (PG, реестр,
  валидация, версии, LISTEN/NOTIFY, fallback-кэш). Реестр стартует с
  безопасного первого набора: `feature.*`, `elo.*`, `matchmaking.*`,
  `reputation.*`, `economy.limits.*`, `vac.*`, `moderation` уже-PG
  ключи — как ссылки, не дубли.
- **Этап 5**: точечная замена чтения env/хардкода на
  `await settings.get(...)` только в местах, где значение читается
  на каждую операцию (не кэшируется в объектах). Для снимков
  (текущий матч) — фиксировать применённые значения при создании.
- **Секреты и Discord resource IDs каналов первого уровня** —
  code-managed до отдельного решения (проверка существования ресурса
  требует Discord API — реализовать в backend панели).

## 7. Статус блокирующих дефектов (post-Stage 5)

1. **Shop catalog wipe** — ИСПРАВЛЕНО: `shop_catalog.sync_to_store`
   (PG авторитетен; JSON→PG миграция однократно; defaults только при
   пустом PG), `is_active` у ShopItem, CRUD API + hide/show.
2. **Games sync overwrite** — ИСПРАВЛЕНО: `minigame_init` и
   `games_config.sync` — insert-only; `games_api` PUT обновляет PG;
   бот читает `minigames` из PG (`minigame_store._use_db`).
3. **Dual JSON/PG shop storage** — ИСПРАВЛЕНО: PG — единственный
   источник каталога; JSON-store больше не перезаписывает каталог.
   Инвентарь/покупки — в PG (`user_inventory`), не затронуты.
4. **Runtime settings в legacy JSON** — ИСПРАВЛЕНО: все runtime-
   настройки в `admin_config_values` (PG); `settings.json` остаётся
   fallback только при отсутствии DATABASE_URL.
5. **Hardcoded module values** — ЧАСТИЧНО: rating.* (классический ELO),
   matchmaking balance weights/tolerance/epsilon, reputation.* (все
   суммы/сроки), vac.max_days/decay_hours читаются через
   `settings_service.peek`. Остались code-managed: ladder VAC,
   EloConfig (ultimatum, не используется в classic-формуле),
   пороги OCR (модуль отсутствует в ветке), Discord resource IDs.
6. **is_admin/is_org для панели** — ИСПРАВЛЕНО: отдельная panel-RBAC
   (owner/admin/moderator/organizer/viewer + `economy.finance`),
   Discord-проверки не переиспользованы.
7. **Турниры в JSON** — ОБОСНОВАНО: активные турниры остаются в
   JSON-store (атомарность матчей); панель управляет только
   runtime-параметрами (лимиты/составы через `tournament.*`),
   состояние читает через bot-service heartbeats. Мутации состояния —
   только Discord-команды (осознанное ограничение v1).
