# Discord Activity "Дурак" - Статус разработки

## Этап 1: Юридические документы ✅ ГОТОВО

**Созданные файлы:**
- `TERMS.md` - Условия использования
- `PRIVACY.md` - Политика конфиденциальности
- `LEGAL_HOSTING.md` - Инструкция по хостингу

**URL для Discord Developer Portal:**
- Terms: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/TERMS.md`
- Privacy: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/PRIVACY.md`

## Этап 2: Frontend (React + Vite) ✅ БАЗОВАЯ СТРУКТУРА

**Созданные файлы:**
- `durak-activity/package.json` - Зависимости проекта
- `durak-activity/vite.config.js` - Конфигурация Vite
- `durak-activity/index.html` - HTML входной файл
- `durak-activity/src/main.jsx` - Точка входа React
- `durak-activity/src/App.jsx` - Главный компонент приложения
- `durak-activity/src/discord-sdk.js` - Инициализация Discord SDK
- `durak-activity/src/index.css` - Глобальные стили
- `durak-activity/src/App.css` - Стили приложения

**Компоненты:**
- `src/components/Card.jsx` - Компонент карты с SVG стилизацией
- `src/components/Card.css` - Стили карты
- `src/components/Lobby.jsx` - Экран лобби
- `src/components/Lobby.css` - Стили лобби
- `src/components/GameTable.jsx` - Игровой стол
- `src/components/GameTable.css` - Стили игрового стола

**Игровая логика:**
- `src/game/deck.js` - Логика колоды, перетасовки, сравнения карт

## Этап 3: Backend (Node.js + Socket.IO) ✅ БАЗОВАЯ СТРУКТУРА

**Созданные файлы:**
- `durak-activity/server/package.json` - Зависимости сервера
- `durak-activity/server/server.js` - Socket.IO сервер с базовой логикой

## Что реализовано:

✅ Discord SDK интеграция
✅ React + Vite настройка
✅ Компонент карты с SVG стилизацией
✅ Экран лобби с настройками
✅ Игровой стол UI
✅ Базовая логика колоды
✅ Сравнение карт
✅ Проверка возможности побить карту
✅ Проверка возможности подкинуть карту
✅ Socket.IO сервер структура
✅ Присоединение к игре
✅ Базовая синхронизация состояния

## Что нужно доделать:

⏳ **Полная многопользовательская логика:**
- Присоединение к голосовому каналу Discord
- Синхронизация игроков в одном канале
- Порядок ходов
- Смена атакующего/защищающегося

⏳ **Игровая механика:**
- Раздача карт из колоды
- Механика козырной карты
- Взятие карт при невозможности побить
- Бито и переход хода
- Определение победителя/проигравшего
- Логика перевода (для переводного дурака)

⏳ **Деплой:**
- Деплой на Vercel/Netlify/Railway
- Получение публичного URL
- Настройка URL Mappings в Discord Developer Portal

⏳ **Интеграция с ботом:**
- Добавление команды `/durak` в `bot.py`
- Создание `InviteTarget.embedded_application`
- Интеграция с экономикой бота (опционально)

## Как запустить локально:

### Frontend:
```bash
cd durak-activity
npm install
npm run dev
```
Откроется на http://localhost:3000

### Backend:
```bash
cd durak-activity/server
npm install
npm start
```
Запустится на http://localhost:3001

## Как задеплоить:

### Вариант 1: Vercel (Frontend только)
```bash
cd durak-activity
npm install -g vercel
vercel
```

### Вариант 2: Railway (Full Stack)
1. Создать проект на Railway
2. Подключить GitHub репозиторий
3. Автоматический деплой

## Discord Developer Portal:

1. Перейти: https://discord.com/developers/applications/1782609823367873392
2. "URL Mappings" → добавить URL деплоя
3. "Embedded App" → включить, настроить permissions
4. "Information" → добавить Terms и Privacy URLs

## Следующие шаги:

1. Завершить игровую логику (многопользовательская синхронизация)
2. Деплой на хостинг
3. Настроить Discord Developer Portal
4. Добавить команду в бота
5. Тестирование в голосовом канале

## Документация:

Полная документация в `durak-activity/README.md`
