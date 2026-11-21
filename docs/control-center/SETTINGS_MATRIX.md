# Settings Matrix — key → consumer → runtime effect → test

Состояние после FINAL COMPLETION. `consumers_pending=True` в реестре
означает: значение хранится в PG и возвращается API, но живой consumer
в этой ветке отсутствует — UI показывает бейдж «consumer pending».

## Реально применяемые без рестарта

| Key | Consumer | Runtime effect | Test |
|---|---|---|---|
| `feature.tournaments_enabled` | `cogs/tournament.py::tournament_create` | блокирует создание турнира | guard pattern |
| `feature.matchmaking_enabled` | `cogs/mm.py::_create_search` | блокирует вход в очередь MM (история сохраняется) | guard pattern |
| `feature.minigames_enabled` | `cogs/games/coin_flip.py` | блокирует запуск игры | guard pattern |
| `feature.welcome_enabled` | `services/welcome_card.py` | welcome-сообщения вкл/выкл | engine tests |
| `feature.coin_drop_enabled` | `cogs/coin_drop.py` | ивенты монет вкл/выкл | engine tests |
| `rating.base_win_change` / `base_loss_change` | `utils/rating_calculator.py::get_base_elo_change` | ±ELO за матч (новые расчёты) | test_rating_base_change_* |
| `rating.kill_weight` / `death_weight` | `rating_calculator.calculate_kd_bonus` | веса K/D-бонуса | test_rating_kd_bonus_weights |
| `matchmaking.balance_weight_{sum,top,dist,spread}` | `utils/smart_balance` | веса балансировки 4v4 | test_smart_balance_weights |
| `matchmaking.balance_sum_tolerance` / `balance_epsilon` | `smart_balance` | пороги выбора состава | test_smart_balance_tolerance |
| `reputation.manual_amount` / `manual_cooldown_hours` | `services/reputation.py` | ручная выдача REP | test_reputation_amount_dynamic |
| `reputation.invite_amount` / `invite_verification_hours` | `services/reputation.py` | награды за инвайты | same |
| `reputation.boost_amount` / `boost_verification_days` | `services/reputation.py` | бусты | same |
| `reputation.server_tag_amount` | `services/reputation.py` | тег сервера | same |
| `vac.max_days` | `services/vac_service.parse_duration` | лимит срока VAC | test_vac_max_days_parse |
| `vac.decay_hours` | `storage/vac_store._decay_seconds` | затухание уровня VAC | test_vac_decay_seconds |
| `economy.max_balance` | `services/economy_bridge._max_balance` | кламп выигрышей/выплат | fallback test |

## Хранятся, consumer pending (честный статус в UI)

- `elo.*` (11 ключей) — `utils/elo_config.py::ELO_CONFIG` не имеет
  живых consumer'ов в этой ветке (ultimatum-калькулятор его не импортирует;
  CLASSIC-формула живёт в `rating_calculator`).
- `tournament.max_captains` / `max_players_per_circle` / `circles` —
  константы используются только legacy-пакетом `bot/` (не активный cog).
- `economy.min_bet` / `max_bet` — реальные лимиты ставок per-game
  хранятся в таблице `minigames` (редактируются через Games API).
- `matchmaking.ready_timeout_seconds` / `match_start_retry_cooldown` —
  `RANKED_READY_TIMEOUT_SECONDS` объявлен в config.py, но не читается
  `cogs/mm.py` (нет consumer в ветке).
- `elo.performance_*`, `elo.rank_protection_*` — см. выше.

## Shop / Games каталоги

- `shop_items` (PG) — авторитетен; `ShopItem.is_active` скрывает без
  удаления; покупки/инвентарь в PG не затрагиваются; стартовая
  инициализация insert-only (миграция JSON→PG однократно).
- `minigames` (PG) — `is_active`, ставки, множитель, PvP/PvE правятся
  через `/api/games`; `minigame_init`/`games_config` не затирают PG.

## Admin Actions (bot executor)

| Action | Handler | Эффект |
|---|---|---|
| `settings.notify` | `refresh_all()` | инвалидация кэша настроек |
| `shop.resync_catalog` | `sync_to_store(shop_store)` | перечитать каталог |
| `games.resync` | `minigame_store.get_available_games()` | прогрев/проверка PG |
| `service.ping` | ping | heartbeat-диагностика |

## Не управляется панелью (code-managed, осознанно)

- Discord resource IDs (каналы/роли), секреты, токены.
- Состояние активных турниров/очередей (JSON-store, Discord-команды).
- OCR-модуль — отсутствует в ветке.
- VAC ladder durations (кортеж в `vac_store`), `bot/` legacy config.
