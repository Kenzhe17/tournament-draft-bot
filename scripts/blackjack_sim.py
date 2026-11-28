"""BLACKJACK 2.0 — offline-симуляция правил (фиксированный seed).

НЕ используется в live и не влияет на реальные раздачи. Цель —
зафиксировать RTP/house edge для нескольких стратегий игрока на
честной колоде (в live — secrets.SystemRandom; здесь —
детерминированный random.Random(seed) той же 52-карточной колоды).

Выплаты считаются ТОЛЬКО через games.blackjack_engine.gross_payout —
та же функция, что и в settlement (gross 2*bet при win, bet при
push, 0 при loss).

Стратегии (доступны только Hit/Stand, без split/double):
- stand17:      hit пока total < 17, stand на 17+ (включая soft 17)
- dealer_mimic: повторяет правила дилера (hit <17 и на soft 17)
- basic:        упрощённая basic strategy (H17, без pair-split):
                hard 13-16 stand vs дилер 2-6 иначе hit;
                hard 12 stand vs 4-6; <=11 hit; 17+ stand;
                soft 18 stand vs 2,7,8 иначе hit; soft 17 hit;
                soft <=16 hit; soft 19+ stand
- random:       случайный hit/stand (reference baseline)

Методология: каждая стратегия играет ОДНИ И ТЕ ЖЕ раздачи
(одинаковый seed → одинаковая последовательность колод), поэтому
различия RTP отражают только качество решений, а не дисперсию колод.

Запуск:  python scripts/blackjack_sim.py [rounds] [seed]
Отчёт пишется в scripts/blackjack_sim_report.md (коммитится).
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games.blackjack_engine import (
    RANKS,
    SUITS,
    Round,
    gross_payout,
    hand_is_soft,
    hand_total,
    is_natural,
)

BET = 100
REPORT_PATH = Path(__file__).resolve().parent / "blackjack_sim_report.md"


def _dealer_up_index(game: Round) -> int:
    return RANKS.index(game.dealer[0][0])


def strat_stand17(game: Round) -> Round:
    if hand_total(game.player) < 17:
        return game.hit()
    return game.stand()


def strat_dealer_mimic(game: Round) -> Round:
    """Действует как дилер: hit при total<17 и на soft 17."""
    total = hand_total(game.player)
    if total < 17 or (total == 17 and hand_is_soft(game.player)):
        return game.hit()
    return game.stand()


def strat_basic(game: Round) -> Round:
    """Упрощённая basic strategy (H17, Hit/Stand only)."""
    cards = game.player
    total = hand_total(cards)
    up = _dealer_up_index(game)
    if hand_is_soft(cards):
        if total >= 19:
            return game.stand()
        if total == 18 and up in (RANKS.index("2"), RANKS.index("7"),
                                  RANKS.index("8")):
            return game.stand()
        return game.hit()  # soft <=17 и soft 18 vs 3-6,9,10,A
    # hard totals
    if total >= 17:
        return game.stand()
    if total <= 11:
        return game.hit()
    if 13 <= total <= 16:
        return game.stand() if up <= RANKS.index("6") else game.hit()
    if total == 12:
        return game.stand() if RANKS.index("4") <= up <= RANKS.index("6") \
            else game.hit()
    return game.hit()  # hard <12 уже покрыт; страховка


def strat_random(game: Round, rng: random.Random) -> Round:
    if hand_total(game.player) >= 21:
        return game.stand()
    return game.hit() if rng.random() < 0.5 else game.stand()


def _play(game: Round, strategy, rng: random.Random) -> Round:
    while game.phase == "player":
        game = (strategy(game, rng) if strategy is strat_random
                else strategy(game))
    return game


def _sim(strategy, rounds: int, seed: int) -> dict:
    rng = random.Random(seed)
    counts = Counter()
    wagered = 0
    paid = 0
    naturals = 0
    natural_both = 0
    for _ in range(rounds):
        cards = [(r, s) for s in SUITS for r in RANKS]
        rng.shuffle(cards)
        game = Round.deal_deck(cards)
        game = _play(game, strategy, rng)
        counts[game.outcome] += 1
        if game.reason:
            counts[f"reason:{game.reason}"] += 1
        wagered += BET
        paid += gross_payout(game.outcome, BET)  # та же функция, что в store
        if is_natural(game.player):
            naturals += 1
        if is_natural(game.player) and is_natural(game.dealer):
            natural_both += 1
    n = rounds
    rtp = paid / wagered
    return {
        "rounds": n,
        "win": counts["win"],
        "loss": counts["loss"],
        "push": counts["push"],
        "wagered": wagered,
        "paid": paid,
        "rtp": rtp,
        "house_edge": 1 - rtp,
        "player_natural_rate": naturals / n,
        "both_natural_rate": natural_both / n,
        "reasons": {k.split(":", 1)[1]: v for k, v in counts.items()
                    if k.startswith("reason:")},
    }


def _report(rounds: int, seed: int, results: list[tuple[str, dict]]) -> str:
    lines = [
        "# BLACKJACK 2.0 — результаты симуляции",
        "",
        f"- Раздач на стратегию: **{rounds:,}**",
        f"- Seed: `{seed}` (детерминированный `random.Random`)",
        ("- Колода: 52 карты, одна на раздачу; дилер берёт до hard 17 "
         "и на soft 17; равные очки — победа дилера; natural платит "
         "gross 2×bet."),
        ("- Каждая стратегия играет одни и те же раздачи (общий seed) — "
         "различия RTP отражают только решения, не дисперсию колод."),
        ("- Выплаты через `games.blackjack_engine.gross_payout` — та же "
         "функция, что в production settlement."),
        "- Симуляция НЕ используется для управления исходами live-раздач.",
        "",
        ("| Стратегия | Wins | Losses | Pushes | Wagered | Paid | RTP | "
         "House Edge | Player BJ | Both BJ |"),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, res in results:
        lines.append(
            f"| {name} | {res['win']:,} | {res['loss']:,} | "
            f"{res['push']:,} | {res['wagered']:,} | {res['paid']:,} | "
            f"{res['rtp']:.4f} | {res['house_edge']:.4f} | "
            f"{res['player_natural_rate']:.4%} | "
            f"{res['both_natural_rate']:.4%} |")
    lines += ["", "## Причины исходов (по стратегиям)", ""]
    for name, res in results:
        lines.append(f"- **{name}**: `{res['reasons']}`")
    lines += [
        "",
        "## Методология",
        "",
        ("1. `random.Random(seed)` тасует каждую колоду (в live — "
         "`secrets.SystemRandom`, CSPRNG; симуляция офлайн, "
         "детерминирована)."),
        ("2. Раздача — `Round.deal_deck` из `games.blackjack_engine` — "
         "тот же код правил, что в production."),
        ("3. Ходы игрока — стратегия из таблицы выше; дилер ходит по "
         "правилам движка (hard/soft 17)."),
        ("4. `gross_payout(outcome, bet)` — идентично settlement в "
         "`storage/blackjack_store._settle_in_tx`."),
        "5. RTP = Σ payouts / Σ stakes. House edge = 1 − RTP.",
        "",
        ("Стратегии описывают только Hit/Stand — split/double/insurance "
         "отсутствуют в игре, поэтому 'basic' является аппроксимацией "
         "классической basic strategy без отклонений на состав колоды."),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261128
    print(f"BLACKJACK 2.0 simulation — {rounds:,} deals/strategy, "
          f"seed={seed}")
    results = []
    for name, strat in [
        ("stand17", strat_stand17),
        ("dealer_mimic", strat_dealer_mimic),
        ("basic", strat_basic),
        ("random", strat_random),
    ]:
        res = _sim(strat, rounds, seed)
        results.append((name, res))
        print(f"--- {name} ---")
        for k in ("rounds", "win", "loss", "push", "rtp", "house_edge",
                  "player_natural_rate"):
            v = res[k]
            print(f"  {k}: {v:,.4f}" if isinstance(v, float)
                  else f"  {k}: {v:,}")
        print("  reasons:", res["reasons"])
        print()
    REPORT_PATH.write_text(_report(rounds, seed, results),
                           encoding="utf-8")
    print(f"report -> {REPORT_PATH}")


if __name__ == "__main__":
    main()
