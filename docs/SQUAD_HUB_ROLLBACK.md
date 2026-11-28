# Squad Hub 2.0 — откат и совместимость

## Что меняет миграция

`ensure_squad_schema()` (в `init_db`) применяет только аддитивные
операции: новые колонки `teams` (`join_mode`, `recruiting`,
`purpose`, `description`, `requirements`, `logo_url`,
`password_hash`, `updated_at`, `status_message_id`,
`recruitment_channel_id`, `recruitment_message_id`) и новые
таблицы (`team_invites`, `team_applications`, `team_lfg`,
`team_audit`, `squad_hub_state`). Старые колонки и `team_id`
не трогаются.

Legacy-команды (созданные до Squad Hub, с plaintext-паролем):

- `join_mode = 'password'`, `recruiting = FALSE` — публичный
  набор не включается автоматически, старые команды не
  публикуются без согласия капитана;
- при первой загрузке `load_from_postgres()` plaintext-пароль
  хешируется (pbkdf2) в `password_hash`, а колонка `password`
  затирается. Это **необратимо**: вернуть plaintext нельзя и
  не нужно — см. «Откат» ниже.

Перед созданием уникального индекса
`ux_teams_guild_lower_name` выполняется аудит
(`_audit_legacy_teams`): дубликаты названий без учёта регистра,
дубликаты членства, капитан вне состава, составы > 4 игроков.
При любом конфликте уникальный индекс **не создаётся**,
отчёт пишется в `DATA_DIR/squad_migration_report.json`,
команды не изменяются — требуется ручное разрешение
администратором (автопереименование/удаление запрещены).

## Откат кода

Подготовленная rollback-ветка: **`rollback/squad-hub-compat`**,
SHA **`e34f1db82b59d0c286a974a13d056d1686b2f6fc`** — production
base `feat/classic-riz3-cv2` + патч совместимости
(коммиты поверх актуальной базы). Это и есть «подготовленная
версия» — отдельный redeploy произвольного старого SHA не нужен.

Патч (уже применён в ветке):

- `models/team.py`: поле `password_hash`, `hash_password`/
  `verify_password` (PBKDF2, формат идентичен Squad Hub),
  `check_password()` (хеш → fallback на legacy plaintext),
  `set_password()` (пишет только хеш, plaintext не хранится);
- `storage/team_store.py`: `load_from_postgres`/
  `refresh_from_db` читают `password_hash` (с защитой на
  случай отсутствия колонки до миграции), `_save_to_postgres`
  пишет `password_hash` когда колонка есть;
- `views/team_view.py`: вход по паролю через `check_password`,
  смена пароля через `set_password`;
- `cogs/team.py`: `/team create` сохраняет пароль хешем.
- `storage/team_store.py` (коммит `e34f1db`): удаление команды
  FK-безопасно — `team_applications`/`team_invites` удаляются
  в той же транзакции (через `to_regclass`-проверку на случай
  отсутствия таблиц), ошибка удаления возвращает `False` и не
  трогает кэш (никакого ложного успеха). `team_audit`/`team_lfg`
  без FK на teams — история сохраняется.

Проверенный сценарий (прогнан на временной PostgreSQL):

1. Старая версия → создание команды с паролем (plaintext).
2. Миграция Squad Hub → plaintext хеширован в `password_hash`.
3. Успешное вступление по паролю под Squad Hub.
4. Откат на `rollback/squad-hub-compat` → вступление по тому
   же паролю работает (проверка по хешу).
5. Повторное обновление до Squad Hub → работает без миграций.

Plaintext-пароли не восстанавливаются; новые таблицы при
штатном rollback не удаляются (старый код их игнорирует).

Если ветку применить нельзя — альтернативный откат: перед
откатом в Control Center вернуть legacy-командам вход через
приглашения (капитан рассылает invite), а password-режим
временно не использовать. Новые таблицы (`team_invites` и др.)
старый код просто игнорирует — они аддитивны.

## Откат данных

Данные не откатываются: новые таблицы можно оставить (старый
код их не читает) или удалить вручную:

```sql
DROP TABLE IF EXISTS team_invites, team_applications,
    team_lfg, team_audit, squad_hub_state;
-- колонки teams можно не трогать — старый код их игнорирует.
```

`teams.password_hash` можно оставить — место не занимает.
