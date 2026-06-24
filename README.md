# Tournament Draft Bot

Discord-бот для проведения турниров с драфтом команд (16 игроков, 4 команды, 4 капитана).

## Возможности

- Создание турнира с одним главным Embed-сообщением (все обновления через `message.edit`)
- Добавление 4 капитанов (Discord-пользователи) и 12 игроков (текст)
- Автоматическое распределение игроков по кругам 2/3/4
- Драфт с Select Menu, проверкой очереди и автовыбором
- Генерация полуфиналов и финала с кнопками
- Сохранение состояния в JSON и восстановление после перезапуска

## Требования

- Python 3.10+
- discord.py 2.x

## Установка

```bash
cd tournament-draft-bot
python -m venv .venv

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
copy .env.example .env
```

Укажите токен бота в `.env`:

```
DISCORD_TOKEN=your_bot_token_here
```

### Настройка бота в Discord Developer Portal

1. Создайте приложение на [Discord Developer Portal](https://discord.com/developers/applications)
2. В разделе **Bot** скопируйте токен
3. Включите **Privileged Gateway Intents** → **Server Members Intent**
4. Пригласите бота на сервер с правами `applications.commands` и `Send Messages`

## Облако 24/7

Бот можно запустить в облаке — ПК не нужен. Подробно: **[DEPLOY.md](DEPLOY.md)**.

Быстрый деплой на Railway:

```powershell
.\scripts\deploy-railway.ps1
```

Проект уже создан: [railway.app — tournament-draft-bot](https://railway.com/project/11e62fb3-cde5-40c2-9f24-a77e19216281)

После деплоя добавьте **Volume** с mount path `/app/data`.

## Запуск

```bash
python bot.py
```

## Команды

| Команда | Описание |
|---------|----------|
| `/tournament create` | Создать турнир |
| `/captains add @Cap1 @Cap2 @Cap3 @Cap4` | Добавить капитанов |
| `/player add Player1,Player2,...` | Добавить игроков |
| `/draft start` | Запустить драфт |

Все команды доступны только **администраторам** сервера.

## Структура проекта

```
tournament-draft-bot/
├── bot.py              # Точка входа
├── config.py           # Конфигурация
├── cogs/
│   └── tournament.py   # Slash-команды
├── models/
│   └── tournament.py   # Модель данных
├── storage/
│   └── json_store.py   # JSON-персистентность
├── utils/
│   ├── embeds.py       # Embed-сообщения
│   └── permissions.py  # Проверки прав
└── views/
    ├── draft_view.py   # Select Menu драфта
    ├── matches_view.py # Полуфиналы
    └── final_view.py   # Финал
```

## Правила драфта

- **Круг 2:** порядок 1 → 2 → 3, капитан 4 получает последнего автоматически
- **Круг 3:** порядок 4 → 3 → 2, капитан 1 получает последнего автоматически
- **Круг 4:** порядок 1 → 2 → 3, капитан 4 получает последнего автоматически
