"""Tournaments 2.0: 16/32 × Draft/Balanced SR, bracket, сериализация."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from models.tournament import (
    FormationMode,
    PICK_ORDERS,
    SEMIFINAL_PAIRINGS,
    MatchContext,
    Tournament,
    TournamentPhase,
    TournamentSize,
    resolve_match_context,
)
from utils.balanced_sr import balanced_sr_teams, duplicate_user_ids


def _mk_tournament(size: TournamentSize, formation: FormationMode) -> Tournament:
    return Tournament(
        guild_id=1,
        channel_id=2,
        size=size,
        formation_mode=formation,
    )


def _fill_draft_tournament(t: Tournament) -> None:
    """Заполнить круги для драфта: circle1 = капитаны, 2-4 — пулы."""
    n = t.captain_count
    idx = 0
    for circle in range(1, 5):
        players = []
        for _ in range(n):
            name = f"P{idx}"
            players.append(name)
            t.player_user_ids[name] = 1000 + idx
            idx += 1
        t.set_circle_list(circle, players)
    t.captains = list(t.circle1)


def _run_draft(t: Tournament) -> None:
    """Проиграть драфт до конца, выбирая первого доступного."""
    t.shuffle_circles()
    t.start_draft()
    guard = 0
    while t.phase == TournamentPhase.DRAFT and guard < 500:
        guard += 1
        pos = t.current_picker_position()
        if pos is None:
            break
        key = str(t.current_circle)
        player = t.available[key][0]
        t.pick_player(pos, player)
        t.advance_after_pick()


# ---------------------------------------------------------------------------
# Draft: 16 игроков (4 капитана)
# ---------------------------------------------------------------------------

class TestDraft16:
    def test_pick_orders_4_captains(self):
        orders = PICK_ORDERS[4]
        assert orders["2"]["order"] == [0, 1, 2, 3]
        assert orders["3"]["order"] == [1, 2, 3, 0]
        assert orders["4"]["order"] == [3, 2, 0, 1]

    def test_draft_produces_4_teams_of_4(self):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        assert len(t.teams) == 4
        for team in t.teams:
            assert len(team["players"]) == 4
        names = []
        for team in t.teams:
            names.append(team["captain"])
            names.extend(team["players"])
        assert len(set(names)) == 16

    def test_bracket_semifinals_then_final(self):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        assert t.phase == TournamentPhase.SEMIFINALS
        assert len(t.semifinal_matches) == 2
        assert list(t.semifinal_matches) in [list(p) for p in SEMIFINAL_PAIRINGS]
        teams_in = {i for m in t.semifinal_matches for i in m}
        assert teams_in == {0, 1, 2, 3}

    def test_single_elimination_semifinal(self):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        match = t.semifinal_matches[0]
        winner = match[0]
        loser = match[1]
        t.confirm_semifinal_winner(0, winner)
        assert loser in t.eliminated_teams


# ---------------------------------------------------------------------------
# Draft: 32 игрока (8 капитанов)
# ---------------------------------------------------------------------------

class TestDraft32:
    def test_pick_orders_8_captains(self):
        orders = PICK_ORDERS[8]
        assert orders["2"]["order"] == [0, 1, 2, 3, 4, 5, 6, 7]
        assert orders["3"]["order"] == [4, 7, 3, 6, 2, 5, 1, 0]
        assert orders["4"]["order"] == [5, 6, 7, 1, 0, 2, 3, 4]

    def test_draft_produces_8_teams_of_4(self):
        t = _mk_tournament(TournamentSize.THIRTY_TWO, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        assert len(t.teams) == 8
        names = []
        for team in t.teams:
            assert len(team["players"]) == 4
            names.append(team["captain"])
            names.extend(team["players"])
        assert len(set(names)) == 32

    def test_bracket_qualifiers_fixed_pairs(self):
        t = _mk_tournament(TournamentSize.THIRTY_TWO, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        assert t.phase == TournamentPhase.QUALIFIERS
        assert t.qualifier_matches == [(0, 1), (2, 3), (4, 5), (6, 7)]

    def test_qualifier_to_semifinal_to_final(self):
        t = _mk_tournament(TournamentSize.THIRTY_TWO, FormationMode.ELO)
        _fill_draft_tournament(t)
        _run_draft(t)
        for i in range(len(t.qualifier_matches)):
            t.confirm_qualifier_winner(i, t.qualifier_matches[i][0])
        assert t.phase == TournamentPhase.SEMIFINALS
        assert len(t.semifinal_matches) == 2
        for i in range(len(t.semifinal_matches)):
            t.confirm_semifinal_winner(i, t.semifinal_matches[i][0])
        assert t.phase == TournamentPhase.FINAL
        assert len(t.final_teams) == 2


# ---------------------------------------------------------------------------
# Balanced SR
# ---------------------------------------------------------------------------

def _sr_players(n: int, sr_fn) -> list[tuple[str, float]]:
    return [(f"P{i}", sr_fn(i)) for i in range(n)]


class TestBalancedSRAlgorithm:
    def test_16_players_4_teams_quartiles(self):
        players = _sr_players(16, lambda i: float(i + 1))
        teams = balanced_sr_teams(
            players,
            {n: i + 1 for i, (n, _) in enumerate(players)},
            rng=random.Random(42),
        )
        assert teams is not None
        assert len(teams) == 4
        for team in teams:
            assert len(team) == 4
        all_names = [n for t in teams for n in t]
        assert len(set(all_names)) == 16

        sorted_names = [n for n, _ in sorted(players, key=lambda p: -p[1])]
        quartiles = [set(sorted_names[i * 4:(i + 1) * 4]) for i in range(4)]
        for team in teams:
            for q in quartiles:
                assert len(set(team) & q) == 1

    def test_32_players_8_teams_quartiles(self):
        players = _sr_players(32, lambda i: float(i + 1))
        teams = balanced_sr_teams(players, None, rng=random.Random(7))
        assert teams is not None
        assert len(teams) == 8
        sorted_names = [n for n, _ in sorted(players, key=lambda p: -p[1])]
        quartiles = [set(sorted_names[i * 8:(i + 1) * 8]) for i in range(4)]
        for team in teams:
            assert len(team) == 4
            for q in quartiles:
                assert len(set(team) & q) == 1

    def test_minimizes_sr_spread(self):
        players = _sr_players(16, lambda i: float(i + 1))
        teams = balanced_sr_teams(players, None, rng=random.Random(1))
        srs = dict(players)
        totals = [sum(srs[n] for n in t) for t in teams]
        spread = max(totals) - min(totals)
        assert spread <= 4.0, f"spread too big: {spread} totals={totals}"

    def test_invalid_counts(self):
        assert balanced_sr_teams(_sr_players(7, lambda i: 1.0), None) is None
        assert balanced_sr_teams(_sr_players(10, lambda i: 1.0), None) is None
        assert balanced_sr_teams(_sr_players(12, lambda i: 1.0), None) is not None

    def test_duplicate_user_ids(self):
        assert duplicate_user_ids({"a": 1, "b": 1, "c": 2}) == [1]
        assert duplicate_user_ids({"a": 0, "b": -5, "c": 3}) == []
        assert duplicate_user_ids({"a": 7, "b": 7, "c": 7}) == [7]


class TestBalancedSRTournament:
    @pytest.fixture
    def stats_store(self, monkeypatch):
        import storage.player_stats_store as pss

        sr_map: dict[int, float] = {}

        async def fake_get(guild_id, user_id):
            sr = sr_map.get(user_id)
            if sr is None:
                return None
            return SimpleNamespace(skill_rating=sr, games=10)

        monkeypatch.setattr(pss.player_stats_store, "get", fake_get)
        return sr_map

    def _fill(self, t: Tournament) -> None:
        """Balanced SR: регистрация через общий пул (без кругов)."""
        n = int(t.size.value)
        for i in range(n):
            name = f"P{i}"
            t.players_pool.append(name)
            t.player_user_ids[name] = 2000 + i

    @pytest.mark.asyncio
    async def test_16_balanced_sr_teams_and_bracket(self, stats_store):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        self._fill(t)
        for i in range(16):
            stats_store[2000 + i] = float(16 - i)

        err = await t.distribute_balanced_sr(1)
        assert err is None
        assert len(t.teams) == 4
        for team in t.teams:
            assert len(team["players"]) == 4
        assert t.phase == TournamentPhase.SEMIFINALS
        assert len(t.semifinal_matches) == 2
        # Пул очищен, круги не использовались
        assert t.players_pool == []
        assert t.circle1 == [] and t.circle4 == []

    @pytest.mark.asyncio
    async def test_32_balanced_sr_teams_and_bracket(self, stats_store):
        t = _mk_tournament(TournamentSize.THIRTY_TWO, FormationMode.BALANCED_SR)
        self._fill(t)
        for i in range(32):
            stats_store[2000 + i] = float(32 - i)

        err = await t.distribute_balanced_sr(1)
        assert err is None
        assert len(t.teams) == 8
        assert t.phase == TournamentPhase.QUALIFIERS
        assert t.qualifier_matches == [(0, 1), (2, 3), (4, 5), (6, 7)]

    @pytest.mark.asyncio
    async def test_duplicate_discord_ids_rejected(self, stats_store):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        self._fill(t)
        t.player_user_ids["P5"] = t.player_user_ids["P0"]
        stats_store[2000] = 9.0
        stats_store[2000 + 5] = 8.0

        err = await t.distribute_balanced_sr(1)
        assert err is not None
        assert t.teams == []

    @pytest.mark.asyncio
    async def test_circles_not_required(self, stats_store):
        """Balanced SR не требует заполнения circle1-4."""
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        self._fill(t)
        for i in range(16):
            stats_store[2000 + i] = float(i + 1)
        assert not t.is_setup_complete  # круги пусты — готовность по пулу
        err = await t.distribute_balanced_sr(1)
        assert err is None
        assert len(t.teams) == 4


# ---------------------------------------------------------------------------
# Сериализация / восстановление после рестарта
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_roundtrip_preserves_identity_and_fields(self):
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        t.phase = TournamentPhase.SEMIFINALS
        t.semifinal_matches = [(0, 1), (2, 3)]
        t.semifinal_pending_winners = [0, None]
        t.temp_match_stats = {"m1": {"P1": {"kills": 3}}}
        t.betting_phase = "semifinal"
        t.betting_phase_start_time = 123.45
        t.betting_duration = 180
        t.betting_open = True
        t.betting_results = {"P1": 100}
        t.draft_pick_duration = 45
        t.draft_pick_start_time = "2024-01-01T00:00:00"
        t.match_openers = {("semifinal", 0): 555}
        t.teams = [{"captain": "A", "players": ["A", "B", "C", "D"],
                    "circle1": "A", "circle2": "B", "circle3": "C", "circle4": "D"}]
        t.player_user_ids = {"A": 1, "B": 2}
        t.organizer_id = 99
        t.message_id = 777

        original_id = t.id
        original_creation = t.creation_time

        data = t.to_dict()
        t2 = Tournament.from_dict(data)

        assert t2.id == original_id
        assert t2.creation_time == original_creation
        assert t2.formation_mode == FormationMode.BALANCED_SR
        assert t2.phase == TournamentPhase.SEMIFINALS
        assert t2.size == TournamentSize.SIXTEEN
        assert t2.semifinal_matches == [(0, 1), (2, 3)]
        assert t2.semifinal_pending_winners == [0, None]
        assert t2.temp_match_stats == {"m1": {"P1": {"kills": 3}}}
        assert t2.betting_phase == "semifinal"
        assert t2.betting_phase_start_time == 123.45
        assert t2.betting_open is True
        assert t2.betting_results == {"P1": 100}
        assert t2.draft_pick_duration == 45
        assert t2.draft_pick_start_time == "2024-01-01T00:00:00"
        assert t2.match_openers == {("semifinal", 0): 555}
        assert t2.teams == t.teams
        assert t2.player_user_ids == {"A": 1, "B": 2}
        assert t2.organizer_id == 99
        assert t2.message_id == 777
        assert t2.guild_id == 1 and t2.channel_id == 2

    def test_roundtrip_draft_state(self):
        t = _mk_tournament(TournamentSize.THIRTY_TWO, FormationMode.ELO)
        _fill_draft_tournament(t)
        t.start_draft()
        for _ in range(3):
            pos = t.current_picker_position()
            if pos is None:
                break
            key = str(t.current_circle)
            t.pick_player(pos, t.available[key][0])
            t.advance_after_pick()

        data = t.to_dict()
        t2 = Tournament.from_dict(data)
        assert t2.id == t.id
        assert t2.phase == TournamentPhase.DRAFT
        assert t2.current_circle == t.current_circle
        assert t2.pick_index == t.pick_index
        assert t2.picks == t.picks
        assert t2.available == t.available
        assert t2.captain_order == t.captain_order
        assert t2.captains == t.captains

    def test_old_save_without_id_gets_stable_id(self):
        """Старые сохранения без id/creation_time не падают."""
        t = _mk_tournament(TournamentSize.EIGHT, FormationMode.ELO)
        data = t.to_dict()
        data.pop("id")
        data.pop("creation_time")
        data.pop("formation_mode")
        data.pop("match_openers")
        t2 = Tournament.from_dict(data)
        assert t2.id == f"{t2.guild_id}_{t2.creation_time}"
        assert t2.formation_mode == FormationMode.ELO
        assert t2.match_openers == {}

    def test_restart_during_match_with_ocr(self):
        """Рестарт в середине матча: OCR-сессия, комнаты, pending winners."""
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        t.phase = TournamentPhase.QUALIFIERS
        t.teams = [
            {"captain": f"C{i}", "players": [f"C{i}", f"P{i}a", f"P{i}b", f"P{i}c"],
             "circle1": f"C{i}", "circle2": f"P{i}a", "circle3": f"P{i}b", "circle4": f"P{i}c"}
            for i in range(4)
        ]
        t.qualifier_matches = [(0, 1), (2, 3)]
        t.qualifier_winners = [0, None]
        t.ocr_sessions = {
            "qualifier_0": {"status": "awaiting_mapping", "rooms": [{"panels": {}}]},
        }
        t.match_channels = {"12345": {"match_type": "qualifier", "match_index": 0}}
        t.temp_match_stats = {"qualifier_0": {"P0a": {"kills": 5, "deaths": 2}}}
        t.processed_elo_matches = {"qualifier_0"}
        t.player_user_ids = {f"C{i}": 3000 + i for i in range(4)}
        t.eliminated_teams = {3}
        t.winner_team_index = None

        t2 = Tournament.from_dict(t.to_dict())
        assert t2.id == t.id
        assert t2.phase == TournamentPhase.QUALIFIERS
        assert t2.ocr_sessions == t.ocr_sessions
        assert t2.match_channels == t.match_channels
        assert t2.temp_match_stats == t.temp_match_stats
        assert t2.processed_elo_matches == {"qualifier_0"}
        assert t2.eliminated_teams == {3}
        assert t2.qualifier_winners == [0, None]
        assert t2.player_user_ids == t.player_user_ids

    def test_restart_preserves_pool_registration(self):
        """Регистрация в Balanced SR (pool) переживает рестарт."""
        t = _mk_tournament(TournamentSize.SIXTEEN, FormationMode.BALANCED_SR)
        t.players_pool = [f"P{i}" for i in range(16)]
        t.player_user_ids = {f"P{i}": 4000 + i for i in range(16)}
        t.registration = __import__("models.tournament", fromlist=["RegistrationState"]).RegistrationState.OPEN

        t2 = Tournament.from_dict(t.to_dict())
        assert t2.players_pool == t.players_pool
        assert t2.player_user_ids == t.player_user_ids
        assert t2.formation_mode == FormationMode.BALANCED_SR
        assert t2.id == t.id


# ---------------------------------------------------------------------------
# MatchContext / resolve_match_context (ТЗ §2)
# ---------------------------------------------------------------------------

class TestMatchContext:
    def _tournament_with_match(self, size, formation):
        t = _mk_tournament(size, formation)
        n = int(size.value)
        for i in range(n):
            t.players_pool.append(f"P{i}")
            t.player_user_ids[f"P{i}"] = 5000 + i
        # имитируем уже сформированные команды
        teams = []
        for ti in range(n // 4):
            members = [f"P{ti*4+j}" for j in range(4)]
            teams.append({
                "captain": members[0],
                "players": members,
                "circle1": members[0], "circle2": members[1],
                "circle3": members[2], "circle4": members[3],
            })
        t.teams = teams
        if size == TournamentSize.SIXTEEN:
            t.semifinal_matches = [(0, 1), (2, 3)]
        else:
            t.qualifier_matches = [(0, 1), (2, 3), (4, 5), (6, 7)]
            t.semifinal_matches = [(0, 4), (2, 6)]
            t.final_teams = [0, 2]
        return t

    def test_resolve_qualifier(self):
        t = self._tournament_with_match(TournamentSize.THIRTY_TWO, FormationMode.DRAFT)
        ctx = resolve_match_context(t, "qualifier", 2)
        assert isinstance(ctx, MatchContext)
        assert ctx.tournament_id == t.id
        assert ctx.session_key == "qualifier_2"
        assert ctx.match_id == f"{t.id}_qualifier_2"
        assert (ctx.team_a_index, ctx.team_b_index) == (4, 5)
        # ростеры — Discord ID 4 игроков каждой команды
        assert ctx.team_a_player_ids == tuple(5000 + i for i in range(16, 20))
        assert ctx.team_b_player_ids == tuple(5000 + i for i in range(20, 24))

    def test_resolve_semifinal_and_final(self):
        t = self._tournament_with_match(TournamentSize.THIRTY_TWO, FormationMode.DRAFT)
        ctx = resolve_match_context(t, "semifinal", 1)
        assert (ctx.team_a_index, ctx.team_b_index) == (2, 6)
        ctx_f = resolve_match_context(t, "final", 0)
        assert (ctx_f.team_a_index, ctx_f.team_b_index) == (0, 2)
        assert ctx_f.session_key == "final_0"

    def test_resolve_16_semifinal(self):
        t = self._tournament_with_match(TournamentSize.SIXTEEN, FormationMode.DRAFT)
        ctx = resolve_match_context(t, "semifinal", 0)
        assert ctx.session_key == "semifinal_0"
        assert len(ctx.team_a_player_ids) == 4
        assert len(ctx.team_b_player_ids) == 4

    def test_resolve_invalid(self):
        t = self._tournament_with_match(TournamentSize.SIXTEEN, FormationMode.DRAFT)
        with pytest.raises(ValueError):
            resolve_match_context(t, "bogus", 0)
        with pytest.raises(ValueError):
            resolve_match_context(t, "semifinal", 5)

    def test_context_uses_fresh_state(self):
        """Ростеры берутся из переданного (свежего) состояния, не из View."""
        t = self._tournament_with_match(TournamentSize.SIXTEEN, FormationMode.DRAFT)
        # Меняем ростер команды 0 после «создания view»
        t.player_user_ids["P0"] = 9999
        ctx = resolve_match_context(t, "semifinal", 0)
        assert 9999 in ctx.team_a_player_ids


class TestScopedLocks:
    def test_room_lock_scoped_by_tournament(self):
        from cogs.ocr_upload import _room_lock, _ROOM_LOCKS
        l1 = _room_lock("qualifier_0", "tourn_A")
        l2 = _room_lock("qualifier_0", "tourn_B")
        l1b = _room_lock("qualifier_0", "tourn_A")
        assert l1 is not l2  # разные турниры — разные локи
        assert l1 is l1b  # один турнир — тот же лок
        assert _room_lock("qualifier_0") is not l1  # legacy ключ без tid

    def test_finalize_lock_scoped(self):
        from views.ocr_match_view import _finalize_lock
        a = _finalize_lock("final_0", "tA")
        b = _finalize_lock("final_0", "tB")
        assert a is not b
