"""BLACKJACK 2.0 — offline-симуляция правил (фиксированный seed).

НЕ используется в live. Цель — зафиксировать RTP/house edge для
нескольких стратегий игрока на честной колоде (secrets.SystemRandom
в live; здесь — детерминированный random.Random(seed) той же колоды).

Стратегии:
- always_stand17: hit <17, stand >=17 (наивная)
- basic: упрощённая basic strategy (столл на 12 vs 4-6, жёсткие
  пороги по дилерской карте, soft-правила)
- random: случайный hit/stand до 17+

Запуск:  python scripts/blackjack_sim.py [rounds] [seed]
Отчёт: win/loss/push, gross payout, RTP, house edge.
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
    hand_is_soft,
    hand_total,
    is_natural,
)


def _play(game: Round, strategy) -> Round:
    while game.phase == "player":
        game = strategy(game)
    return game


def strat_always_stand17(game: Round) -> Round:
    total = hand_total(game.player)
    if total < 17:
        return game.hit()
    return game.stand()


def strat_random(game: Round, rng: random.Random) -> Round:
    if hand_total(game.player) >= 21:
        return game.stand()
    if rng.random() < 0.5:
        return game.hit()
    return game.stand()


def _dealer_up(game: Round) -> str:
    return game.dealer[0][0]


def strat_basic(game: Round) -> Round:
    """Упрощённая basic strategy для Hit/Stand (без pair/split)."""
    cards = game.player
    total = hand_total(cards)
    soft = hand_is_soft(cards)
    up = _dealer_up(game)
    up_idx = RANKS.index(up)

    if soft:
        # soft 18+ stand против 2-8? упрощённо: stand >=19, hit <18
        if total >= 19:
            return game.stand()
        if total == 18 and up_idx <= RANKS.index("8"):
            return game.stand()
        return game.hit()
    # hard
    if total >= 17:
        return game.stand()
    if total <= 11:
        return game.hit()
    if total == 12 and up_idx >= RANKS.index("4") and up_idx <= RANKS.index("6"):
        return game.stand()
    if total == 16 and up_idx <= RANKS.index("6"):
        return game.stand()
    return game.hit()


def _sim(strategy, rounds: int, seed: int) -> dict:
    rng = random.Random(seed)
    counts = Counter()
    wagered = 0
    paid = 0
    naturals = 0
    for _ in range(rounds):
        cards = [(r, s) for s in SUITS for r in RANKS]
        rng.shuffle(cards)
        game = Round.deal_deck(cards)
        while game.phase == "player":
            game = strategy(game, rng) if strategy is strat_random else strategy(game)
        counts[game.outcome] += 1
        counts[f"reason:{game.reason}"] += 1
        bet = 100
        wagered += bet
        paid += (2 * bet if game.outcome == "win" else bet if game.outcome == "push" else 0)
        if is_natural(game.player) and len(game.player) == 2:
            naturals += 1
    n = rounds
    return {
        "rounds": n,
        "win": counts["win"],
        "loss": counts["loss"],
        "push": counts["push"],
        "wagered": wagered,
        "paid": paid,
        "rtp": paid / wagered,
        "house_edge": 1 - paid / wagered,
        "player_natural_rate": naturals / n,
        "reasons": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("reason:")},
    }


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261128
    print(f"BLACKJACK 2.0 simulation — {rounds:,} deals, seed={seed}")
    print("Methodology: 52-card shoe, one deck per round, dealer hits soft 17,\n"
          "ties (non-natural) lose for player, natural pays 2x gross.\n")
    for name, strat in [
        ("stand<17", strat_always_stand17),
        ("basic", strat_basic),
        ("random", strat_random),
    ]:
        res = _sim(strat, rounds, seed)
        print(f"--- {name} ---")
        for k in ("rounds", "win", "loss", "push", "rtp", "house_edge",
                  "player_natural_rate"):
            v = res[k]
            print(f"  {k}: {v:,.4f}" if isinstance(v, float) else f"  {k}: {v:,}")
        print("  reasons:", res["reasons"])
        print()


if __name__ == "__main__":
    main()
