# BLACKJACK 2.0 — результаты симуляции

- Раздач на стратегию: **1,000,000**
- Seed: `20261128` (детерминированный `random.Random`)
- Колода: 52 карты, одна на раздачу; дилер берёт до hard 17 и на soft 17; равные очки — победа дилера; natural платит gross 2×bet.
- Каждая стратегия играет одни и те же раздачи (общий seed) — различия RTP отражают только решения, не дисперсию колод.
- Выплаты через `games.blackjack_engine.gross_payout` — та же функция, что в production settlement.
- Симуляция НЕ используется для управления исходами live-раздач.

| Стратегия | Wins | Losses | Pushes | Wagered | Paid | RTP | House Edge | Player BJ | Both BJ |
|---|---|---|---|---|---|---|---|---|---|
| stand17 | 410,480 | 587,699 | 1,821 | 100,000,000 | 82,278,100 | 0.8228 | 0.1772 | 4.8557% | 0.1821% |
| dealer_mimic | 412,170 | 586,009 | 1,821 | 100,000,000 | 82,616,100 | 0.8262 | 0.1738 | 4.8557% | 0.1821% |
| basic | 434,891 | 563,288 | 1,821 | 100,000,000 | 87,160,300 | 0.8716 | 0.1284 | 4.8557% | 0.1821% |
| random | 322,000 | 676,205 | 1,795 | 100,000,000 | 64,579,500 | 0.6458 | 0.3542 | 4.8094% | 0.1795% |

## Причины исходов (по стратегиям)

- **stand17**: `{'higher': 171534, 'player_bust': 269266, 'tie': 92772, 'lower': 179233, 'player_natural': 46736, 'dealer_bust': 192210, 'dealer_natural': 46428, 'both_natural': 1821}`
- **dealer_mimic**: `{'higher': 174190, 'player_bust': 272966, 'lower': 174462, 'player_natural': 46736, 'dealer_bust': 191244, 'tie': 92153, 'dealer_natural': 46428, 'both_natural': 1821}`
- **basic**: `{'higher': 155570, 'player_bust': 170395, 'lower': 264012, 'player_natural': 46736, 'dealer_bust': 232585, 'tie': 82453, 'dealer_natural': 46428, 'both_natural': 1821}`
- **random**: `{'player_bust': 265386, 'dealer_natural': 46444, 'lower': 326415, 'dealer_bust': 193728, 'higher': 81973, 'player_natural': 46299, 'tie': 37960, 'both_natural': 1795}`

## Методология

1. `random.Random(seed)` тасует каждую колоду (в live — `secrets.SystemRandom`, CSPRNG; симуляция офлайн, детерминирована).
2. Раздача — `Round.deal_deck` из `games.blackjack_engine` — тот же код правил, что в production.
3. Ходы игрока — стратегия из таблицы выше; дилер ходит по правилам движка (hard/soft 17).
4. `gross_payout(outcome, bet)` — идентично settlement в `storage/blackjack_store._settle_in_tx`.
5. RTP = Σ payouts / Σ stakes. House edge = 1 − RTP.

Стратегии описывают только Hit/Stand — split/double/insurance отсутствуют в игре, поэтому 'basic' является аппроксимацией классической basic strategy без отклонений на состав колоды.
