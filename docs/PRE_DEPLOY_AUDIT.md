# Squad Hub 2.0 — Pre-Deploy Audit

Release candidate: PR #54
`feat/classic-squad-hub-v2` → base `feat/classic-riz3-cv2` @ `feb8daa`
Rollback: `rollback/squad-hub-compat` @ `e34f1db82b59d0c286a974a13d056d1686b2f6fc`

Выполнять **до** деплоя, read-only. Production не изменять.
Существующие команды не сбрасывать.

Готовый read-only скрипт PG-аудита: `scripts/prod_audit.py`
(запуск внутри Railway: `railway ssh --service core` или
`railway run` при доступном DATABASE_URL). JSON-файлы DATA_DIR
проверяются отдельно через `railway ssh --service core`.

> Хранилища: PostgreSQL — `teams`, `cashcup_*`, `mm_queues`,
> `blackjack_*`, `spontaneous_events`, `ultimatum_*`, `ult2_*`,
> `player_stats`, `rep_balance`, `user_balance`, `user_bank`,
> `user_inventory`, `elo_*`. JSON-файлы в `DATA_DIR` (Volume) —
> classic-турниры (`tournaments/*.json` + `tournaments.manifest.json`),
> BR (`br_tournaments.json`), прочие `json_store`.

## 1. Активные турниры и игровые сессии

### PostgreSQL

```sql
-- Cash Cup: активные статусы 'registration'/'running'
SELECT id, guild_id, status, created_at
FROM cashcup_tournaments
WHERE status IN ('registration','running')
ORDER BY created_at DESC;
```

```sql
-- MM-очереди и активные слоты матчей
SELECT guild_id, channel_id, queue_type, state, match_num
FROM mm_queues;
SELECT * FROM match_channel_slots;
```

```sql
-- Ultimatum / Ultimatum 2 (активные фазы)
SELECT id, guild_id, status FROM ultimatum_tournaments
WHERE status NOT IN ('COMPLETED','CANCELLED');
SELECT id, guild_id, status FROM ult2_tournaments
WHERE status NOT IN ('COMPLETED','CANCELLED','FINISHED');
```

```sql
-- Spontaneous Events
SELECT event_id, guild_id, game_type, state, deadline_at
FROM spontaneous_events WHERE state = 'active';
SELECT * FROM event_channel_state;
```

```sql
-- Открытые blackjack-раунды
SELECT * FROM blackjack_rounds WHERE status = 'active';
```

### JSON / Volume (не SQL!)

```bash
# Classic-турниры: шарды DATA_DIR/tournaments/*.json
ls "$DATA_DIR/tournaments/" | head -50
cat "$DATA_DIR/tournaments.manifest.json" | python -m json.tool | head -40

# BR-турниры
python -c "import json;d=json.load(open('$DATA_DIR/br_tournaments.json'));print(list(d.keys())[:20])"
```

Ожидание: полный список активных сессий зафиксировать.
**При наличии активных игровых сессий, которые прервёт
рестарт — остановить выпуск и отчитаться владельцу.**
Команды, зарегистрированные в активных Cash Cup/BR,
защищены от изменения состава (`roster_locked` /
`check_failed` — fail-closed).

## 2. Команды и участники

```sql
-- Схема teams: team_id PK, team_name, password (legacy),
-- captain_id, guild_id, members BIGINT[],
-- + squad-колонки: join_mode, recruiting, purpose,
--   description, requirements JSONB, logo_url,
--   password_hash, updated_at
SELECT guild_id, COUNT(*) AS teams FROM teams
GROUP BY guild_id ORDER BY teams DESC;

SELECT COUNT(*) AS total_members,
       SUM(array_length(members,1)) AS total_slots
FROM teams;
```

Ожидание: baseline до миграции. После деплоя количество
команд и составы не должны измениться (миграция аддитивна).

## 3. Конфликты legacy-данных

Миграция (`ensure_squad_schema`) при старте пишет отчёт в
`DATA_DIR` и создаёт уникальный индекс имён **только при
отсутствии конфликтов**. Проверить до деплоя:

```sql
-- Дубликаты названий без учёта регистра
SELECT lower(team_name), COUNT(*) FROM teams
GROUP BY 1 HAVING COUNT(*) > 1;

-- Игрок более чем в одной команде одного guild
SELECT guild_id, uid, COUNT(*) FROM teams,
     unnest(members) AS uid
GROUP BY 1,2 HAVING COUNT(*) > 1;

-- Капитан отсутствует в составе
SELECT team_id, team_name, captain_id, members FROM teams
WHERE NOT (captain_id = ANY(members));

-- Составы > 4 игроков
SELECT team_id, team_name, array_length(members,1) AS n
FROM teams WHERE array_length(members,1) > 4;

-- Команды с паролем (станут join_mode='password',
-- recruiting=FALSE — публичный набор не откроется)
SELECT team_id, team_name, captain_id,
       (password <> '') AS has_legacy_pw,
       (password_hash <> '') AS has_hash
FROM teams WHERE password <> '' OR password_hash <> '';
```

При любом конфликте: миграция **не** создаст уникальный
индекс и запишет отчёт — команды не переименовываются и не
удаляются. Требуется ручное решение администратора.

## 4. Прогресс / экономика / статистика (сохранность)

```sql
-- ELO/SR/XP/REP: player_stats (elo, xp, level,
-- reputation, total_earnings, ...), elo_ratings,
-- elo_history, rep_balance, rep_balance_grants,
-- reputation_transactions
SELECT COUNT(*) FROM player_stats;
SELECT COUNT(*) FROM elo_ratings;
SELECT COUNT(*) FROM rep_balance;
SELECT COUNT(*) FROM reputation_transactions;

-- Балансы и инвентарь
SELECT COUNT(*) FROM user_balance;
SELECT COUNT(*) FROM user_bank;
SELECT COUNT(*) FROM user_inventory;
SELECT COUNT(*) FROM shop_items;
```

## 5. Резервные копии

- **PostgreSQL**: свежий `pg_dump` production БД перед деплоем
  (полный дамп, включая `teams`, `team_invites`,
  `team_applications`, `team_audit`, `team_lfg`,
  `squad_hub_state`, `cashcup_*`, `mm_*`, `blackjack_*`,
  `spontaneous_events`, `ultimatum_*`, `ult2_*`, `player_stats`,
  `rep_*`, `user_*`, `elo_*`).
- **Volume / DATA_DIR**: снапшот каталога данных бота —
  `tournaments/`, `br_tournaments.json`, JSON-store файлы,
  `backups/`.
- Проверить целостность: тестовый restore pg_dump на
  staging/in-memory PG, `pg_restore --list` для custom-формата.
- **Сохранить проверенную копию вне Railway Volume**
  (локальная машина / объектное хранилище).

## 6. Проверенный rollback SHA

`rollback/squad-hub-compat` @ `e34f1db82b59d0c286a974a13d056d1686b2f6fc`
(поверх `feat/classic-riz3-cv2` @ `feb8daa`):

- `password_hash` читается/пишется, PBKDF2-совместим с Squad Hub;
- plaintext не восстанавливается;
- `/team delete` проверяет результат `TeamStore.delete()` —
  при ошибке PG показывает ошибку, а не ложный успех;
- удаление команды чистит `team_invites`/`team_applications`
  в одной транзакции (FK), `to_regclass`-guard для БД без
  squad-таблиц; `team_audit`/`team_lfg` без FK — история
  сохраняется;
- squad-таблицы старый код игнорирует (аддитивны).

Действия отката: переключить деплой на SHA `e34f1db`, схему
БД **не** откатывать. См. `docs/SQUAD_HUB_ROLLBACK.md`.

## 7. Сохранность соседних подсистем

- **BR**: JSON-store; регистрация команды в BR под
  `team_registration_lock` (FOR UPDATE на teams) + VAC-проверка
  состава. Тест `test_register_br_team_vs_disband_concurrent`.
- **Cash Cup**: `register_br_team` фиксирует свежие
  name/captain/members под `cashcup_store.mutate` +
  `locked_team_on` в одной транзакции; активный турнир блокирует
  состав (`roster_locked`); повреждённый `state` → `check_failed`.
- **Blackjack**: изменений не было; PG-таблицы `blackjack_rounds`/
  `blackjack_ledger` не затрагиваются миграцией.
- **Spontaneous Events 2.0**: в базе `feb8daa`; ветка отребейжена
  поверх — конфликтов нет; полный suite зелёный.
- **Classic-турниры / MM / Ultimatum**: JSON/PG stores не
  изменялись Squad Hub; покрыты регрессией полного pytest.

## 8. Критерии go/no-go

GO только если:
- [ ] активные сессии всех типов перечислены; при активных
      играх — выпуск остановлен и владелец уведомлён;
- [ ] количество команд/участников зафиксировано (baseline);
- [ ] конфликтов legacy-данных нет ИЛИ есть утверждённый план;
- [ ] pg_dump + Volume-снапшот сняты, проверены, копия вне
      Railway Volume;
- [ ] rollback SHA `e34f1db` подтверждён;
- [ ] CI на финальном HEAD зелёный;
- [ ] получено отдельное разрешение владельца на merge/deploy.
