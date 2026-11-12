"""Маршрутизация OCR-сообщений обычных турниров:

- отдельный канал OCR_MAPPING_CHANNEL_ID для rooms-сессий;
- MM-матчи остаются в канале матча;
- пинги капитанов в collect-сообщении;
- восстановление канала из сохранённого ocr_channel_id.
"""

from types import SimpleNamespace

import pytest

import cogs.ocr_upload as up
from config import OCR_MAPPING_CHANNEL_ID


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    monkeypatch.setattr(up.store, "get", lambda g, c: None)
    monkeypatch.setattr(up.store, "set", lambda t: None)


class _Msg:
    def __init__(self, mid=1):
        self.id = mid


class _Chan:
    """Минимальный текстовый канал с записью send/edit."""

    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.edits = []
        self._mid = 100

    async def send(self, content=None, view=None, files=None,
                   allowed_mentions=None, **kw):
        self.sent.append({
            "content": content, "view": view,
            "allowed_mentions": allowed_mentions,
        })
        self._mid += 1
        return _Msg(self._mid)

    async def fetch_message(self, mid):
        ch = self

        class _M:
            id = int(mid)

            async def edit(self, content=None, view=None,
                           allowed_mentions=None, **kw):
                ch.edits.append({
                    "content": content, "view": view,
                    "allowed_mentions": allowed_mentions,
                })

        return _M()


def _tournament(**kw):
    base = dict(
        ocr_sessions={},
        channel_id=1,
        guild_id=1,
        id="t1",
        qualifier_matches=[(0, 1)],
        semifinal_matches=[],
        final_teams=[0, 1],
        teams=[
            {"captain": "CapA", "players": ["CapA", "P1", "P2", "P3"]},
            {"captain": "CapB", "players": ["CapB", "P4", "P5", "P6"]},
        ],
        team_names={0: "Alpha", 1: "Beta"},
        player_user_ids={
            "CapA": 42, "CapB": 43,
            "P1": 101, "P2": 102, "P3": 103,
            "P4": 104, "P5": 105, "P6": 106,
        },
        match_channels={},
        phase=SimpleNamespace(value="qualifiers"),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _guild(mapping_channel):
    guild = SimpleNamespace(id=1)
    guild.get_channel = lambda cid: (
        mapping_channel if int(cid) == mapping_channel.id else None
    )

    async def _fetch(cid):
        return mapping_channel if int(cid) == mapping_channel.id else None

    guild.fetch_channel = _fetch
    return guild


class TestSessionChannel:
    @pytest.mark.asyncio
    async def test_tournament_session_uses_mapping_channel(
        self, monkeypatch
    ):
        """Обычный турнир: прогресс/комнаты — в OCR_MAPPING_CHANNEL_ID,
        ID канала сохранён в сессии."""
        monkeypatch.setattr(up.store, "get", lambda g, c: None)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        mapping = _Chan(OCR_MAPPING_CHANNEL_ID)
        match_ch = _Chan(555)
        tournament = _tournament()
        guild = _guild(mapping)
        user = SimpleNamespace(id=42, display_name="CapA", name="capa")

        session = await up.create_rooms_session(
            guild, match_ch, user, tournament,
            "qualifier", 0, 1,
        )
        assert session is not None
        assert session["ocr_channel_id"] == OCR_MAPPING_CHANNEL_ID
        assert session["progress_channel_id"] == OCR_MAPPING_CHANNEL_ID
        # Прогресс-сообщение ушло в канал сопоставления, не в канал матча.
        assert len(mapping.sent) == 1
        assert len(match_ch.sent) == 0

    @pytest.mark.asyncio
    async def test_mm_session_keeps_match_channel(self, monkeypatch):
        """MM-матч: OCR остаётся в канале матча."""
        monkeypatch.setattr(up.store, "get", lambda g, c: None)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        mapping = _Chan(OCR_MAPPING_CHANNEL_ID)
        match_ch = _Chan(555)
        tournament = _tournament(mm_search_channel_id=999)
        guild = _guild(mapping)
        user = SimpleNamespace(id=42, display_name="CapA", name="capa")

        session = await up.create_rooms_session(
            guild, match_ch, user, tournament,
            "qualifier", 0, 1,
        )
        assert session["ocr_channel_id"] == 555
        assert session["progress_channel_id"] == 555
        assert len(match_ch.sent) == 1
        assert len(mapping.sent) == 0

    @pytest.mark.asyncio
    async def test_mapping_channel_fallback_to_match_channel(
        self, monkeypatch
    ):
        """Канал не найден → fallback на канал вызова, сессия жива."""
        monkeypatch.setattr(up.store, "get", lambda g, c: None)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        guild = SimpleNamespace(id=1)
        guild.get_channel = lambda cid: None

        async def _fetch(cid):
            return None

        guild.fetch_channel = _fetch
        match_ch = _Chan(555)
        tournament = _tournament()
        user = SimpleNamespace(id=42, display_name="CapA", name="capa")

        session = await up.create_rooms_session(
            guild, match_ch, user, tournament,
            "qualifier", 0, 1,
        )
        assert session["ocr_channel_id"] == 555
        assert len(match_ch.sent) == 1


class TestStatsChannelFor:
    @pytest.mark.asyncio
    async def test_prefers_ocr_channel_id(self):
        mapping = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(mapping)
        session = {
            "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_channel_id": 999,
        }
        ch = await up._stats_channel_for(guild, session)
        assert ch is mapping

    @pytest.mark.asyncio
    async def test_legacy_session_uses_progress_channel(self):
        """Старая сессия без ocr_channel_id — старый канал, без переноса."""
        old = _Chan(999)
        guild = SimpleNamespace(id=1)
        guild.get_channel = lambda cid: old if cid == 999 else None

        async def _fetch(cid):
            return old if cid == 999 else None

        guild.fetch_channel = _fetch
        session = {"progress_channel_id": 999}
        ch = await up._stats_channel_for(guild, session)
        assert ch is old


class TestCaptainPings:
    def _room(self):
        return {
            "room_index": 1,
            "status": "collecting_files",
            "collect_pending": False,
            "pending": [{
                "sha": "x", "kind": "classic",
                "pipe": {"panels": {
                    "left": {"rows": [{}, {}, {}, {}]},
                    "right": {"rows": [{}, {}, {}, {}]},
                }},
            }],
        }

    @pytest.mark.asyncio
    async def test_collect_message_pings_both_captains(self):
        ch = _Chan(1)
        tournament = _tournament()
        session = {"teams": [0, 1], "gen": 1}
        room = self._room()

        await up._ensure_room_collect_message(
            ch, tournament, session, room,
            "qualifier", 0,
        )
        assert len(ch.sent) == 1
        msg = ch.sent[0]
        assert "Капитаны: <@42> <@43>" in msg["content"]
        assert "Комната 1" in msg["content"]
        am = msg["allowed_mentions"]
        assert am is not None
        assert am.users is True
        assert am.roles is False
        assert am.everyone is False
        # В content нет пингов не-капитанов.
        assert "<@101>" not in msg["content"]
        assert "<@104>" not in msg["content"]

    @pytest.mark.asyncio
    async def test_collect_message_edit_no_resend(self):
        """Повторный вызов редактирует то же сообщение — пинг
        не дублируется (edit не уведомляет)."""
        ch = _Chan(1)
        tournament = _tournament()
        session = {"teams": [0, 1], "gen": 1}
        room = self._room()

        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        assert len(ch.sent) == 1
        assert len(ch.edits) == 1

    @pytest.mark.asyncio
    async def test_no_captain_ids_no_ping_line(self):
        ch = _Chan(1)
        tournament = _tournament(player_user_ids={})
        session = {"teams": [0, 1], "gen": 1}
        room = self._room()

        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        assert "Капитаны:" not in ch.sent[0]["content"]

    @pytest.mark.asyncio
    async def test_captains_from_match_context(self):
        """Капитаны берутся из актуальной пары матча, а не из
        session["teams"] (которые могут устареть)."""
        ch = _Chan(1)
        tournament = _tournament(
            qualifier_matches=[(1, 0)],  # порядок B, A
        )
        session = {"teams": [0, 1], "gen": 1}
        room = self._room()

        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        # Порядок из контекста: сначала команда B (idx 1 → CapB=43),
        # затем A (idx 0 → CapA=42).
        assert "Капитаны: <@43> <@42>" in ch.sent[0]["content"]


class TestConfirmAndAdminChannel:
    @pytest.mark.asyncio
    async def test_confirm_channel_prefers_ocr_channel(self):
        from views.ocr_match_view import _rooms_confirm_channel

        mapping = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(mapping)
        session = {
            "mode": "rooms",
            "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_channel_id": 999,
        }
        assert _rooms_confirm_channel(guild, session) is mapping

    @pytest.mark.asyncio
    async def test_admin_call_goes_to_ocr_channel(self, monkeypatch):
        """📣 Вызов админа для rooms-сессии — в OCR-канал, не в
        старый stats-канал."""
        from views.ocr_match_view import _notify_admin_call

        mapping = _Chan(OCR_MAPPING_CHANNEL_ID)
        stats = _Chan(31337)
        guild = SimpleNamespace(id=1)

        def _get(cid):
            if int(cid) == OCR_MAPPING_CHANNEL_ID:
                return mapping
            if int(cid) == 31337:
                return stats
            return None

        guild.get_channel = _get

        async def _fetch(cid):
            return _get(cid)

        guild.fetch_channel = _fetch

        tournament = _tournament(
            ocr_sessions={
                "qualifier_0": {
                    "mode": "rooms",
                    "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
                }
            }
        )
        user = SimpleNamespace(
            id=42, display_name="CapA", name="capa",
            mention="<@42>",
        )
        interaction = SimpleNamespace(
            guild=guild, user=user, client=SimpleNamespace(),
        )
        await _notify_admin_call(
            interaction, tournament, "qualifier", 0, 1,
        )
        assert len(mapping.sent) == 1
        assert "<@&" in mapping.sent[0] or "вызвал администратора" in str(
            mapping.sent[0]
        )
        # Старый stats-канал не получил дубликат.
        assert len(stats.sent) == 0

    @pytest.mark.asyncio
    async def test_admin_call_legacy_session_stats_channel(
        self, monkeypatch
    ):
        """Сессия без ocr_channel_id — уведомление в stats-канал,
        как раньше."""
        from views.ocr_match_view import _notify_admin_call
        import views.ocr_match_view as om

        monkeypatch.setattr(om, "OCR_STATS_CHANNEL_ID", 31337)
        stats = _Chan(31337)
        guild = SimpleNamespace(id=1)
        guild.get_channel = lambda cid: (
            stats if int(cid) == 31337 else None
        )

        async def _fetch(cid):
            return guild.get_channel(cid)

        guild.fetch_channel = _fetch
        tournament = _tournament(ocr_sessions={})
        user = SimpleNamespace(
            id=42, display_name="CapA", name="capa",
            mention="<@42>",
        )
        interaction = SimpleNamespace(
            guild=guild, user=user, client=SimpleNamespace(),
        )
        await _notify_admin_call(
            interaction, tournament, "qualifier", 0, 1,
        )
        assert len(stats.sent) == 1
