"""Регрессия: RoomCollectView._on_cancel — ранний defer до
cancel_rooms_session (исправление discord 10062 Unknown interaction),
безопасность отмены и идемпотентность."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import cogs.ocr_upload as ou


class _Resp:
    """interaction.response: defer/is_done/send_message/edit_original."""

    def __init__(self, events=None, fail_defer=False):
        self.events = events if events is not None else []
        self.fail_defer = fail_defer
        self.deferred = False
        self.messages = []

    async def defer(self, **kw):
        if self.fail_defer:
            raise RuntimeError("interaction expired")
        self.deferred = True
        self.events.append("defer")

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def send_message(self, text=None, **kw):
        self.messages.append(text)
        self.events.append("send_message")


class _Followup:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append(text)
        self.events.append("followup")


def _interaction(user=None, events=None, fail_defer=False):
    user = user or SimpleNamespace(
        id=42, display_name="Admin", name="admin",
        guild_permissions=SimpleNamespace(administrator=True),
        mention="<@42>",
    )
    resp = _Resp(events=events, fail_defer=fail_defer)
    edits = []

    async def edit_original_response(content=None, **kw):
        edits.append(content)
        if events is not None:
            events.append("edit_original")

    return SimpleNamespace(
        response=resp,
        followup=_Followup(events=events),
        edit_original_response=edit_original_response,
        _edits=edits,
        user=user,
        guild=SimpleNamespace(id=1, name="g"),
        channel=SimpleNamespace(id=2, name="c"),
    )


def _tournament(session=None, with_progress=False):
    key = ou._session_key("qualifier", 0)
    if session is None:
        session = {
            "match_type": "qualifier",
            "match_index": 0,
            "mode": "rooms",
            "status": "collecting",
            "rooms_expected": 1,
            "rooms_received": 0,
            "rooms": [],
            "gen": 1,
            "uploaded_by": 42,
            "teams": ["A", "B"],
        }
    if with_progress:
        session["progress_channel_id"] = 77
        session["progress_message_id"] = 88
        session["rooms"] = [
            {"room_index": 1, "message_ids": [99], "message_id": 98}
        ]
    return SimpleNamespace(
        channel_id=2, id=9, ocr_sessions={key: session},
        processed_elo_matches=set(),
        mm_search_channel_id=0,
        phase=SimpleNamespace(value="qualifiers"),
    )


def _guild(fetch_channel_impl=None):
    async def _fetch(cid):
        if fetch_channel_impl:
            return await fetch_channel_impl(cid)
        raise RuntimeError("no channel")

    return SimpleNamespace(
        id=1, name="g",
        get_channel=lambda cid: None,
        fetch_channel=_fetch,
    )


@pytest.fixture
def store_env(monkeypatch):
    """store с get/set; tournament подставляется тестом."""
    holder = {}

    def _get(g, c):
        return holder.get("tournament")

    def _set(t):
        holder["tournament"] = t

    monkeypatch.setattr(
        ou, "store", SimpleNamespace(get=_get, set=_set)
    )
    return holder


class TestCancelDeferOrdering:
    @pytest.mark.asyncio
    async def test_defer_before_slow_cancel(self, monkeypatch, store_env):
        """defer() вызывается ДО cancel_rooms_session — даже если
        отмена длится >3с, interaction уже отвечен (нет 10062)."""
        tournament = _tournament()
        store_env["tournament"] = tournament
        events = []

        async def slow_cancel(guild, user, t, mt, mi):
            events.append("cancel_start")
            await asyncio.sleep(0.05)  # имитация долгой отмены
            events.append("cancel_end")
            return "ok"

        monkeypatch.setattr(ou, "cancel_rooms_session", slow_cancel)
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction(events=events)
        await view._on_cancel(inter)
        assert events[0] == "defer"
        assert events.index("defer") < events.index("cancel_start")
        assert inter.response.deferred is True
        # Ответ ушёл через edit_original_response
        assert any("отменён" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_defer_failure_still_replies(self, monkeypatch, store_env):
        """Если defer сам упал (interaction истёк) — отмена всё равно
        выполняется, а пользователь получает followup."""
        tournament = _tournament()
        store_env["tournament"] = tournament
        events = []

        async def fake_cancel(guild, user, t, mt, mi):
            return "ok"

        monkeypatch.setattr(ou, "cancel_rooms_session", fake_cancel)
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction(events=events, fail_defer=True)
        await view._on_cancel(inter)
        assert any("отменён" in str(s) for s in inter.followup.sent)


class TestCancelOutcomes:
    @pytest.mark.asyncio
    async def test_successful_cancel(self, store_env):
        tournament = _tournament()
        store_env["tournament"] = tournament
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction()
        await view._on_cancel(inter)
        key = ou._session_key("qualifier", 0)
        assert key not in tournament.ocr_sessions
        assert any("отменён" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_forbidden_non_admin(self, store_env):
        tournament = _tournament()
        store_env["tournament"] = tournament
        user = SimpleNamespace(
            id=7, display_name="U", name="u",
            guild_permissions=SimpleNamespace(administrator=False),
        )
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction(user=user)
        await view._on_cancel(inter)
        key = ou._session_key("qualifier", 0)
        assert key in tournament.ocr_sessions  # не тронута
        assert any("администратор" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_session_already_completed(self, store_env):
        tournament = _tournament(
            session={
                "match_type": "qualifier", "match_index": 0,
                "mode": "rooms", "status": "completed",
                "gen": 1,
            }
        )
        store_env["tournament"] = tournament
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction()
        await view._on_cancel(inter)
        key = ou._session_key("qualifier", 0)
        assert key in tournament.ocr_sessions
        assert any("завершена" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_no_session(self, store_env):
        tournament = _tournament(session={})
        tournament.ocr_sessions = {}
        store_env["tournament"] = tournament
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction()
        await view._on_cancel(inter)
        assert any("нет" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_tournament_missing(self, store_env):
        store_env["tournament"] = None
        view = ou.RoomCollectView(1, 2, SimpleNamespace(), "qualifier", 0, 1)
        inter = _interaction()
        await view._on_cancel(inter)
        assert any("не найден" in str(e) for e in inter._edits)

    @pytest.mark.asyncio
    async def test_double_click_idempotent(self, store_env):
        """Два одновременных нажатия: вторая отмена видит отсутствие
        сессии под _room_lock — без двойного side-effect."""
        tournament = _tournament()
        store_env["tournament"] = tournament
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        i1 = _interaction()
        i2 = _interaction(user=SimpleNamespace(
            id=43, display_name="A2", name="a2",
            guild_permissions=SimpleNamespace(administrator=True),
            mention="<@43>",
        ))
        await asyncio.gather(view._on_cancel(i1), view._on_cancel(i2))
        texts = [str(e) for e in i1._edits] + [str(e) for e in i2._edits]
        assert sum("Комплект скриншотов отменён" in t for t in texts) == 1

    @pytest.mark.asyncio
    async def test_discord_api_error_in_cleanup_still_ok(self, store_env):
        """Ошибка Discord API при гашении сообщений комнаты не
        ломает отмену — пользователь всё равно получает «отменён»."""
        tournament = _tournament(with_progress=True)
        store_env["tournament"] = tournament

        async def _fetch_msg(mid):
            raise RuntimeError("discord 500")

        prog_channel = SimpleNamespace(fetch_message=_fetch_msg)

        def _get_channel(cid):
            return prog_channel if cid == 77 else None

        guild = _guild()
        guild.get_channel = _get_channel
        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        inter = _interaction()
        inter.guild = guild
        await view._on_cancel(inter)
        key = ou._session_key("qualifier", 0)
        assert key not in tournament.ocr_sessions
        assert any("отменён" in str(e) for e in inter._edits)
