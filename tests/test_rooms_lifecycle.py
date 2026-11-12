"""ТЗ Ranked MM: жизненный цикл rooms OCR-сессии.

Покрывает чистую логику (без Discord API):
- занятость комплекта и права на догрузку файлов;
- фиксация победителя первым принятым файлом (_rooms_claim_bundle);
- гейт готовности check_ocr_session_ready;
- разрешение победителя _rooms_confirmed_winner.
"""

import asyncio
from types import SimpleNamespace

import pytest


def _user(uid, name, admin=False):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        name=name,
        guild=SimpleNamespace(
            id=1, roles=[], get_role=lambda rid: None
        ),
        roles=[],
        guild_permissions=SimpleNamespace(administrator=admin),
    )


def _tournament(mm=True):
    """MM-турнир: team0 = P1..P4 (uids 1..4, капитан P1),
    team1 = P5..P8 (uids 5..8, капитан P5)."""
    names = [f"P{i}" for i in range(1, 9)]
    return SimpleNamespace(
        id=100,
        guild_id=1,
        channel_id=2,
        mm_search_channel_id=555 if mm else None,
        teams=[
            {"players": names[0:4], "captain": "P1"},
            {"players": names[4:8], "captain": "P5"},
        ],
        player_user_ids={n: i + 1 for i, n in enumerate(names)},
        team_names={},
        ocr_sessions={},
        processed_elo_matches=[],
        qualifier_matches=[(0, 1)],
        final_teams=[0, 1],
    )


def _session(**kw):
    s = {
        "mode": "rooms",
        "match_type": "final",
        "match_index": 0,
        "status": "collecting",
        "rooms_expected": 2,
        "rooms_received": 0,
        "rooms": [],
        "gen": 1,
        "uploaded_by": 1,
        "uploaded_by_name": "P1",
        "winner_pending": True,
        "winning_team_index": None,
        "teams": [0, 1],
        "maps": {},
        "panel_picks": {},
        "card_state": "active",
    }
    s.update(kw)
    return s


# ---------------------------------------------------------------------------
# Занятость комплекта и права
# ---------------------------------------------------------------------------

class TestBundleClaimed:
    def test_new_session_unclaimed(self):
        from cogs.ocr_upload import _rooms_bundle_claimed

        assert not _rooms_bundle_claimed(_session())

    def test_winner_pending_false_claimed(self):
        from cogs.ocr_upload import _rooms_bundle_claimed

        assert _rooms_bundle_claimed(_session(winner_pending=False))

    def test_pinned_winner_claimed(self):
        from cogs.ocr_upload import _rooms_bundle_claimed

        assert _rooms_bundle_claimed(_session(winning_team_index=0))

    def test_legacy_session_with_rooms_claimed(self):
        from cogs.ocr_upload import _rooms_bundle_claimed

        s = _session()
        del s["winner_pending"]
        s["rooms"] = [{"room_index": 1}]
        assert _rooms_bundle_claimed(s)


class TestFileSubmitAllowed:
    CAP_A = staticmethod(lambda: _user(1, "P1"))
    CAP_B = staticmethod(lambda: _user(5, "P5"))
    STRANGER = staticmethod(lambda: _user(77, "Random"))
    ADMIN = staticmethod(lambda: _user(88, "Admin", admin=True))

    def test_unclaimed_any_captain_or_admin(self):
        from cogs.ocr_upload import _rooms_file_submit_allowed

        t = _tournament()
        s = _session()
        assert _rooms_file_submit_allowed(t, s, self.CAP_A())
        assert _rooms_file_submit_allowed(t, s, self.CAP_B())
        assert _rooms_file_submit_allowed(t, s, self.ADMIN())
        assert not _rooms_file_submit_allowed(t, s, self.STRANGER())

    def test_claimed_only_owner_winner_captain_admin(self):
        from cogs.ocr_upload import _rooms_file_submit_allowed

        t = _tournament()
        # Комплект занял капитан команды 0 — он же победитель
        s = _session(
            winner_pending=False,
            uploaded_by=1,
            winning_team_index=0,
        )
        assert _rooms_file_submit_allowed(t, s, self.CAP_A())
        # Другой капитан не может перехватить чужой комплект
        assert not _rooms_file_submit_allowed(t, s, self.CAP_B())
        assert _rooms_file_submit_allowed(t, s, self.ADMIN())
        assert not _rooms_file_submit_allowed(t, s, self.STRANGER())

    def test_admin_bundle_captains_may_help(self):
        """Админ занял комплект без команды — капитаны могут
        помогать с загрузкой, победителя выберет админ явно."""
        from cogs.ocr_upload import _rooms_file_submit_allowed

        t = _tournament()
        s = _session(
            winner_pending=False,
            uploaded_by=88,
            winning_team_index=None,
        )
        assert _rooms_file_submit_allowed(t, s, self.CAP_A())
        assert _rooms_file_submit_allowed(t, s, self.CAP_B())
        assert _rooms_file_submit_allowed(t, s, self.ADMIN())
        assert not _rooms_file_submit_allowed(t, s, self.STRANGER())

    def test_winner_captain_can_continue(self):
        """Капитан зафиксированной команды-победителя может
        догружать файлы, даже если uploaded_by — другой участник."""
        from cogs.ocr_upload import _rooms_file_submit_allowed

        t = _tournament()
        s = _session(
            winner_pending=False,
            uploaded_by=2,          # P2 — участник, не капитан
            winning_team_index=0,   # победитель — team0, капитан P1
        )
        assert _rooms_file_submit_allowed(t, s, self.CAP_A())
        assert not _rooms_file_submit_allowed(t, s, self.CAP_B())

    def test_non_mm_tournament_uses_legacy_rule(self):
        """Обычный турнир: права не ужесточаются — капитан/орг."""
        from cogs.ocr_upload import _rooms_file_submit_allowed

        t = _tournament(mm=False)
        s = _session(
            winner_pending=False,
            uploaded_by=1,
            winning_team_index=0,
        )
        # Оба капитана матча сохраняют доступ (прежнее поведение)
        assert _rooms_file_submit_allowed(t, s, self.CAP_B())


# ---------------------------------------------------------------------------
# Фиксация победителя первым файлом
# ---------------------------------------------------------------------------

class TestClaimBundle:
    @pytest.mark.asyncio
    async def test_first_file_pins_winner(self, monkeypatch):
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session()
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        guild = SimpleNamespace(id=1)
        cap_b = _user(5, "P5")
        status, _, s = await up._rooms_claim_bundle(
            guild, tournament, "final_0", session, cap_b
        )
        assert status == "ok"
        assert s["winner_pending"] is False
        assert s["uploaded_by"] == 5
        assert s["uploaded_by_name"] == "P5"
        assert s["winning_team_index"] == 1

    @pytest.mark.asyncio
    async def test_second_captain_rejected_after_claim(self, monkeypatch):
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session()
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        guild = SimpleNamespace(id=1)
        # Капитан B занял комплект первым файлом
        st1, _, _ = await up._rooms_claim_bundle(
            guild, tournament, "final_0", session, _user(5, "P5")
        )
        assert st1 == "ok"
        # Капитан A пытается продолжить — отказ, победитель не меняется
        st2, _, s2 = await up._rooms_claim_bundle(
            guild, tournament, "final_0", session, _user(1, "P1")
        )
        assert st2 == "forbidden"
        assert s2["winning_team_index"] == 1
        assert s2["uploaded_by"] == 5

    @pytest.mark.asyncio
    async def test_owner_can_continue(self, monkeypatch):
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session(
            winner_pending=False, uploaded_by=5, winning_team_index=1,
        )
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        st, _, _ = await up._rooms_claim_bundle(
            SimpleNamespace(id=1), tournament, "final_0",
            session, _user(5, "P5"),
        )
        assert st == "ok"

    @pytest.mark.asyncio
    async def test_admin_claim_leaves_winner_unset(self, monkeypatch):
        """Админ отправил первый файл — победитель не определяется
        по его Discord ID, остаётся для явного выбора."""
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session()
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        st, _, s = await up._rooms_claim_bundle(
            SimpleNamespace(id=1), tournament, "final_0",
            session, _user(88, "Admin", admin=True),
        )
        assert st == "ok"
        assert s["uploaded_by"] == 88
        assert s["winning_team_index"] is None

    @pytest.mark.asyncio
    async def test_stale_generation_rejected(self, monkeypatch):
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session()
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        # Кнопка со старым gen (сессия пересоздана → gen вырос)
        stale = _session(gen=1)
        session["gen"] = 2
        st, _, _ = await up._rooms_claim_bundle(
            SimpleNamespace(id=1), tournament, "final_0",
            stale, _user(1, "P1"),
        )
        assert st == "stale"

    @pytest.mark.asyncio
    async def test_concurrent_claims_single_winner(self, monkeypatch):
        """Два капитана одновременно → ровно один становится
        владельцем комплекта и победителем."""
        import asyncio
        import cogs.ocr_upload as up

        tournament = _tournament()
        session = _session()
        tournament.ocr_sessions["final_0"] = session
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        guild = SimpleNamespace(id=1)

        async def go(uid, name):
            return await up._rooms_claim_bundle(
                guild, tournament, "final_0", session, _user(uid, name)
            )

        results = await asyncio.gather(
            go(1, "P1"), go(5, "P5")
        )
        oks = [r for r in results if r[0] == "ok"]
        forbiddens = [r for r in results if r[0] == "forbidden"]
        assert len(oks) == 1
        assert len(forbiddens) == 1
        final = tournament.ocr_sessions["final_0"]
        assert final["winning_team_index"] in (0, 1)
        assert final["uploaded_by"] == final["uploaded_by"]


# ---------------------------------------------------------------------------
# Гейт готовности и разрешение победителя
# ---------------------------------------------------------------------------

def _bound_row(name, ocr, k, d, a, dmg, uid):
    return {
        "ocr_name": ocr, "kills": k, "deaths": d, "assists": a,
        "dmg": dmg, "discord_name": name, "discord_id": uid,
        "discord_display": name, "source": "self",
    }


def _room(i, rows_a, rows_b):
    return {
        "room_index": i,
        "sha": f"sha{i}",
        "status": "awaiting_mapping",
        "panels": {"left": {"name": "A", "rows": []},
                   "right": {"name": "B", "rows": []}},
        "maps": {
            "0": {"panel": "left", "rows": rows_a, "confirmed": True},
            "1": {"panel": "right", "rows": rows_b, "confirmed": True},
        },
        "panel_picks": {},
        "message_id": None,
    }


def _full_session(t, rooms_expected=1, **kw):
    rows_a = [
        _bound_row("P1", "N1", 5, 2, 1, 100, 1),
        _bound_row("P2", "N2", 3, 4, 0, 80, 2),
        _bound_row("P3", "N3", 1, 1, 1, 40, 3),
        _bound_row("P4", "N4", 0, 0, 0, 10, 4),
    ]
    rows_b = [
        _bound_row(f"P{i}", f"N{i}", 4, 3, 2, 90, i)
        for i in (5, 6, 7, 8)
    ]
    base = dict(
        status="awaiting_mapping",
        rooms_expected=rooms_expected,
        rooms_received=len([r for r in range(1, rooms_expected + 1)]),
        rooms=[_room(i, rows_a, rows_b) for i in range(1, rooms_expected + 1)],
        winner_pending=False,
        uploaded_by=1,
        winning_team_index=0,
    )
    base.update(kw)
    s = _session(**base)
    t.ocr_sessions["final_0"] = s
    return s


class TestCheckOcrSessionReady:
    def test_ready_when_all_mapped_and_stats_complete(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t)
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert ready, reason

    def test_not_ready_without_rooms(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _session(status="awaiting_mapping")
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason == "unmapped"

    def test_not_ready_when_mapping_incomplete(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t)
        # Отвязываем одну строку — комната больше не сопоставлена
        s["rooms"][0]["maps"]["0"]["rows"][0]["discord_id"] = None
        s["rooms"][0]["maps"]["0"]["rows"][0]["discord_name"] = None
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason in ("unmapped", "missing_stats")

    def test_not_ready_when_stats_missing(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t)
        s["rooms"][0]["maps"]["0"]["rows"][0]["kills"] = None
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason == "stats_incomplete"

    def test_not_ready_when_roster_player_missing(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t)
        # P4 не привязан ни в одной комнате → нет статистики
        for r in s["rooms"]:
            for tm in r["maps"].values():
                for row in tm["rows"]:
                    if row["discord_id"] == 4:
                        row["discord_id"] = None
                        row["discord_name"] = None
        ready, reason, detail = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason in ("unmapped", "missing_stats")

    def test_closed_session_not_ready(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t, status="completed")
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason == "closed"

    def test_winner_required_by_default(self):
        from views.ocr_match_view import check_ocr_session_ready

        t = _tournament()
        s = _full_session(t, winning_team_index=None)
        ready, reason, _ = check_ocr_session_ready(t, s, 0, 1)
        assert not ready
        assert reason == "winner_unset"
        # Без требования победителя — данные готовы (админ-комплект
        # ждёт явного выбора команды при подтверждении)
        ready2, reason2, _ = check_ocr_session_ready(
            t, s, 0, 1, require_winner=False
        )
        assert ready2, reason2


class TestConfirmedWinner:
    def test_pinned_winner_wins_over_uploader(self):
        from views.ocr_match_view import _rooms_confirmed_winner

        t = _tournament()
        s = _session(
            winner_pending=False, uploaded_by=5, winning_team_index=1,
        )
        # uploaded_by — капитан B, но победитель зафиксирован иначе
        assert _rooms_confirmed_winner(t, s, 0, 1) == 1

    def test_fallback_to_uploader_team(self):
        from views.ocr_match_view import _rooms_confirmed_winner

        t = _tournament()
        s = _session(uploaded_by=5)  # legacy: winning_team_index None
        assert _rooms_confirmed_winner(t, s, 0, 1) == 1

    def test_nonparticipant_uploader_gives_none(self):
        from views.ocr_match_view import _rooms_confirmed_winner

        t = _tournament()
        s = _session(uploaded_by=88, winning_team_index=None)
        assert _rooms_confirmed_winner(t, s, 0, 1) is None

    def test_out_of_range_pinned_falls_back(self):
        from views.ocr_match_view import _rooms_confirmed_winner

        t = _tournament()
        s = _session(
            winner_pending=False, uploaded_by=5, winning_team_index=7,
        )
        assert _rooms_confirmed_winner(t, s, 0, 1) == 1


class TestSessionMmWinner:
    """Единая winner-логика для classic/squad в Ranked MM."""

    def test_pinned_winner(self):
        from views.ocr_match_view import _session_mm_winner

        t = _tournament()
        s = _session(winning_team_index=1, uploaded_by=1)
        assert _session_mm_winner(t, s, 0, 1) == 1

    def test_fallback_to_uploader_team(self):
        from views.ocr_match_view import _session_mm_winner

        t = _tournament()
        s = _session(winning_team_index=None, uploaded_by=5)
        assert _session_mm_winner(t, s, 0, 1) == 1

    def test_admin_uploader_gives_none(self):
        from views.ocr_match_view import _session_mm_winner

        t = _tournament()
        s = _session(winning_team_index=None, uploaded_by=88)
        assert _session_mm_winner(t, s, 0, 1) is None

    def test_mm_winner_for_uploader_helper(self):
        import cogs.ocr_upload as up

        t = _tournament()
        assert up._mm_winner_for_uploader(t, 0, 1, 5) == 1
        assert up._mm_winner_for_uploader(t, 0, 1, 88) is None
        # Не-MM турнир → None (winner по OCR, как раньше)
        assert up._mm_winner_for_uploader(
            _tournament(mm=False), 0, 1, 5
        ) is None

    def test_mm_upload_allowed_reupload(self):
        import cogs.ocr_upload as up

        t = _tournament()
        s = _session(
            winner_pending=False, uploaded_by=1, winning_team_index=0,
        )
        # Отправитель и админ — можно; чужой капитан — нельзя
        assert up._mm_upload_allowed(s, t, _user(1, "P1"))
        assert up._mm_upload_allowed(s, t, _user(88, "A", admin=True))
        assert not up._mm_upload_allowed(s, t, _user(5, "P5"))
        # Капитан зафиксированной команды-победителя — можно
        s2 = _session(
            winner_pending=False, uploaded_by=2, winning_team_index=0,
        )
        assert up._mm_upload_allowed(s2, t, _user(1, "P1"))
        assert not up._mm_upload_allowed(s2, t, _user(5, "P5"))


class _FakeResponse:
    def __init__(self):
        self.done = False

    def is_done(self):
        return self.done

    async def defer(self, ephemeral=False):
        self.done = True

    async def send_message(self, *a, **kw):
        self.done = True


class _FakeFollowup:
    def __init__(self):
        self.msgs = []

    async def send(self, content=None, **kw):
        self.msgs.append(content)


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class TestAutoFinalizeMmRooms:
    """Автоматическая финализация Ranked MM без кнопки подтверждения."""

    @pytest.mark.asyncio
    async def test_ready_session_finalizes_automatically(
        self, monkeypatch
    ):
        import views.ocr_match_view as v

        t = _tournament()
        s = _full_session(t)
        calls = []

        async def fake_finalize(interaction, tournament, mt, mi, winner):
            calls.append(winner)
            # Имитация успешной финализации
            s["status"] = "completed"

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        inter = _FakeInteraction(_user(1, "P1"))
        done, reason, _ = await v._maybe_auto_finalize_mm_rooms(
            inter, t, "final", 0
        )
        assert done and reason == ""
        assert calls == [0]  # зафиксированный победитель team0
        assert inter.followup.msgs  # уведомление отправлено

    @pytest.mark.asyncio
    async def test_not_ready_returns_reason_no_finalize(
        self, monkeypatch
    ):
        import views.ocr_match_view as v

        t = _tournament()
        s = _full_session(t)
        s["rooms"][0]["maps"]["0"]["rows"][0]["kills"] = None
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        done, reason, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert not done
        assert reason == "stats_incomplete"
        assert not calls

    @pytest.mark.asyncio
    async def test_unmapped_never_finalizes(self, monkeypatch):
        import views.ocr_match_view as v

        t = _tournament()
        s = _session(status="awaiting_mapping")
        t.ocr_sessions["final_0"] = s
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        done, reason, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert not done and reason == "unmapped"
        assert not calls

    @pytest.mark.asyncio
    async def test_second_call_after_finalize_is_noop(
        self, monkeypatch
    ):
        """Повторный запуск после завершения — без второго ELO."""
        import views.ocr_match_view as v

        t = _tournament()
        s = _full_session(t)
        calls = []

        async def fake_finalize(interaction, tournament, mt, mi, winner):
            calls.append(winner)
            s["status"] = "completed"

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        inter = _FakeInteraction(_user(1, "P1"))
        d1, _, _ = await v._maybe_auto_finalize_mm_rooms(
            inter, t, "final", 0
        )
        d2, _, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert d1 and not d2
        assert calls == [0]  # финализация ровно один раз

    @pytest.mark.asyncio
    async def test_finalizing_status_blocks_auto_retry(
        self, monkeypatch
    ):
        import views.ocr_match_view as v

        t = _tournament()
        s = _full_session(t, status="finalizing")
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        done, _, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert not done and not calls

    @pytest.mark.asyncio
    async def test_non_mm_tournament_noop(self, monkeypatch):
        import views.ocr_match_view as v

        t = _tournament(mm=False)
        s = _full_session(t)
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        done, _, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert not done and not calls

    @pytest.mark.asyncio
    async def test_admin_bundle_waits_for_explicit_pick(
        self, monkeypatch
    ):
        """Комплект админа (winner=None) не финализируется
        автоматически — нужна явная привязка к команде."""
        import views.ocr_match_view as v

        t = _tournament()
        s = _full_session(
            t, winner_pending=False, uploaded_by=88,
            winning_team_index=None,
        )
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        done, reason, _ = await v._maybe_auto_finalize_mm_rooms(
            _FakeInteraction(_user(1, "P1")), t, "final", 0
        )
        assert not done and reason == "winner_unset"
        assert not calls


class TestMappingLockUnification:
    """_mapping_lock и _room_lock — один лок на матч: mapping не может
    перезаписать finalizing/completed, finalize не стартует поверх
    незавершённой мутации."""

    def test_mapping_lock_is_room_lock(self):
        import cogs.ocr_upload as up
        import views.ocr_match_view as v

        key = v._session_key("final", 0)
        assert v._mapping_lock(key) is up._room_lock(key)

    def test_finalize_and_mapping_share_lock(self):
        import cogs.ocr_upload as up
        import views.ocr_match_view as v

        # Ключи, используемые finalize/cancel/claim и mapping-путями,
        # должны совпадать — иначе сериализации нет.
        assert v._session_key("final", 0) == up._session_key("final", 0)


class TestRecoverStuckFinalizing:
    @pytest.mark.asyncio
    async def test_recovers_finalizing_session(self, monkeypatch):
        import views.ocr_match_view as v
        from services.ocr_recovery import recover_stuck_finalizing

        t = _tournament()
        s = _full_session(t, status="finalizing")
        calls = []

        async def fake_finalize(interaction, tournament, mt, mi, winner):
            calls.append((mt, mi, winner))
            s["status"] = "completed"

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        bot = SimpleNamespace(
            get_guild=lambda gid: SimpleNamespace(id=gid),
            fetch_guild=lambda gid: None,
            user=None,
        )
        n = await recover_stuck_finalizing(bot, t)
        assert n == 1
        assert calls == [("final", 0, 0)]
        assert s["status"] == "completed"

    @pytest.mark.asyncio
    async def test_skips_non_finalizing(self, monkeypatch):
        from services.ocr_recovery import recover_stuck_finalizing

        t = _tournament()
        _full_session(t, status="awaiting_mapping")
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        import views.ocr_match_view as v
        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        bot = SimpleNamespace(
            get_guild=lambda gid: SimpleNamespace(id=gid), user=None,
        )
        n = await recover_stuck_finalizing(bot, t)
        assert n == 0 and not calls

    @pytest.mark.asyncio
    async def test_skips_unresolved_winner(self, monkeypatch):
        """Админ-комплект без победителя не финализируется
        автоматически — остаётся для ручного выбора."""
        import views.ocr_match_view as v
        from services.ocr_recovery import recover_stuck_finalizing

        t = _tournament()
        s = _full_session(
            t, status="finalizing", uploaded_by=88,
            winning_team_index=None,
        )
        calls = []

        async def fake_finalize(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(v, "finalize_ocr_match", fake_finalize)
        bot = SimpleNamespace(
            get_guild=lambda gid: SimpleNamespace(id=gid), user=None,
        )
        n = await recover_stuck_finalizing(bot, t)
        assert n == 0 and not calls
        assert s["status"] == "finalizing"  # не тронута


class TestCancelAllModes:
    @pytest.mark.asyncio
    async def test_cancel_classic_session(self, monkeypatch):
        """Админ-отмена работает и для classic/squad (не только rooms)."""
        import cogs.ocr_upload as up

        tournament = _tournament()
        # Classic-сессия: без mode="rooms"
        tournament.ocr_sessions["final_0"] = {
            "match_type": "final", "match_index": 0,
            "status": "awaiting_mapping",
            "uploaded_by": 1, "maps": {"0": {"rows": []}},
            "gen": 1,
        }
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        guild = SimpleNamespace(
            id=1,
            get_channel=lambda cid: None,
            fetch_channel=lambda cid: (_ for _ in ()).throw(
                Exception("no channel")
            ),
        )
        admin = _user(88, "Admin", admin=True)
        res = await up.cancel_rooms_session(
            guild, admin, tournament, "final", 0
        )
        assert res == "ok"
        assert "final_0" not in tournament.ocr_sessions

    @pytest.mark.asyncio
    async def test_cancel_idempotent_and_forbidden(self, monkeypatch):
        import cogs.ocr_upload as up

        tournament = _tournament()
        tournament.ocr_sessions["final_0"] = _session(
            status="awaiting_mapping"
        )
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda cid: None,
            fetch_channel=lambda cid: (_ for _ in ()).throw(
                Exception("no channel")
            ),
        )
        # Не-админ → forbidden, сессия жива
        res = await up.cancel_rooms_session(
            guild, _user(1, "P1"), tournament, "final", 0
        )
        assert res == "forbidden"
        assert "final_0" in tournament.ocr_sessions
        # Админ → ok; повтор → none (сессия удалена)
        res = await up.cancel_rooms_session(
            guild, _user(88, "A", admin=True), tournament, "final", 0
        )
        assert res == "ok"
        res = await up.cancel_rooms_session(
            guild, _user(88, "A", admin=True), tournament, "final", 0
        )
        assert res == "none"

    @pytest.mark.asyncio
    async def test_cancel_blocked_after_finalize(self, monkeypatch):
        """После начисления ELO обычная отмена запрещена."""
        import cogs.ocr_upload as up

        tournament = _tournament()
        tournament.processed_elo_matches = ["100_final_0"]
        tournament.ocr_sessions["final_0"] = _session(
            status="awaiting_mapping"
        )
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda cid: None,
            fetch_channel=lambda cid: (_ for _ in ()).throw(
                Exception("no channel")
            ),
        )
        res = await up.cancel_rooms_session(
            guild, _user(88, "A", admin=True), tournament, "final", 0
        )
        assert res == "done"
        assert "final_0" in tournament.ocr_sessions



# ---------------------------------------------------------------------------
# Squad: готовность к финализации и логика первого отправителя
# ---------------------------------------------------------------------------

def _squad_row(name, uid, **kw):
    r = {
        "ocr_name": f"ocr_{name}", "kills": 5, "deaths": 2,
        "assists": 1, "dmg": 100,
        "discord_name": name, "discord_id": uid,
        "discord_display": name, "source": "self",
    }
    r.update(kw)
    return r


def _squad_session(t, **kw):
    """Полностью готовая squad-сессия: оба отряда назначены,
    4 привязанных игрока на команду, уникальные Discord ID 1..8."""
    s = {
        "mode": "squad",
        "match_type": "final",
        "match_index": 0,
        "status": "awaiting_mapping",
        "teams": [0, 1],
        "squads": {"0": {"rows": []}, "1": {"rows": []}},
        "squad_pool": [],
        "panels": {},
        "maps": {
            "0": {
                "panel": "left",
                "rows": [
                    _squad_row(n, i + 1)
                    for i, n in enumerate(t.teams[0]["players"])
                ],
                "confirmed": True,
            },
            "1": {
                "panel": "right",
                "rows": [
                    _squad_row(n, i + 5)
                    for i, n in enumerate(t.teams[1]["players"])
                ],
                "confirmed": True,
            },
        },
        "panel_picks": {},
        "uploaded_by": 1,
        "uploaded_by_name": "P1",
        "winner_pending": False,
        "winning_team_index": 0,
        "gen": 1,
    }
    s.update(kw)
    t.ocr_sessions["final_0"] = s
    return s


class TestSquadSessionReady:
    def test_ready(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        assert _squad_session_ready(t, s, 0, 1) == (True, "ready")

    def test_missing_squad(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["squads"].pop("1")
        s["maps"].pop("1")
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "squad_missing"

    def test_unbound_row_blocks(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["0"]["rows"][0]["discord_name"] = None
        s["maps"]["0"]["rows"][0]["discord_id"] = None
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "unmapped"

    def test_duplicate_discord_id_blocks(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["1"]["rows"][0]["discord_id"] = 1
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "duplicate_id"

    def test_foreign_player_blocks(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["1"]["rows"][0]["discord_name"] = "P1"
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "foreign_player"

    def test_missing_discord_id_blocks(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["0"]["rows"][0]["discord_id"] = None
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "no_discord_id"

    def test_missing_stats_block(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["1"]["rows"][2]["dmg"] = None
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "missing_stats"

    def test_roster_count_mismatch(self):
        from views.ocr_match_view import _squad_session_ready

        t = _tournament()
        s = _squad_session(t)
        s["maps"]["0"]["rows"] = s["maps"]["0"]["rows"][:3]
        ready, reason = _squad_session_ready(t, s, 0, 1)
        assert not ready and reason == "roster_mismatch"


# ---------------------------------------------------------------------------
# Первый отправитель: squad-загрузки двух капитанов под _room_lock
# ---------------------------------------------------------------------------

class _Msg:
    def __init__(self, uid, name="Cap"):
        self.author = SimpleNamespace(
            id=uid, display_name=name, mention=f"<@{uid}>"
        )
        self.guild = SimpleNamespace(id=1)
        self.channel = SimpleNamespace(id=2)
        self.replies = []

    async def reply(self, *a, **kw):
        self.replies.append((a, kw))


def _squad_cog():
    from cogs.ocr_upload import OCRUploadCog

    cog = object.__new__(OCRUploadCog)

    async def _pub(*a, **kw):
        return None

    cog._publish_squad_message = _pub
    return cog


def _squad_payload(team):
    return {
        "players": [
            {"name": f"G{i}", "kills": 3, "deaths": 1,
             "assists": 2, "dmg": 80}
            for i in range(4)
        ],
        "banner": None,
    }


async def _squad_upload(cog, t, team, uid):
    """Реальная цепочка: _handle_squad_upload сам держит _room_lock
    только на мутации; состояние передаётся устаревшим (как после
    долгого OCR), Discord-publish заглушен."""
    msg = _Msg(uid)
    stats_ch = SimpleNamespace(id=3)
    await cog._handle_squad_upload(
        msg, t, 0, 1,
        {"squad_team": team, "tchannel": 2},
        "final", 0, "final_0", None, False, False,
        stats_ch, b"img", _squad_payload(team),
    )
    return msg


class TestFirstSenderSquad:
    @pytest.mark.asyncio
    async def test_second_squad_does_not_change_winner(self, monkeypatch):
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        await _squad_upload(cog, t, 0, 1)
        s = t.ocr_sessions["final_0"]
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1
        assert set(s["squads"]) == {"0"}

        await _squad_upload(cog, t, 1, 5)
        assert set(s["squads"]) == {"0", "1"}
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1
        assert s["status"] == "mapping"

    @pytest.mark.asyncio
    async def test_concurrent_first_uploads_single_winner(self, monkeypatch):
        """Два капитана шлют первый squad-файл одновременно:
        ровно одна сессия, победитель = команда реального
        первого писателя под _room_lock."""
        import cogs.ocr_upload as up
        import views.ocr_match_view as v

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        await asyncio.gather(
            _squad_upload(cog, t, 0, 1),
            _squad_upload(cog, t, 1, 5),
        )
        s = t.ocr_sessions["final_0"]
        assert s is not None
        winner_team = v._team_of_user(t, (0, 1), s["uploaded_by"])
        assert winner_team == s["winning_team_index"]
        assert set(s["squads"]) == {"0", "1"}

    @pytest.mark.asyncio
    async def test_third_squad_for_filled_team_rejected(self, monkeypatch):
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        await _squad_upload(cog, t, 0, 1)
        s = t.ocr_sessions["final_0"]
        rows_before = s["squads"]["0"]["rows"]

        msg = await _squad_upload(cog, t, 0, 2)
        assert s["squads"]["0"]["rows"] is rows_before
        assert any("уже загружен" in str(r) for r in msg.replies)

    @pytest.mark.asyncio
    async def test_squad_rejects_foreign_team_index(self, monkeypatch):
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        msg = await _squad_upload(cog, t, 7, 1)
        assert "final_0" not in t.ocr_sessions
        assert msg.replies


# ---------------------------------------------------------------------------
# Порядок принятия: pending-клейм ДО OCR + Discord API вне _room_lock
# ---------------------------------------------------------------------------

def _pending_claim(t, uid=1, team=None):
    """Сессия в том виде, в каком _on_message создаёт её ДО OCR."""
    import cogs.ocr_upload as up

    t1, t2 = 0, 1
    s = {
        "match_type": "final", "match_index": 0,
        "status": "awaiting_ocr", "mode": "pending",
        "teams": [t1, t2],
        "panels": {}, "squads": {}, "squad_pool": [],
        "maps": {}, "panel_picks": {},
        "uploaded_by": uid, "uploaded_by_name": f"U{uid}",
        "session_id": "pend1", "match_id": "100_final_0",
        "guild_id": 1, "winner_pending": False,
        "winning_team_index": up._mm_winner_for_uploader(
            t, t1, t2, uid
        ),
        "created_at": "now", "gen": 1, "ocr_pending": True,
    }
    t.ocr_sessions["final_0"] = s
    return s


class TestPendingUploadClaim:
    def test_release_only_empty_own_claim(self, monkeypatch):
        import cogs.ocr_upload as up

        monkeypatch.setattr(up.store, "set", lambda x: None)
        t = _tournament()
        s = _pending_claim(t, uid=1)
        # Чужой пользователь не снимает клейм.
        assert not up._release_pending_upload_claim(t, "final_0", 5)
        assert "final_0" in t.ocr_sessions
        # Владелец снимает пустой клейм.
        assert up._release_pending_upload_claim(t, "final_0", 1)
        assert "final_0" not in t.ocr_sessions

    def test_release_keeps_filled_claim(self, monkeypatch):
        import cogs.ocr_upload as up

        monkeypatch.setattr(up.store, "set", lambda x: None)
        t = _tournament()
        s = _pending_claim(t, uid=1)
        s["squads"]["0"] = {"rows": [1]}
        assert not up._release_pending_upload_claim(t, "final_0", 1)
        assert "final_0" in t.ocr_sessions

    def test_release_ignores_non_pending(self, monkeypatch):
        import cogs.ocr_upload as up

        monkeypatch.setattr(up.store, "set", lambda x: None)
        t = _tournament()
        t.ocr_sessions["final_0"] = _session(status="awaiting_mapping")
        t.ocr_sessions["final_0"]["uploaded_by"] = 1
        assert not up._release_pending_upload_claim(t, "final_0", 1)
        assert "final_0" in t.ocr_sessions

    @pytest.mark.asyncio
    async def test_other_captain_cannot_fill_pending_claim(
        self, monkeypatch
    ):
        """Капитан B, чей OCR завершился раньше, НЕ может заполнить
        pending-клейм капитана A — winner остаётся за A."""
        import cogs.ocr_upload as up

        t = _tournament()
        s = _pending_claim(t, uid=1)  # капитан команды 0
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        # Капитан команды 1 (uid 5) — его файл «завершил OCR первым».
        msg = await _squad_upload(cog, t, 1, 5)
        assert s["uploaded_by"] == 1
        assert s["winning_team_index"] == 0
        assert not s.get("squads")
        assert msg.replies  # отказ

    @pytest.mark.asyncio
    async def test_owner_fills_own_pending_claim(self, monkeypatch):
        """Владелец клейма получает squad-сессию с тем же победителем."""
        import cogs.ocr_upload as up

        t = _tournament()
        s = _pending_claim(t, uid=1)
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        await _squad_upload(cog, t, 0, 1)
        assert s["mode"] == "squad"
        assert s["status"] in ("awaiting_mapping", "mapping")
        assert s["uploaded_by"] == 1
        assert s["winning_team_index"] == 0
        assert set(s["squads"]) == {"0"}

    @pytest.mark.asyncio
    async def test_pool_upload_keeps_first_sender(self, monkeypatch):
        """squad_pool-путь (без squad_team): вторая загрузка идёт
        в пул, победитель первого отправителя не меняется."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        # Первый файл — капитан команды 0, без привязки команды.
        msg1 = _Msg(1)
        await cog._handle_squad_upload(
            msg1, t, 0, 1, {}, "final", 0, "final_0",
            None, False, False, SimpleNamespace(id=3),
            b"img", _squad_payload(0),
        )
        s = t.ocr_sessions["final_0"]
        assert s["winning_team_index"] == 0
        assert len(s["squad_pool"]) == 1

        # Второй файл — капитан команды 1 → тоже в пул.
        msg2 = _Msg(5)
        await cog._handle_squad_upload(
            msg2, t, 0, 1, {}, "final", 0, "final_0",
            None, False, False, SimpleNamespace(id=3),
            b"img", _squad_payload(1),
        )
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1
        assert len(s["squad_pool"]) == 2


class TestDiscordApiOutsideLock:
    @pytest.mark.asyncio
    async def test_publish_runs_without_room_lock(self, monkeypatch):
        """_publish_squad_message выполняется когда _room_lock
        уже свободен — медленный Discord не держит лок."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()
        lock_state = []

        async def slow_pub(*a, **kw):
            # Проверяем: лок НЕ захвачен во время Discord API.
            lock = up._room_lock("final_0")
            lock_state.append(lock.locked())
            await asyncio.sleep(0.01)
            return 777

        cog._publish_squad_message = slow_pub
        msg = await _squad_upload(cog, t, 0, 1)
        assert lock_state == [False]
        # writeback message_id под локом после публикации.
        s = t.ocr_sessions["final_0"]
        entry = s["squads"]["0"]
        assert entry.get("message_id") == 777

    @pytest.mark.asyncio
    async def test_slow_discord_does_not_block_cancel(self, monkeypatch):
        """Пока publish «висит» в Discord API, отмена комплекта
        не ждёт _room_lock — лока нет."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        release = asyncio.Event()

        async def hanging_pub(*a, **kw):
            await release.wait()
            return None

        cog._publish_squad_message = hanging_pub

        async def do_upload():
            await _squad_upload(cog, t, 0, 1)

        async def do_cancel():
            # ждём создания сессии
            for _ in range(100):
                if t.ocr_sessions.get("final_0"):
                    break
                await asyncio.sleep(0.005)
            guild = SimpleNamespace(
                id=1,
                get_channel=lambda c: None,
                fetch_channel=lambda c: (_ for _ in ()).throw(
                    Exception("no channel")
                ),
            )
            return await asyncio.wait_for(
                up.cancel_rooms_session(
                    guild, _user(88, "A", admin=True), t, "final", 0
                ),
                timeout=3,
            )

        up_task = asyncio.create_task(do_upload())
        cancel_task = asyncio.create_task(do_cancel())
        await asyncio.sleep(0.05)
        release.set()
        _, cres = await asyncio.gather(up_task, cancel_task)
        # Отмена либо успела до publish-writeback, либо видит
        # финализированную/удалённую сессию — но не висит вечно.
        assert cres in ("ok", "done", "none")


# ---------------------------------------------------------------------------
# Restart pending-клейма + публикация после отмены + полный on_message
# ---------------------------------------------------------------------------

class TestPendingClaimOwner:
    def test_helper_owner_and_guards(self, monkeypatch):
        import cogs.ocr_upload as up

        monkeypatch.setattr(up.store, "set", lambda x: None)
        t = _tournament()
        s = _pending_claim(t, uid=1)
        assert up._is_pending_claim_owner(s, 1)
        assert up._is_pending_claim_owner(s, "1")
        assert not up._is_pending_claim_owner(s, 5)
        # Заполненная squad-сессия — не pending.
        s["mode"] = "squad"
        assert not up._is_pending_claim_owner(s, 1)
        # Не-awaiting_ocr статус — не pending.
        s["mode"] = "pending"
        s["status"] = "awaiting_mapping"
        assert not up._is_pending_claim_owner(s, 1)
        assert not up._is_pending_claim_owner(None, 1)

    @pytest.mark.asyncio
    async def test_owner_refills_claim_after_restart(
        self, monkeypatch
    ):
        """Pending-клейм «переживает рестарт» (он в JsonStore):
        владелец повторно отправляет файл и заполняет его,
        победитель не меняется; чужой капитан — отказ."""
        import cogs.ocr_upload as up

        t = _tournament()
        s = _pending_claim(t, uid=1)
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        cog = _squad_cog()

        # «Рестарт»: состояние только в t.ocr_sessions — новых
        # in-flight OCR-задач нет. Владелец досылает файл.
        await _squad_upload(cog, t, 0, 1)
        assert s["mode"] == "squad"
        assert s["uploaded_by"] == 1
        assert s["winning_team_index"] == 0
        assert set(s["squads"]) == {"0"}

        # Чужой капитан по-прежнему не может перехватить/заполнить.
        msg = await _squad_upload(cog, t, 1, 5)
        # squad-режим с loaded<2 — вторая загрузка легальна,
        # но winner и uploaded_by остаются за первым.
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1


class TestPublishAfterCancel:
    @pytest.mark.asyncio
    async def test_cancel_during_publish_neutralizes_message(
        self, monkeypatch
    ):
        """Discord publish «висит», админ отменяет комплект:
        message_id не сохраняется, устаревшее сообщение гасится."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        neutralized = []

        async def rec_neutralize(channel, mid):
            neutralized.append(mid)

        monkeypatch.setattr(
            up, "_neutralize_stale_ocr_message", rec_neutralize
        )
        cog = _squad_cog()

        async def canceling_pub(*a, **kw):
            # Имитация admin-cancel, пока Discord API «отвечал».
            async with up._room_lock("final_0"):
                t.ocr_sessions.pop("final_0", None)
            return 555

        cog._publish_squad_message = canceling_pub
        await _squad_upload(cog, t, 0, 1)

        assert "final_0" not in t.ocr_sessions
        assert neutralized == [555]

    @pytest.mark.asyncio
    async def test_cancel_during_edit_neutralizes_old_message(
        self, monkeypatch
    ):
        """Reupload edit-путь: сообщение уже имеет message_id;
        отмена во время edit → гасим старый message_id."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        neutralized = []

        async def rec_neutralize(channel, mid):
            neutralized.append(mid)

        monkeypatch.setattr(
            up, "_neutralize_stale_ocr_message", rec_neutralize
        )
        cog = _squad_cog()

        # Первый upload — сообщение опубликовано (id=777).
        async def pub1(*a, **kw):
            return 777

        cog._publish_squad_message = pub1
        await _squad_upload(cog, t, 0, 1)
        s = t.ocr_sessions["final_0"]
        assert s["squads"]["0"]["message_id"] == 777

        # Reupload того же отряда — edit-путь (publish → None).
        async def canceling_pub(*a, **kw):
            async with up._room_lock("final_0"):
                t.ocr_sessions.pop("final_0", None)
            return None

        cog._publish_squad_message = canceling_pub
        msg = _Msg(1)
        await cog._handle_squad_upload(
            msg, t, 0, 1,
            {"squad_team": 0, "tchannel": 2, "reupload": True},
            "final", 0, "final_0",
            s, True, True, SimpleNamespace(id=3),
            b"img2", _squad_payload(0),
        )
        assert "final_0" not in t.ocr_sessions
        assert neutralized == [777]

    @pytest.mark.asyncio
    async def test_new_bundle_message_survives(self, monkeypatch):
        """Отмена старого комплекта + новый комплект: нейтрализуется
        только сообщение СТАРОГО publish, не нового."""
        import cogs.ocr_upload as up

        t = _tournament()
        monkeypatch.setattr(up.store, "get", lambda g, c: t)
        monkeypatch.setattr(up.store, "set", lambda x: None)
        neutralized = []

        async def rec_neutralize(channel, mid):
            neutralized.append(mid)

        monkeypatch.setattr(
            up, "_neutralize_stale_ocr_message", rec_neutralize
        )
        cog = _squad_cog()

        async def cancel_then_new(*a, **kw):
            # Отменяем старую сессию и создаём новую — затем
            # возвращаем id сообщения, опубликованного для СТАРОЙ.
            async with up._room_lock("final_0"):
                old = t.ocr_sessions.pop("final_0", None)
                t.ocr_sessions["final_0"] = {
                    "match_type": "final", "match_index": 0,
                    "status": "awaiting_mapping", "mode": "squad",
                    "teams": [0, 1], "squads": {}, "squad_pool": [],
                    "maps": {}, "panels": {}, "uploaded_by": 5,
                    "winning_team_index": 1, "gen": 9,
                }
            return 999

        cog._publish_squad_upload = None  # не используется
        cog._publish_squad_message = cancel_then_new
        await _squad_upload(cog, t, 0, 1)
        # Новый комплект не тронут; старое сообщение (999)
        # помечено устаревшим — его кнопки неактивны.
        assert t.ocr_sessions["final_0"]["uploaded_by"] == 5
        assert neutralized == [999]


# ---------------------------------------------------------------------------
# Полный путь on_message: реальный приём → pre-claim → OCR → handler
# ---------------------------------------------------------------------------

class _FakeAttachment:
    def __init__(self, data: bytes, name="shot.png"):
        self._data = data
        self.content_type = "image/png"
        self.filename = name
        self.size = len(data)

    async def read(self):
        return self._data


async def _raise_channel(cid):
    raise Exception("no such channel")


def _fake_guild():
    return SimpleNamespace(
        id=1,
        get_channel=lambda c: None,
        fetch_channel=_raise_channel,
    )


class _FakeChannel:
    def __init__(self, cid=7):
        self.id = cid
        self.sent = []
        self._n = 0

    async def send(self, *a, **k):
        self._n += 1
        self.sent.append((a, k))
        return SimpleNamespace(id=9000 + self._n)

    async def fetch_message(self, mid):
        raise Exception("not found")


def _fake_message(uid, name, data, channel):
    user = _user(uid, name)
    user.bot = False
    user.mention = f"<@{uid}>"
    user.guild = _fake_guild()
    msg = SimpleNamespace(
        author=user,
        guild=user.guild,
        channel=channel,
        attachments=[_FakeAttachment(data)],
        replies=[],
    )

    async def _reply(content=None, **kw):
        msg.replies.append(str(content))

    async def _react(e):
        pass

    msg.reply = _reply
    msg.add_reaction = _react
    return msg


def _squad_parse_payload():
    return {
        "players": [
            {"name": f"GN{i}", "kills": 3, "deaths": 1,
             "assists": 2, "dmg": 80}
            for i in range(4)
        ],
        "banner": None,
    }


def _classic_parse_payload():
    return {
        "team_a_name": "ALPHA",
        "team_b_name": "BRAVO",
        "team_a": [
            {"name": f"A{i}", "kills": 5, "deaths": 2,
             "assists": 3, "dmg": 100}
            for i in range(4)
        ],
        "team_b": [
            {"name": f"B{i}", "kills": 4, "deaths": 3,
             "assists": 2, "dmg": 90}
            for i in range(4)
        ],
        "score": [2, 1],
        "banner": "win",
        "top5": False,
    }


def _patch_on_message_common(monkeypatch, t, meta_for):
    """Общие заглушки для on_message: store, pending-meta, каналы."""
    import cogs.ocr_upload as up

    monkeypatch.setattr(up.store, "get_by_guild", lambda gid: [t])
    monkeypatch.setattr(up.store, "get", lambda g, c: t)
    monkeypatch.setattr(up.store, "set", lambda x: None)
    monkeypatch.setattr(up, "pop_pending_upload", lambda *a: None)
    monkeypatch.setattr(
        up, "pop_pending_upload_any",
        lambda *a: meta_for(a[1] if len(a) > 1 else 0),
    )
    try:
        import services.match_announcer as ma

        async def _uc(*a, **kw):
            return None

        monkeypatch.setattr(ma, "update_match_card", _uc)
    except Exception:
        pass


class TestOnMessageOrdering:
    @pytest.mark.asyncio
    async def test_squad_first_accepted_wins_over_faster_ocr(
        self, monkeypatch
    ):
        """Полный путь: капитан A (uid 1, team0) отправил файл первым,
        его OCR медленный; капитан B (uid 5) шлёт позже — но уже НЕ
        может запустить OCR, т.к. pending-клейм занят. После
        завершения OCR A его squad заполняет клейм, winner = team0;
        повторная отправка B идёт вторым отрядом без смены winner."""
        import threading

        import cogs.ocr_upload as up

        t = _tournament()
        t.match_channels = {}
        _patch_on_message_common(
            monkeypatch, t,
            lambda uid: {
                "match_type": "final", "match_index": 0,
                "tchannel": 2, "squad_team": 0 if uid == 1 else 1,
            },
        )

        events = {
            b"AAAA": threading.Event(),
            b"BBBB": threading.Event(),
            b"CCCC": threading.Event(),
        }

        def fake_boxes(data):
            events[data].wait(10)
            return ["box"]

        monkeypatch.setattr(up, "_ocr_boxes", fake_boxes)
        monkeypatch.setattr(
            up, "_parse_squad", lambda boxes: _squad_parse_payload()
        )
        monkeypatch.setattr(up, "_parse_classic", lambda boxes: None)

        cog = _squad_cog()
        pub_ids = iter(range(600, 700))

        async def _pub(*a, **kw):
            return next(pub_ids)

        cog._publish_squad_message = _pub

        chan = _FakeChannel()
        msg_a = _fake_message(1, "CapA", b"AAAA", chan)
        msg_b = _fake_message(5, "CapB", b"BBBB", chan)

        task_a = asyncio.create_task(cog.on_message(msg_a))
        for _ in range(200):
            if t.ocr_sessions.get("final_0"):
                break
            await asyncio.sleep(0.005)
        s = t.ocr_sessions["final_0"]
        assert s["status"] == "awaiting_ocr"
        assert s["uploaded_by"] == 1
        assert s["winning_team_index"] == 0

        await cog.on_message(msg_b)
        assert any("уже загружен" in r for r in msg_b.replies)
        assert not events[b"BBBB"].is_set()

        events[b"AAAA"].set()
        await asyncio.wait_for(task_a, timeout=10)
        assert s["mode"] == "squad"
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1
        assert set(s["squads"]) == {"0"}

        events[b"CCCC"].set()
        msg_b2 = _fake_message(5, "CapB", b"CCCC", chan)
        msg_b2.attachments[0]._data = b"BBBB"
        await cog.on_message(msg_b2)
        assert set(s["squads"]) == {"0", "1"}
        assert s["winning_team_index"] == 0
        assert s["status"] == "mapping"

    @pytest.mark.asyncio
    async def test_classic_first_accepted_wins(self, monkeypatch):
        """Classic: первый принятый файл определяет победителя,
        а не порядок завершения OCR."""
        import threading

        import cogs.ocr_upload as up

        t = _tournament()
        t.match_channels = {}
        _patch_on_message_common(
            monkeypatch, t,
            lambda uid: {
                "match_type": "final", "match_index": 0,
                "tchannel": 2,
            },
        )

        events = {
            b"AAAA": threading.Event(),
            b"BBBB": threading.Event(),
        }

        def fake_boxes(data):
            events[data].wait(10)
            return ["box"]

        monkeypatch.setattr(up, "_ocr_boxes", fake_boxes)
        monkeypatch.setattr(up, "_parse_squad", lambda boxes: None)
        monkeypatch.setattr(
            up, "_parse_classic", lambda boxes: _classic_parse_payload()
        )

        import services.ocr_service as ocrs

        monkeypatch.setattr(
            ocrs, "detect_winner_side", lambda d, b: "left"
        )

        send_ids = iter(range(700, 800))

        async def _send(channel, data, view, caption=None):
            return SimpleNamespace(id=next(send_ids))

        monkeypatch.setattr(up, "_send_results_message", _send)

        cog = object.__new__(up.OCRUploadCog)
        chan = _FakeChannel()
        msg_a = _fake_message(1, "CapA", b"AAAA", chan)
        msg_b = _fake_message(5, "CapB", b"BBBB", chan)

        task_a = asyncio.create_task(cog.on_message(msg_a))
        for _ in range(200):
            if t.ocr_sessions.get("final_0"):
                break
            await asyncio.sleep(0.005)
        s = t.ocr_sessions["final_0"]
        assert s["status"] == "awaiting_ocr"
        assert s["winning_team_index"] == 0

        await cog.on_message(msg_b)
        assert any("уже загружен" in r for r in msg_b.replies)

        events[b"AAAA"].set()
        await asyncio.wait_for(task_a, timeout=10)
        # Classic-путь заменяет pending-клейм новым dict —
        # перечитываем актуальную сессию.
        s = t.ocr_sessions["final_0"]
        assert s["status"] == "awaiting_mapping"
        assert s["winning_team_index"] == 0
        assert s["uploaded_by"] == 1
        assert s.get("message_id") is not None


# ---------------------------------------------------------------------------
# Отмена комплекта: рабочая карточка матча сохраняется
# ---------------------------------------------------------------------------

class _CancelMsg:
    def __init__(self, mid):
        self.id = mid
        self.edits = []
        self.deleted = False
        self.attachments = []

    async def edit(self, **kw):
        self.edits.append(kw)

    async def delete(self):
        self.deleted = True


class _CancelChan:
    def __init__(self, cid, messages):
        self.id = cid
        self._messages = messages

    async def fetch_message(self, mid):
        m = self._messages.get(int(mid))
        if m is None:
            raise Exception("not found")
        return m

    async def send(self, **kw):
        raise Exception("unexpected send")


def _cancel_guild(channels):
    return SimpleNamespace(
        id=1,
        get_channel=lambda cid: channels.get(int(cid)),
        fetch_channel=lambda cid: (_ for _ in ()).throw(Exception("nf")),
    )


class TestCancelKeepsWorkCard:
    @pytest.mark.asyncio
    async def test_progress_eq_work_card_not_blanked(self, monkeypatch):
        """progress_message_id == work_message_id (обычный турнир):
        отмена НЕ должна перезаписывать карточку текстом
        «комплект отменён» — она остаётся рабочей и update_match_card
        возвращает её в состояние «Ожидание скриншотов»."""
        import cogs.ocr_upload as up
        import services.match_announcer as ma

        tournament = _tournament(mm=False)
        tournament.match_cards = {
            "final_0": {"work_message_id": 500, "work_channel_id": 9}
        }
        sess = _session(
            status="collecting",
            progress_message_id=500,
            progress_channel_id=9,
            ocr_channel_id=9,
        )
        tournament.ocr_sessions["final_0"] = sess

        work_msg = _CancelMsg(500)
        chan = _CancelChan(9, {500: work_msg})
        guild = _cancel_guild({9: chan})

        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        calls = []

        async def fake_update(*a, **kw):
            calls.append(kw.get("state"))

        monkeypatch.setattr(ma, "update_match_card", fake_update)

        res = await up.cancel_rooms_session(
            guild, _user(88, "A", admin=True), tournament, "final", 0
        )
        assert res == "ok"
        assert "final_0" not in tournament.ocr_sessions
        # Карточка матча запрошена обратно в active-состояние
        assert calls == ["active"]
        # И главное: сообщение НЕ было затёрто «комплект отменён»
        for e in work_msg.edits:
            assert "отменён" not in str(e.get("content", ""))

    @pytest.mark.asyncio
    async def test_separate_progress_message_still_cancelled(
        self, monkeypatch
    ):
        """Если progress-сообщение отдельное от work-карточки
        (legacy/MM), оно по-прежнему гасится текстом отмены."""
        import cogs.ocr_upload as up
        import services.match_announcer as ma

        tournament = _tournament(mm=False)
        tournament.match_cards = {
            "final_0": {"work_message_id": 500, "work_channel_id": 9}
        }
        sess = _session(
            status="collecting",
            progress_message_id=600,
            progress_channel_id=9,
            ocr_channel_id=9,
        )
        tournament.ocr_sessions["final_0"] = sess

        prog_msg = _CancelMsg(600)
        chan = _CancelChan(9, {600: prog_msg})
        guild = _cancel_guild({9: chan})

        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        async def fake_update(*a, **kw):
            return None

        monkeypatch.setattr(ma, "update_match_card", fake_update)

        res = await up.cancel_rooms_session(
            guild, _user(88, "A", admin=True), tournament, "final", 0
        )
        assert res == "ok"
        assert prog_msg.edits
        assert "отменён" in str(prog_msg.edits[0].get("content", ""))
