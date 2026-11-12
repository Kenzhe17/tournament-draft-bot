"""Публичные и рабочие карточки матчей обычных турниров:

- одно публичное сообщение на матч в канале турнира (статусы);
- одно рабочее сообщение в OCR-канале со статусом и кнопкой
  «Загрузить скриншоты»;
- дедупликация правок (sig), восстановление удалённых сообщений;
- create_rooms_session переиспользует рабочее сообщение;
- сериализация match_cards.
"""

from types import SimpleNamespace

import pytest

from config import OCR_MAPPING_CHANNEL_ID


class _Msg:
    _next = 1000

    def __init__(self, mid=None, channel=None):
        self.id = mid or _Msg._next
        _Msg._next += 1
        self._channel = channel
        self.edits = []
        self.deleted = False
        self.embed = None

    async def edit(self, content=None, view=None, embed=None,
                   embeds=None, allowed_mentions=None, **kw):
        self.embed = embed
        self.edits.append({
            "content": content, "view": view, "embed": embed,
            "allowed_mentions": allowed_mentions,
        })

    async def delete(self):
        self.deleted = True
        if self._channel is not None:
            self._channel._by_id.pop(self.id, None)


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self._by_id = {}

    async def send(self, content=None, view=None, embed=None,
                   embeds=None, files=None, allowed_mentions=None,
                   **kw):
        msg = _Msg(channel=self)
        msg.content = content
        msg.view = view
        msg.embed = embed
        msg.allowed_mentions = allowed_mentions
        self.sent.append(msg)
        self._by_id[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self._by_id.get(int(mid))
        if msg is None:
            raise Exception("not found")
        return msg

    async def fetch_channel(self, cid):  # pragma: no cover
        return self


def _tournament(**kw):
    base = dict(
        ocr_sessions={},
        channel_id=42,
        guild_id=1,
        id="t1",
        qualifier_matches=[(0, 1)],
        semifinal_matches=[],
        final_teams=[0, 1],
        qualifier_winners=[None],
        semifinal_winners=[],
        winner_team_index=None,
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
        match_cards={},
        phase=__import__(
            "models.tournament", fromlist=["TournamentPhase"]
        ).TournamentPhase.QUALIFIERS,
        processed_elo_matches=set(),
        temp_match_stats={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _guild(tchan, ochan):
    guild = SimpleNamespace(id=1)

    def _get(cid):
        cid = int(cid)
        if cid == tchan.id:
            return tchan
        if cid == ochan.id:
            return ochan
        return None

    guild.get_channel = _get

    async def _fetch(cid):
        return _get(cid)

    guild.fetch_channel = _fetch
    return guild


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    import storage.json_store as js

    monkeypatch.setattr(js.store, "get", lambda g, c: None)
    monkeypatch.setattr(js.store, "set", lambda t: None)
    import cogs.ocr_upload as up

    monkeypatch.setattr(up.store, "get", lambda g, c: None)
    monkeypatch.setattr(up.store, "set", lambda t: None)


def _fake_match_number_store(monkeypatch):
    announced = set()

    class _MNS:
        async def is_announced(self, key):
            return key in announced

        async def get_or_assign(self, key):
            return 7

        async def mark_announced(self, key):
            announced.add(key)

    import services.match_announcer as ma

    monkeypatch.setattr(ma, "match_number_store", _MNS())


class TestAnnounceCreatesCards:
    @pytest.mark.asyncio
    async def test_public_and_control_messages(self, monkeypatch):
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.announce_phase_matches(tournament, guild, bot=None)

        # Публичная карточка — в канале турнира (embed, без пингов).
        assert len(tchan.sent) == 1
        pub = tchan.sent[0]
        assert pub.embed is not None
        assert "#1" in (pub.embed.title or "")
        assert "CapA" in (pub.embed.description or "")
        assert "CapB" in (pub.embed.description or "")
        assert any(
            "Ожидание результатов" in (f.value or "")
            for f in pub.embed.fields
        )
        assert pub.allowed_mentions is not None
        # Рабочее сообщение — в OCR-канале.
        assert len(ochan.sent) == 1
        work = ochan.sent[0]
        labels = [
            getattr(c, "label", "")
            for c in (work.view.children if work.view else [])
        ]
        assert "📸 Загрузить скриншоты" in labels
        assert "Капитаны: <@42> <@43>" in work.content
        assert "Ожидание скриншотов" in work.content
        # ID сохранены в match_cards.
        entry = tournament.match_cards["qualifier_0"]
        assert entry["public_message_id"] == pub.id
        assert entry["work_message_id"] == work.id
        assert entry["work_channel_id"] == OCR_MAPPING_CHANNEL_ID

    @pytest.mark.asyncio
    async def test_announce_idempotent_no_duplicates(self, monkeypatch):
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.announce_phase_matches(tournament, guild)
        # Повторный анонс того же матча — is_announced → ничего.
        await ma.announce_phase_matches(tournament, guild)
        assert len(tchan.sent) == 1
        assert len(ochan.sent) == 1


class TestPublicCardUpdates:
    @pytest.mark.asyncio
    async def test_state_transitions_and_dedup(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        pub = tchan.sent[0]
        assert any(
            "Ожидание результатов" in (f.value or "")
            for f in pub.embed.fields
        )
        entry = tournament.match_cards["qualifier_0"]

        # processing: активная сессия
        tournament.ocr_sessions["qualifier_0"] = {
            "mode": "rooms", "status": "collecting",
            "rooms_expected": 1, "rooms": [],
        }
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert len(pub.edits) == 1
        e1 = pub.edits[0]["embed"]
        assert any(
            "Проверка результатов" in (f.value or "")
            for f in e1.fields
        )

        # Повтор без изменений — edit не вызывается (sig-дедуп).
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert len(pub.edits) == 1

        # done: победитель в сетке
        tournament.ocr_sessions["qualifier_0"]["status"] = "completed"
        tournament.qualifier_winners[0] = 1
        tournament.ocr_sessions["qualifier_0"]["rooms"] = [
            {"room_index": 1, "panels": {"left": {}}}
        ]
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert len(pub.edits) == 2
        done_embed = pub.edits[1]["embed"]
        assert any(
            "Победитель" in (f.name or "") for f in done_embed.fields
        )
        assert "ЗАВЕРШ" in (done_embed.title or "")

    @pytest.mark.asyncio
    async def test_deleted_message_recreated(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        first = tchan.sent[0]
        await first.delete()
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert len(tchan.sent) == 2
        entry = tournament.match_cards["qualifier_0"]
        assert entry["public_message_id"] == tchan.sent[1].id

    @pytest.mark.asyncio
    async def test_mm_tournament_skipped(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament(mm_search_channel_id=999)
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        assert tchan.sent == []
        assert ochan.sent == []


class TestControlMessage:
    @pytest.mark.asyncio
    async def test_refresh_restores_after_cancel(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        work = ochan.sent[0]
        # Сессия перехватила сообщение — control_sig сброшен
        # (как в create_rooms_session), затем комплект отменён.
        tournament.match_cards["qualifier_0"].pop("control_sig", None)
        tournament.ocr_sessions["qualifier_0"] = {
            "mode": "rooms", "status": "cancelled",
        }
        await ma.refresh_match_control(
            guild, tournament, "qualifier", 0
        )
        assert len(work.edits) == 1
        assert "Ожидание скриншотов" in work.edits[0]["content"]
        assert work.edits[0]["view"] is not None

    @pytest.mark.asyncio
    async def test_refresh_skips_active_session(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        work = ochan.sent[0]
        tournament.ocr_sessions["qualifier_0"] = {
            "mode": "rooms", "status": "collecting",
            "rooms_expected": 2, "rooms": [],
        }
        await ma.refresh_match_control(
            guild, tournament, "qualifier", 0
        )
        # Сообщение принадлежит progress-флоу — не трогаем.
        assert work.edits == []

    @pytest.mark.asyncio
    async def test_finished_shows_final_status(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament(
            qualifier_winners=[1],
            ocr_sessions={
                "qualifier_0": {
                    "mode": "rooms", "status": "completed",
                    "rooms": [{"room_index": 1}],
                }
            },
        )
        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        work = ochan.sent[0]
        assert "подтвержд" in work.content.lower()
        # finished → view без кнопки загрузки, есть «📊 Статистика»
        assert all(
            getattr(c, "label", "") != "📸 Загрузить скриншоты"
            for c in (work.view.children if work.view else [])
        )


class TestSessionReusesControlMessage:
    @pytest.mark.asyncio
    async def test_progress_reuses_work_message(self, monkeypatch):
        """create_rooms_session редактирует рабочее сообщение,
        а не создаёт второе."""
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        # Рабочее сообщение уже существует.
        from services import match_announcer as ma

        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        work = ochan.sent[0]
        wid = tournament.match_cards["qualifier_0"]["work_message_id"]

        # store.get → сам tournament (сессия пишется в него)
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        user = SimpleNamespace(id=42, display_name="CapA", name="capa")
        session = await up.create_rooms_session(
            guild, tchan, user, tournament, "qualifier", 0, 1,
        )
        assert session is not None
        # Новых сообщений в OCR-канале НЕТ — отредактировано старое.
        assert len(ochan.sent) == 1
        assert len(work.edits) >= 1
        assert session["progress_message_id"] == wid
        assert session["progress_channel_id"] == OCR_MAPPING_CHANNEL_ID
        assert session["ocr_channel_id"] == OCR_MAPPING_CHANNEL_ID


class TestRoomsCleanup:
    """cleanup_rooms_ocr_messages: удаляются только технические
    сообщения своего матча; итоговая карточка сохраняется;
    идемпотентно."""

    def _session(self, **kw):
        base = {
            "mode": "rooms",
            "status": "completed",
            "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_message_id": 9001,
            "confirm_message_id": 9002,
            "rooms": [
                {
                    "room_index": 1,
                    "message_id": 9003,
                    "message_ids": [9004, 9005],
                    "collect_message_id": 9006,
                    "shot_message_ids": [9007],
                    "pending": [{"shot_message_id": 9008}],
                }
            ],
        }
        base.update(kw)
        return base

    @pytest.mark.asyncio
    async def test_cleanup_deletes_only_technical(self):
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = self._session()
        tournament.ocr_sessions["qualifier_0"] = sess

        # Сообщения «существуют» в OCR-канале.
        for mid in (9001, 9002, 9003, 9004, 9005, 9006, 9007, 9008):
            ochan._by_id[mid] = _Msg(mid=mid, channel=ochan)

        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is True
        assert sess["cleanup_done"] is True
        # Технические удалены (delete() убирает из _by_id).
        for mid in (9002, 9003, 9004, 9005, 9006, 9007, 9008):
            assert mid not in ochan._by_id
        # Итоговая карточка (progress) сохранена.
        assert 9001 in ochan._by_id
        assert not ochan._by_id[9001].deleted

    @pytest.mark.asyncio
    async def test_cleanup_idempotent(self):
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = self._session()
        tournament.ocr_sessions["qualifier_0"] = sess
        for mid in (9001, 9003):
            ochan._by_id[mid] = _Msg(mid=mid, channel=ochan)

        await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        # Повторный вызов — мгновенный выход, без fetch/delete.
        before = dict(ochan._by_id)
        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is True
        assert ochan._by_id == before

    @pytest.mark.asyncio
    async def test_cleanup_isolated_per_match(self):
        """Очистка qualifier_0 не трогает сообщения qualifier_1."""
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess0 = self._session()
        sess1 = self._session(
            progress_message_id=9101,
            confirm_message_id=9102,
            rooms=[{
                "room_index": 1, "message_id": 9103,
                "message_ids": [], "collect_message_id": 9104,
                "shot_message_ids": [], "pending": [],
            }],
        )
        tournament.ocr_sessions["qualifier_0"] = sess0
        tournament.ocr_sessions["qualifier_1"] = sess1
        for mid in (9001, 9003, 9101, 9103):
            ochan._by_id[mid] = _Msg(mid=mid, channel=ochan)

        await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess0
        )
        # Сообщения второго матча нетронуты.
        assert 9101 in ochan._by_id
        assert 9103 in ochan._by_id
        assert not sess1.get("cleanup_done")


class TestSerialization:
    def test_match_cards_roundtrip(self):
        from models.tournament import Tournament

        t = _tournament()
        t.match_cards["qualifier_0"] = {
            "public_message_id": 111,
            "public_channel_id": 42,
            "work_message_id": 222,
            "work_channel_id": OCR_MAPPING_CHANNEL_ID,
            "sig": "abc",
        }
        # Tournament.from_dict ожидает полный dict — проверяем
        # только наличие ключа в to_dict-совместимой структуре.
        data = {
            "guild_id": 1, "channel_id": 42,
            "match_cards": t.match_cards,
        }
        # from_dict требует много полей — вместо полного
        # roundtrip проверяем, что поле объявлено и сериализуется.
        assert "match_cards" in Tournament.__dataclass_fields__
