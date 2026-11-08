# AGENTS.md — r!z3 Tournament Bot

Discord-бот турниров и мини-игр для одного сервера. Python 3.12, discord.py 2.7+, PostgreSQL (asyncpg) + JSON-fallback хранилища, RapidOCR для скриншотов результатов, Free Fire API, Discord Components V2 UI, деплой на Railway.

## Структура

- `bot.py` — точка входа, `TournamentBot`, фоновые задачи (дни рождения, синк команд), `setup_hook` грузит все cog'и.
- `config.py` — все env-переменные и захардкоженные ID каналов/ролей сервера.
- `cogs/` — slash-команды и обработчики (`tournament`, `br`, `mm`, `ultimatum`, `freefire`, `statistic`, `ocr_upload`, `pet`, `elo`, `team`, `bank`, `rob`, `sell`, `coin_drop`, `moderation`, `bot_settings`, `wardrobe`, `emoji`, `isolate`, `thread`, `math_quiz`, `groups`).
- `views/` — UI: кнопки, модалки, Components V2 view (`br_view`, `ocr_match_view`, `room_modal`, `room_buttons`, `setup_view`, `final_view`, `matches_view`, `profile_view`, `birthdays_view`, `marry_view` и др.).
- `models/` — dataclass-модели (`tournament`, `br_tournament`, `team`, `player_stats`, `bet`, `match_history`, `pet`, `minigame` и др.).
- `storage/` — персистентность: `db.py` (asyncpg, все таблицы), `json_store.py` и JSON-сторы в `DATA_DIR` (`data/`), `*_store.py`.
- `services/` — внешние интеграции: `ocr_service` (RapidOCR ONNX), `cs_parser`/`cs_squad_parser` (разбор CS 4x4), `freefire_service` + `ff_catalog`, `image_processor`, `pet_ability_service`.
- `utils/` — `design/` (адаптер classic Embed/View → Components V2: `compose`, `text_card`, `smart_view`, `swap_view`), `embeds.py` (карточки турнира), `cosmetics`, `permissions`, `elo_config`, `rating_calculator`.
- `games/` — ~40 модулей мини-игр; `cogs/games/` — тонкие команды-обёртки.
- `ultimatum/` — отдельная система «ультиматума» (модели, store, elo, matchmaking).
- `dashboard/` — отдельное Next.js приложение (Vercel), к боту отношения не имеет, деплоится самостоятельно.
- `tests/` — пусто; `test_*.py` в корне — разовые ad-hoc скрипты, не pytest-набор.

## Документация (docs/)

- `docs/CODEX_HANDOFF.md` — главный документ передачи.
- `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/FEATURES.md`, `docs/DEVELOPMENT_STATUS.md`, `docs/DEPLOYMENT.md`.
- Старые корневые доки (`README.md`, `DEPLOY.md`, `ELO_SYSTEM.md`, `BETTING_SYSTEM.md`, `EMOJI_SETUP.md`, `REMAKE_SUMMARY.md`, `ANALYSIS.md`) — исторические, могут устареть; при конфликте верить коду.

## Установка и запуск

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env   # заполнить DISCORD_TOKEN и пр.
python bot.py
```

Проверки без запуска бота: `python -m compileall bot.py cogs views utils storage models services`.

## Жёсткие правила

1. Не удалять и не перестраивать данные PostgreSQL; миграции — только аддитивные (`ADD COLUMN IF NOT EXISTS`).
2. Никаких разрушительных миграций и `DROP` без явного разрешения.
3. Не дублировать начисление ELO/SR/монет/опыта — результат матча обрабатывается один раз (`processed_elo_matches`, `processed_match_ids`).
4. Критические операции (деньги, ELO, брак, rep) — через транзакции или идемпотентные проверки.
5. Не переписывать рабочие модули без необходимости; сохранять обратную совместимость команд.
6. Никаких секретов в коде и коммитах: `.env` в `.gitignore`, в репо только `.env.example` с заглушками.
7. Новые slash-команды: `@app_commands.describe`/группы из `cogs/groups.py`, привязка `binding` в `cog_load` (см. `bind_commands`), регистрация группы в `bot.py`/`setup_hook`.
8. UI — только через `utils/design` (`compose`/`text_card`/`smart_view`); обычный `discord.Embed` + `compose()` автоматически становится Components V2. Не смешивать `content=` с V2-view.
9. После существенных изменений: `compileall`, по возможности прогон затронутых команд на тестовом сервере.
10. Не менять production Railway/деплой без явного разрешения пользователя.

## Осторожно

- В `config.py` есть дефолтный `MOD_LOG_WEBHOOK_URL` с реальным webhook — считать секретом, не копировать в новые места.
- История git содержит реальный `DISCORD_TOKEN` в `.env.example` — токен нужно ротировать (см. `docs/CODEX_HANDOFF.md`).
- JSON-сторы (`DATA_DIR`) живут в Railway volume `/app/data`; без volume состояние теряется при редеплое.
