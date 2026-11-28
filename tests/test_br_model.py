"""Unit-тесты модели BR турнира (Solo/Duo/Squad)."""

import pytest

from models.br_tournament import (
    BRFormat, BRTournament, BRTournamentFormation, BRTournamentPhase,
    PLACE_POINTS,
)


def make(fmt="squad", formation="random", maps=3, **kw):
    return BRTournament(
        id="", guild_id=1, channel_id=2,
        formation=BRTournamentFormation(formation),
        format=BRFormat(fmt), map_count=maps, **kw)


def add_players(t, n, start=1):
    for i in range(n):
        uid = start + i
        ok, err = t.register_player(f"P{uid}", uid)
        assert ok, err


class TestFormats:
    def test_rules_constants(self):
        solo, duo, squad = make("solo"), make("duo"), make("squad")
        assert (solo.slot_size, solo.min_slots, solo.max_slots) == (1, 12, 48)
        assert (duo.slot_size, duo.min_slots, duo.max_slots) == (2, 5, 24)
        assert (squad.slot_size, squad.min_slots, squad.max_slots) == (4, 5, 12)
        assert duo.min_players == 10 and duo.max_players == 48
        assert squad.min_players == 20 and squad.max_players == 48
        assert solo.place_cap == 48 and duo.place_cap == 24 and squad.place_cap == 12

    def test_map_rotation_repeats(self):
        t = make(maps=7)
        assert t.get_map_sequence() == [
            "bermuda", "purgatory", "kalahari",
            "bermuda", "purgatory", "kalahari", "bermuda"]


class TestRegistration:
    def test_solo_register_creates_slots(self):
        t = make("solo")
        add_players(t, 12)
        assert len(t.filled_slots()) == 12
        assert t.draw_error() == ""

    def test_solo_below_min_blocks_draw(self):
        t = make("solo")
        add_players(t, 11)
        err = t.draw_error()
        assert "минимум 12" in err and "добавьте 1" in err

    def test_solo_duplicate_rejected(self):
        t = make("solo")
        add_players(t, 1)
        ok, err = t.register_player("X", 1)
        assert not ok

    def test_duo_random_odd_blocks_with_hint(self):
        t = make("duo", "random")
        add_players(t, 11)
        err = t.draw_error()
        assert "кратно 2" in err
        assert "Добавьте 1" in err

    def test_duo_random_draw_pairs(self):
        t = make("duo", "random")
        add_players(t, 10)
        ok, err = t.draw()
        assert ok, err
        assert t.phase == BRTournamentPhase.DRAFT
        assert len(t.teams) == 5
        assert all(len(s["member_ids"]) == 2 for s in t.teams)

    def test_squad_random_needs_multiple_of_four(self):
        t = make("squad", "random")
        add_players(t, 21)
        err = t.draw_error()
        assert "кратно 4" in err and "Добавьте 3" in err
        add_players(t, 3, start=100)
        assert t.draw_error() == ""
        ok, _ = t.draw()
        assert ok and len(t.teams) == 6

    def test_solo_draw_goes_straight_to_map(self):
        t = make("solo")
        add_players(t, 12)
        ok, _ = t.draw()
        assert ok
        assert t.phase == BRTournamentPhase.PLAYING
        assert t.current_map_index == 0 and t.current_map == "bermuda"

    def test_duo_team_invite_flow(self):
        t = make("duo", "team")
        ok, err, idx = t.create_duo_invite(1, "Cap", 2)
        assert ok, err
        # слот pending — не считается заполненным
        assert len(t.filled_slots()) == 0
        # лимит пар считается по подтверждённым
        for n in range(3, 8):  # ещё 5 пар
            ok, _, _ = t.create_duo_invite(100 + n, f"C{n}", 200 + n)
            assert ok
            t.respond_duo_invite(len(t.teams) - 1, 200 + n, f"M{n}", True)
        # подтверждаем первую
        ok, msg = t.respond_duo_invite(idx, 2, "Mate", True)
        assert ok
        assert len(t.filled_slots()) == 6
        # слот занимает место, member_ids стабильны
        slot = t.teams[idx]
        assert slot["member_ids"] == [1, 2]
        assert slot["captain_id"] == 1

    def test_duo_invite_decline_and_expiry(self):
        t = make("duo", "team")
        t.create_duo_invite(1, "Cap", 2)
        ok, _ = t.respond_duo_invite(0, 2, "Mate", False)
        assert ok and len(t.teams) == 0
        t.create_duo_invite(1, "Cap", 2)
        t.teams[0]["invite_expires"] = 0  # просрочено
        ok, msg = t.respond_duo_invite(0, 2, "Mate", True)
        assert not ok and "истёк" in msg

    def test_squad_team_register_snapshot(self):
        t = make("squad", "team")
        for n in range(5):
            ids = [n * 10 + k for k in range(1, 5)]
            names = {i: f"P{i}" for i in ids}
            ok, err = t.register_team_slot(f"team_{n}", f"T{n}", ids[0], ids, names)
            assert ok, err
        assert len(t.registered_teams) == 5
        assert t.draw_error() == ""
        ok, _ = t.draw()
        assert ok and t.phase == BRTournamentPhase.DRAFT
        slot = t.teams[0]
        assert slot["team_id"] == "team_0"
        assert len(slot["member_ids"]) == 4

    def test_leave_frees_slot(self):
        t = make("duo", "team")
        t.create_duo_invite(1, "Cap", 2)
        t.respond_duo_invite(0, 2, "Mate", True)
        assert t.remove_player(1)
        assert len(t.filled_slots()) == 0


class TestResults:
    def _played_tournament(self, fmt="squad", n_slots=None, maps=2):
        t = make(fmt, "random", maps=maps)
        size = t.slot_size
        n = n_slots or t.min_slots
        add_players(t, n * size)
        t.draw()
        if t.phase == BRTournamentPhase.DRAFT:
            t.begin_games()
        # сыграть все карты
        for _ in range(maps):
            t.mark_map_played()
        assert t.phase == BRTournamentPhase.COLLECT
        return t

    def test_input_blocked_before_last_map(self):
        t = make("solo")
        add_players(t, 12)
        t.draw()
        assert t.phase == BRTournamentPhase.PLAYING
        ok, err = t.set_result_entry(0, 0, place=1, kills=5)
        assert not ok and "после последней карты" in err

    def test_map_keys_by_index_not_name(self):
        t = self._played_tournament(maps=4)  # bermuda дважды
        # map_0 и map_3 обе bermuda — ключи разные
        t.set_result_entry(0, 0, place=1, kills=3)
        t.set_result_entry(3, 0, place=2, kills=4)
        s = t.teams[0]["stats"]
        assert "map_0" in s and "map_3" in s
        assert s["map_0"]["place"] == 1 and s["map_3"]["place"] == 2

    def test_place_cap_per_format(self):
        t = self._played_tournament("duo", n_slots=5, maps=1)
        ok, err = t.set_result_entry(0, 0, place=25, kills=0)
        assert not ok and "24" in err
        ok, _ = t.set_result_entry(0, 0, place=24, kills=0)
        assert ok

    def test_unique_place_per_map(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        t.set_result_entry(0, 0, place=3, kills=0)
        ok, err = t.set_result_entry(0, 1, place=3, kills=0)
        assert not ok and "уже занято" in err
        # на другой карте то же место можно
        t2 = self._played_tournament("squad", n_slots=5, maps=2)
        t2.set_result_entry(0, 0, place=3, kills=0)
        ok, _ = t2.set_result_entry(1, 1, place=3, kills=0)
        assert ok

    def test_dns_distinct_from_empty(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        assert t.results_status() == (0, 5)
        t.set_result_entry(0, 0, dns=True)
        filled, _ = t.results_status()
        assert filled == 1
        e = t.get_team_stats_for_map(t.teams[0], 0)
        assert e["dns"] and e["kills"] == 0 and e["place"] is None

    def test_kills_range(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        ok, err = t.set_result_entry(0, 0, place=1, kills=51)
        assert not ok
        ok, _ = t.set_result_entry(0, 0, place=1, kills=50)
        assert ok

    def test_batch_atomic(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        entries = [
            {"slot": 0, "place": 1, "kills": 5},
            {"slot": 1, "place": 1, "kills": 3},  # дубль места
            {"slot": 2, "place": 2, "kills": 1},
        ]
        ok, errors = t.set_results_batch(0, entries)
        assert not ok and errors
        assert t.results_status()[0] == 0  # ничего не сохранено
        entries[1]["place"] = 3
        ok, errors = t.set_results_batch(0, entries)
        assert ok, errors
        assert t.results_status()[0] == 3

    def test_batch_swap_and_clear(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        ok, errs = t.set_results_batch(0, [
            {"slot": 0, "place": 1, "kills": 0},
            {"slot": 1, "place": 2, "kills": 0},
            {"slot": 2, "place": 3, "kills": 0},
        ])
        assert ok, errs
        # обмен местами 1↔2 через пакет
        ok, errs = t.set_results_batch(0, [
            {"slot": 0, "place": 2, "kills": 0},
            {"slot": 1, "place": 1, "kills": 0},
        ])
        assert ok, errs
        assert t.get_team_stats_for_map(t.teams[0], 0)["place"] == 2
        assert t.get_team_stats_for_map(t.teams[1], 0)["place"] == 1
        # очистка записи (пусто ≠ DNS)
        ok, _ = t.set_results_batch(0, [{"slot": 2, "clear": True}])
        assert ok
        assert t.get_team_stats_for_map(t.teams[2], 0) is None
        # одиночная очистка
        ok, _ = t.set_result_entry(0, 0, clear=True)
        assert ok
        assert t.get_team_stats_for_map(t.teams[0], 0) is None

    def test_scoring_and_tiebreak(self):
        t = self._played_tournament("squad", n_slots=5, maps=2)
        # слот 0: 1 место обе карты; слот 1: 2 места; слот 2: DNS+киллы
        for m in range(2):
            t.set_result_entry(m, 0, place=1, kills=20)
            t.set_result_entry(m, 1, place=2, kills=5)
        t.set_result_entry(0, 2, dns=True)
        t.set_result_entry(1, 2, place=5, kills=50)
        for si in (3, 4):
            for m in range(2):
                t.set_result_entry(m, si, place=10, kills=0)
        res = t.calculate_final_results()
        # слот 0: (12+20)*2=64; слот 2: 0 + (6+50)=56; слот 1: (9+5)*2=28
        assert res[0]["slot_index"] == 0
        assert res[0]["booyahs"] == 2
        assert res[1]["slot_index"] == 2
        assert res[2]["slot_index"] == 1
        # место на последней карте: DNS хуже сыгранного
        assert t._slot_last_place(t.teams[2]) > t._slot_last_place(t.teams[1])

    def test_results_complete_requires_all(self):
        t = self._played_tournament("squad", n_slots=5, maps=2)
        assert not t.results_complete()
        for si in range(5):
            for m in range(2):
                # уникальное место внутри каждой карты
                ok, err = t.set_result_entry(m, si, place=si + 1, kills=m)
                assert ok, err
        assert t.results_complete()


class TestLegacy:
    def test_old_json_reads_as_squad(self):
        data = {
            "id": "x", "guild_id": 1, "channel_id": 2,
            "formation": "random", "phase": "purgatory",
            "players": [{"name": "A", "id": 10}, {"name": "B", "id": 11}],
            "teams": [{"captain": "A", "players": ["B", "C", "D"],
                       "name": "T1", "stats": {"map_0": {"place": 1, "kills": 3}}}],
            "map_count": 3,
        }
        t = BRTournament.from_dict(data)
        assert t.format == BRFormat.SQUAD
        assert t.phase == BRTournamentPhase.PLAYING
        assert t.current_map_index == 1
        assert t.legacy_scoring is True
        slot = t.teams[0]
        assert 10 in slot["member_ids"] or slot["member_ids"]  # backfill

    def test_legacy_results_phase_is_published(self):
        data = {"id": "x", "guild_id": 1, "channel_id": 2,
                "formation": "team", "phase": "results", "teams": [], "players": []}
        t = BRTournament.from_dict(data)
        assert t.phase == BRTournamentPhase.RESULTS
        assert t.results_published is True

    def test_roundtrip(self):
        t = make("duo", "random", maps=2)
        add_players(t, 10)
        t.draw()
        t.begin_games()
        t.set_result_entry  # noqa
        d = t.to_dict()
        t2 = BRTournament.from_dict(d)
        assert t2.format == BRFormat.DUO
        assert len(t2.teams) == 5
        assert t2.phase == BRTournamentPhase.PLAYING


class TestPlacePoints:
    def test_scale(self):
        assert [PLACE_POINTS[i] for i in range(1, 13)] == [12, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0]
