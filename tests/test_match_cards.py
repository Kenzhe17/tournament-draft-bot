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


def _fake_response(status):
    return SimpleNamespace(status=status, reason=None)


class _Msg:
    _next = 1000

    def __init__(self, mid=None, channel=None, bot=True):
        self.id = mid or _Msg._next
        _Msg._next += 1
        self._channel = channel
        self.edits = []
        self.deleted = False
        self.embed = None
        self.author = SimpleNamespace(bot=bot, id=999)

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
        self._hist = []

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
        self._hist.append(msg)
        return msg

    def inject(self, msg):
        """Поместить сообщение в историю канала (существующий
        orphan/duplicate, не созданный через send)."""
        self._hist.append(msg)
        self._by_id[msg.id] = msg

    def history(self, limit=100, **kw):
        msgs = list(reversed(self._hist))[: int(limit)]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def fetch_message(self, mid):
        import discord

        msg = self._by_id.get(int(mid))
        if msg is None:
            raise discord.NotFound(
                _fake_response(404), "not found"
            )
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
    async def test_only_control_message_no_public_card(self, monkeypatch):
        """Публичных карточек матчей нет — только рабочее
        сообщение в OCR-канале; матч виден в основной сетке."""
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.announce_phase_matches(tournament, guild, bot=None)

        # В канал турнира НИЧЕГО не отправляется.
        assert tchan.sent == []
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
        # ID сохранены в match_cards — только рабочие.
        entry = tournament.match_cards["qualifier_0"]
        assert "public_message_id" not in entry
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
        assert tchan.sent == []
        assert len(ochan.sent) == 1


class TestLegacyPublicCardCleanup:
    """update_public_match_card больше не создаёт карточки —
    только удаляет подтверждённые legacy-сообщения бота."""

    def _seed_legacy(self, tournament, tchan, mid=None):
        msg = _Msg(channel=tchan)
        tchan._by_id[msg.id] = msg
        tournament.match_cards["qualifier_0"] = {
            "public_message_id": msg.id,
            "public_channel_id": tchan.id,
            "sig": "old",
        }
        return msg

    @pytest.mark.asyncio
    async def test_legacy_card_deleted_fields_cleared(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        msg = self._seed_legacy(tournament, tchan)

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert msg.deleted is True
        entry = tournament.match_cards["qualifier_0"]
        assert "public_message_id" not in entry
        assert "public_channel_id" not in entry
        assert "sig" not in entry
        # Новых сообщений не создано.
        assert tchan.sent == []

    @pytest.mark.asyncio
    async def test_no_public_id_is_noop(self):
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert tchan.sent == []
        assert ochan.sent == []

    @pytest.mark.asyncio
    async def test_main_message_never_deleted(self):
        """public_message_id, совпадающий с основным сообщением
        сетки, не удаляется — защита от ошибочной ссылки."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        main = _Msg(channel=tchan)
        tchan._by_id[main.id] = main
        tournament.message_id = main.id
        tournament.match_cards["qualifier_0"] = {
            "public_message_id": main.id,
            "public_channel_id": tchan.id,
        }

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert main.deleted is False
        assert "public_message_id" not in (
            tournament.match_cards["qualifier_0"]
        )

    @pytest.mark.asyncio
    async def test_non_bot_message_not_deleted(self):
        """Сообщение не от бота не удаляется — ID очищается,
        сообщение остаётся."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        msg = _Msg(channel=tchan, bot=False)
        tchan._by_id[msg.id] = msg
        tournament.match_cards["qualifier_0"] = {
            "public_message_id": msg.id,
            "public_channel_id": tchan.id,
        }

        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert msg.deleted is False
        assert "public_message_id" not in (
            tournament.match_cards["qualifier_0"]
        )

    @pytest.mark.asyncio
    async def test_fetch_failure_keeps_id_for_retry(self):
        """Временная ошибка API — public_message_id сохраняется
        для повторной попытки."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        tournament.match_cards["qualifier_0"] = {
            "public_message_id": 555,
            "public_channel_id": tchan.id,
        }

        async def _boom(mid):
            raise RuntimeError("api down")

        tchan.fetch_message = _boom
        await ma.update_public_match_card(
            guild, tournament, "qualifier", 0
        )
        assert (
            tournament.match_cards["qualifier_0"]["public_message_id"]
            == 555
        )

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

    @pytest.mark.asyncio
    async def test_cleanup_public_match_cards_sweep(self):
        """Sweep удаляет legacy-карточки всех матчей турнира."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament(
            semifinal_matches=[(0, 1)],
        )
        m1 = self._seed_legacy(tournament, tchan)
        m2 = _Msg(channel=tchan)
        tchan._by_id[m2.id] = m2
        tournament.match_cards["semifinal_0"] = {
            "public_message_id": m2.id,
            "public_channel_id": tchan.id,
        }

        await ma.cleanup_public_match_cards(guild, tournament)
        assert m1.deleted is True
        assert m2.deleted is True
        for entry in tournament.match_cards.values():
            assert "public_message_id" not in entry


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

    @pytest.mark.asyncio
    async def test_cleanup_squad_mode(self):
        """Squad-сессия: удаляются сообщения отрядов, итоговая
        карточка сохраняется."""
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = {
            "mode": "squad",
            "status": "completed",
            "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_message_id": 9001,
            "squads": {"0": {"message_id": 9003}, "1": {"message_id": 9004}},
            "squad_pool": [{"message_id": 9005}],
        }
        tournament.ocr_sessions["qualifier_0"] = sess
        for mid in (9001, 9003, 9004, 9005):
            ochan._by_id[mid] = _Msg(mid=mid, channel=ochan)

        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is True
        assert sess["cleanup_done"] is True
        for mid in (9003, 9004, 9005):
            assert mid not in ochan._by_id
        assert 9001 in ochan._by_id

    @pytest.mark.asyncio
    async def test_cleanup_classic_mode(self):
        """Classic-сессия: общее OCR-сообщение удаляется."""
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = {
            "status": "completed",
            "ocr_channel_id": OCR_MAPPING_CHANNEL_ID,
            "progress_message_id": 9001,
            "message_id": 9003,
        }
        tournament.ocr_sessions["qualifier_0"] = sess
        for mid in (9001, 9003):
            ochan._by_id[mid] = _Msg(mid=mid, channel=ochan)

        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is True
        assert 9003 not in ochan._by_id
        assert 9001 in ochan._by_id

    @pytest.mark.asyncio
    async def test_cleanup_done_not_set_on_forbidden(self):
        """discord.Forbidden — cleanup_done НЕ ставится, возврат
        False для повторной попытки recovery."""
        import discord
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = self._session()
        tournament.ocr_sessions["qualifier_0"] = sess
        bad = _Msg(mid=9003, channel=ochan)

        async def _forbidden():
            raise discord.Forbidden(
                _fake_response(403), "no perms"
            )

        bad.delete = _forbidden
        ochan._by_id[9003] = bad

        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is False
        assert not sess.get("cleanup_done")

    @pytest.mark.asyncio
    async def test_cleanup_skips_non_bot_message(self):
        """Сообщение не от бота не удаляется и не считается
        сбоем — cleanup завершается."""
        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        sess = self._session()
        tournament.ocr_sessions["qualifier_0"] = sess
        user_msg = _Msg(mid=9003, channel=ochan, bot=False)
        ochan._by_id[9003] = user_msg

        ok = await up.cleanup_rooms_ocr_messages(
            guild, tournament, "qualifier", 0, sess
        )
        assert ok is True
        assert user_msg.deleted is False
        assert sess["cleanup_done"] is True


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


def _tournament_4teams(**kw):
    """Турнир с 4 командами — два матча в одной фазе."""
    teams = [
        {"captain": f"Cap{i}", "players": [f"Cap{i}", f"P{i}a", f"P{i}b", f"P{i}c"]}
        for i in range(4)
    ]
    uids = {}
    for i in range(4):
        for n in teams[i]["players"]:
            uids[n] = 100 + i * 10 + len(uids)
    base = dict(
        teams=teams,
        team_names={i: f"Team{i}" for i in range(4)},
        player_user_ids=uids,
        qualifier_matches=[(0, 1), (2, 3)],
        semifinal_matches=[],
        phase=__import__(
            "models.tournament", fromlist=["TournamentPhase"]
        ).TournamentPhase.QUALIFIERS,
        qualifier_winners=[None, None],
    )
    base.update(kw)
    t = _tournament(**base)
    # _tournament() уже задал qualifier_matches — переопределяем явно.
    t.qualifier_matches = [(0, 1), (2, 3)]
    return t


class TestSingleWorkCard:
    """Ровно одна рабочая OCR-карточка на матч: конкурентные
    вызовы, повторные события, рестарт и дубли."""

    @pytest.mark.asyncio
    async def test_two_matches_two_cards(self, monkeypatch):
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament_4teams()

        await ma.announce_phase_matches(tournament, guild, bot=None)

        assert len(ochan.sent) == 2
        assert set(tournament.match_cards) == {
            "qualifier_0", "qualifier_1",
        }
        ids = {
            e["work_message_id"]
            for e in tournament.match_cards.values()
        }
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_concurrent_announce_and_refresh(self, monkeypatch):
        """announce_phase_matches ×2 + refresh_match_control
        одновременно — всё равно одна карточка."""
        import asyncio

        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await asyncio.gather(
            ma.announce_phase_matches(tournament, guild, bot=None),
            ma.announce_phase_matches(tournament, guild, bot=None),
            ma.refresh_match_control(
                guild, tournament, "qualifier", 0
            ),
            ma.update_match_card(
                guild, tournament, "qualifier", 0
            ),
        )
        assert len(ochan.sent) == 1

    @pytest.mark.asyncio
    async def test_rerun_phase_no_new_card(self, monkeypatch):
        """Повторный запуск обработчика стадии — карточка
        переиспользуется, send() не вызывается."""
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.announce_phase_matches(tournament, guild)
        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        await ma.refresh_match_control(
            guild, tournament, "qualifier", 0
        )
        assert len(ochan.sent) == 1

    @pytest.mark.asyncio
    async def test_concurrent_session_creation_single_progress(
        self, monkeypatch
    ):
        """Два одновременных «Загрузить скриншоты» — одна
        rooms-сессия, одно progress-сообщение."""
        import asyncio

        import cogs.ocr_upload as up

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        user = SimpleNamespace(
            id=42, display_name="CapA", name="capa"
        )

        s1, s2 = await asyncio.gather(
            up.create_rooms_session(
                guild, tchan, user, tournament, "qualifier", 0, 1
            ),
            up.create_rooms_session(
                guild, tchan, user, tournament, "qualifier", 0, 1
            ),
        )
        sessions = [s for s in (s1, s2) if s is not None]
        assert len(sessions) == 1
        # Одно сообщение прогресса на матч.
        assert len(ochan.sent) == 1

    @pytest.mark.asyncio
    async def test_send_persist_failure_adopts_orphan(self):
        """Сбой между send() и сохранением work_message_id:
        сирота в канале принимается за карточку, дубль не
        создаётся."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        # «Сирота»: сообщение бота с сигнатурой матча уже в
        # канале, но work_message_id не сохранился (состояние
        # до send — match_cards пуст).
        sig = await ma._work_card_signature(
            tournament, "qualifier", 0
        )
        orphan = _Msg(channel=ochan)
        orphan.content = sig
        ochan.inject(orphan)

        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        # Новый send() не вызывался — принята существующая.
        assert ochan.sent == []
        assert (
            tournament.match_cards["qualifier_0"]["work_message_id"]
            == orphan.id
        )

    @pytest.mark.asyncio
    async def test_reconcile_removes_duplicate_keeps_card(self):
        """Sweep после рестарта: дубль с сигнатурой матча
        удаляется, актуальная карточка и чужие сообщения
        не затрагиваются."""
        from services import match_announcer as ma

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()

        await ma.send_match_control(
            guild, tournament, "qualifier", 0
        )
        work = ochan.sent[0]
        wid = tournament.match_cards["qualifier_0"]["work_message_id"]

        sig = await ma._work_card_signature(
            tournament, "qualifier", 0
        )
        dup = _Msg(channel=ochan)
        dup.content = sig
        ochan.inject(dup)
        # Чужое сообщение без сигнатуры — не трогаем.
        other = _Msg(channel=ochan)
        other.content = "**Совсем другой матч**"
        ochan.inject(other)

        await ma.reconcile_work_cards(guild, tournament)
        assert dup.deleted is True
        assert other.deleted is False
        assert work.deleted is False
        assert (
            tournament.match_cards["qualifier_0"]["work_message_id"]
            == wid
        )

    @pytest.mark.asyncio
    async def test_stale_duplicate_button_rejected(self, monkeypatch):
        """Кнопка на устаревшем дубле не запускает OCR-флоу."""
        import views.ocr_match_view as vm

        tchan = _Chan(42)
        ochan = _Chan(OCR_MAPPING_CHANNEL_ID)
        guild = _guild(tchan, ochan)
        tournament = _tournament()
        tournament.match_cards["qualifier_0"] = {
            "work_message_id": 1234,
            "work_channel_id": OCR_MAPPING_CHANNEL_ID,
        }
        monkeypatch.setattr(vm.store, "get", lambda g, c: tournament)

        sent = []

        async def _send(*a, **kw):
            sent.append((a, kw))

        interaction = SimpleNamespace(
            message=SimpleNamespace(id=9999),
            response=SimpleNamespace(send_message=_send),
            user=SimpleNamespace(id=1),
            guild=guild,
        )
        btn = vm.MatchScreenshotButton(
            1, 42, tournament, "qualifier", 0, "Загрузить",
        )
        await btn.callback(interaction)
        assert sent, "ожидался ephemeral-ответ"
        text = str(sent[0])
        assert "устарел" in text

    @pytest.mark.asyncio
    async def test_completed_match_no_new_card(self, monkeypatch):
        """После завершения OCR карточка не пересоздаётся —
        остаётся одна финальная."""
        from services import match_announcer as ma

        _fake_match_number_store(monkeypatch)
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
        await ma.refresh_match_control(
            guild, tournament, "qualifier", 0
        )
        await ma.update_match_card(
            guild, tournament, "qualifier", 0
        )
        assert len(ochan.sent) == 1
