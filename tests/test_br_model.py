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

    def test_solo_draw_goes_straight_to_games(self):
        t = make("solo")
        add_players(t, 12)
        ok, _ = t.draw()
        assert ok
        assert t.phase == BRTournamentPhase.PLAYING
        assert t.schedule_lines()[0] == "1. Бермуды"

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
        # «Завершить игры» — единственный переход к вводу результатов
        assert t.finish_games()
        assert t.phase == BRTournamentPhase.COLLECT
        return t

    def test_input_allowed_during_playing(self):
        """«Завершить игры» убрано: ввод результатов доступен в PLAYING
        по каждой карте независимо, без перехода в COLLECT."""
        t = make("solo")
        add_players(t, 12)
        t.draw()
        assert t.phase == BRTournamentPhase.PLAYING
        ok, err = t.set_result_entry(0, 0, place=1, kills=5)
        assert ok, err
        # карта 1 заполнена не полностью — статус partial
        assert t.map_status(0) == "partial"
        # запись на другую карту независима
        ok, err = t.set_result_entry(1, 0, place=1, kills=0)
        assert ok, err
        # до старта (REGISTRATION/DRAFT) ввод закрыт
        t2 = make("solo")
        add_players(t2, 12)
        ok, err = t2.set_result_entry(0, 0, place=1, kills=0)
        assert not ok

    def test_map_status_and_missing_results(self):
        t = self._played_tournament("squad", n_slots=5, maps=3)
        assert t.map_status(0) == "empty"
        for si in range(5):
            ok, _ = t.set_result_entry(0, si, place=si + 1, kills=0)
            assert ok
        assert t.map_status(0) == "filled"
        assert t.map_status(1) == "empty"
        # частичная запись
        ok, _ = t.set_result_entry(1, 0, place=1, kills=0)
        assert ok
        assert t.map_status(1) == "partial"
        missing = t.missing_results()
        assert [m["map_index"] for m in missing] == [1, 2]
        assert missing[0]["filled"] == 1 and missing[0]["expected"] == 5

    def test_per_map_room_credentials(self):
        t = self._played_tournament("squad", n_slots=5, maps=3)
        # legacy глобальные данные — fallback для всех карт
        t.room_data = {"id": "111", "password": "old"}
        assert t.room_credentials(0) == {"id": "111", "password": "old"}
        assert t.room_credentials(2) == {"id": "111", "password": "old"}
        # per-map данные имеют приоритет
        t.set_room(1, "222", "new")
        assert t.room_credentials(1) == {"id": "222", "password": "new"}
        assert t.room_credentials(0) == {"id": "111", "password": "old"}
        # roundtrip
        t2 = BRTournament.from_dict(t.to_dict())
        assert t2.room_credentials(1) == {"id": "222", "password": "new"}
        assert t2.room_credentials(2) == {"id": "111", "password": "old"}

    def test_publish_from_playing_phase(self):
        """Публикация доступна прямо из PLAYING после заполнения всех карт."""
        t = make("squad", "random", maps=1)
        add_players(t, 20)
        t.draw()
        t.begin_games()
        assert t.phase == BRTournamentPhase.PLAYING
        for si in range(5):
            ok, _ = t.set_result_entry(0, si, place=si + 1, kills=0)
            assert ok
        ok, err = t.freeze_results()
        assert ok, err
        assert t.commit_publish()
        assert t.phase == BRTournamentPhase.RESULTS
        # uncommit возвращает в PLAYING (фазу до заморозки)
        t.uncommit_publish()
        assert t.phase == BRTournamentPhase.PLAYING
        assert t.publish_pending is not None

    def test_finish_games_idempotent_and_phase_bound(self):
        t = self._played_tournament("squad", n_slots=5, maps=1)
        # повторный вызов не меняет состояние
        assert not t.finish_games()
        assert t.phase == BRTournamentPhase.COLLECT
        # из других фаз — тоже нет
        t2 = make("squad", "random")
        add_players(t2, 20)
        t2.draw()
        t2.begin_games()
        assert t2.phase == BRTournamentPhase.PLAYING
        assert t2.finish_games()
        assert t2.phase == BRTournamentPhase.COLLECT

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


class TestPublishSnapshot:
    def _collect(self, fmt="squad", n_slots=5, maps=2):
        t = make(fmt, "random", maps=maps)
        add_players(t, n_slots * t.slot_size)
        t.draw()
        if t.phase == BRTournamentPhase.DRAFT:
            t.begin_games()
        t.finish_games()
        for si in range(n_slots):
            for m in range(maps):
                ok, err = t.set_result_entry(m, si, place=si + 1, kills=m)
                assert ok, err
        return t

    def test_freeze_blocks_edits_and_commit_uses_snapshot(self):
        t = self._collect(maps=1)
        # снимок до правки: слот 0 = место 1 + 0 киллов → 12 очков
        ok, err = t.freeze_results()
        assert ok, err
        snap_points = t.calculate_final_results()[0]["total_points"]
        # параллельная правка отклоняется — в архив не попадёт 20
        ok, err = t.set_result_entry(0, 0, place=1, kills=50)
        assert not ok and "зафиксированы" in err
        ok, errs = t.set_results_batch(0, [{"slot": 0, "place": 1, "kills": 50}])
        assert not ok and errs
        # коммит: снимок становится опубликованным итогом
        assert t.commit_publish()
        assert t.phase == BRTournamentPhase.RESULTS
        assert t.results_snapshot is not None
        res = t.calculate_final_results()
        assert res[0]["total_points"] == snap_points  # не 20+
        # правки после публикации тоже закрыты
        assert not t.set_result_entry(0, 0, place=2, kills=0)[0]

    def test_freeze_idempotent_same_snapshot(self):
        t = self._collect(maps=1)
        assert t.freeze_results()[0]
        first = [dict(r) for r in t.publish_pending]
        assert t.freeze_results()[0]  # повтор — тот же снимок
        assert t.publish_pending == first

    def test_freeze_requires_complete(self):
        t = make("squad", "random", maps=1)
        add_players(t, 20)
        t.draw()
        t.begin_games()
        t.finish_games()
        ok, err = t.freeze_results()
        assert not ok and "Не все записи" in err

    def test_pending_survives_roundtrip(self):
        t = self._collect(maps=1)
        t.freeze_results()
        t2 = BRTournament.from_dict(t.to_dict())
        assert t2.publish_pending is not None
        assert t2.phase == BRTournamentPhase.COLLECT
        assert t2.commit_publish()
        assert t2.results_snapshot is not None
        t3 = BRTournament.from_dict(t2.to_dict())
        assert t3.results_snapshot is not None
        assert t3.phase == BRTournamentPhase.RESULTS


class TestStatKeyMigration:
    def _with_stats(self, maps, stats):
        t = make("squad", "random", maps=maps)
        add_players(t, 20)
        t.draw()
        t.teams[0]["stats"] = dict(stats)
        return BRTournament.from_dict(t.to_dict())

    def test_unique_name_key_migrates_to_map_n(self):
        t = self._with_stats(1, {"bermuda": {"place": 1, "kills": 3, "dns": False}})
        stats = t.teams[0]["stats"]
        assert stats["map_0"] == {"place": 1, "kills": 3, "dns": False}
        # исходная запись сохранена, map_N в приоритете
        assert stats["bermuda"]["place"] == 1
        assert t.get_team_stats_for_map(t.teams[0], 0)["place"] == 1
        assert t.migration_notes == []

    def test_repeated_name_not_copied_and_noted(self):
        t = self._with_stats(4, {"bermuda": {"place": 1, "kills": 3, "dns": False},
                                 "purgatory": {"place": 2, "kills": 1, "dns": False}})
        stats = t.teams[0]["stats"]
        # purgatory уникально → map_1; bermuda повторяется → остаётся как есть
        assert stats["map_1"]["place"] == 2
        assert "map_0" not in stats and "map_3" not in stats
        assert stats["bermuda"]["place"] == 1
        assert t.get_team_stats_for_map(t.teams[0], 0) is None
        assert t.get_team_stats_for_map(t.teams[0], 3) is None
        assert any("не привязан" in n for n in t.migration_notes)

    def test_explicit_map_n_has_priority(self):
        t = self._with_stats(3, {"map_0": {"place": 4, "kills": 0, "dns": False},
                                 "bermuda": {"place": 1, "kills": 9, "dns": False}})
        stats = t.teams[0]["stats"]
        # map_0 не перезаписан именем; bermuda→map_0 пропущен (цель занята)
        assert stats["map_0"]["place"] == 4
        assert t.get_team_stats_for_map(t.teams[0], 0)["place"] == 4

    def test_migration_idempotent_and_status_consistent(self):
        t = self._with_stats(3, {"bermuda": {"place": 1, "kills": 3, "dns": False},
                                 "kalahari": {"place": 2, "kills": 1, "dns": False}})
        filled, expected = t.results_status()
        # до fill всех слотов — считаем только этот слот: 2 записи из 5*3
        assert filled == 2 and expected == 15
        t2 = BRTournament.from_dict(t.to_dict())
        assert t2.teams[0]["stats"].get("map_0", {}).get("place") == 1
        assert t2.teams[0]["stats"].get("map_2", {}).get("place") == 2
        assert t2.migration_notes == []


class TestPublishedHistory:
    def test_published_repeated_name_keeps_historical_winner(self):
        """Опубликованный турнир из 4 карт: имя-ключ bermuda читается
        исторически (на обеих картах с этим именем) — победитель не
        меняется после миграции/перезагрузки."""
        t = make("squad", "random", maps=4)
        add_players(t, 20)
        t.draw()
        # слот 0: legacy-ключ bermuda — место 1 + 10 киллов
        t.teams[0]["stats"] = {"bermuda": {"place": 1, "kills": 10, "dns": False}}
        # слот 1: явные map_N — место 3 на всех картах
        t.teams[1]["stats"] = {
            f"map_{m}": {"place": 3, "kills": 0, "dns": False} for m in range(4)}
        d = t.to_dict()
        d["phase"] = "results"
        d["results_published"] = True
        t2 = BRTournament.from_dict(d)

        # историческое чтение: bermuda учитывается на map_0 и map_3
        assert t2.get_team_stats_for_map(t2.teams[0], 0)["place"] == 1
        assert t2.get_team_stats_for_map(t2.teams[0], 3)["place"] == 1
        # миграция опубликованные не трогает, замечаний нет
        assert "map_0" not in t2.teams[0]["stats"]
        assert t2.migration_notes == []
        # снимок заморожен при загрузке: слот 0 — победитель
        # (44 очка против 32 у слота 1)
        assert t2.results_snapshot is not None
        assert t2.results_snapshot[0]["slot_index"] == 0
        # повторная загрузка — тот же снимок, победитель стабилен
        t3 = BRTournament.from_dict(t2.to_dict())
        assert t3.results_snapshot == t2.results_snapshot
        assert t3.results_snapshot[0]["slot_index"] == 0


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
        t2 = BRTournament.from_dict(t.to_dict())
        assert t2.format == BRFormat.DUO
        assert len(t2.teams) == 5
        assert t2.phase == BRTournamentPhase.PLAYING

    def test_legacy_scoring_survives_reload(self):
        """Миграция: legacy-турнир после save/load остаётся на
        исторической сортировке (раньше флаг терялся при наличии "format")."""
        data = {
            "id": "x", "guild_id": 1, "channel_id": 2,
            "formation": "random", "phase": "bermuda",
            "players": [{"name": "A", "id": 10}],
            "teams": [], "map_count": 3,
        }
        t = BRTournament.from_dict(data)
        assert t.legacy_scoring is True
        t2 = BRTournament.from_dict(t.to_dict())  # имитация reload
        assert t2.legacy_scoring is True
        assert t2.phase == BRTournamentPhase.PLAYING

    def test_no_score_copy_on_repeated_map_names(self):
        """Миграция: legacy-статистика по имени карты не копируется
        на повтор карты в расписании."""
        t = make("squad", "random", maps=4)  # bermuda встречается дважды
        add_players(t, 20)
        t.draw()
        t.begin_games()
        t.finish_games()
        stats = t.teams[0].setdefault("stats", {})
        stats["bermuda"] = {"place": 1, "kills": 3, "dns": False}
        stats["purgatory"] = {"place": 2, "kills": 1, "dns": False}
        # map_1 (purgatory, уникально) читает legacy-ключ
        e1 = t.get_team_stats_for_map(t.teams[0], 1)
        assert e1 is not None and e1["place"] == 2
        # bermuda повторяется → legacy-ключ неоднозначен, не читается
        assert t.get_team_stats_for_map(t.teams[0], 0) is None
        assert t.get_team_stats_for_map(t.teams[0], 3) is None
        # map_i ключи всегда работают
        t.set_result_entry(0, 0, place=4, kills=0)
        t.set_result_entry(3, 0, place=5, kills=0)
        assert t.get_team_stats_for_map(t.teams[0], 0)["place"] == 4
        assert t.get_team_stats_for_map(t.teams[0], 3)["place"] == 5

    def test_room_visibility_rules(self):
        # squad team: комнату видит только капитан
        t = make("squad", "team")
        ids = [1, 2, 3, 4]
        names = {i: f"P{i}" for i in ids}
        t.register_team_slot("tm", "T", 1, ids, names)
        t.phase = BRTournamentPhase.PLAYING
        assert t.can_view_room(1) is True    # капитан
        assert t.can_view_room(2) is False   # член отряда
        assert t.can_view_room(99) is False  # посторонний
        # squad random: видят все члены слота
        t2 = make("squad", "random")
        add_players(t2, 20)
        t2.draw()
        t2.begin_games()
        member = t2.teams[0]["member_ids"][0]
        assert t2.can_view_room(member) is True
        # duo team: оба игрока пары
        t3 = make("duo", "team")
        t3.create_duo_invite(7, "Cap", 8)
        t3.respond_duo_invite(0, 8, "Mate", True)
        t3.phase = BRTournamentPhase.PLAYING
        assert t3.can_view_room(7) and t3.can_view_room(8)
        # pending-инвайт слот не даёт доступа
        t4 = make("duo", "team")
        t4.create_duo_invite(7, "Cap", 8)
        t4.phase = BRTournamentPhase.PLAYING
        assert t4.can_view_room(7) is False


class TestPlacePoints:
    def test_scale(self):
        assert [PLACE_POINTS[i] for i in range(1, 13)] == [12, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 0]
