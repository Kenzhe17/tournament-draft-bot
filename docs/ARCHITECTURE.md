# ARCHITECTURE.md — r!z3 Tournament Bot

## Общая схема

```
Discord Gateway
      │
   bot.py (TournamentBot, discord.py commands.Bot)
      │  setup_hook(): load_extension всех cogs + sync команд
      ├── cogs/*        slash-команды, callbacks кнопок/модалок
      ├── views/*       UI-компоненты (Button/Modal/View/LayoutView)
      ├── models/*      dataclass-сущности (Tournament, BRTournament, Team, PlayerStats…)
      ├── storage/*     db.py (PostgreSQL/asyncpg) + JSON-сторы (DATA_DIR)
      ├── services/*    OCR (RapidOCR), парсеры CS 4x4, Free Fire API, питомцы
      └── utils/*       design/ (CV2-адаптер), embeds, permissions, cosmetics
```

Фоновые задачи в `bot.py`: `birthday_loop` (поздравления в 00:00 Asia/Almaty), синхронизация slash-команд, восстановление persistent views (`custom_id` без timeout — views создаются с `timeout=None` и перепривязываются при старте).

## Ключевые потоки

### Турнир (8/16/32 игрока → команды по 4)

1. `/tournament create` → `views/setup_view.py` SetupView (круги, орг-меню, выход) → embed `build_setup_embed`.
2. Регистрация → драфт (`FormationMode`: RANDOM / ELO / SKILL) → `models/tournament.py` формирует `teams[]` (`captain`, `players`, `circle1..4`, `team_names`).
3. Сетка: 8→1 матч, 16→полуфиналы, 32→отбор+полуфиналы+финал. Embed'ы: `utils/embeds.py` (`build_qualifiers_embed`, `build_semifinals_embed`, `build_final_embed`), матчи/комнаты — `views/matches_view.py`, `views/final_view.py`.
4. Комната: `RoomButton` → `views/room_modal.py` `RoomModal` → одна V2-карточка «КОМНАТА ОТКРЫТА» в канал `1561683314480255057` + анонс с `num_X <@…>` строками в `1200125075156910181`.
5. Результат: `MatchScreenshotButton` → `views/ocr_match_view.py` → `cogs/ocr_upload.py` (`on_message` ловит аттачмент) → RapidOCR → `services/cs_parser.py`/`cs_squad_parser.py` → публичное сообщение в `OCR_STATS_CHANNEL_ID=1568608461250367569` с кнопками сопоставления → `kd_input_view.process_match_result` начисляет ELO/монеты/статистику один раз (`processed_elo_matches`). Параллельно `_upsert_result_message` постит карточку «ИТОГ МАТЧА» в `RESULT_CHANNEL_ID=1568290145138900992`.

### Battle Royale (`/br`)

`cogs/br.py` создаёт `BRTournament` (`models/br_tournament.py`, JSON-стор `br_store`). Регистрация (random ≤48 или команды через `team_store`), до 12 команд по 4. Фазы: Бермуды → Чистилище → Калахари (до `map_count` карт). Статистика мест/убийств вводится организатором по группам (`BRTeamGroupView` → модалки). Комната и данные — `BRRoomModal`/`BRDataButton` в `views/br_view.py`: карточка «КОМНАТА ОТКРЫТА» со всеми командами в `1563195265395982466` + анонс в общий канал. Очки: место {12,9,8,7,6,5,4,3,2,1,0,0} + убийства.

### Ultimatum (`/ultimatum`)

Отдельный пакет `ultimatum/` (свои models/store/elo/matchmaking) + `cogs/ultimatum.py`, `views/ultimatum_*.py`.

### Прочие системы

- **Matchmaking** `/mm` — `cogs/mm.py`, `storage/mm_search_store.py`, `views/mm_search_view.py`.
- **Free Fire** `/ff info|stats|ban|banner|wishlist` — `cogs/freefire.py`, `services/freefire_service.py` (siambhau FreeFireApi, `FREEFIRE_API_KEY`, регион CIS/RU), каталог `services/ff_catalog.py`.
- **Статистика сервера** `/statistic` — `cogs/statistic.py`, OCR через `services/ocr_service.py` (RapidOCR, executor=2 потока).
- **Питомцы/магазин/гардероб** — `cogs/pet.py`, `cogs/wardrobe.py`, `models/pets_catalog.py`, `storage/pet_store.py`, `shop_store.py`, `services/pet_ability_service.py`, `pet_effects.py`.
- **Экономика** — `storage/user_balance_store.py`, `user_bank_store.py`, `economy.py`, `cogs/bank.py`, `rob.py`, `sell.py`, `coin_drop.py`; кражи/магазин-сейф/комиссии.
- **Мини-игры** — `games/` (~40 модулей: слоты, рулетка, покер, судоку, wordle,millionaire…) + `cogs/games/` + `storage/minigame_store.py`, `question_store.py`, `case_store.py` (кейсы), `storage/games_config.py`.
- **Профиль/соц** — `views/profile_view.py` (настройки: ДР/ник/описание, кулдаун 7 дней), `storage/profile_cooldowns.py`, `rep_store.py` (`/rep`, 3/день, 1 раз на пару), брак в `cogs/tournament.py` (`/player marry|divorce`, `views/marry_view.py`), `birthdays_view.py` + `birthday_loop` в `bot.py`, `history_view.py`, `leaderboard_view.py`.
- **Модерация** — `cogs/moderation.py` (warn/mute, `MOD_LOG_WEBHOOK_URL`).

## UI-слой (Components V2)

`utils/design/` — адаптер: любой `discord.Embed` + `discord.ui.View` через `compose()`/`smart_view()`/`swap_view()` превращается в `LayoutView` с `Container`/`TextDisplay`/`Section`/`ActionRow`. `meta.py` инжектит аватар игрока (thumbnail), футер `powered by r!z3`, чистит title от `| /cmd`. Правила: ActionRow — внутри Container карточки; V2-сообщения не принимают `content=` (только `compose(..., text=...)`); изображения embed → `MediaGallery`/`Section` accessory.

## Хранилища

- **PostgreSQL** (`DATABASE_URL`, asyncpg pool в `storage/db.py`): таблицы `player_stats`, `user_balance`, `user_bank`, `questions`, `question_history`, `minigames`, `minigame_sessions`, `minigame_stats`, `cases`, `case_history`, `whoami_cooldowns`, `bets`, `match_odds`, `betting_stats`, `bonus_cooldowns`, `shop_items`, `user_inventory`, `teams`, `match_history`, `pets`, `pet_invitations`, `db_migrations`. Миграции — `ALTER TABLE … IF NOT EXISTS` в `init_db()`.
- **JSON** (`DATA_DIR`, default `./data`, на Railway volume `/app/data`): `tournaments.json`, `teams.json`, BR-стор, `rep_given.json`, `profile_cooldowns.json`, ставки, матчмейкинг, питомцы и пр. (`storage/json_store.py` и `*_store.py`).

## Конфигурация и «магические» ID

`config.py` читает env (`DISCORD_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `BOT_OWNER_ID`, `FREEFIRE_API_KEY`, роли `ORG_*`, `MANAGER_ROLE_ID`, `MUTE_ROLE_ID`, `NOTIFICATION_*`, `TOURNAMENT_CHANNELS`…) и содержит захардкоженные ID каналов сервера: данные комнат `1561683314480255057` (турнир) / `1563195265395982466` (BR), OCR-статистика `1568608461250367569`, итоги матчей `1568290145138900992`, общий канал `1200125075156910181`. При переносе на другой сервер их надо параметризовать.

## dashboard/

Next.js-приложение (Vercel) с собственным `DATABASE_URL`/OAuth env — отдельный деплой, не часть бота.
