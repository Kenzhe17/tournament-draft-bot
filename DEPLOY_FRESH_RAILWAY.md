# Развёртывание r!z3 CLASSIC на новом Railway-аккаунте (чистая установка)

Эта инструкция — для **нового** аккаунта Railway с **полностью пустыми**
PostgreSQL и Redis. Старые данные не переносятся. Старый Railway-проект
не удаляется и продолжает работать независимо.

> Ветка: `feat/classic-riz3-cv2` (PR #12). Не мержить в `main` без
> отдельного разрешения — деплой можно делать прямо с ветки.

---

## 1. Что изменилось в CLASSIC-сборке

- Нет OCR, нет PNG-рендеров, нет Chromium/Playwright — контейнеру не нужны
  OCR-модели и браузер. Образ легче и стартует быстрее.
- Турниры: `/tournament create size:<8|16|32> formation:<draft|balanced-sr|random>`,
  только правила Kazakhstan, до 5 побед.
- `/mm squad`: автозапуск при 8 игроках, ровно 2 постоянных канала пула
  (`1569282744909824061`, `1569282781270245417` — зашиты в `config.py`,
  env `MM_MATCH_CHANNEL_POOL` может переопределить).
- Статистика — только `K/D`; ассисты/урон не пишутся (колонки в PG
  остаются для истории, но не заполняются).

---

## 2. Предварительно в Discord

1. Пригласить бота на сервер с правами: Manage Roles, Manage Channels,
   Manage Messages, Send Messages, Embed Links, Use External Emojis,
   Add Reactions, Connect (voice), Manage Nicknames (по необходимости).
2. Создать роли организаторов (`org`, `ORG-1`, `ORG-2`, `ORG-3`) и выдать
   их боту/уполномоченным.
3. Создать **два текстовых канала** для MM-матчей и скопировать их ID
   (режим разработчика → правый клик по каналу → Copy Channel ID).
   Каналы должны быть в одном гильдии, что и бот.
4. Создать каналы организаторов и записать их ID для `TOURNAMENT_CHANNELS`
   и `ORG_ROLE_MAP` (формат `channel_id:role_id` через запятую).

---

## 3. Railway: новый проект

1. [railway.app](https://railway.app) → **New Project** →
   **Deploy from GitHub repo** → `Kenzhe17/tournament-draft-bot`.
2. В настройках сервиса **Branch** указать `feat/classic-riz3-cv2`
   (или сначала смержить PR — по вашему решению).
3. **Settings → Root Directory**: `/` (код в корне).
4. **Settings → Config**: `Dockerfile` подхватится автоматически
   (Railway определяет Dockerfile в корне).

### Volume (обязательно)

- **Volumes → Add Volume**, Mount Path: `/app/data`
- Хранит JSON-состояние турниров/очередей между рестартами.

---

## 4. PostgreSQL (чистый)

1. В том же проекте: **New → Database → PostgreSQL**.
2. Скопировать **Public Network URL** (или `DATABASE_URL`) из
   Variables сервиса БД — вида
   `postgresql://postgres:***@*.proxy.rlwy.net:PORT/railway`.
3. Ничего не импортировать — схема создаётся ботом автоматически
   (`storage/db.py` при первом подключении: `player_stats`,
   `match_history`, `match_channel_slots`, `match_finalizations` и др.).

## 5. Redis (опционально)

Redis используется только для кэшей/кулдаунов мини-игр; при отсутствии
`REDIS_URL` бот работает без него (graceful fallback). Если хотите:
**New → Database → Redis** → скопировать `REDIS_URL`.

---

## 6. Variables (Environment)

Обязательные:

```
DISCORD_TOKEN=<токен бота>
DATABASE_URL=<Public URL PostgreSQL из шага 4>
BOT_OWNER_ID=<ваш Discord ID>
GUILD_ID=<ID сервера>
```

Рекомендуемые:

```
ALLOWED_GUILD_IDS=<ID сервера>          # изоляция: бот только на этом сервере
TOURNAMENT_CHANNELS=<id1>,<id2>,...     # каналы организаторов
ORG_ROLE_MAP=<chan_id>:<role_id>,...    # канал → роль организатора
ORG_ROLE_ID=<id роли org>
ORG_1_ROLE_ID= / ORG_2_ROLE_ID= / ORG_3_ROLE_ID=
EXTRA_ADMIN_USER_IDS=<csv id админов/организаторов>
LOG_CHANNEL_ID=<id канала логов>
MM_MATCH_CHANNEL_POOL=                  # пусто = два канала по умолчанию
REDIS_URL=<если поднимали Redis>
DATA_DIR=/app/data
```

Не нужны (можно не задавать — кода больше нет):
`OCR_*`, `RANKED_READY_TIMEOUT_SECONDS` (Ready Check удалён).

Остальные переменные (`WELCOME_CHANNEL_ID`, `BR_*`, `ULT_*`, эмодзи и пр.)
— по желанию, см. `.env.example`.

---

## 7. Деплой и проверка

1. **Deploy** → дождаться статуса *Success*.
2. `railway logs` — ищите `Logged in as ...` и отсутствие ошибок БД.
3. В Discord:
   - `/mm squad` в поисковом канале → набрать 8 игроков →
     автоматический запуск, карточка матча в одном из двух каналов пула.
   - Капитаны (первые в составе команды) → «Заполнить K/D» → `K/D`.
   - Организатор → победитель + счёт `5:3` → «Подтвердить».
   - Канал освобождается, слот пула → FREE.
   - `/tournament create size:8 formation:draft` → регистрация →
     старт → драфт → матчи с ручным K/D → подтверждение организатором.
   - `/player profile` (или профиль-команда) — CV2 без PNG.
4. Проверить PG: таблицы созданы, `match_channel_slots` содержит
   ровно 2 строки со state `free` (после освобождения матча).

---

## 8. Rollback / откат

- **Код**: вернуть сервис на ветку `main` (или revert PR) — старая
  OCR/PNG-система поднимется снова (но потребует вернуть зависимости —
  они удалены из `requirements.txt` ветки CLASSIC).
- **Данные**: схема PG совместима в обе стороны; JSON-store турниров
  CLASSIC читается старым кодом (поля `match_channels`,
  `temp_match_stats` сохраняются). Активные CLASSIC-матчи после отката
  потеряют CV2-кнопки — завершить их вручную до отката.
- **Каналы пула**: остаются на месте, права «resting» (@everyone не
  может писать) сохраняются; при необходимости почистить вручную.

## 9. Старый Railway

Не удалять. Он продолжает работать на `main` со своими данными.
Переключение production на CLASSIC — только после явного разрешения:
смержить PR → переключить ветку сервиса на `main` → деплой.
