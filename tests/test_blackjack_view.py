"""BLACKJACK 2.0 — тесты CV2-view: payload, dispatch, ownership, stale-кнопки.

Заменяет legacy tests/test_blackjack.py (старый free-play движок удалён).
"""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from games.blackjack_engine import OUT_WIN, Round
from storage.blackjack_store import StoredRound
from views import blackjack_view as bv


def _interaction(user_id=1, guild_id=42, channel_id=10):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=channel_id,
        client=SimpleNamespace(get_channel=lambda c: None,
                               fetch_channel=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _stored(phase="player", outcome=None, stake=500, rid="a" * 32,
            message_id=777, deadline=None):
    game = Round(
        deck=[("2", "♣"), ("3", "♦")],
        player=[("10", "♠"), ("9", "♥")],
        dealer=[("10", "♦"), ("8", "♣")],
        phase=phase,
        outcome=outcome,
        reason="higher" if outcome else None,
    )
    return StoredRound(
        round_id=rid, guild_id=42, user_id=1, channel_id=10,
        message_id=message_id, stake=stake, game=game,
        status="settled" if phase == "done" else "active",
        version=1, deadline=deadline, outcome=outcome,
        reason=game.reason, payout=None,
    )


def _walk(item):
    yield item
    for child in getattr(item, "children", []) or []:
        yield from _walk(child)


def _texts(view) -> str:
    """Весь текстовый контент + подписи кнопок контейнера."""
    out = []
    for item in view.container.children:
        for node in _walk(item):
            content = getattr(node, "content", None)
            if content:
                out.append(str(content))
            label = getattr(node, "label", None)
            if label:
                out.append(str(label))
    return "\n".join(out)


class BuildViewTests(unittest.TestCase):
    def test_active_round_shows_cards_totals_buttons(self):
        stored = _stored(deadline=datetime.now(UTC) + timedelta(seconds=60))
        view, file = bv.build_game_view(stored)
        text = _texts(view)
        self.assertIn("BLACKJACK", text)
        self.assertIn("Дилер", text)
        self.assertIn("Игрок", text)
        self.assertIn("Ставка", text)
        self.assertIn("Взять карту", text)
        self.assertIn("Стоп", text)
        self.assertIn("авто-Stand", text)
        # кастомные id кнопок содержат round_id
        ids = [getattr(c, "custom_id", "") for row in view.container.children
               for c in getattr(row, "children", [])]
        self.assertIn(f"bj2:hit:{stored.round_id}", ids)
        self.assertIn(f"bj2:stand:{stored.round_id}", ids)
        # PNG прилагается
        self.assertIsNotNone(file)
        self.assertTrue(any(
            isinstance(c, discord.ui.MediaGallery)
            for c in view.container.children))

    def test_done_round_shows_result_and_replay_buttons(self):
        stored = _stored(phase="done", outcome=OUT_WIN, stake=500)
        view, _file = bv.build_game_view(stored)
        text = _texts(view)
        self.assertIn("Выплата", text)
        self.assertIn("Изменение", text)
        self.assertIn("Сыграть ещё", text)
        self.assertIn("Изменить ставку", text)
        self.assertIn("Закрыть", text)
        ids = [getattr(c, "custom_id", "") for row in view.container.children
               for c in getattr(row, "children", [])]
        self.assertIn(f"bj2:again:{stored.round_id}", ids)
        self.assertIn(f"bj2:close:{stored.round_id}", ids)

    def test_hidden_dealer_card(self):
        stored = _stored()
        view, _ = bv.build_game_view(stored)
        text = _texts(view)
        # дилер показывает только первую карту + скрытые
        self.assertIn("?", text)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_matches_custom_ids(self):
        import re
        rx = re.compile(bv._ACTION_RE)
        for action in ("hit", "stand", "again", "change", "close"):
            cid = f"bj2:{action}:{'a' * 32}"
            m = rx.fullmatch(cid)
            self.assertIsNotNone(m)
            self.assertEqual(m.group("action"), action)
            self.assertEqual(m.group("rid"), "a" * 32)
        self.assertIsNone(rx.fullmatch("bj2:hit:short"))
        self.assertIsNone(rx.fullmatch("other:x:" + "a" * 32))

    async def test_stranger_gets_ephemeral_no_state_change(self):
        stored = _stored()
        inter = _interaction(user_id=999)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action",
                          AsyncMock()) as ap:
            await bv.handle_round_action(inter, "hit", stored.round_id)
        inter.response.send_message.assert_awaited_once()
        self.assertTrue(
            inter.response.send_message.call_args.kwargs["ephemeral"])
        ap.assert_not_awaited()

    async def test_owner_hit_updates_message(self):
        stored = _stored()
        updated = _stored()
        inter = _interaction(user_id=1)
        apply = AsyncMock(return_value=updated)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action", apply):
            await bv.handle_round_action(inter, "hit", stored.round_id)
            apply.assert_awaited_once_with(stored.round_id, "hit")
        inter.response.edit_message.assert_awaited_once()
        kwargs = inter.response.edit_message.call_args.kwargs
        self.assertIn("view", kwargs)
        self.assertIn("attachments", kwargs)  # PNG прикреплён

    async def test_stale_button_after_finish(self):
        stored = _stored()  # awaiting_player False? status settled
        stored.status = "settled"
        stored.game.phase = "done"
        stored.game.outcome = OUT_WIN
        inter = _interaction(user_id=1)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action",
                          AsyncMock(side_effect=bv.RoundNotActive("x"))):
            await bv.handle_round_action(inter, "hit", stored.round_id)
        inter.response.send_message.assert_awaited_once()
        self.assertTrue(
            inter.response.send_message.call_args.kwargs["ephemeral"])

    async def test_close_edits_message_without_state_change(self):
        stored = _stored(phase="done", outcome=OUT_WIN)
        inter = _interaction(user_id=1)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)):
            await bv.handle_round_action(inter, "close", stored.round_id)
        inter.response.edit_message.assert_awaited_once()


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_bet_menu_owner_only(self):
        view = bv.BetMenuView(42, 1, 10, 5000)
        stranger = _interaction(user_id=2)
        ok = await view.interaction_check(stranger)
        self.assertFalse(ok)
        self.assertTrue(stranger.response.send_message.call_args.kwargs["ephemeral"])
        owner = _interaction(user_id=1)
        self.assertTrue(await view.interaction_check(owner))

    async def test_menu_shows_rules_before_stake(self):
        view = bv.BetMenuView(42, 1, 10, 5000)
        text = _texts(view)
        self.assertIn("Правила", text)
        self.assertIn("равные очки", text.lower())
        self.assertIn("500", text)  # пресеты

    async def test_confirm_modal_rejects_non_integer(self):
        modal = bv.BetModal(42, 1, 10)
        modal.stake = SimpleNamespace(value="abc")
        inter = _interaction(user_id=1)
        await modal.on_submit(inter)
        inter.response.send_message.assert_awaited_once()
        self.assertTrue(
            inter.response.send_message.call_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
