# Мегаплан: список всех ошибок внедрения и preventive checklist

Краткий список всех технических ошибок при внедрении мини-игр и команд за историю проекта + preventive checklist для предотвращения повторений.

---

## Обзор
За историю проекта выявлено **669 коммитов**, из которых значительная часть - исправления ошибок при внедрении новых функций. Ниже приведён полный список технических ошибок с краткими решениями и preventive checklist.

---

## 1. Ошибки импортов и модулей

### 1.1 ModuleNotFoundError / ImportError
**Примеры:**
- `5ec61de`: `from database import` → `from storage.db import` (user_bank_store)
- `df57991`: ModuleNotFoundError: No module named 'games' → добавлен `sys.path.append`
- `da21829`: Импорт из games/coin_flip → логика внедрена в cog
- `183e52b`: Ошибки импорта в games command
- `dcfac3e`: import error в rps.py → economy.py moved to storage
- `481e626`: games directory not in Python path → добавлен в sys.path

**Решение:**
- Проверять структуру директорий перед импортом
- Использовать абсолютные импорты (`from storage.db import` вместо `from database import`)
- Добавлять отсутствующие директории в `sys.path` при необходимости
- Внедрять логику игр прямо в cogs вместо кросс-модульных импортов

**Preventive:**
- Всегда использовать абсолютные импорты относительно проекта
- Проверять PYTHONPATH при деплое на Railway
- Избегать циклических импортов между cogs и games/

### 1.2 NameError / AttributeError при импорте
**Примеры:**
- `44e45a5`: `TournamentBot` не определён → `"TournamentBot"` (строковый type hint)
- `449ac21`: NameError for Tournament type hint → `Any` вместо конкретного типа
- `576`: NameError в room_modal.py → `Any` для избежания circular import

**Решение:**
- Использовать строковые type hints (`"TournamentBot"`) для избежания NameError
- Использовать `Any` для type hints при circular imports
- Добавлять `TYPE_CHECKING` guard

**Preventive:**
- Всегда использовать `TYPE_CHECKING` для type hints
- Для circular imports использовать строковые type hints или `Any`

---

## 2. Ошибки indentation и синтаксиса

### 2.1 IndentationError
**Примеры:**
- `f7c9c28`: IndentationError в utils/embeds.py → убран остаточный код
- `27c4db0`: IndentationError в tournament.py → добавлено тело функции
- `d96e738`: IndentationError (rank command вне класса) → исправлен отступ
- `36fb0a3`: Critical indentation errors preventing cog loading

**Решение:**
- Использовать 4 spaces для отступов (стандарт PEP 8)
- Проверять структуру классов перед коммитом
- Удалять остаточный код после рефакторинга

**Preventive:**
- Использовать линтер (black, flake8) перед коммитом
- Проверять отступы при добавлении новых методов в классы

### 2.2 SyntaxError / f-string errors
**Примеры:**
- `6a95dbe`: Syntax error в match_stats_view.py
- `2601894`: f-string syntax error
- `57c5811`: Button label syntax error
- `f6d2d39`: cancel_btn syntax error

**Решение:**
- Проверять синтаксис f-strings
- Проверять синтаксис Button/Select параметров

**Preventive:**
- Использовать линтер перед коммитом
- Проверять синтаксис при добавлении UI компонентов

---

## 3. Ошибки баз данных

### 3.1 Отсутствие таблицы / колонок
**Примеры:**
- `bb07a63`: user_balance table не существует → error handling + logging
- `2a3eeaf`: Database initialization errors → исправлен порядок инициализации
- `391286a`: Добавлена таблица match_odds

**Решение:**
- Добавлять миграции в `storage/db.py` при создании новых таблиц
- Проверять существование таблицы перед использованием
- Добавлять error handling для отсутствующих колонок

**Preventive:**
- Всегда добавлять миграцию в `init_db()` при создании новых store
- Использовать `CREATE TABLE IF NOT EXISTS` и `ALTER TABLE ADD COLUMN IF NOT EXISTS`

### 3.2 Неправильное использование pool/connection
**Примеры:**
- `4a2ac3c`: get_db_connection не существует → get_pool + async with
- `bb07a63`: user_balance table не создана

**Решение:**
- Использовать `get_pool()` вместо ручного подключения
- Использовать `async with pool.acquire() as conn:` вместо `conn.close()`

**Preventive:**
- Всегда использовать connection pool pattern
- Никогда не вызывать `conn.close()` в async контексте

---

## 4. Ошибки Discord API

### 4.1 Invalid Form Body (color)
**Примеры:**
- `fa66b74`: color 0x4539717 > 16777215 → изменён на валидный цвет
- `439f4ed`: Исправлен цвет embed в /rob

**Решение:**
- Проверять что color <= 16777215 (0xFFFFFF)
- Использовать `discord.Color.from_rgb()` для валидации

**Preventive:**
- Всегда проверять что hex color <= 0xFFFFFF
- Использовать discord.Color для создания цветов

### 4.2 Unknown interaction (10062)
**Примеры:**
- `8f5a764`: Unknown interaction error → defer + followup
- `81c706c`: Unknown interaction error в CircleSelectButton

**Решение:**
- Использовать `await interaction.response.defer()` в начале callback
- Использовать `interaction.followup.send()` для последующих ответов

**Preventive:**
- Всегда defer interaction в начале callback если есть шанс повторного ответа
- Никогда не вызывать response.send_message после defer

### 4.3 AttributeError: property 'view' has no setter
**Примеры:**
- `e964ef5`: AttributeError в coin_flip → переименован параметр в game_view

**Решение:**
- Переименовать параметр view в game_view для избежания конфликта с discord.ui.Button.view

**Preventive:**
- Не использовать `view` как имя параметра в классах Button/Select
- Использовать `game_view` как стандартное имя

### 4.4 Duplicate callback
**Примеры:**
- `3ee0e8e`: Duplicate callback в JoinPoolButton → удалён дубликат

**Решение:**
- Удалять дублирующиеся callback методы

**Preventive:**
- Проверять что у каждого custom_id только один callback

---

## 5. Ошибки бизнес-логики

### 5.1 ValueError / AttributeError в player stats
**Примеры:**
- AttributeError при отсутствии kd_ratio/avg_kills → try-except с fallback 0.0

**Решение:**
- Добавлять try-except для доступа к опциональным атрибутам
- Использовать fallback значения (0.0, None)

**Preventive:**
- Всегда проверять наличие атрибутов с hasattr или try-except
- Предоставлять fallback значения для новых полей

### 5.2 TypeError (duplicate class)
**Примеры:**
- `8feb403`: Duplicate CancelWinnerButton → удалён дубликат с другой сигнатурой

**Решение:**
- Удалять дублирующиеся классы с разными сигнатурами

**Preventive:**
- Проверять что у каждого класса уникальное имя
- Не переопределять классы в одном файле

### 5.3 NameError: tournament not defined
**Примеры:**
- `ec1c4ab`: tournament → self.tournament в callback

**Решение:**
- Использовать self.tournament вместо tournament

**Preventive:**
- Всегда использовать self. для доступа к атрибутам объекта

---

## 6. Ошибки эмодзи

### 6.1 Invalid emoji
**Примеры:**
- `27d58ed`: Invalid emoji error → удалён emoji из GameSelect
- `e9f60b5`: icon_ prefix для эмодзи
- `f081b13`: replace_emojis вызов для эмодзи

**Решение:**
- Использовать `replace_emojis()` для всех эмодзи
- Использовать префикс `icon_` для кастомных эмодзи

**Preventive:**
- Всегда оборачивать эмодзи в `replace_emojis()`
- Не использовать кастомные emoji ID напрямую

---

## 7. Ошибки при внедрении новых cogs

### 7.1 Отсутствие load_extension
**Примеры:**
- `e2457e7`: Добавлен load_extension для cogs.bank
- `5e8c240`: Добавлен load_extension для Coin Flip cog

**Решение:**
- Добавлять `await self.load_extension("cogs.имя")` в bot.py

**Preventive:**
- Всегда добавлять load_extension для нового cog в bot.py
- Проверять что cog существует в директории cogs/

### 7.2 Type hints в __init__
**Примеры:**
- `44e45a5`: TournamentBot не определён → строковый type hint

**Решение:**
- Использовать `"TournamentBot"` вместо TournamentBot

**Preventive:**
- Всегда использовать строковые type hints в cogs для избежания circular imports

---

## 8. Ошибки при работе с балансом

### 8.1 ValueError: Amount must be positive
**Примеры:**
- `ce02da0`: add_balance с отрицательным значением → subtract_balance

**Решение:**
- Использовать `subtract_balance()` вместо `add_balance(guild_id, user_id, -amount)`

**Preventive:**
- Всегда использовать правильные методы (add/subtract) для операций
- Не передавать отрицательные значения в add_balance

---

## PREVENTIVE CHECKLIST

### Перед внедрением нового cog/команды:
- [ ] Проверить структуру импортов (абсолютные пути)
- [ ] Добавить `TYPE_CHECKING` guard для type hints
- [ ] Использовать строковые type hints (`"TournamentBot"`) для избежания circular imports
- [ ] Добавить `await self.load_extension("cogs.имя")` в bot.py
- [ ] Проверить отступы (4 spaces, PEP 8)
- [ ] Запустить линтер (black/flake8) перед коммитом

### Перед использованием Discord UI:
- [ ] Использовать `game_view` вместо `view` для параметра
- [ ] Defer interaction в начале callback если есть шанс повторного ответа
- [ ] Использовать `interaction.followup.send()` после defer
- [ ] Проверить что color <= 16777215 (0xFFFFFF)
- [ ] Убедиться что у каждого custom_id только один callback

### Перед работой с базой данных:
- [ ] Добавить миграцию в `storage/db.py` для новых таблиц
- [ ] Использовать `CREATE TABLE IF NOT EXISTS`
- [ ] Использовать `ALTER TABLE ADD COLUMN IF NOT EXISTS`
- [ ] Использовать `get_pool()` + `async with pool.acquire() as conn:`
- [ ] Не вызывать `conn.close()` в async контексте

### Перед работой с store/balance:
- [ ] Использовать `subtract_balance()` для списания
- [ ] Не передавать отрицательные значения в `add_balance()`
- [ ] Добавлять try-except для опциональных атрибутов
- [ ] Предоставлять fallback значения (0.0, None)

### Перед работой с эмодзи:
- [ ] Всегда оборачивать эмодзи в `replace_emojis()`
- [ ] Использовать префикс `icon_` для кастомных эмодзи
- [ ] Не использовать кастомные emoji ID напрямую

### Перед деплоем на Railway:
- [ ] Проверить PYTHONPATH и структуру директорий
- [ ] Проверить что все импорты используют абсолютные пути
- [ ] Проверить что Database URL настроен
- [ ] Проверить что таблицы созданы в миграциях

---

## Общие принципы:
1. **Single-file pattern**: Внедрять логику игр прямо в cogs для избежания cross-module imports
2. **Async with**: Всегда использовать `async with pool.acquire() as conn:` для database
3. **Defer-followup**: Всегда defer interaction + followup для Discord UI
4. **Type hints**: Использовать строковые type hints в cogs
5. **Error handling**: Добавлять try-except для опциональных атрибутов
6. **Linting**: Использовать линтер перед коммитом
