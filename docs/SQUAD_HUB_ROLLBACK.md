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

Откат = deploy предыдущего SHA **плюс** патч совместимости
паролей. Простой redeploy старого SHA недостаточен: старый код
сравнивал `team.password` как plaintext, а после миграции там
пустая строка — вход по паролю сломался бы.

Патч (на коде до Squad Hub, файл `views/team_view.py`,
обработчик `PasswordJoinModal.on_submit`):

```python
# было:
if self.password.value != self.team.password:
# стало:
from models.team import verify_password  # или локальная копия
stored = getattr(self.team, "password_hash", "") or self.team.password
if not verify_password(self.password.value, stored):
```

`verify_password` (есть в `models/team.py` начиная с Squad Hub)
принимает и pbkdf2-хеш, и legacy-plaintext — поэтому патч
совместим с обеими сторонами миграции и **не возвращает
небезопасное хранение**: plaintext в БД после миграции уже
отсутствует, проверка идёт по хешу.

Если патч применить нельзя — альтернативный откат: перед
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
