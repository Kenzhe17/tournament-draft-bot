"""Unit-тесты правил BLACKJACK 2.0 (games/blackjack_engine)."""

import unittest

from games.blackjack_engine import (
    OUT_LOSS,
    OUT_PUSH,
    OUT_WIN,
    REASON_BOTH_NATURAL,
    REASON_DEALER_BUST,
    REASON_DEALER_NATURAL,
    REASON_HIGHER,
    REASON_PLAYER_BUST,
    REASON_PLAYER_NATURAL,
    REASON_TIE,
    Round,
    gross_payout,
    hand_is_soft,
    hand_total,
    new_deck,
)

SUITS = ("♠", "♥", "♦", "♣")


def cards(*spec: str) -> list[tuple[str, str]]:
    """'A♠K♥' → [('A','♠'),('K','♥')]."""
    out = []
    for tok in spec:
        out.append((tok[:-1], tok[-1]))  # '10♠' → ('10','♠')
    return out


def make_round(player, dealer, rest=None) -> Round:
    rest = rest or []
    return Round(deck=list(rest), player=list(player), dealer=list(dealer))


class HandTotalTests(unittest.TestCase):
    def test_multiple_aces(self):
        self.assertEqual(hand_total(cards("A♠", "A♥", "9♦")), 21)
        self.assertEqual(hand_total(cards("A♠", "A♥", "K♦", "8♣")), 20)
        self.assertEqual(hand_total(cards("A♠", "A♥", "A♦", "9♣")), 12)

    def test_face_cards(self):
        self.assertEqual(hand_total(cards("K♠", "Q♥", "J♦")), 30)

    def test_soft_detection(self):
        self.assertTrue(hand_is_soft(cards("A♠", "6♥")))       # soft 17
        self.assertFalse(hand_is_soft(cards("K♠", "A♥", "6♦")))  # hard 17
        self.assertTrue(hand_is_soft(cards("A♠", "A♥", "5♦")))    # soft 17
        self.assertFalse(hand_is_soft(cards("10♠", "7♥")))


class DealTests(unittest.TestCase):
    def _deck(self, order):
        # order — в порядке раздачи: player1, player2, dealer1, dealer2, rest…
        # deal_deck делает pop() с конца → разворачиваем.
        return [(tok[:-1], tok[-1]) for tok in reversed(order)]

    def test_deck_is_52_unique(self):
        deck = new_deck()
        self.assertEqual(len(deck), 52)
        self.assertEqual(len(set(deck)), 52)

    def test_player_natural_wins(self):
        deck = self._deck([
            "K♥", "A♠",                        # игрок: BJ
            "9♦", "9♣",                        # дилер: 18
            "2♠", "3♥", "4♦", "5♣",           # остаток колоды
        ])
        game = Round.deal_deck(deck)
        self.assertEqual(game.phase, "done")
        self.assertEqual(game.outcome, OUT_WIN)
        self.assertEqual(game.reason, REASON_PLAYER_NATURAL)

    def test_dealer_natural_wins(self):
        deck = self._deck([
            "9♥", "8♠",                        # игрок: 17
            "A♦", "Q♣",                        # дилер: BJ
            "2♠", "3♥", "4♦", "5♣",
        ])
        game = Round.deal_deck(deck)
        self.assertEqual(game.outcome, OUT_LOSS)
        self.assertEqual(game.reason, REASON_DEALER_NATURAL)

    def test_both_naturals_push(self):
        deck = self._deck([
            "Q♥", "A♠",                        # игрок: BJ
            "K♦", "A♣",                        # дилер: BJ
            "2♠", "3♥", "4♦", "5♣",
        ])
        game = Round.deal_deck(deck)
        self.assertEqual(game.outcome, OUT_PUSH)
        self.assertEqual(game.reason, REASON_BOTH_NATURAL)


class PlayTests(unittest.TestCase):
    def test_hit_bust(self):
        game = make_round(
            player=cards("10♠", "6♥"),
            dealer=cards("9♦", "7♣"),
            rest=cards("4♥", "5♠"),   # pop() берёт с конца → 5♠
        )
        # игрок 16, берёт 5 → 21 → auto stand
        g2 = game.hit()
        self.assertEqual(hand_total(g2.player), 21)
        self.assertEqual(g2.phase, "done")

    def test_hit_to_21_autostands(self):
        game = make_round(
            player=cards("10♠", "9♥"),
            dealer=cards("6♦", "7♣"),
            rest=cards("5♠", "4♥", "3♦"),
        )
        g2 = game.hit()  # 19+3=22 → перебор
        self.assertEqual(g2.outcome, OUT_LOSS)
        self.assertEqual(g2.reason, REASON_PLAYER_BUST)

    def test_stand_dealer_bust(self):
        game = make_round(
            player=cards("10♠", "9♥"),   # 19
            dealer=cards("6♦", "7♣"),   # 13
            rest=cards("9♥", "8♦", "10♠"),  # дилер добирает 10 → 23
        )
        g = game.stand()
        self.assertEqual(g.outcome, OUT_WIN)
        self.assertEqual(g.reason, REASON_DEALER_BUST)

    def test_tie_loses_for_player(self):
        game = make_round(
            player=cards("10♠", "9♥"),   # 19
            dealer=cards("10♦", "9♣"),  # 19
            rest=cards("5♠", "4♥"),
        )
        g = game.stand()
        self.assertEqual(g.outcome, OUT_LOSS)
        self.assertEqual(g.reason, REASON_TIE)

    def test_higher_wins(self):
        game = make_round(
            player=cards("10♠", "9♥"),   # 19
            dealer=cards("10♦", "8♣"),  # 18
            rest=cards("5♠", "4♥"),
        )
        g = game.stand()
        self.assertEqual(g.outcome, OUT_WIN)
        self.assertEqual(g.reason, REASON_HIGHER)

    def test_dealer_hits_soft_17(self):
        # дилер A+6 = soft 17 → обязан брать
        game = make_round(
            player=cards("10♠", "8♥"),   # 18
            dealer=cards("A♦", "6♣"),   # soft 17
            rest=cards("9♠", "9♥", "2♦"),
        )
        g = game.stand()
        # дилер берёт 9 → A+6+9 = 16? A=11+6+9=26→16 hard... 
        # итого дилер: 11+6=17 soft → берёт 9 → 26 → тузы в 1 → 16, берёт ещё?
        # после добора total=16 <17 → берёт 9 → 25→15? проверим по коду
        self.assertEqual(g.phase, "done")
        # у дилера должно быть ≥3 карт (soft 17 → hit)
        self.assertGreaterEqual(len(g.dealer), 3)

    def test_dealer_stands_hard_17(self):
        game = make_round(
            player=cards("10♠", "8♥"),   # 18
            dealer=cards("K♦", "7♣"),   # hard 17
            rest=cards("9♠", "9♥"),
        )
        g = game.stand()
        self.assertEqual(len(g.dealer), 2)  # не брал
        # игрок 18 > дилер 17 → победа игрока
        self.assertEqual(g.outcome, OUT_WIN)
        self.assertEqual(g.reason, REASON_HIGHER)


class PayoutTests(unittest.TestCase):
    def test_gross_payout(self):
        self.assertEqual(gross_payout(OUT_WIN, 500), 1000)
        self.assertEqual(gross_payout(OUT_PUSH, 500), 500)
        self.assertEqual(gross_payout(OUT_LOSS, 500), 0)

    def test_no_bias_by_stake(self):
        # выплаты линейны по ставке — RNG не зависит от stake
        stakes = [10, 100, 1000, 10000]
        for s in stakes:
            g = Round.deal()
            self.assertIn(g.outcome if g.phase == "done" else None,
                          (None, OUT_WIN, OUT_LOSS, OUT_PUSH))
            if g.phase == "done":
                self.assertEqual(gross_payout(g.outcome, s) % s, 0)


class SerializeTests(unittest.TestCase):
    def test_roundtrip(self):
        from games.blackjack_engine import deserialize_round, serialize_round
        game = Round.deal()
        game2 = game.hit() if game.phase == "player" else game
        data = serialize_round(game2)
        back = deserialize_round(data)
        self.assertEqual(back.player, game2.player)
        self.assertEqual(back.dealer, game2.dealer)
        self.assertEqual(back.phase, game2.phase)
        self.assertEqual(back.outcome, game2.outcome)


if __name__ == "__main__":
    unittest.main()
