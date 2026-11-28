"""BLACKJACK 2.0 — чистые правила блэкджека (без Discord/БД).

Одна колода (52 карты) на раздачу, тасование secrets.SystemRandom,
результат не меняется после старта. Дилер берёт до hard 17 и на soft 17,
останавливается на 18+ или hard 17. Равные очки (не natural) —
проигрыш игрока. Natural у обоих — возврат ставки; natural игрока
выигрывает, если у дилера нет natural; natural дилера — иначе.

Выплаты: победа или natural → gross 2*bet (net +bet);
поражение → 0 (net -bet); оба natural → gross bet (net 0).
Без split/double/insurance.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

RULES_VERSION = "bj2.0.0"

BET_MIN = 10
BET_MAX = 10000
BET_PRESETS = (100, 500, 1000)
TURN_TIMEOUT_SECONDS = 90

RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
SUITS = ("♠", "♥", "♦", "♣")
VALUES = dict(zip(RANKS, (*range(2, 11), 10, 10, 10, 11)))
RED_SUITS = {"♥", "♦"}

Card = tuple[str, str]  # (rank, suit)


def new_deck(rng: secrets.SystemRandom | None = None) -> list[Card]:
    """52 карты, тасование только через SystemRandom (CSPRNG)."""
    rng = rng or secrets.SystemRandom()
    deck = [(rank, suit) for suit in SUITS for rank in RANKS]
    rng.shuffle(deck)
    return deck


def hand_total(cards: tuple[Card, ...] | list[Card]) -> int:
    """Лучший итог руки: тузы 11→1 по необходимости (несколько тузов)."""
    total = sum(VALUES[rank] for rank, _ in cards)
    for rank, _ in cards:
        if total <= 21:
            break
        if rank == "A":
            total -= 10
    return total


def hand_is_soft(cards: tuple[Card, ...] | list[Card]) -> bool:
    """True, если хотя бы один туз считается за 11."""
    aces = sum(1 for rank, _ in cards if rank == "A")
    if aces == 0:
        return False
    # Сумма без тузов; k тузов за 11, остальные за 1, итог ≤ 21.
    # soft ⇔ существует k ≥ 1: non_ace + 10k + aces ≤ 21
    non_ace = sum(VALUES[rank] for rank, _ in cards if rank != "A")
    k = (21 - non_ace - aces) // 10
    return k > 0


def is_natural(cards: tuple[Card, ...] | list[Card]) -> bool:
    """Natural blackjack: ровно 2 карты и итог 21."""
    return len(cards) == 2 and hand_total(cards) == 21


# Исходы
OUT_WIN = "win"
OUT_LOSS = "loss"
OUT_PUSH = "push"

# Причины для отображения
REASON_PLAYER_NATURAL = "player_natural"
REASON_DEALER_NATURAL = "dealer_natural"
REASON_BOTH_NATURAL = "both_natural"
REASON_PLAYER_BUST = "player_bust"
REASON_DEALER_BUST = "dealer_bust"
REASON_HIGHER = "higher"
REASON_LOWER = "lower"
REASON_TIE = "tie"

REASON_TEXT = {
    REASON_PLAYER_NATURAL: "Блэкджек у игрока",
    REASON_DEALER_NATURAL: "Блэкджек у дилера",
    REASON_BOTH_NATURAL: "Блэкджек у обоих — возврат ставки",
    REASON_PLAYER_BUST: "Перебор у игрока",
    REASON_DEALER_BUST: "Перебор у дилера",
    REASON_HIGHER: "У игрока больше очков",
    REASON_LOWER: "У дилера больше очков",
    REASON_TIE: "Равные очки — победа дилера",
}


def gross_payout(outcome: str, bet: int) -> int:
    """Gross-выплата: win/natural → 2*bet, loss → 0, push → bet."""
    if outcome == OUT_WIN:
        return 2 * bet
    if outcome == OUT_PUSH:
        return bet
    return 0


@dataclass
class Round:
    """Состояние раздачи. Неизменяемый переход: hit/stand → новый Round."""

    deck: list[Card] = field(default_factory=list)   # оставшаяся колода
    player: list[Card] = field(default_factory=list)
    dealer: list[Card] = field(default_factory=list)
    phase: str = "player"                            # player | done
    outcome: str | None = None                       # win|loss|push
    reason: str | None = None
    rules_version: str = RULES_VERSION

    @classmethod
    def deal(cls, rng: secrets.SystemRandom | None = None) -> Round:
        return cls.deal_deck(new_deck(rng))

    @classmethod
    def deal_deck(cls, deck: list[Card]) -> Round:
        """Раздача из готовой (уже перемешанной) колоды.
        Используется симуляцией/тестами; live — deal() с SystemRandom."""
        deck = list(deck)
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        game = cls(deck=deck, player=player, dealer=dealer)
        p_nat = is_natural(player)
        d_nat = is_natural(dealer)
        if p_nat or d_nat:
            if p_nat and d_nat:
                return game._finish(OUT_PUSH, REASON_BOTH_NATURAL)
            if p_nat:
                return game._finish(OUT_WIN, REASON_PLAYER_NATURAL)
            return game._finish(OUT_LOSS, REASON_DEALER_NATURAL)
        return game

    def _finish(self, outcome: str, reason: str) -> Round:
        return Round(
            deck=self.deck, player=list(self.player), dealer=list(self.dealer),
            phase="done", outcome=outcome, reason=reason,
            rules_version=self.rules_version,
        )

    def _draw(self) -> Card:
        return self.deck.pop()

    def hit(self) -> Round:
        """Взять карту. Перебор/21 → завершение."""
        if self.phase != "player":
            return self
        player = [*self.player, self._draw()]
        total = hand_total(player)
        if total > 21:
            return Round(
                deck=self.deck, player=player, dealer=list(self.dealer),
                phase="done", outcome=OUT_LOSS, reason=REASON_PLAYER_BUST,
                rules_version=self.rules_version,
            )
        if total == 21:
            return self._with(player=player).stand()
        return self._with(player=player)

    def stand(self) -> Round:
        """Стоп: дилер добирает по правилам, затем сравнение."""
        if self.phase != "player":
            return self
        dealer = list(self.dealer)
        deck = list(self.deck)
        while _dealer_should_hit(dealer):
            dealer.append(deck.pop())
        player_total = hand_total(self.player)
        dealer_total = hand_total(dealer)
        if dealer_total > 21:
            outcome, reason = OUT_WIN, REASON_DEALER_BUST
        elif player_total > dealer_total:
            outcome, reason = OUT_WIN, REASON_HIGHER
        elif player_total == dealer_total:
            outcome, reason = OUT_LOSS, REASON_TIE
        else:
            outcome, reason = OUT_LOSS, REASON_LOWER
        return Round(
            deck=deck, player=list(self.player), dealer=dealer,
            phase="done", outcome=outcome, reason=reason,
            rules_version=self.rules_version,
        )

    def _with(self, **kw) -> Round:
        data = {
            "deck": self.deck, "player": self.player, "dealer": self.dealer,
            "phase": self.phase, "outcome": self.outcome, "reason": self.reason,
            "rules_version": self.rules_version,
            **kw,
        }
        return Round(**data)


def _dealer_should_hit(dealer: list[Card]) -> bool:
    """Дилер берёт до hard 17 и на soft 17; стоит на 18+ и hard 17."""
    total = hand_total(dealer)
    return total < 17 or (total == 17 and hand_is_soft(dealer))


def serialize_round(game: Round) -> dict:
    return {
        "deck": [list(c) for c in game.deck],
        "player": [list(c) for c in game.player],
        "dealer": [list(c) for c in game.dealer],
        "phase": game.phase,
        "outcome": game.outcome,
        "reason": game.reason,
        "rules_version": game.rules_version,
    }


def deserialize_round(data: dict) -> Round:
    return Round(
        deck=[tuple(c) for c in data["deck"]],
        player=[tuple(c) for c in data["player"]],
        dealer=[tuple(c) for c in data["dealer"]],
        phase=data["phase"],
        outcome=data.get("outcome"),
        reason=data.get("reason"),
        rules_version=data.get("rules_version", RULES_VERSION),
    )
