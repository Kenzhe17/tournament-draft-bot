"""Тесты Double Elimination engine v2 + balanced_sr formation."""

import random
import pytest

from services.double_elim import (
    DoubleElimEngine, DEMatch, MatchStatus,
    match_index_for, match_id_for_index,
    _BRACKET_4_TEAMS, _BRACKET_8_TEAMS,
)
from services.team_formation import (
    SRPlayer, form_teams_balanced_sr, NEUTRAL_SR,
)


# ---------------------------------------------------------------------------
# Хелперы: симуляция полного турнира с заданным «сценарием» победителей
# ---------------------------------------------------------------------------

def _play(engine: DoubleElimEngine, match_id: str, winner: int) -> DEMatch:
    engine.start_match(match_id)
    return engine.complete_match(match_id, winner)


def _winner_of(engine, match_id):
    return engine.get_match(match_id).winner_team


def _loser_of(engine, match_id):
    return engine.get_match(match_id).loser_team


def _team_of(engine, match_id, side):
    m = engine.get_match(match_id)
    return m.team_a if side == "a" else m.team_b


# ---------------------------------------------------------------------------
# Сетка 4 команды (16 игроков)
# ---------------------------------------------------------------------------

class TestBracket4:
    def test_initial_ready_matches(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        ready = {m.match_id for m in eng.get_ready_matches()}
        assert ready == {"U1", "U2"}
        # seeds: U1=1v4 → team0 vs team3; U2=2v3 → team1 vs team2
        assert eng.get_match("U1").team_a == 0
        assert eng.get_match("U1").team_b == 3
        assert eng.get_match("U2").team_a == 1
        assert eng.get_match("U2").team_b == 2
        # остальные — waiting
        for mid in ("U3", "L1", "L2", "G1"):
            assert eng.get_match(mid).status == MatchStatus.WAITING

    def test_u3_created_after_u1_u2(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)
        assert eng.get_match("U3").status == MatchStatus.WAITING
        _play(eng, "U2", 2)
        u3 = eng.get_match("U3")
        assert u3.status == MatchStatus.READY
        assert u3.team_a == 0 and u3.team_b == 2

    def test_l1_losers_and_elimination(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)   # team3 → L1.a
        _play(eng, "U2", 1)   # team2 → L1.b
        l1 = eng.get_match("L1")
        assert l1.status == MatchStatus.READY
        _play(eng, "L1", l1.team_a)  # team3 wins L1 → L2
        # team2: проиграл U2 + L1 = 2 поражения → выбыл;
        # team3: проиграл U1, выиграл L1 = 1 поражение
        assert eng.team_losses(2) == 2
        assert eng.team_losses(3) == 1
        # L2 = w(L1)=team3 vs l(U3). U3 ещё не сыгран.
        assert eng.get_match("L2").status == MatchStatus.WAITING

    def test_second_loss_eliminates(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)  # 3 loses
        _play(eng, "U2", 1)  # 2 loses
        _play(eng, "L1", 3)  # 3 wins L1 (still 1 loss)
        _play(eng, "U3", 0)  # U3: 0 vs 1 → winner 0, loser 1
        # L2 = wL1=3 vs lU3=1
        l2 = eng.get_match("L2")
        assert l2.status == MatchStatus.READY
        _play(eng, "L2", 3)  # team1 loses → 2 losses → eliminated
        assert eng.team_losses(1) == 2
        # L2 winner → G1.b
        g1 = eng.get_match("G1")
        assert g1.status == MatchStatus.READY
        assert g1.team_a == 0  # w(U3)
        assert g1.team_b == 3  # w(L2)

    def test_full_tournament_upper_wins_g1(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)   # 3 → lower
        _play(eng, "U2", 1)   # 2 → lower
        _play(eng, "L1", 2)   # 2 wins L1
        _play(eng, "U3", 0)   # 0 wins upper final; 1 → L2.b
        _play(eng, "L2", 2)   # 2 wins L2 → G1.b
        g1 = _play(eng, "G1", 0)  # upper (team0) wins G1
        assert eng.get_match("G2") is None
        assert eng.get_champion() == 0
        assert eng.is_finished()
        # standings: 1st=0, 2nd=2 (финалист нижней сетки)
        places = dict()
        for place, team in eng.get_standings():
            places.setdefault(team, place)
        assert places[0] == 1
        assert places[2] == 2
        # проигравшие матчей выбывания: L2→1 (3rd), L1→3 (4th)
        assert eng.get_match("L2").loser_team == 1
        assert eng.get_match("L1").loser_team == 3

    def test_g1_lower_win_opens_g2(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)
        _play(eng, "U2", 1)
        _play(eng, "L1", 2)
        _play(eng, "U3", 0)
        _play(eng, "L2", 2)
        # G1: upper=0 vs lower=2 → lower wins
        _play(eng, "G1", 2)
        g2 = eng.get_match("G2")
        assert g2 is not None
        assert g2.status == MatchStatus.READY
        assert g2.team_a == 0  # l(G1) = upper
        assert g2.team_b == 2  # w(G1) = lower
        # никто не чемпион пока
        assert eng.get_champion() is None
        _play(eng, "G2", 2)  # lower wins G2 → champion
        assert eng.get_champion() == 2
        # G2 winner has 1 loss total? team2: lost L1? нет —
        # team2 проиграл U2, выиграл L1/L2/G1/G2 → 1 поражение
        assert eng.team_losses(2) == 1

    def test_no_team_two_active_matches(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)
        _play(eng, "U2", 1)
        # team3 и team2 в L1 — обе готовы только к L1
        playing = {m.team_a for m in eng._matches.values()
                   if m.status == MatchStatus.PLAYING}
        playing |= {m.team_b for m in eng._matches.values()
                    if m.status == MatchStatus.PLAYING}
        # запускаем L1
        eng.start_match("L1")
        in_play = set()
        for m in eng._matches.values():
            if m.status == MatchStatus.PLAYING:
                in_play.update(t for t in (m.team_a, m.team_b) if t is not None)
        assert len(in_play) == len(set(in_play)) or True
        # уникальность: ни одна команда не в двух PLAYING
        seen = {}
        for m in eng._matches.values():
            if m.status == MatchStatus.PLAYING:
                for t in (m.team_a, m.team_b):
                    assert t not in seen, f"team {t} in two matches"
                    seen[t] = m.match_id

    def test_idempotent_complete(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        eng.start_match("U1")
        eng.complete_match("U1", 0)
        # повтор — no-op
        m = eng.complete_match("U1", 0)
        assert m.winner_team == 0
        assert eng.team_losses(3) == 1
        # другой победитель — ошибка
        with pytest.raises(ValueError):
            eng.complete_match("U1", 3)

    def test_cannot_complete_waiting(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        with pytest.raises(ValueError):
            eng.complete_match("U3", 0)

    def test_complete_nonparticipant_rejected(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        with pytest.raises(ValueError):
            eng.complete_match("U1", 2)  # team2 не играет в U1


# ---------------------------------------------------------------------------
# Сетка 8 команд (32 игрока)
# ---------------------------------------------------------------------------

class TestBracket8:
    def test_initial_ready(self):
        eng = DoubleElimEngine(8, list(range(8)))
        ready = {m.match_id for m in eng.get_ready_matches()}
        assert ready == {"U1", "U2", "U3", "U4"}
        # U1: 1v8 → 0v7; U2: 4v5 → 3v4; U3: 2v7 → 1v6; U4: 3v6 → 2v5
        assert (eng.get_match("U1").team_a, eng.get_match("U1").team_b) == (0, 7)
        assert (eng.get_match("U2").team_a, eng.get_match("U2").team_b) == (3, 4)
        assert (eng.get_match("U3").team_a, eng.get_match("U3").team_b) == (1, 6)
        assert (eng.get_match("U4").team_a, eng.get_match("U4").team_b) == (2, 5)

    def test_upper_bracket_full(self):
        eng = DoubleElimEngine(8, list(range(8)))
        # Раунд 1: все U1-U4, победители — seed 1,4,2,3 → команды 0,3,1,2
        _play(eng, "U1", 0)
        _play(eng, "U2", 3)
        _play(eng, "U3", 1)
        _play(eng, "U4", 2)
        u5 = eng.get_match("U5")
        u6 = eng.get_match("U6")
        assert u5.status == MatchStatus.READY
        assert (u5.team_a, u5.team_b) == (0, 3)
        assert u6.status == MatchStatus.READY
        assert (u6.team_a, u6.team_b) == (1, 2)
        _play(eng, "U5", 0)
        _play(eng, "U6", 1)
        u7 = eng.get_match("U7")
        assert u7.status == MatchStatus.READY
        assert (u7.team_a, u7.team_b) == (0, 1)
        # Нижняя: L1 = lU1(7) vs lU2(4); L2 = lU3(6) vs lU4(5)
        l1 = eng.get_match("L1")
        l2 = eng.get_match("L2")
        assert l1.status == MatchStatus.READY
        assert (l1.team_a, l1.team_b) == (7, 4)
        assert l2.status == MatchStatus.READY
        assert (l2.team_a, l2.team_b) == (6, 5)

    def test_lower_bracket_never_returns_upper(self):
        eng = DoubleElimEngine(8, list(range(8)))
        _play(eng, "U1", 0)  # 7 → lower
        _play(eng, "U2", 3)
        _play(eng, "U3", 1)
        _play(eng, "U4", 2)
        # team7 должен попасть только в нижнюю сетку
        for m in eng._matches.values():
            if m.bracket == "upper" and m.status != MatchStatus.COMPLETED:
                assert 7 not in (m.team_a, m.team_b)
        # L1: lU1=7 vs lU2=4
        l1 = eng.get_match("L1")
        assert l1.status == MatchStatus.READY

    def test_two_losses_eliminate_8(self):
        eng = DoubleElimEngine(8, list(range(8)))
        _play(eng, "U1", 0)   # 7 loses (1)
        _play(eng, "U2", 3)   # 4 loses (1)
        _play(eng, "L1", 7)   # 7 wins L1
        _play(eng, "U5", 0)   # U5: 0 vs 3 → team3 первое поражение
        assert eng.team_losses(3) == 1
        # team4: U2 + L1 = 2 поражения → выбыл после L1
        assert eng.team_losses(4) == 2
        # lU5=3 должен попасть в L4.b
        # продолжаем: U6
        _play(eng, "U3", 1)
        _play(eng, "U4", 2)
        _play(eng, "U6", 1)
        _play(eng, "U7", 0)   # upper champion path; loser 1 → L6.b
        # L2 = lU3=6 vs lU4=5
        _play(eng, "L2", 6)
        # L3 = wL1=7 vs lU6=2
        l3 = eng.get_match("L3")
        assert l3.status == MatchStatus.READY
        assert (l3.team_a, l3.team_b) == (7, 2)
        _play(eng, "L3", 7)
        # L4 = wL2=6 vs lU5=3
        l4 = eng.get_match("L4")
        assert l4.status == MatchStatus.READY
        assert (l4.team_a, l4.team_b) == (6, 3)
        _play(eng, "L4", 6)
        # L5 = wL3=7 vs wL4=6
        l5 = eng.get_match("L5")
        assert l5.status == MatchStatus.READY
        assert (l5.team_a, l5.team_b) == (7, 6)
        _play(eng, "L5", 7)
        # L6 = wL5=7 vs lU7=1
        l6 = eng.get_match("L6")
        assert l6.status == MatchStatus.READY
        assert (l6.team_a, l6.team_b) == (7, 1)
        _play(eng, "L6", 7)
        # G1 = wU7=0 vs wL6=7
        g1 = eng.get_match("G1")
        assert g1.status == MatchStatus.READY
        assert (g1.team_a, g1.team_b) == (0, 7)
        _play(eng, "G1", 0)  # верхняя выигрывает — G2 не нужен
        # Все 14 матчей сыграны (без G2)
        done = [m for m in eng._matches.values()
                if m.status == MatchStatus.COMPLETED]
        assert len(done) == 14
        assert eng.get_match("G2") is None
        assert eng.get_champion() == 0

    def test_g2_scenario_8(self):
        eng = DoubleElimEngine(8, list(range(8)))
        # Быстрый прогон до G1: deterministic winners
        # U1:0, U2:3, U3:1, U4:2, U5:0, U6:1, U7:0
        for mid, w in [("U1", 0), ("U2", 3), ("U3", 1), ("U4", 2),
                       ("U5", 0), ("U6", 1), ("U7", 0)]:
            _play(eng, mid, w)
        # Нижняя сетка — ведём team7 к G1.b
        _play(eng, "L1", 7)   # wL1=7
        _play(eng, "L2", 6)   # wL2=6 (lU3=6 vs lU4=5 → winner 6)
        _play(eng, "L3", 7)   # wL3=7 vs lU6=2
        _play(eng, "L4", 6)   # wL4=6 vs lU5=3
        _play(eng, "L5", 7)   # 7 vs 6
        _play(eng, "L6", 7)   # 7 vs lU7=1
        g1 = eng.get_match("G1")
        # upper=0, lower=7 → lower wins G1 → G2
        _play(eng, "G1", 7)
        g2 = eng.get_match("G2")
        assert g2 is not None
        assert (g2.team_a, g2.team_b) == (0, 7)
        assert eng.get_champion() is None
        _play(eng, "G2", 0)
        assert eng.get_champion() == 0
        # 15 матчей
        done = [m for m in eng._matches.values()
                if m.status == MatchStatus.COMPLETED]
        assert len(done) == 15

    def test_every_eliminated_has_two_losses(self):
        eng = DoubleElimEngine(8, list(range(8)))
        # Полный прогон — все 8 команд должны иметь 0,1 или 2 поражения;
        # команды не в финале — ровно 2
        for mid, w in [("U1", 0), ("U2", 3), ("U3", 1), ("U4", 2),
                       ("U5", 0), ("U6", 1), ("U7", 0),
                       ("L1", 7), ("L2", 6), ("L3", 7), ("L4", 6),
                       ("L5", 7), ("L6", 7)]:
            _play(eng, mid, w)
        _play(eng, "G1", 0)
        assert eng.is_finished()
        # team0 — чемпион (0 поражений); team7 — финалист нижней
        # сетки: проиграл U1 и G1 = 2 поражения
        assert eng.team_losses(0) == 0
        assert eng.team_losses(7) == 2
        # остальные 6 команд — по 2 поражения
        for t in (1, 2, 3, 4, 5, 6):
            assert eng.team_losses(t) == 2, f"team {t} losses"

    def test_standings_8(self):
        eng = DoubleElimEngine(8, list(range(8)))
        for mid, w in [("U1", 0), ("U2", 3), ("U3", 1), ("U4", 2),
                       ("U5", 0), ("U6", 1), ("U7", 0),
                       ("L1", 7), ("L2", 6), ("L3", 7), ("L4", 6),
                       ("L5", 7), ("L6", 7)]:
            _play(eng, mid, w)
        _play(eng, "G1", 0)
        places = {}
        for place, team in eng.get_standings():
            places.setdefault(team, place)
        assert places[0] == 1
        assert places[7] == 2
        # L6 loser = team1 → 3rd; L5 loser = team6 → 4th
        assert eng.get_match("L6").loser_team == 1
        assert eng.get_match("L5").loser_team == 6


# ---------------------------------------------------------------------------
# Сериализация / рестарт
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_roundtrip_mid_upper(self):
        eng = DoubleElimEngine(8, list(range(8)), tournament_id="t1")
        _play(eng, "U1", 0)
        _play(eng, "U2", 3)
        data = eng.to_dict()
        eng2 = DoubleElimEngine.from_dict(data)
        assert eng2.get_match("U5").status == MatchStatus.READY
        assert eng2.get_match("U5").team_a == 0
        assert eng2.get_match("U5").team_b == 3
        assert eng2.team_losses(7) == 1
        assert eng2.tournament_id == "t1"

    def test_roundtrip_mid_lower_with_g2_pending(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3], tournament_id="t2")
        _play(eng, "U1", 0)
        _play(eng, "U2", 1)
        _play(eng, "L1", 2)
        _play(eng, "U3", 0)
        _play(eng, "L2", 2)
        _play(eng, "G1", 2)  # → G2 created
        assert eng.get_match("G2") is not None
        data = eng.to_dict()
        eng2 = DoubleElimEngine.from_dict(data)
        assert eng2.get_match("G2").status == MatchStatus.READY
        assert eng2.get_champion() is None
        _play(eng2, "G2", eng2.get_match("G2").team_b)
        assert eng2.get_champion() is not None

    def test_match_index_mapping(self):
        # 4 команды: U1=1..G1=6, G2=7
        assert match_index_for("U1", 4) == 1
        assert match_index_for("G1", 4) == 6
        assert match_index_for("G2", 4) == 7
        assert match_id_for_index(7, 4) == "G2"
        # 8 команд: U1=1..G1=14, G2=15
        assert match_index_for("U7", 8) == 7
        assert match_index_for("L6", 8) == 13
        assert match_index_for("G1", 8) == 14
        assert match_index_for("G2", 8) == 15
        assert match_id_for_index(15, 8) == "G2"
        assert match_id_for_index(14, 8) == "G1"

    def test_idempotent_after_restart(self):
        eng = DoubleElimEngine(4, [0, 1, 2, 3])
        _play(eng, "U1", 0)
        eng2 = DoubleElimEngine.from_dict(eng.to_dict())
        # повторное завершение U1 — no-op, счётчик поражений не растёт
        eng2.complete_match("U1", 0)
        assert eng2.team_losses(3) == 1
        assert eng2.team_losses(0) == 0


# ---------------------------------------------------------------------------
# Balanced SR formation
# ---------------------------------------------------------------------------

class TestBalancedSR:
    def _players(self, n, sr_fn=None):
        return [SRPlayer(user_id=1000 + i, name=f"p{i}",
                         sr=(sr_fn(i) if sr_fn else 1.0 + i * 0.1))
                for i in range(n)]

    def test_16_players_4_teams_no_duplicates(self):
        teams, sums = form_teams_balanced_sr(self._players(16), seed=42)
        assert len(teams) == 4
        all_ids = [p.user_id for t in teams for p in t]
        assert len(all_ids) == 16
        assert len(set(all_ids)) == 16
        for t in teams:
            assert len(t) == 4
        # команды отсортированы по сумме убыв. (seed1 = сильнейшая)
        assert sums == sorted(sums, reverse=True)

    def test_32_players(self):
        teams, sums = form_teams_balanced_sr(self._players(32), seed=7)
        assert len(teams) == 8  # 8 команд × 4 игрока
        assert all(len(t) == 4 for t in teams)
        assert len({p.user_id for t in teams for p in t}) == 32
        assert sums == sorted(sums, reverse=True)
        # 8 сильнейших (капитаны) — по одному в каждой команде
        top8 = {p.user_id for p in sorted(
            self._players(32), key=lambda p: -p.sr)[:8]}
        counts = [sum(1 for p in t if p.user_id in top8)
                  for t in teams]
        assert sorted(counts) == [1] * 8

    def test_balanced_sums(self):
        players = self._players(16)
        teams, sums = form_teams_balanced_sr(players, seed=1)
        # разница max-min должна быть минимальной из возможных;
        # при SR 1.0..2.5 равномерно — почти идеально
        assert max(sums) - min(sums) < 0.6

    def test_deterministic_same_seed(self):
        players = self._players(16)
        t1, s1 = form_teams_balanced_sr(players, seed=123)
        t2, s2 = form_teams_balanced_sr(list(reversed(players)), seed=123)
        # состав команд может отличаться порядком, но суммы те же
        assert sorted(s1) == sorted(s2)
        ids1 = sorted(p.user_id for t in t1 for p in t)
        ids2 = sorted(p.user_id for t in t2 for p in t)
        assert ids1 == ids2

    def test_captains_are_top_group(self):
        """Первая группа (сильнейшие 4/8 игроков) — капитаны:
        каждый капитан = самый сильный в своей команде? Нет —
        капитаны это первая группа целиком, распределённая
        по командам. Проверяем: 4 сильнейших игрока в разных
        командах (по одному на команду)."""
        players = self._players(16)
        teams, _ = form_teams_balanced_sr(players, seed=5)
        top4 = sorted(players, key=lambda p: -p.sr)[:4]
        top_ids = {p.user_id for p in top4}
        # каждый top4 в отдельной команде
        counts = [sum(1 for p in t if p.user_id in top_ids)
                  for t in teams]
        assert sorted(counts) == [1, 1, 1, 1]

    def test_neutral_sr_fallback(self):
        players = [SRPlayer(user_id=i, sr=NEUTRAL_SR) for i in range(16)]
        teams, sums = form_teams_balanced_sr(players, seed=1)
        # все равны → суммы равны
        assert len(set(round(s, 6) for s in sums)) == 1

    def test_invalid_size(self):
        with pytest.raises(ValueError):
            form_teams_balanced_sr(self._players(8), seed=1)


# ---------------------------------------------------------------------------
# Модель Tournament: round-trip
# ---------------------------------------------------------------------------

class TestTournamentPersistence:
    def test_id_roundtrip(self):
        from models.tournament import Tournament, TournamentSize
        t = Tournament(guild_id=1, channel_id=2, size=TournamentSize.SIXTEEN
                       if hasattr(TournamentSize, 'SIXTEEN') else TournamentSize("16"))
        t.engine_version = 2
        t.bracket_v2 = {"version": 2, "matches": {}}
        d = t.to_dict()
        assert d["id"] == t.id
        assert d["creation_time"] == t.creation_time
        assert d["formation_mode"] == t.formation_mode.value
        assert d["engine_version"] == 2
        assert d["bracket_v2"]["version"] == 2

    def test_from_dict_restores_id(self):
        from models.tournament import Tournament, TournamentSize, TournamentPhase
        t = Tournament(guild_id=1, channel_id=2, size=TournamentSize("16"),
                       phase=TournamentPhase.SETUP)
        orig_id = t.id
        d = t.to_dict()
        t2 = Tournament.from_dict(d)
        assert t2.id == orig_id
        assert t2.creation_time == t.creation_time
