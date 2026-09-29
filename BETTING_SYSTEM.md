# Система ставок на турниры

## Обзор

Система ставок использует динамические коэффициенты, которые меняются в зависимости от распределения ставок между командами. Основная цель - сбалансировать ставки, стимулируя пользователей ставить на менее популярную команду.

## Модели данных

### Bet (Ставка)
```python
@dataclass
class Bet:
    guild_id: int
    user_id: int
    user_name: str
    match_id: str
    team_name: str
    amount: int
    odds: float  # Коэффициент на момент ставки (фиксированный для выплаты)
```

### MatchOdds (Коэффициенты матча)
```python
@dataclass
class MatchOdds:
    team_a_name: str
    team_b_name: str
    team_a_odds: float
    team_b_odds: float
    team_a_buffer: int = 0  # Буфер при достижении минимума
    team_b_buffer: int = 0  # Буфер при достижении минимума
```

## Инициализация коэффициентов

Коэффициенты инициализируются на основе разницы среднего ELO команд:

```
elo_diff = avg_elo_b - avg_elo_a
odds_diff = elo_diff * 0.002
base_odds = 1.9

Если Team B имеет больше ELO:
  odds_a = 1.9 + odds_diff
  odds_b = 1.9 - odds_diff

Если Team A имеет больше ELO:
  odds_a = 1.9 - abs(odds_diff)
  odds_b = 1.9 + abs(odds_diff)

Ограничения: минимум 1.1x, максимум 2.7x
```

## Динамические коэффициенты

### Сдвиг коэффициентов

При каждой ставке коэффициенты обновляются:

```
shift_odds = shift_amount / 500 * 0.1
```

**500 монет ставки = 0.1x изменение коэффициента**

### Логика изменения

Когда пользователь ставит на команду:
- **Коэффициент выбранной команды УМЕНЬШАЕТСЯ** (становится более привлекательным)
- **Коэффициент противоположной команды УВЕЛИЧИВАЕТСЯ** (становится менее привлекательным)

Это стимулирует пользователей ставить на команду, на которую меньше ставок.

### Система буферов

Если коэффициент достигает минимума (1.1x), излишек сдвига сохраняется в буфер:

```
Если new_odds < 1.1:
  overflow = (1.1 - new_odds) / 0.1 * 100
  odds = 1.1
  buffer += int(overflow)
```

Буфер противоположной команды используется сначала, если он доступен.

### Ограничения

- Минимальный коэффициент: 1.1x
- Максимальный коэффициент: 2.7x

## Обработка ставок

### Новая ставка

1. Проверяется баланс пользователя
2. Списывается полная сумма ставки
3. Сохраняются текущие коэффициенты для выплаты
4. Коэффициенты обновляются с сдвигом на полную сумму

### Добавление к существующей ставке

**КРИТИЧЕСКАЯ ПРОБЛЕМА:**

Текущая реализация в `bet_modal.py` и `betting_view.py` имеет баг:

```python
# Строки 82-86 в bet_modal.py
existing_bet = await bet_store.get_user_bet(self.guild_id, interaction.user.id, match_id)
if existing_bet:
    # User already has a bet - only deduct additional amount
    additional_amount = amount  # ОШИБКА: должно быть amount - existing_bet.amount
    await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, additional_amount)
```

**Проблема:** Переменная `amount` - это полная сумма, которую ввёл пользователь (например, 590), а не разница между новой и старой ставкой.

**Ожидаемое поведение:**
- Пользователь вводит итоговую сумму ставки (например, 590)
- Если у него уже есть ставка на 333, списывается только 257 (590 - 333)
- `shift_amount` должен быть 257

**Текущее поведение:**
- Пользователь вводит 590
- Списывается 590 (полная сумма)
- В `bet_store.py` вычисляется `additional_amount = 590 - 333 = 257`
- Но со счёта уже списано 590, а не 257
- `shift_amount` = 257 (правильно)
- Коэффициент сдвигается только на 257

Это объясняет, почему коэффициент растёт медленнее, чем ожидается.

### Сохранение ставки в bet_store.py

Логика в `bet_store.py` (строки 182-194):

```python
existing_bet = await self.get_user_bet(bet.guild_id, bet.user_id, bet.match_id)
if existing_bet:
    # Add to existing bet, keep original odds
    additional_amount = bet.amount - existing_bet.amount
    bet.amount += existing_bet.amount
    bet.odds = existing_bet.odds
    shift_amount = additional_amount
else:
    # New bet, use current odds
    bet.odds = current_bet_odds
    shift_amount = bet.amount
```

Эта часть логики **ПРАВИЛЬНАЯ** - она корректно вычисляет разницу.

## Выплата ставок

### Расчёт выигрыша

```python
def resolve_match_bets(guild_id, match_id, winning_team_name):
    for bet in bets:
        if bet.team_name == winning_team_name:
            payout = bet.amount * winning_odds  # ФИНАЛЬНЫЕ коэффициенты
            payouts[bet.user_id] = payout
        else:
            payouts[bet.user_id] = 0
```

**ВАЖНО:** Выплата использует **финальные коэффициенты** на момент победы, а не коэффициенты на момент ставки. Это отличается от комментария в модели `Bet`, который говорит "fixed for payout".

### Статистика ставок

Записывается прибыль/убыток для каждого пользователя:
- Победа: `profit = payout - bet_amount`
- Поражение: `loss = bet_amount`

## Хранение данных

### PostgreSQL (основной режим)

- Таблица `bets`: хранит все ставки
- Таблица `match_odds`: хранит текущие коэффициенты и буферы
- Использует `ON CONFLICT ... DO UPDATE` для обновления существующих ставок

### JSON fallback (если БД недоступна)

- `data/bets.json`: хранит ставки
- `data/odds.json`: хранит коэффициенты

## Защита от race conditions

Для каждого матча используется отдельный `asyncio.Lock`:

```python
lock = self._get_lock(bet.match_id)
async with lock:
    # 所有操作 в критической секции
```

Это предотвращает одновременное обновление коэффициентов несколькими пользователями.

## Пример работы системы

### Сценарий 1: Новая ставка

1. Начальные коэффициенты: Team A = 1.9x, Team B = 1.9x
2. Пользователь ставит 100 на Team A
3. Списывается 100 со счёта
4. Сдвиг: `100 / 500 * 0.1 = 0.02x`
5. Новые коэффициенты: Team A = 1.88x, Team B = 1.92x
6. Коэффициент ставки: 1.9x (фиксированный)

### Сценарий 2: Увеличение ставки

1. Текущая ставка пользователя: 333 на Team B
2. Пользователь вводит 590 (итоговая сумма)
3. Разница: 590 - 333 = 257
4. Списывается 257 со счёта
5. Новая сумма ставки: 590
6. Сдвиг: `257 / 500 * 0.1 = 0.0514x`
7. Коэффициент ставки остаётся прежним (из первой ставки)

### Сценарий 3: Уменьшение ставки

1. Текущая ставка пользователя: 1000 на Team A
2. Пользователь вводит 500 (итоговая сумма)
3. Разница: 1000 - 500 = 500
4. Возвращается 500 на счёт (рефанд)
5. Новая сумма ставки: 500
6. Сдвиг: `500 / 500 * 0.1 = 0.1x` (уменьшение тоже сдвигает коэффициенты)
7. Team A коэффициент УМЕНЬШАЕТСЯ, Team B коэффициент УВЕЛИЧИВАЕТСЯ
8. Коэффициент ставки остаётся прежним (из первой ставки)

### Сценарий 3: Текущее поведение (С БАГОМ)

1. Текущая ставка пользователя: 333 на Team B
2. Пользователь вводит 590
3. Списывается 590 со счёта (ОШИБКА - должно быть 257)
4. В bet_store вычисляется разница: 590 - 333 = 257
5. Сдвиг: 0.0257x
6. Но со счёта списано 590, а не 257

## Требуемые исправления

### 1. Исправить логику списания в bet_modal.py

Заменить:
```python
if existing_bet:
    additional_amount = amount
    await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, additional_amount)
```

На:
```python
if existing_bet:
    if amount > existing_bet.amount:
        # Increasing bet - deduct additional amount
        additional_amount = amount - existing_bet.amount
        await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, additional_amount)
    elif amount < existing_bet.amount:
        # Decreasing bet - refund difference
        refund_amount = existing_bet.amount - amount
        await user_balance_store.add_balance(self.guild_id, interaction.user.id, refund_amount)
    else:
        # Same amount - no balance change
        additional_amount = 0
```

### 2. Исправить логику списания в betting_view.py

Аналогичное исправление в строках 210-227.

### 3. Исправить логику сдвига в bet_store.py

Логика сдвига теперь использует абсолютное значение разницы:

```python
if existing_bet:
    additional_amount = bet.amount - existing_bet.amount
    bet.odds = existing_bet.odds
    # Always shift odds by absolute amount (even when decreasing)
    # Any bet on a team decreases its odds, regardless of amount change
    shift_amount = abs(additional_amount)
```

Это означает, что ЛЮБАЯ ставка на команду уменьшает её коэффициент:
- Увеличение ставки: списывается разница, коэффициент выбранной команды УМЕНЬШАЕТСЯ
- Уменьшение ставки: возвращается разница, коэффициент выбранной команды УМЕНЬШАЕТСЯ
- Та же сумма: никаких изменений в балансе, но коэффициент УМЕНЬШАЕТСЯ на 0

### 3. Явно указать в UI, что пользователь вводит итоговую сумму

Добавить подсказку в модальное окно:
- "Введите ИТОГОВУЮ сумму ставки (включая предыдущие ставки на этот матч)"

### 4. Рассмотреть альтернативный подход

Вместо итоговой суммы пользователь может вводить ДОПОЛНИТЕЛЬНУЮ сумму. Тогда:
- UI: "Введите сумму ДОБАВЛЕНИЯ к ставке"
- Логика: списывается введённая сумма
- bet_store: вычисляется `new_total = existing + additional`

## Файлы реализации

- `storage/bet_store.py` - основная логика ставок и коэффициентов
- `models/bet.py` - модели данных
- `views/bet_modal.py` - модальное окно ввода суммы (из cogs/tournament.py)
- `views/betting_view.py` - альтернативное модальное окно (старое)
- `storage/db.py` - база данных
- `storage/user_balance_store.py` - управление балансом
