# r!z3 Control Center — Staging Runbook

Ветка: `feat/classic-control-center` · Railway project: `riz3-classic` · Environment: `staging`

## Архитектура staging

| Сервис | Назначение | Образ |
|---|---|---|
| `bot-staging` | Discord-бот + admin action executor | корневой `Dockerfile`, `SERVICE_ROLE` unset → `python bot.py` |
| `panel` | FastAPI Control Center + React static | тот же `Dockerfile`, `SERVICE_ROLE=panel` → `uvicorn control_center.main:app` |
| `Postgres`/`Redis` | staging-only | ⚠️ не provisioned — упор в лимит 3 volumes/project |

Panel domain: `https://panel-staging-b7f974de.up.railway.app`

## Обязательные секреты (только staging, НЕ копировать из production)

### bot-staging
- `DISCORD_TOKEN` — токен **отдельного** Discord-приложения/бота
- `DATABASE_URL` — отдельный staging Postgres
- `REDIS_URL` — отдельный staging Redis (если используется)
- `BOT_OWNER_ID`, `ALLOWED_GUILD_IDS` — владелец и **тестовый** guild
- Остальные `config.py` env — ID каналов/ролей тестового сервера

### panel
- `SERVICE_ROLE=panel` (уже задано)
- `CONTROL_CENTER_PUBLIC_URL=https://panel-staging-b7f974de.up.railway.app` (задано)
- `CONTROL_CENTER_DISCORD_CLIENT_ID` / `CONTROL_CENTER_DISCORD_CLIENT_SECRET` — OAuth2 того же staging-приложения
- `CONTROL_CENTER_SESSION_SECRET` — случайная строка ≥32 chars
- `CONTROL_CENTER_OWNER_IDS` — Discord user ID владельца
- `CONTROL_CENTER_COOKIE_SECURE` — `true` (HTTPS domain)
- OAuth2 redirect URI в Discord Developer Portal:
  `https://panel-staging-b7f974de.up.railway.app/auth/callback`
- `DATABASE_URL` — та же staging БД, что у bot-staging (общие таблицы `admin_*`)

## Запрещено

- Подключать staging-бота к production-серверу или production `DATABASE_URL`.
- Копировать production `DISCORD_TOKEN` / OAuth secrets в staging.

## Порядок вывода в staging

1. Создать Discord-приложение + бота + тестовый сервер (вне Railway).
2. Решить staging Postgres: освободить volume в проекте, поднять внешний PG
   (Neon/Supabase) или расширить план Railway → `railway add -d postgres` в env `staging`.
3. Выставить переменные выше → Railway redeploy обоих сервисов.
4. Применить PG-миграции (`storage/db.py` / alembic если есть — проверить `docs/`).
5. Smoke: OAuth login → dashboard → изменение настройки → наблюдение в Discord.

## E2E чеклист (live Discord, тестовый сервер)

| # | Сценарий | Проверка |
|---|---|---|
| 1 | OAuth2 login | редирект Discord → callback → сессия → `/api/me` |
| 2 | Settings без рестарта | поменять `rating.base_win_change` → pg_notify → новое значение в боте |
| 3 | Shop item | создать товар → виден в Discord shop-команде |
| 4 | Economy adjust | начислить монеты → баланс в Discord + строка в журнале |
| 5 | Идемпотентность | повторить adjust с тем же op_id → без двойного начисления |
| 6 | Tournament | создать тестовый турнир → `tournament.cancel` из панели → состояние в Discord |
| 7 | Registration | open/close через панель → кнопки регистрации в Discord реагируют |
| 8 | VAC | выдать/снять тестовый VAC → статус в Discord |
| 9 | Moderation | claim/resolve жалобы → событие в `moderation_events` |
| 10 | Discord channels | сменить notification channel → следующее уведомление в новом канале |
| 11 | CV2 template | опубликовать `welcome.card`/`tournament.announcement` → новое сообщение по шаблону |
| 12 | RBAC | не-owner пользователь не может finance/moderation действия |
| 13 | Restart recovery | рестарт bot-staging → настройки/шаблоны/балансы сохранены |

## Rollback

- Откат кода: `git revert` + push (сервисы пересоберутся).
- Откат данных: `admin_config_store` хранит версии — rollback через API
  `POST /api/settings/{key}/rollback`; шаблоны — через Design UI.
- Полный откат staging: удалить env `staging` в Railway dashboard.
