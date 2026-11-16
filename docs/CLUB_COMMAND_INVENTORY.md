# Статическая инвентаризация команд

База: `2f142c1`; рабочие изменения включены. Источник регистрации — `bot.py`. Это декларации в загружаемых расширениях, а не подтверждение синхронизации Discord. Закомментированные команды и альтернативный пакет `bot/cogs` исключены.

| Расширение | Команда / декоратор | Обработчик | Строка |
|---|---|---|---|
| `cogs.tournament` | `tournament_group / create` | `tournament_create` | 246 |
| `cogs.tournament` | `tournament_group / delete` | `tournament_delete` | 428 |
| `cogs.tournament` | `tournament_group / deleteall` | `tournament_deleteall` | 484 |
| `cogs.tournament` | `tournament_group / log` | `tournament_log` | 538 |
| `cogs.tournament` | `tournament_group / test` | `tournament_test` | 578 |
| `cogs.tournament` | `app_commands / top` | `top` | 812 |
| `cogs.tournament` | `app_commands / help` | `help_command` | 829 |
| `cogs.tournament` | `economy_group / balance` | `balance` | 840 |
| `cogs.tournament` | `server_group / check2fa` | `check2fa` | 866 |
| `cogs.tournament` | `app_commands / shop` | `shop` | 935 |
| `cogs.tournament` | `app_commands / inventory` | `inventory` | 993 |
| `cogs.tournament` | `games_group / list` | `games` | 1143 |
| `cogs.tournament` | `player_group / profile` | `profile` | 1426 |
| `cogs.tournament` | `player_set_group / bio` | `player_set_bio` | 1442 |
| `cogs.tournament` | `player_set_group / birthday` | `player_set_birthday` | 1467 |
| `cogs.tournament` | `player_group / history` | `history` | 1550 |
| `cogs.tournament` | `app_commands / rep` | `rep` | 1582 |
| `cogs.tournament` | `app_commands / marry` | `marry` | 1664 |
| `cogs.tournament` | `player_group / divorce` | `divorce` | 1703 |
| `cogs.tournament` | `app_commands / birthdays` | `birthdays` | 1729 |
| `cogs.tournament` | `app_commands / браки` | `marriages_cmd` | 1749 |
| `cogs.tournament` | `app_commands / birthdaytest` | `birthdaytest` | 1774 |
| `cogs.tournament` | `app_commands / welcometest` | `welcometest` | 1785 |
| `cogs.tournament` | `player_group / rank` | `rank` | 1803 |
| `cogs.tournament` | `economy_group / bet` | `betting_stats` | 1868 |
| `cogs.tournament` | `app_commands / booyah` | `booyah` | 2053 |
| `cogs.tournament` | `commands / elo` | `set_elo` | 2086 |
| `cogs.tournament` | `admin_group / edit` | `edit_player` | 2108 |
| `cogs.tournament` | `admin_group / fix_userid` | `fix_userid` | 2181 |
| `cogs.tournament` | `admin_group / fix` | `fix_bot_stats` | 2203 |
| `cogs.tournament` | `economy_group / pay` | `pay` | 2446 |
| `cogs.tournament` | `economy_group / gift` | `gift` | 2534 |
| `cogs.tournament` | `admin_group / debug_emoji` | `debug_emoji` | 2691 |
| `cogs.tournament` | `app_commands / whoami` | `whoami` | 2741 |
| `cogs.tournament` | `app_commands / daily` | `daily` | 2799 |
| `cogs.tournament` | `server_group / role` | `role` | 2887 |
| `cogs.tournament` | `server_group / welcome` | `welcome` | 2913 |
| `cogs.tournament` | `admin_group / reset` | `reset` | 2951 |
| `cogs.tournament` | `admin_group / resetprogress` | `resetprogress` | 2982 |
| `cogs.games.coin_flip` | `games_group / coin_flip` | `coin_flip` | 509 |
| `cogs.games.dice_roll` | `games_group / dice_roll` | `dice_roll` | 261 |
| `cogs.games.guess_number` | `games_group / guess_number` | `guess_number` | 331 |
| `cogs.games.high_low` | `games_group / high_low` | `high_low` | 315 |
| `cogs.games.roulette` | `games_group / roulette` | `roulette` | 443 |
| `cogs.games.stats` | `games_group / stats` | `game_stats` | 25 |
| `cogs.rob` | `crime_group / rob` | `rob` | 601 |
| `cogs.rob` | `crime_group / robgroup` | `robgroup` | 673 |
| `cogs.bank` | `economy_group / bank` | `bank` | 58 |
| `cogs.math_quiz` | `games_group / mathquiz` | `math_quiz` | 253 |
| `cogs.sell` | `economy_group / sell` | `sell_item` | 62 |
| `cogs.thread` | `admin_group / delete_thread` | `thread_delete` | 30 |
| `cogs.coin_drop` | `admin_group / forcedrop` | `force_drop` | 264 |
| `cogs.pet` | `pet / profile` | `pet_profile` | 116 |
| `cogs.pet` | `pet / release` | `release` | 121 |
| `cogs.pet` | `pet / coowner` | `coowner` | 156 |
| `cogs.pet` | `pet / coowner_remove` | `coowner_remove` | 224 |
| `cogs.pet` | `pet / catalog` | `catalog` | 294 |
| `cogs.pet` | `pet / top` | `top` | 308 |
| `cogs.isolate` | `admin_group / isolate` | `isolate` | 38 |
| `cogs.server_audit` | `admin_group / server_audit` | `server_audit` | 38 |
| `cogs.br` | `br_group / create` | `br_create` | 55 |
| `cogs.br` | `br_group / delete` | `br_delete` | 199 |
| `cogs.br` | `br_group / test_fill` | `br_test` | 259 |
| `cogs.br` | `br_group / test_teams` | `br_test_team` | 376 |
| `cogs.br` | `br_group / test_stats` | `br_stats_test` | 448 |
| `cogs.mm` | `mm_group / squad` | `mm_squad` | 165 |
| `cogs.mm` | `mm_group / freechannel` | `mm_freechannel` | 175 |
| `cogs.mm` | `mm_group / delete` | `mm_delete` | 260 |
| `cogs.mm` | `mm_group / test` | `mm_test` | 288 |
| `cogs.ultimatum` | `ultimatum_group / создать` | `ult_create` | 210 |
| `cogs.ultimatum` | `ultimatum_group / удалить` | `ult_delete` | 259 |
| `cogs.ultimatum` | `ultimatum_group / сетка` | `ult_bracket` | 278 |
| `cogs.ultimatum` | `ultimatum_group / статус` | `ult_status` | 297 |
| `cogs.ultimatum` | `ultimatum_group / история` | `ult_history` | 339 |
| `cogs.ultimatum` | `ultimatum_group / исправить` | `ult_fix` | 370 |
| `cogs.ultimatum` | `ultimatum_group / test_fill` | `test_fill` | 391 |
| `cogs.ultimatum` | `ultimatum_group / test_sim` | `test_sim` | 471 |
| `cogs.elo` | `player_group / ulti` | `show_elo` | 35 |
| `cogs.elo` | `player_group / ulti_history` | `show_history` | 72 |
| `cogs.team` | `team / create` | `create` | 26 |
| `cogs.team` | `team / delete` | `delete` | 72 |
| `cogs.team` | `team / status` | `status` | 108 |
| `cogs.bot_settings` | `server_group / off` | `toggle` | 28 |
| `cogs.freefire` | `ff_group / info` | `ff_info` | 113 |
| `cogs.freefire` | `ff_group / stats` | `ff_stats` | 153 |
| `cogs.emoji` | `app_commands / getemojiid` | `getemojiid` | 23 |
| `cogs.reputation` | `reputation_group / profile` | `rep_profile` | 438 |
| `cogs.reputation` | `reputation_group / history` | `rep_history` | 483 |
| `cogs.reputation` | `reputation_group / stats` | `rep_stats` | 522 |
| `cogs.reputation` | `reputation_group / adjust` | `rep_adjust` | 558 |
| `cogs.reputation` | `tag_group / claim` | `tag_claim` | 619 |
| `cogs.reputation` | `app_commands / invites` | `invites` | 678 |
| `cogs.reputation` | `reputation_group / invites_debug` | `invites_debug` | 707 |
| `cogs.info` | `info_group / publish` | `publish` | 155 |
| `cogs.info` | `info_group / update` | `update` | 193 |
| `cogs.info` | `info_group / status` | `status` | 232 |
| `cogs.info` | `info_group / validate` | `validate` | 271 |
| `cogs.club_hub` | `app_commands / hub` | `hub` | 31 |
| `cogs.club_hub` | `server_group / hub_publish` | `publish` | 41 |
| `cogs.club_hub` | `server_group / room_cards_secure` | `secure_rooms` | 79 |
| `cogs.vac` | `app_commands / vac` | `vac_cmd` | 229 |
| `cogs.vac` | `app_commands / unvac` | `unvac_cmd` | 328 |
| `cogs.vac` | `app_commands / vacs` | `vacs_cmd` | 372 |
| `cogs.banner_admin` | `banner_group / preview` | `banner_preview` | 41 |
| `cogs.banner_admin` | `banner_group / update` | `banner_update` | 70 |
| `cogs.banner_admin` | `banner_group / status` | `banner_status` | 90 |
| `cogs.banner_admin` | `banner_group / leaderboard` | `banner_leaderboard` | 121 |
| `cogs.banner_admin` | `banner_group / auto` | `banner_auto` | 153 |

Всего деклараций: 108. `/mm` удаляется из дерева перед синхронизацией при штатном `MATCHMAKING_ENABLED=false`.

## Загружаемые расширения

- `cogs.tournament`
- `cogs.games.coin_flip`
- `cogs.games.dice_roll`
- `cogs.games.guess_number`
- `cogs.games.high_low`
- `cogs.games.roulette`
- `cogs.games.stats`
- `cogs.rob`
- `cogs.bank`
- `cogs.math_quiz`
- `cogs.sell`
- `cogs.thread`
- `cogs.coin_drop`
- `cogs.pet`
- `cogs.isolate`
- `cogs.server_audit`
- `cogs.br`
- `cogs.mm`
- `cogs.ultimatum`
- `cogs.elo`
- `cogs.team`
- `cogs.bot_settings`
- `cogs.moderation`
- `cogs.freefire`
- `cogs.wardrobe`
- `cogs.emoji`
- `cogs.reputation`
- `cogs.info`
- `cogs.club_hub`
- `cogs.vac`
- `cogs.banner`
- `cogs.banner_admin`
