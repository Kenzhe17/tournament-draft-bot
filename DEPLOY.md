# Деплой в облако — бот работает 24/7 без вашего ПК

Бот должен крутиться **на сервере в интернете**. Ваш компьютер может быть выключен.

---

## Быстрый старт — Railway (рекомендуется)

### Способ 1: одна команда (Railway CLI уже установлен)

```powershell
cd C:\Users\Nota\Projects\tournament-draft-bot
.\scripts\deploy-railway.ps1
```

Скрипт:
1. Войдёт в Railway (браузер, если нужно)
2. Создаст проект
3. Передаст `DISCORD_TOKEN` из `.env`
4. Задеплоит бота в облако

**После деплоя обязательно** в [railway.app](https://railway.app):
- Откройте проект → сервис → **Volumes** → **Add Volume**
- **Mount Path:** `/app/data`

Без volume JSON с турнирами сбросится при перезапуске контейнера.

### Способ 2: через сайт (без CLI)

1. Загрузите проект на **GitHub** (создайте репозиторий, запушьте код).
2. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**.
3. **Variables** → `DISCORD_TOKEN` = ваш токен, `DATA_DIR` = `/app/data`.
4. **Volumes** → mount `/app/data`.
5. Дождитесь деплоя — бот **Online** в Discord.

---

## Полезные команды Railway

```powershell
railway logs      # логи бота
railway status    # статус деплоя
railway up        # повторный деплой после изменений
```

---

## Альтернативы

### Fly.io

```powershell
winget install Fly.io.flyctl
cd C:\Users\Nota\Projects\tournament-draft-bot
fly auth login
fly launch --no-deploy
fly volumes create tournament_data --region ams --size 1
fly secrets set DISCORD_TOKEN=ваш_токен
fly deploy
```

### Render

1. Push на GitHub.
2. [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**.
3. Укажите `DISCORD_TOKEN` при деплое.

Render прочитает `render.yaml` автоматически.

### Свой VPS / Docker

```powershell
cd C:\Users\Nota\Projects\tournament-draft-bot
docker compose up -d
```

`restart: unless-stopped` держит бота после перезагрузки сервера.

---

## Чеклист перед деплоем

| Пункт | Где |
|-------|-----|
| Токен в переменных окружения | Railway Variables / `.env` локально |
| **Server Members Intent** | Discord Developer Portal → Bot |
| Volume `/app/data` | Railway / Fly / Render |
| Локальный бот остановлен | Иначе два процесса с одним токеном |

---

## После деплоя

- Бот онлайн **24/7** — ПК можно выключать
- Обновление: `git push` (GitHub) или `railway up` (CLI)
- Логи: панель Railway или `railway logs`

**Не публикуйте токен в чатах и GitHub** — только в Variables / `.env`.

---

## Частые проблемы

**Деплой сразу Failed («no associated build»)**
- Откройте [ваш проект Railway](https://railway.com/project/11e62fb3-cde5-40c2-9f24-a77e19216281) и проверьте ошибку в UI.
- Частая причина: нужна **привязка карты** или закончились бесплатные кредиты.
- Переменные `DISCORD_TOKEN` и `DATA_DIR=/app/data` уже должны быть в сервисе.
- Повторный деплой: `railway up --detach`

**Турнир сбросился**
- Не подключён Volume на `/app/data`.

**Два бота с одним токеном**
- Остановите локальный бот перед облачным деплоем.
