# r!z3 Control Center — деплой (Railway)

Три компонента, разворачиваются как отдельные Railway-сервисы из одного
репозитория:

| Сервис | Каталог | Команда запуска |
|---|---|---|
| Bot (Discord) | корень репо | `python bot.py` |
| Control Center API | `control_center/` | `uvicorn control_center.main:create_app --factory --host 0.0.0.0 --port $PORT` |
| Panel (frontend) | `panel/` | `npm run build` → раздача `panel/dist` (static) |

## Переменные окружения

### Bot
- `DISCORD_TOKEN` — токен бота (secret)
- `DATABASE_URL` — PostgreSQL (secret)
- `REDIS_URL` — Redis
- `BOT_OWNER_ID`, `EXTRA_ADMIN_USER_IDS`, `ALLOWED_GUILD_IDS` — доступ
- ID каналов/ролей — как в `config.py`
- `JSON_STORE_MODE` — `single|sharded|auto`
- `PG_POOL_MIN/MAX`, `PG_COMMAND_TIMEOUT`, `PG_CONNECT_TIMEOUT`
- `VAC_MAX_DAYS` — fallback-дефолт (runtime: `vac.max_days` в панели)
- REP_*/MM_*/ELO — env-дефолты, переопределяются runtime-настройками

### Control Center API
- `DATABASE_URL` — та же PostgreSQL (admin_config_* таблицы)
- `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` — OAuth2 (secret)
- `SESSION_SECRET` — HMAC-ключ сессий (secret, ≥32 байта)
- `PUBLIC_BASE_URL` — публичный URL API (callback OAuth, CORS)
- `PANEL_GUILD_ID` — Discord guild ID, к которому привязана панель

### Panel
- `VITE_API_URL` — URL API (опционально; в проде лучше same-origin
  reverse proxy, тогда оставить пустым)

## Схема БД

Миграции не нужны: `storage/db.py::init_db` и
`storage/admin_config_store.py::ensure_schema` создают таблицы
идемпотентно (`CREATE TABLE IF NOT EXISTS`) при старте бота/API.

## Порядок безопасного релиза

1. Задеплоить API + Panel на staging, проверить `/api/health`.
2. Запустить бота — настройки применяются из PG (дефолты = текущие
   константы, поведение не меняется).
3. Проверить цикл: изменение в панели → `pg_notify` → бот применяет
   без рестарта → перезапуск → значение сохранено.
4. Только после staging E2E — production (отдельное разрешение).

## Откат

- Настройки: versioned rollback через `/api/settings/{key}/rollback`.
- Код: revert коммита; схема БД обратно-совместима (новые таблицы
  аддитивны, `is_active` имеет DEFAULT TRUE).
