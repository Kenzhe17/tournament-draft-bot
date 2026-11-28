"""BLACKJACK 2.0 — тесты CV2-view: payload, dispatch, ownership, stale-кнопки.

Заменяет legacy tests/test_blackjack.py (старый free-play движок удалён).
"""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from games.blackjack_engine import OUT_WIN, Round
from storage.blackjack_store import StoredRound
from views import blackjack_view as bv


def _interaction(user_id=1, guild_id=42, channel_id=10):
    state = {"done": False}

    async def _defer(**_kw):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=channel_id,
        client=SimpleNamespace(get_channel=lambda c: None,
                               fetch_channel=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(side_effect=_defer),
            is_done=Mock(side_effect=lambda: state["done"]),
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
        # кастомные id кнопок содержат round_id и версию состояния
        ids = [getattr(c, "custom_id", "") for row in view.container.children
               for c in getattr(row, "children", [])]
        self.assertIn(f"bj2:hit:{stored.round_id}:v{stored.version}", ids)
        self.assertIn(f"bj2:stand:{stored.round_id}:v{stored.version}", ids)
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
        self.assertIn(f"bj2:again:{stored.round_id}:v{stored.version}", ids)
        self.assertIn(f"bj2:close:{stored.round_id}:v{stored.version}", ids)

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
            cid = f"bj2:{action}:{'a' * 32}:v3"
            m = rx.fullmatch(cid)
            self.assertIsNotNone(m)
            self.assertEqual(m.group("action"), action)
            self.assertEqual(m.group("rid"), "a" * 32)
            self.assertEqual(m.group("ver"), "3")
        self.assertIsNone(rx.fullmatch("bj2:hit:short"))
        self.assertIsNone(rx.fullmatch("bj2:hit:" + "a" * 32))  # без версии
        self.assertIsNone(rx.fullmatch("other:x:" + "a" * 32 + ":v1"))

    async def test_stranger_gets_ephemeral_no_state_change(self):
        stored = _stored()
        inter = _interaction(user_id=999)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action",
                          AsyncMock()) as ap:
            await bv.handle_round_action(inter, "hit", stored.round_id, 1)
        inter.followup.send.assert_awaited_once()  # deferred → followup
        self.assertTrue(
            inter.followup.send.call_args.kwargs["ephemeral"])
        ap.assert_not_awaited()

    async def test_owner_hit_updates_message(self):
        stored = _stored()
        updated = _stored()
        inter = _interaction(user_id=1)
        apply = AsyncMock(return_value=updated)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action", apply), \
             patch.object(bv.blackjack_store, "claim_card_render",
                          AsyncMock(return_value=True)), \
             patch.object(bv.blackjack_store, "finish_card_render",
                          AsyncMock()):
            await bv.handle_round_action(inter, "hit", stored.round_id, 1)
            apply.assert_awaited_once_with(
                stored.round_id, "hit", expected_version=1)
        inter.edit_original_response.assert_awaited_once()
        kwargs = inter.edit_original_response.call_args.kwargs
        self.assertIn("view", kwargs)
        self.assertIn("attachments", kwargs)  # PNG прикреплён

    async def test_stale_version_button_rejected(self):
        """Кнопка со старой версией → StaleVersion → ephemeral + refresh,
        состояние не меняется."""
        stored = _stored()
        inter = _interaction(user_id=1)
        apply = AsyncMock(side_effect=bv.StaleVersion("x"))
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action", apply), \
             patch.object(bv.blackjack_store, "claim_card_render",
                          AsyncMock(return_value=False)):
            await bv.handle_round_action(inter, "hit", stored.round_id, 0)
            apply.assert_awaited_once_with(
                stored.round_id, "hit", expected_version=0)
        inter.followup.send.assert_awaited_once()
        inter.edit_original_response.assert_not_awaited()

    async def test_stale_button_after_finish(self):
        stored = _stored()  # awaiting_player False? status settled
        stored.status = "settled"
        stored.game.phase = "done"
        stored.game.outcome = OUT_WIN
        inter = _interaction(user_id=1)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action",
                          AsyncMock(side_effect=bv.RoundNotActive("x"))), \
             patch.object(bv.blackjack_store, "claim_card_render",
                          AsyncMock(return_value=False)):
            await bv.handle_round_action(inter, "hit", stored.round_id, 1)
        inter.followup.send.assert_awaited_once()
        self.assertTrue(
            inter.followup.send.call_args.kwargs["ephemeral"])

    async def test_close_edits_message_without_state_change(self):
        stored = _stored(phase="done", outcome=OUT_WIN)
        stored.card_closed = False
        inter = _interaction(user_id=1)
        mark_closed = AsyncMock()
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "mark_card_closed",
                          mark_closed), \
             patch.object(bv.blackjack_store, "claim_card_render",
                          AsyncMock(return_value=True)), \
             patch.object(bv.blackjack_store, "finish_card_render",
                          AsyncMock()):
            await bv.handle_round_action(inter, "close", stored.round_id, 1)
        mark_closed.assert_awaited_once_with(stored.round_id)
        inter.edit_original_response.assert_awaited_once()

    async def test_close_blocked_when_card_transferred(self):
        """§6: «Закрыть» после передачи карточки новой раздаче не
        должно стереть чужую публичную карту."""
        stored = _stored(phase="done", outcome=OUT_WIN, message_id=777)
        transferred = _stored(phase="done", outcome=OUT_WIN,
                              message_id=None)  # detach_message уже отработал
        inter = _interaction(user_id=1)
        inter.message = SimpleNamespace(id=777)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(side_effect=[stored, transferred])):
            await bv.handle_round_action(inter, "close", stored.round_id, 1)
        inter.edit_original_response.assert_not_awaited()
        inter.followup.send.assert_awaited_once()
        self.assertTrue(
            inter.followup.send.call_args.kwargs["ephemeral"])

    async def test_play_edit_skipped_when_card_transferred(self):
        """§6: hit/stale-edit не перезаписывает карточку, уже
        принадлежащую другой раздаче."""
        stored = _stored()
        updated = _stored()  # message_id=777 у updated
        inter = _interaction(user_id=1)
        inter.message = SimpleNamespace(id=555)  # карточка уже не наша
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "apply_action",
                          AsyncMock(return_value=updated)):
            await bv.handle_round_action(inter, "hit", stored.round_id, 1)
        inter.edit_original_response.assert_not_awaited()
        inter.followup.send.assert_awaited_once()

    async def test_close_rejected_for_active_round(self):
        """§7: «Закрыть» возможно только для завершённой раздачи —
        серверная проверка, не только отсутствие кнопки в UI."""
        stored = _stored()  # status='active'
        inter = _interaction(user_id=1)
        inter.message = SimpleNamespace(id=777)
        mark_closed = AsyncMock()
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bv.blackjack_store, "mark_card_closed",
                          mark_closed):
            await bv.handle_round_action(inter, "close", stored.round_id, 1)
        mark_closed.assert_not_awaited()
        inter.edit_original_response.assert_not_awaited()
        inter.followup.send.assert_awaited_once()

    async def test_again_blocked_after_close(self):
        """§7: устаревшие Again/Change на закрытой карточке не создают
        новую раздачу — сервер отклоняет по card_closed."""
        stored = _stored(phase="done", outcome=OUT_WIN)
        stored.card_closed = True
        inter = _interaction(user_id=1)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)):
            await bv.handle_round_action(inter, "again", stored.round_id, 1)
            await bv.handle_round_action(inter, "change", stored.round_id, 1)
        for call in inter.followup.send.call_args_list:
            self.assertNotIsInstance(
                call.kwargs.get("view"), bv.ConfirmStakeView)

    async def test_again_creates_confirm_for_new_round(self):
        """«Сыграть ещё» → ephemeral ConfirmStakeView; новая раздача
        получит НОВОЕ публичное сообщение (reuse упразднён)."""
        stored = _stored(phase="done", outcome=OUT_WIN, message_id=None)
        inter = _interaction(user_id=1)
        with patch.object(bv.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)):
            await bv.handle_round_action(inter, "again", stored.round_id, 1)
        view = inter.followup.send.call_args.kwargs["view"]
        self.assertIsInstance(view, bv.ConfirmStakeView)
        self.assertFalse(hasattr(view, "reuse"))


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
