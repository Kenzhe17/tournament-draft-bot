"""Правила блэкджека и переходы карточки Discord."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from games.blackjack import BlackjackRound, BlackjackView, hand_total, start_blackjack


def deal(*ranks: str) -> BlackjackRound:
    suits = ("♠", "♥", "♦", "♣")
    return BlackjackRound.deal(
        tuple((rank, suits[index % len(suits)]) for index, rank in enumerate(ranks))
    )


def interaction(user_id: int = 1, channel_id: int = 10):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=AsyncMock(),
        edit_original_response=AsyncMock(),
        guild_id=42,
        channel_id=channel_id,
    )


class BlackjackRulesTests(unittest.TestCase):
    def test_multiple_aces(self):
        self.assertEqual(hand_total((("A", "♠"), ("A", "♥"), ("9", "♦"))), 21)
        self.assertEqual(hand_total((("A", "♠"), ("A", "♥"), ("K", "♦"), ("8", "♣"))), 20)

    def test_opening_blackjack_resolves_immediately(self):
        self.assertEqual(deal("A", "A", "K", "Q").result, "Ничья — блэкджек у обоих.")
        self.assertEqual(deal("A", "8", "K", "9").result, "Блэкджек! Вы выиграли.")
        self.assertEqual(deal("7", "A", "K", "Q").result, "У дилера блэкджек. Вы проиграли.")

    def test_player_bust_does_not_draw_for_dealer(self):
        game = deal("10", "4", "9", "6", "5")
        result = game.hit()
        self.assertEqual(result.result, "Перебор. Вы проиграли.")
        self.assertEqual(game.dealer, result.dealer)

    def test_hit_to_21_resolves_against_dealer(self):
        result = deal("10", "9", "6", "7", "5", "5").hit()
        self.assertEqual(result.result, "Ничья.")
        self.assertEqual(hand_total(result.dealer), 21)

    def test_dealer_draws_below_17_and_stands_on_soft_17(self):
        self.assertEqual(deal("K", "8", "Q", "8", "8").stand().result, "У дилера перебор. Вы выиграли.")
        result = deal("K", "A", "7", "6", "4").stand()
        self.assertEqual(result.result, "Ничья.")
        self.assertEqual(len(result.dealer), 2)


class BlackjackViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_checks_configured_channel(self):
        wrong_channel = interaction(channel_id=11)
        correct_channel = interaction()
        with patch("games.blackjack.GAMES_CHANNEL_ID", 10), patch(
            "games.blackjack.guild_allowed", return_value=True
        ):
            await start_blackjack(wrong_channel)
            await start_blackjack(correct_channel)
        self.assertTrue(wrong_channel.response.send_message.call_args.kwargs["ephemeral"])
        view = correct_channel.response.send_message.call_args.kwargs["view"]
        self.assertEqual(view.player_id, 1)

    async def test_only_the_player_can_act(self):
        view = BlackjackView(interaction(), 1, deal("5", "8", "8", "9", "3"))
        stranger = interaction(user_id=2)
        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(stranger.response.send_message.call_args.kwargs["ephemeral"])

    async def test_concurrent_clicks_commit_one_move(self):
        source = interaction()
        view = BlackjackView(source, 1, deal("5", "8", "8", "9", "3"))
        first = interaction()
        second = interaction()
        await asyncio.gather(view.hit(first), view.stand(second))
        next_view = first.response.edit_message.call_args.kwargs["view"]
        self.assertEqual(hand_total(next_view.game.player), 16)
        second.response.edit_message.assert_not_awaited()
        second.response.send_message.assert_awaited_once()
        await view.on_timeout()
        source.edit_original_response.assert_not_awaited()

    async def test_timeout_removes_buttons(self):
        source = interaction()
        view = BlackjackView(source, 1, deal("5", "8", "8", "9", "3"))
        await view.on_timeout()
        expired_view = source.edit_original_response.call_args.kwargs["view"]
        self.assertFalse(expired_view.is_dispatchable())
