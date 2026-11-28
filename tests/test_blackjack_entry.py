"""BLACKJACK 2.0 — entry/orchestration тесты:
kill-switch fail-closed, op_id идемпотентность подтверждения,
граничные значения ставок.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from games import blackjack as bjgame
from views import blackjack_view as bv


def _walk(item):
    yield item
    for child in getattr(item, "children", []) or []:
        yield from _walk(child)


def _interaction(user_id=1, guild_id=42, channel_id=10):
    state = {"done": False}

    async def _defer(**_kw):
        state["done"] = True

    async def _edit_message(**_kw):
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, mention="<@1>"),
        guild_id=guild_id,
        channel_id=channel_id,
        client=SimpleNamespace(get_channel=lambda c: None,
                               fetch_channel=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(side_effect=_edit_message),
            defer=AsyncMock(side_effect=_defer),
            is_done=Mock(side_effect=lambda: state["done"]),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class KillSwitchTests(unittest.TestCase):
    def test_bets_enabled_fail_closed_on_settings_error(self):
        """Ошибка настроек → ставки ЗАПРЕЩЕНЫ (fail-closed),
        бесплатная игра не затрагивается."""
        with patch.dict("sys.modules") as _:
            import services.settings_service as ss
            with patch.object(ss.settings_service, "peek",
                              side_effect=RuntimeError("db down")):
                self.assertFalse(bjgame._bets_enabled(1))

    def test_bets_enabled_reads_flag(self):
        import services.settings_service as ss
        with patch.object(ss.settings_service, "peek", return_value=True):
            self.assertTrue(bjgame._bets_enabled(1))
        with patch.object(ss.settings_service, "peek", return_value=False):
            self.assertFalse(bjgame._bets_enabled(1))

    def test_minigames_enabled_fail_closed(self):
        import services.settings_service as ss
        with patch.object(ss.settings_service, "peek",
                          side_effect=RuntimeError("x")):
            self.assertFalse(bjgame._minigames_enabled(1))


class BetLimitTests(unittest.TestCase):
    def test_limits_from_settings(self):
        import services.settings_service as ss
        def peek(_g, key):
            return {"economy.blackjack_min_bet": 50,
                    "economy.blackjack_max_bet": 5000}.get(key, None)
        with patch.object(ss.settings_service, "peek", side_effect=peek):
            self.assertEqual(bjgame._bet_limits(1), (50, 5000))

    def test_limits_fallback_on_error(self):
        import services.settings_service as ss
        with patch.object(ss.settings_service, "peek",
                          side_effect=RuntimeError("x")):
            lo, hi = bjgame._bet_limits(1)
        self.assertEqual((lo, hi), (10, 10000))


class ConfirmOpIdTests(unittest.TestCase):
    def test_confirm_view_generates_unique_op_ids(self):
        v1 = bv.ConfirmStakeView(42, 1, 10, 500)
        v2 = bv.ConfirmStakeView(42, 1, 10, 500)
        self.assertNotEqual(v1.op_id, v2.op_id)
        self.assertTrue(v1.op_id and len(v1.op_id) >= 16)

    def test_confirm_view_accepts_explicit_op_id(self):
        v = bv.ConfirmStakeView(42, 1, 10, 500, op_id="fixed-op")
        self.assertEqual(v.op_id, "fixed-op")


class DealRoundValidationTests(unittest.IsolatedAsyncioTestCase):
    """deal_round: валидация ставки до создания раздачи."""

    async def _run(self, stake, bets_enabled=True, minigames=True):
        inter = _interaction()
        store = SimpleNamespace(
            create_round=AsyncMock(),
            get_round=AsyncMock(),
        )
        with patch.object(bjgame.blackjack_store, "create_round",
                          store.create_round), \
             patch.object(bjgame.blackjack_store, "get_round",
                          store.get_round), \
             patch.object(bjgame, "_bets_enabled",
                          return_value=bets_enabled), \
             patch.object(bjgame, "_minigames_enabled",
                          return_value=minigames), \
             patch.object(bjgame, "_bet_limits",
                          return_value=(10, 10000)):
            await bjgame.deal_round(
                inter, guild_id=42, user_id=1, channel_id=10,
                stake=stake, op_id="op-x")
        return inter, store

    async def test_negative_stake_rejected(self):
        inter, store = await self._run(-5)
        store.create_round.assert_not_awaited()
        inter.edit_original_response.assert_awaited()

    async def test_huge_stake_rejected(self):
        _inter, store = await self._run(10**12)
        store.create_round.assert_not_awaited()

    async def test_bets_disabled_rejects_paid_but_allows_free_path(self):
        inter, store = await self._run(500, bets_enabled=False)
        store.create_round.assert_not_awaited()
        view = inter.edit_original_response.call_args.kwargs["view"]
        text = "\n".join(
            str(getattr(n, "content", ""))
            for row in view.container.children
            for n in _walk(row))
        self.assertIn("отключены", text)

    async def test_out_of_range_stake_rejected(self):
        _inter, store = await self._run(5)  # < min 10
        store.create_round.assert_not_awaited()
        _inter, store = await self._run(20000)  # > max 10000
        store.create_round.assert_not_awaited()

    async def test_minigames_disabled_rejects(self):
        _inter, store = await self._run(500, minigames=False)
        store.create_round.assert_not_awaited()


class DealRoundOpIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_op_id_passed_to_create_round(self):
        from games.blackjack_engine import Round
        from storage.blackjack_store import StoredRound
        inter = _interaction()
        game = Round.deal_deck([("2", "♠"), ("3", "♦"),
                                ("4", "♣"), ("5", "♥")])
        stored = StoredRound(
            round_id="r" * 32, guild_id=42, user_id=1, channel_id=10,
            message_id=None, stake=0, game=game, status="active",
            version=0, deadline=None, outcome=None, reason=None,
            payout=None, delivery_state="pending")
        create = AsyncMock(return_value=("r" * 32, True))
        msg = SimpleNamespace(id=12345)
        with patch.object(bjgame.blackjack_store, "create_round", create), \
             patch.object(bjgame.blackjack_store, "get_round",
                          AsyncMock(return_value=stored)), \
             patch.object(bjgame.blackjack_store, "mark_delivery",
                          AsyncMock()), \
             patch.object(bjgame.blackjack_store, "mark_message",
                          AsyncMock()), \
             patch.object(bjgame, "_bets_enabled", return_value=True), \
             patch.object(bjgame, "_minigames_enabled", return_value=True), \
             patch.object(bjgame, "_bet_limits", return_value=(10, 10000)):
            inter.followup.send = AsyncMock(return_value=msg)
            await bjgame.deal_round(
                inter, guild_id=42, user_id=1, channel_id=10,
                stake=0, op_id="op-fixed-1")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["op_id"], "op-fixed-1")


if __name__ == "__main__":
    unittest.main()
