# DATABASE.md — PostgreSQL и JSON-хранилища

## Подключение

- `DATABASE_URL` (env) → `asyncpg.create_pool` в `storage/db.py` (`get_pool`, `init_db`, `close_pool`).
- Если `DATABASE_URL` пуст — часть сторов работает в JSON-режиме (`TeamStore`, `br_store` и др. имеют fallback).
- `init_db()` вызывается при старте бота и **аддитивно** создаёт/дополняет таблицы (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).

## Таблицы (по `storage/db.py`)

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `player_stats` | Профиль/рейтинг игрока | PK `(guild_id, user_id)`; `elo`, `xp`, `level`, `coins`-поля (`total_earnings`, `daily_tournament_coins`, `daily_coins_date`), `kills/deaths/assists/damage`, `win_streak`, `reputation`, `birthday`, `spouse_id`, `description`, `avatar_url`, `finals`, `tournament_participations` |
| `user_balance` | Баланс монет | `(guild_id, user_id)` |
| `user_bank` | Банк/сейф | `(guild_id, user_id)` |
| `questions` / `question_history` | Банк вопросов мини-игр | текст, категория, история |
| `minigames` | Конфиги мини-игр | `command_name` и др. |
| `minigame_sessions` / `minigame_stats` | Активные сессии и статистика игр | — |
| `cases` / `case_history` | Кейсы и история открытий | — |
| `whoami_cooldowns` | Кулдауны Who-is | — |
| `bets` | Ставки игроков | PK `(guild_id, user_id, tournament_id, match_id)`; `team_index`, `match_type`, `odds` |
| `match_odds` | Коэффициенты матчей (динамические) | `team_a_name`, `team_b_name`, odds |
| `betting_stats` | Статистика ставок | — |
| `bonus_cooldowns` | Кулдауны бонусов/стрики | `streak` |
| `shop_items` / `user_inventory` | Магазин косметики и инвентарь | — |
| `teams` | Команды BR/тимов | `team_id`, `team_name`, `password`, `captain_id`, `members` (JSON list), `guild_id` |
| `match_history` | История матчей игрока | результат, K/D, ELO-изменение |
| `pets` / `pet_invitations` | Питомцы и приглашения | `level` CHECK 1..100, `hunger_started_at` |
| `db_migrations` | Учёт применённых миграций | — |

Полный DDL — в `storage/db.py::init_db()` (≈700 строк, единственный источник истины).

## JSON-хранилища (`DATA_DIR`, на Railway — volume `/app/data`)

`storage/json_store.py` + отдельные `*_store.py`:

- `tournaments.json` — активные/завершённые турниры (`Tournament` сериализуется целиком, включая `ocr_sessions`, `temp_match_stats`, `processed_elo_matches`, `processed_match_ids`).
- `teams.json` — команды (`TeamStore`, дублируется в PG `teams`).
- BR-турниры, `rep_given.json` (rep-история), `profile_cooldowns.json`, ставки-легаси, mm-поиск, питомцы/гардероб/кейсы — по мере использования `*_store.py`.

**Важно:** JSON-сторы не транзакционны и при отсутствии volume теряются при редеплое.

## Правила безопасных изменений

1. Только аддитивные миграции (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`).
2. Новые поля `player_stats` добавлять и в `db.py`, и в дефолты `models/player_stats.py` + сериализацию store'а.
3. Не менять PK существующих таблиц; `bets` уже мигрирован на составной PK — не откатывать.
4. Финансовые/ELO-операции — в транзакциях (`async with conn.transaction()`) или идемпотентно.
5. Перед миграцией на проде — бэкап/проверка на тестовой БД; production Railway не трогать без разрешения.
