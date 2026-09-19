# Tournament Draft Bot

Discord-бот для проведения турниров с драфтом команд, системой ставок и статистикой игроков.

## Возможности

- **Турниры:** Поддержка турниров на 8, 16 и 32 игрока
- **Драфт:** Система драфта с ротацией капитанов и автовыбором
- **Ставки:** Полноценная система ставок с балансами и выплатами
- **Статистика:** ELO-рейтинг, K/D, история матчей, лидерборды
- **База данных:** PostgreSQL с миграциями и JSON fallback
- **Интерфейс:** Все в одном Embed-сообщении с кнопками и модалами

## Требования

- Python 3.10+
- discord.py 2.x
- PostgreSQL (опционально, для базы данных)

## Установка

```bash
cd tournament-draft-bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
```

Укажите токен бота в `.env`:

```
DISCORD_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://user:password@host:port/dbname  # опционально
```

### Настройка бота в Discord Developer Portal

1. Создайте приложение на [Discord Developer Portal](https://discord.com/developers/applications)
2. В разделе **Bot** скопируйте токен
3. Включите **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
4. Пригласите бота на сервер с правами:
   - `applications.commands`
   - `Send Messages`
   - `Read Messages/View Channels`

## Облако 24/7

Бот можно запустить в облаке — ПК не нужен. Подробно: **[DEPLOY.md](DEPLOY.md)**.

Быстрый деплой на Railway:

```powershell
.\scripts\deploy-railway.ps1
```

После деплоя добавьте **Volume** с mount path `/app/data`.

## Запуск

```bash
python bot.py
```

## Команды

### Турнир

| Команда | Описание |
|---------|----------|
| `/tournament create` | Создать турнир (8/16/32 игроков) |
| `/tournament delete` | Удалить активный турнир |
| `/start` | Запустить драфт |
| `/close` | Закрыть регистрацию |
| `/open` | Открыть регистрацию |
| `/test` | Заполнить турнир тестовыми данными |
| `/limit` | Включить/выключить лимит для круга |
| `/replace` | Заменить игрока |
| `/delete_player` | Удалить игрока из турнира |

### Статистика

| Команда | Описание |
|---------|----------|
| `/leaderboard` | Таблица лидеров по ELO |
| `/coins` | Таблица лидеров по монетам |
| `/profile` | Профиль игрока |
| `/booyah` | Рекорды турнира |
| `/reset_leaderboard` | Сбросить статистику (админ) |

### Другое

| Команда | Описание |
|---------|----------|
| `!elo @player 1000` | Установить ELO игрока (админ) |

Админ-команды доступны только **администраторам** сервера.

## Структура проекта

```
tournament-draft-bot/
├── bot.py                      # Точка входа
├── cogs/
│   └── tournament.py           # Slash-команды
├── models/
│   ├── tournament.py            # Модель турнира
│   ├── bet.py                  # Модель ставки
│   └── player_stats.py         # Статистика игрока
├── storage/
│   ├── db.py                   # PostgreSQL база данных
│   ├── json_store.py           # JSON fallback
│   ├── bet_store.py            # Хранилище ставок
│   ├── player_stats_store.py    # Хранилище статистики
│   ├── user_balance_store.py   # Хранилище балансов
│   └── betting_stats_store.py  # Статистика ставок
├── utils/
│   ├── embeds.py               # Embed-сообщения
│   └── permissions.py          # Проверки прав
└── views/
    ├── draft_view.py           # Драфт
    ├── matches_view.py         # Квалификации и полуфиналы
    ├── final_view.py           # Финал
    ├── bet_views.py            # Ставки
    ├── bet_modal.py            # Модал ввода суммы
    ├── leaderboard_view.py     # Лидерборд
    └── kd_input_modal.py       # Ввод K/D
```

## Режимы формирования

- **Manual:** Ручное распределение игроков по кругам
- **ELO:** Автоматическое распределение по ELO-рейтингу

## Ротация драфта

### 2 капитана (8 игроков)
- Круг 2: 1 → 2
- Круг 3: 2 → 1
- Круг 4: 2 → 1

### 4 капитана (16 игроков)
- Круг 2: 1 → 2 → 3 → 4
- Круг 3: 2 → 3 → 4 → 1
- Круг 4: 4 → 3 → 1 → 2

### 8 капитанов (32 игрока)
- Круг 2: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
- Круг 3: 5 → 8 → 4 → 7 → 3 → 6 → 2 → 1
- Круг 4: 6 → 7 → 8 → 2 → 1 → 3 → 4 → 5

## Система ставок

- Игроки могут делать ставки на матчи
- Ставки принимаются до закрытия админом
- Выплаты рассчитываются пропорционально банку
- Баланс сохраняется в базе данных

## Статистика игроков

- **ELO:** Рейтинг с динамическим обновлением
- **K/D:** Соотношение убийств к смертям
- **Win Rate:** Процент побед
- **Монеты:** Валюта для ставок и наград
