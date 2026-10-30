# Discord Activity "Дурак" - Статус разработки

## Этап 1: Юридические документы ✅ ГОТОВО

**Созданные файлы:**
- `TERMS.md` - Условия использования
- `PRIVACY.md` - Политика конфиденциальности
- `LEGAL_HOSTING.md` - Инструкция по хостингу

**URL для Discord Developer Portal:**
- Terms: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/TERMS.md`
- Privacy: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/PRIVACY.md`

## Этап 2: Frontend (React + Vite) ✅ ГОТОВО

**Созданные файлы:**
- `durak-activity/package.json` - Зависимости проекта
- `durak-activity/vite.config.js` - Конфигурация Vite
- `durak-activity/index.html` - HTML входной файл
- `durak-activity/src/main.jsx` - Точка входа React
- `durak-activity/src/App.jsx` - Главный компонент приложения
- `durak-activity/src/discord-sdk.js` - Инициализация Discord SDK
- `durak-activity/src/index.css` - Глобальные стили
- `durak-activity/src/App.css` - Стили приложения
- `durak-activity/vercel.json` - Конфигурация Vercel

**Компоненты:**
- `src/components/Card.jsx` - Компонент карты с SVG стилизацией
- `src/components/Card.css` - Стили карты
- `src/components/Lobby.jsx` - Экран лобби
- `src/components/Lobby.css` - Стили лобби
- `src/components/GameTable.jsx` - Игровой стол с Socket.IO
- `src/components/GameTable.css` - Стили игрового стола

**Игровая логика:**
- `src/game/deck.js` - Логика колоды, перетасовки, сравнения карт

## Этап 3: Backend (Node.js + Socket.IO) ✅ ГОТОВО

**Созданные файлы:**
- `durak-activity/server/package.json` - Зависимости сервера
- `durak-activity/server/server.js` - Socket.IO сервер с полной логикой

## Этап 4: Интеграция с ботом ✅ ГОТОВО

**Созданные файлы:**
- `cogs/durak.py` - Команда `/durak` для запуска игры
- Обновлен `bot.py` - добавлена загрузка durak cog

## Что реализовано:

✅ Discord SDK интеграция и авторизация
✅ React + Vite настройка
✅ Компонент карты с SVG стилизацией
✅ Экран лобби с настройками режима и количества игроков
✅ Игровой стол UI с отображением всех игроков
✅ Полная логика колоды и сравнения карт
✅ Socket.IO реалтайм сервер
✅ Многопользовательская синхронизация
✅ Порядок ходов (атакующий → защищающийся)
✅ Смена атакующего/защищающегося
✅ Раздача карт из колоды
✅ Механика козырной карты
✅ Взятие карт при невозможности побить
✅ Бито и переход хода
✅ Досыпание карт до 6
✅ Определение победителя (когда карты кончились)
✅ Логика перевода (для переводного дурака)
✅ Отображение чьей сейчас ход
✅ Bot команда `/durak` с приглашением
✅ Vercel конфигурация для деплоя

## Что осталось сделать:

⏳ **Деплой:**
- Деплой frontend на Vercel
- Деплой backend на Railway/Render
- Получение публичных URL
- Настройка URL Mappings в Discord Developer Portal

⏳ **Discord Developer Portal:**
- Настроить URL Mappings
- Включить Embedded App
- Настроить permissions (identify, guilds)
- Добавить Terms и Privacy URLs

⏳ **Тестирование:**
- Тестирование в голосовом канале Discord
- Проверка многопользовательской игры
- Проверка обоих режимов (подкидной/переводной)

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

### Вариант 1: Vercel (Frontend)
```bash
cd durak-activity
npm install -g vercel
vercel
```

### Вариант 2: Railway (Backend)
1. Создать проект на Railway
2. Подключить GitHub репозиторий
3. Автоматический деплой backend из папки `durak-activity/server`

## Discord Developer Portal:

1. Перейти: https://discord.com/developers/applications/1782609823367873392
2. "URL Mappings" → добавить URL деплоя frontend
3. "Embedded App" → включить, настроить permissions (identify, guilds)
4. "Information" → добавить Terms и Privacy URLs из Этапа 1

## Использование:

В Discord:
1. Зайдите в голосовой канал
2. Напишите `/durak`
3. Нажмите на ссылку в сообщении
4. Дождитесь загрузки игры
5. Пригласите других игроков

## Game Rules:

### Podkidnoy Durak (Подкидной)
- Атакующий кладет карту
- Защищающийся должен побить или взять
- Можно подкидывать карты того же ранга
- После "Бито" карты уходят в сброс
- Если защищающийся взял - он ходит следующим

### Perevodnoy Durak (Переводной)
- Защищающийся может перевести атаку следующему игроку
- Перевод только картой того же ранга
- Если все перевели - последний защищается
- Стратегическая игра с передачей атаки

## Документация:

Полная документация в `durak-activity/README.md`

