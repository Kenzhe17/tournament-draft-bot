# DEPLOYMENT.md — установка, запуск, Railway

## Локально

```bash
# Python 3.12+ (на машине разработки Python может отсутствовать —
# тогда проверка только статическая: python -m compileall ...)
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # заполнить DISCORD_TOKEN и нужные ID
python bot.py
```

Ключевые env (см. `.env.example`): `DISCORD_TOKEN`, `DATABASE_URL` (пусто = JSON-режим), `REDIS_URL` (опц.), `BOT_OWNER_ID`, `FREEFIRE_API_KEY`, роли `ORG_1..3_ROLE_ID`/`ORG_ROLE_ID`/`MANAGER_ROLE_ID`/`MUTE_ROLE_ID`, `NOTIFICATION_ROLE_ID`/`NOTIFICATION_CHANNEL_ID`, `TOURNAMENT_CHANNELS`, `BR_ROLE_ID`/`BR_ROOM_CHANNEL_ID`, `EMOJI_*` кастомные эмодзи, `WELCOME_BANNER_URL`, `ORGANIZER_THUMBNAIL_URL`, `WARN/UNWARN_GIF_URL`, `LOG_CHANNEL_ID`.

`DATA_DIR` — каталог JSON-состояния (default `./data`; на Railway `/app/data`).

## Проверки

- Статика: `python -m compileall bot.py cogs views utils storage models services games ultimatum`.
- Автотестов нет (`tests/` пуст). `test_*.py` в корне — разовые скрипты (ELO, ultimatum) — запускать осознанно, они могут требовать БД/токен.
- Smoke на сервере: `/tournament create`, `/pet catalog`, `/birthdays`, `/player profile`, `/games roulette`, `/rep`, `/player marry`, `/ff info`.

## Railway (production)

- Деплой из GitHub-репозитория (`Kenzhe17/tournament-draft-bot`, ветка `main`) или `railway up` / `scripts/deploy-railway.ps1`.
- Сборка: `Dockerfile` (python:3.12-slim, apt-либы для OCR, предзагрузка RapidOCR ONNX-моделей) или `nixpacks.toml`.
- **Обязательно:** Volume смонтирован на `/app/data` — иначе JSON-состояние (турниры, BR, rep, кулдауны) сбрасывается при редеплое.
- Env-переменные — в Railway Variables (не коммитить).
- Логи: `railway logs`; диагностика кнопок — строка `compose:`/`TOURNAMENT-CREATE` в логах и ephemeral-ответе `/tournament create`.
- Не деплоить/не менять production-конфиг без разрешения пользователя.

## dashboard/ (опционально)

Next.js-приложение → Vercel. Свои env: `DATABASE_URL`, `DISCORD_CLIENT_ID/SECRET`, `NEXTAUTH_SECRET`, `NEXTAUTH_URL`. См. `dashboard/README.md`.

## Секреты

- Никогда не коммитить `.env` (в `.gitignore`), токены, пароли БД, webhook.
- Известная проблема: токен бота попадал в историю `.env.example` и `MOD_LOG_WEBHOOK_URL` захардкожен в `config.py` — требуется ротация (см. `docs/CODEX_HANDOFF.md`).
