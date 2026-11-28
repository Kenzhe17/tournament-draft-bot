# Squad Hub 2.0 — Pre-Deploy Audit

Release candidate: PR #54
`feat/classic-squad-hub-v2` @ `db194e1` → base `feat/classic-riz3-cv2` @ `a4c7c2b`
Rollback: `rollback/squad-hub-compat` @ `1865919fb696b5f397e0204a33e5098c73390d07`

Выполнять **до** деплоя, read-only. Production не изменять.
Существующие команды не сбрасывать.

## 1. Активные турниры

```sql
-- Активные/незавершённые турниры всех типов
SELECT id, guild_id, status, created_at
FROM cashcup_tournaments
WHERE status NOT IN ('finished','cancelled','archived')
ORDER BY created_at DESC;
```

```sql
-- BR-турниры в активных фазах (если хранятся отдельно —
-- проверить таблицы br_* по фактической схеме)
SELECT * FROM br_tournaments WHERE status = 'active';
```

Ожидание: список зафиксировать. Команды, зарегистрированные в
активных турнирах, защищены от изменения состава
(`roster_locked` / `check_failed` fail-closed).

## 2. Количество существующих команд

```sql
SELECT guild_id, COUNT(*) AS teams,
       COUNT(*) FILTER (WHERE captain_id IS NOT NULL) AS with_captain
FROM teams GROUP BY guild_id ORDER BY teams DESC;
```

```sql
-- Распределение по размеру состава и join-режиму
SELECT array_length(members,1) AS size, COUNT(*)
FROM teams GROUP BY 1 ORDER BY 1;
```

Ожидание: зафиксировать baseline. После деплоя количество
команд не должно измениться (миграция аддитивна, команды не
создаются/не удаляются автоматически).

## 3. Конфликты legacy-данных

Миграция (`ensure_squad_schema`) при старте пишет отчёт в
`DATA_DIR` (team-migration report JSON) и создаёт уникальный
индекс имён **только при отсутствии конфликтов**.

Проверить до деплоя:

```sql
-- Дубликаты названий без учёта регистра
SELECT lower(name), COUNT(*) FROM teams
GROUP BY 1 HAVING COUNT(*) > 1;
```

```sql
-- Игрок более чем в одной команде одного guild
SELECT guild_id, unnest(members) AS uid, COUNT(*)
FROM teams GROUP BY 1,2 HAVING COUNT(*) > 1;
```

```sql
-- Капитан отсутствует в составе
SELECT team_id, name, captain_id, members FROM teams
WHERE NOT (captain_id = ANY(members));
```

```sql
-- Составы > 4 игроков
SELECT team_id, name, array_length(members,1) AS n
FROM teams WHERE array_length(members,1) > 4;
```

```sql
-- Команды с паролем (станут join_mode='password',
-- recruiting=FALSE — публичный набор не откроется)
SELECT team_id, name, captain_id FROM teams WHERE password <> '';
```

При любом конфликте: миграция **не** создаст уникальный индекс
и запишет отчёт — команды не переименовываются и не удаляются.
Требуется ручное решение администратора.

## 4. Резервные копии

- **PostgreSQL**: свежий `pg_dump` production БД перед деплоем
  (включая `teams`, `team_invites`, `team_applications`,
  `team_audit`, `team_lfg`, `squad_hub_state`,
  `cashcup_tournaments`, `br_*`, blackjack-таблицы).
- **Volume / DATA_DIR**: снапшот каталога данных бота
  (JSON-файлы, аватары, `backups/` от `disband_many`).
- Проверить, что бэкап **восстановим** (тестовый restore на
  staging или хотя бы проверка целостности дампа).

## 5. Проверенный rollback SHA

`rollback/squad-hub-compat` @ `1865919`
(base `a4c7c2b` + совместимость password_hash + FK-safe delete).

Проверено:
- `password_hash` читается/пишется, PBKDF2-совместим с Squad Hub;
- plaintext не восстанавливается;
- удаление команды чистит `team_invites`/`team_applications`
  в одной транзакции (FK), ошибка → `False`, кэш не трогается;
- `team_audit`/`team_lfg` без FK — история сохраняется;
- новые таблицы Squad Hub старый код игнорирует.

Действия отката: переключить образ/деплой на SHA `1865919`,
схему БД **не** откатывать (аддитивна). См.
`docs/SQUAD_HUB_ROLLBACK.md`.

## 6. Сохранность соседних подсистем

- **BR**: регистрация в BR-турнире идёт через
  `team_registration_lock` (FOR UPDATE на teams) + VAC-проверка
  состава перед фиксацией. Проверить: `tests/test_br_*` и
  сценарий «регистрация ↔ disband» покрыт
  `test_register_br_team_vs_disband_concurrent`.
- **Cash Cup**: `register_br_team` фиксирует свежие
  name/captain/members под `cashcup_store.mutate` +
  `locked_team_on` в одной транзакции. Активный турнир блокирует
  изменение состава (`roster_locked`), повреждённый `state` →
  `check_failed` (fail-closed).
- **Blackjack**: изменений не было; прогон полного pytest
  подтверждает.
- **Spontaneous Events 2.0**: в базе `a4c7c2b`, ветка
  отребейжена поверх — конфликты отсутствуют; полный suite
  зелёный.

## 7. Критерии go/no-go

GO только если:
- [ ] активные турниры перечислены и команды в них известны;
- [ ] количество команд зафиксировано (baseline);
- [ ] конфликтов legacy-данных нет ИЛИ есть утверждённый план;
- [ ] pg_dump + Volume-снапшот сняты и проверены;
- [ ] rollback SHA `1865919` подтверждён;
- [ ] CI на PR #54 зелёный;
- [ ] получено отдельное разрешение владельца на деплой.
