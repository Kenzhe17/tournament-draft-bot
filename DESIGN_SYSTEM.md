# Дизайн-система r!z3 — Minimal Dark UI

Единый визуальный стандарт для всех embed, кнопок и select menu бота.
**Новые команды обязаны использовать `utils/design/`** — не создавать
`discord.Embed`/`discord.ui.Button` напрямую.

## Структура

```
utils/design/
├── colors.py       # палитра и категории
├── emojis.py       # семантические кастомные эмодзи
├── formatters.py   # числа, валюта, проценты, рейтинги
├── embeds.py       # DesignEmbed + section/kv
└── components.py   # design_button / design_select / ButtonRole
```

## Embed

```python
from utils.design import DesignEmbed

embed = DesignEmbed(
    title="Передача монет",
    description="Операция выполнена.",
    category="success",      # цвет полосы слева
    author=interaction.user, # ник + аватар в Author
    footer="DISCORD SERVER r1z3",  # только при необходимости
)
embed.add_section("Информация", "• Сумма: 1,250 🪙")   # полноширинная секция
embed.add_kv("Игрок", interaction.user.mention)        # компактная пара
```

### Категории (цвет левой полосы)

| category | HEX | Когда |
|---|---|---|
| `success` | #2ECC71 | успешное действие, результат |
| `error` | #E74C3C | ошибка, отказ |
| `warning` | #F1C40F | предупреждение |
| `info` | #5865F2 | информация, турниры (`tournament`), игры (`game`) |
| `economy` | #F0B232 | баланс, магазин, ставки, экономика |
| `neutral` | #747F8D | профиль, статистика, нейтральное |

Доменные алиасы: `tournament`, `game`, `minigame`, `shop`, `economy`,
`profile`, `stats`, `moderation`, `admin`.

### Правила

- Title — короткий, **bold** не обязателен.
- Description — максимум 1–2 строки.
- Секции — через `add_field(inline=False)` (нативные разделители Discord).
- Не использовать `---`, юникод-линии, thumbnail без смысла.
- Footer — только при необходимости.
- Аватар пользователя — через `author=member`, не thumbnail.

## Кнопки

```python
from utils.design import design_button, ButtonRole, partial_emoji

btn = design_button("Купить", emoji="check", role=ButtonRole.SUCCESS)
view.add_item(btn)
```

Роли: `PRIMARY` (главное действие), `SECONDARY` (по умолчанию),
`SUCCESS` (подтверждение), `DANGER` (удаление/опасное).
В subclass-кнопках: `emoji=partial_emoji("back")` в `super().__init__`.

## Эмодзи

```python
from utils.design import emoji, partial_emoji, E

emoji("money")        # <:money:ID> или unicode-фолбэк для текста embed
partial_emoji("back") # PartialEmoji для кнопок/селектов
E.money               # то же через прокси
```

- Все эмодзи — только через семантические ключи, ID в `EMOJI_*` env.
- Фолбэк — unicode, пока ID не загружены.
- Карта загрузки пака: `EMOJI_PACK_MAP.md`.
- `custom_id`, callbacks, бизнес-логика НЕ меняются.

## Форматтеры

```python
from utils.design import fmt_int, fmt_money, fmt_percent, fmt_games, fmt_elo_change

fmt_int(3190)          # "3,190"
fmt_money(1250)        # "1,250 <money>"
fmt_percent(12.5)      # "12.5%"
fmt_games(5)           # "5 игр"
fmt_elo_change(120)    # "+120"
```

## Миграция

Старые `discord.Embed(` заменяются на `DesignEmbed(` + `category=`.
`color=` можно передавать для исключений (редкость, фазы турнира).
Поле `author` заменяет `set_author(name=display_name, icon_url=avatar)`.
