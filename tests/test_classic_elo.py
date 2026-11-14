"""Тесты классической системы ELO (восстановлена из коммита 831874f).

Покрывает:
- паритет utils.rating_calculator с эталонной реализацией коммита 831874f;
- победа/поражение для топ-3, мест 4-6, 7-10 и вне топ-10;
- положительный ELO при поражении и прочие исторические особенности;
- личный бонус и catch-up бонус;
- process_match_result: идемпотентность, транзакционность, повторная
  финализация после сбоя БД, сохранение ELO.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import rating_calculator as rc  # noqa: E402
from models.player_stats import PlayerStats  # noqa: E402


# ===========================================================================
# Эталонная реализация из коммита 831874fd8ac3061cfc42d4cc368d8b3d64b39cf6
# (utils/rating_calculator.py на тот момент) — для проверки паритета.
# ===========================================================================


def _ref_get_base_elo_change(position: int, team_won: bool) -> int:
    if team_won:
        return 10
    return -10


def _ref_calculate_kd_bonus(kills: int, deaths: int) -> int:
    return (kills * 2) - deaths


def _ref_get_elo_multiplier_by_rank(leaderboard_rank: int) -> float:
    if leaderboard_rank <= 3:
        return 0.5
    elif leaderboard_rank <= 6:
        return 0.6
    elif leaderboard_rank <= 10:
        return 0.7
    else:
        return 1.2


def _ref_get_loss_reduction_by_rank(leaderboard_rank: int) -> float:
    if leaderboard_rank <= 3:
        return 2.0
    elif leaderboard_rank <= 6:
        return 1.5
    elif leaderboard_rank <= 10:
        return 1.3
    else:
        return 1.0


def _ref_calculate_personal_bonus(
    current_kills: int,
    current_deaths: int,
    avg_kills: float,
    avg_deaths: float,
) -> int:
    if avg_kills <= 0:
        return 0
    kills_improvement = current_kills - avg_kills
    deaths_improvement = avg_deaths - current_deaths
    bonus = (kills_improvement * 2) + deaths_improvement
    return max(0, bonus)


def _ref_calculate_catch_up_bonus(current_elo: int, avg_server_elo: float) -> int:
    if avg_server_elo <= 0:
        return 0
    elo_diff = avg_server_elo - current_elo
    if elo_diff > 300:
        return 5
    elif elo_diff > 200:
        return 3
    elif elo_diff > 100:
        return 1
    return 0


def _ref_calculate_balanced_elo_change(
    position: int,
    team_won: bool,
    kills: int,
    deaths: int,
    current_elo: int = 1000,
    avg_kills: float = 0.0,
    avg_deaths: float = 0.0,
    avg_server_elo: float = 0.0,
    lobby_avg_elo: float = 0.0,
    leaderboard_rank: int = 999,
) -> int:
    base_change = _ref_get_base_elo_change(position, team_won)

    is_top_player = leaderboard_rank <= 3
    if not team_won and is_top_player:
        kd_bonus = 0
    else:
        kd_bonus = _ref_calculate_kd_bonus(kills, deaths)

    total_base = base_change + kd_bonus

    if team_won:
        elo_multiplier = _ref_get_elo_multiplier_by_rank(leaderboard_rank)
        adjusted_change = int(total_base * elo_multiplier)
    else:
        loss_multiplier = _ref_get_loss_reduction_by_rank(leaderboard_rank)
        adjusted_change = int(total_base * loss_multiplier)
        if adjusted_change > 0:
            inverted_multiplier = 1.0 / loss_multiplier
            adjusted_change = int(total_base * inverted_multiplier)

    if avg_kills > 0 and not is_top_player:
        personal_bonus = _ref_calculate_personal_bonus(
            kills, deaths, avg_kills, avg_deaths
        )
        adjusted_change += personal_bonus

    if not is_top_player:
        catch_up_bonus = _ref_calculate_catch_up_bonus(current_elo, avg_server_elo)
        adjusted_change += catch_up_bonus

    return int(adjusted_change)


# ===========================================================================
# Паритет с эталоном 831874f
# ===========================================================================


def test_parity_with_reference_831874f():
    """calculate_balanced_elo_change совпадает с кодом коммита 831874f
    на всей матрице входных данных."""
    ranks = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15, 50, 999]
    won_opts = [True, False]
    kd_opts = [(0, 0), (1, 0), (5, 3), (8, 2), (3, 8), (12, 12), (20, 1)]
    elo_opts = [500, 1000, 1500, 2500]
    avg_opts = [(0.0, 0.0), (4.0, 5.0), (7.5, 2.0)]
    server_opts = [0.0, 1100.0, 1400.0]

    for rank in ranks:
        for won in won_opts:
            for k, d in kd_opts:
                for elo in elo_opts:
                    for ak, ad in avg_opts:
                        for srv in server_opts:
                            got = rc.calculate_balanced_elo_change(
                                position=2,
                                team_won=won,
                                kills=k,
                                deaths=d,
                                current_elo=elo,
                                avg_kills=ak,
                                avg_deaths=ad,
                                avg_server_elo=srv,
                                lobby_avg_elo=elo,
                                leaderboard_rank=rank,
                            )
                            want = _ref_calculate_balanced_elo_change(
                                position=2,
                                team_won=won,
                                kills=k,
                                deaths=d,
                                current_elo=elo,
                                avg_kills=ak,
                                avg_deaths=ad,
                                avg_server_elo=srv,
                                lobby_avg_elo=elo,
                                leaderboard_rank=rank,
                            )
                            assert got == want, (
                                f"rank={rank} won={won} k={k} d={d} elo={elo} "
                                f"avg={ak}/{ad} srv={srv}: {got} != {want}"
                            )


def test_parity_unit_functions():
    assert rc.get_base_elo_change(1, True) == 10
    assert rc.get_base_elo_change(4, False) == -10
    assert rc.calculate_kd_bonus(5, 3) == 7
    assert rc.calculate_kd_bonus(0, 8) == -8
    assert rc.get_elo_multiplier_by_rank(1) == 0.5
    assert rc.get_elo_multiplier_by_rank(3) == 0.5
    assert rc.get_elo_multiplier_by_rank(4) == 0.6
    assert rc.get_elo_multiplier_by_rank(6) == 0.6
    assert rc.get_elo_multiplier_by_rank(7) == 0.7
    assert rc.get_elo_multiplier_by_rank(10) == 0.7
    assert rc.get_elo_multiplier_by_rank(11) == 1.2
    assert rc.get_elo_multiplier_by_rank(999) == 1.2
    assert rc.get_loss_reduction_by_rank(1) == 2.0
    assert rc.get_loss_reduction_by_rank(4) == 1.5
    assert rc.get_loss_reduction_by_rank(7) == 1.3
    assert rc.get_loss_reduction_by_rank(11) == 1.0


# ===========================================================================
# Поведение по рангам
# ===========================================================================


def _change(won, k, d, rank=999, elo=1000, avg_k=0.0, avg_d=0.0, srv=0.0):
    return rc.calculate_balanced_elo_change(
        position=1,
        team_won=won,
        kills=k,
        deaths=d,
        current_elo=elo,
        avg_kills=avg_k,
        avg_deaths=avg_d,
        avg_server_elo=srv,
        leaderboard_rank=rank,
    )


def test_top3_win():
    # base +10, kd=5*2-3=7 -> 17 * 0.5 = 8
    assert _change(True, 5, 3, rank=1) == 8
    assert _change(True, 5, 3, rank=3) == 8


def test_top3_loss_no_kd_bonus():
    # top-3 при поражении: K/D-бонус отключён -> -10 * 2.0 = -20
    assert _change(False, 8, 2, rank=2) == -20
    # даже с положительным K/D остаётся -20
    assert _change(False, 15, 0, rank=1) == -20


def test_places_4_to_6():
    # rank 4-6: победа x0.6, поражение x1.5
    # win: (10+7)*0.6 = 10.2 -> 10 ; loss: (-10+7)*1.5 = -4.5 -> -4
    assert _change(True, 5, 3, rank=4) == 10
    assert _change(True, 5, 3, rank=6) == 10
    assert _change(False, 5, 3, rank=5) == -4


def test_places_7_to_10_loss_can_be_positive():
    # rank 7-10: поражение x1.3; (-10+7)=-3 *1.3 = -3.9 -> -3
    assert _change(False, 5, 3, rank=8) == -3
    # (-10 + (4*2-1)=7... k=9,d=1 -> kd=17, total=7) *1.3=9.1 -> 9 > 0
    # -> обратный множитель: int(7 / 1.3) = 5  (положительный ELO за поражение)
    assert _change(False, 9, 1, rank=8) == 5


def test_outside_top10():
    # rank >10 / 999: победа x1.2, поражение x1.0
    assert _change(True, 5, 3, rank=11) == 20   # 17*1.2=20.4 -> 20
    assert _change(True, 5, 3, rank=999) == 20
    assert _change(False, 1, 8, rank=20) == -16  # (-10-6)*1.0


def test_positive_elo_on_loss_old_formula():
    """Старая система может давать +ELO за поражение — не скрываем."""
    # rank 7-10, очень хороший K/D при поражении -> положительный итог
    assert _change(False, 9, 1, rank=8) > 0


def test_personal_bonus():
    # не топ-3, avg_kills>0: personal = (k-avg_k)*2 + (avg_d - d)
    # k=10,d=2, avg_k=4, avg_d=5 -> (6)*2 + 3 = 15
    base = _change(True, 10, 2, rank=20, avg_k=4.0, avg_d=5.0)
    without = _change(True, 10, 2, rank=20)
    assert base - without == 15


def test_personal_bonus_not_for_top3():
    with_avg = _change(True, 10, 2, rank=2, avg_k=4.0, avg_d=5.0)
    without = _change(True, 10, 2, rank=2)
    assert with_avg == without


def test_catch_up_bonus():
    # elo=800, server avg=1200 -> diff=400 > 300 -> +5
    base = _change(True, 5, 3, rank=20, elo=800, srv=1200.0)
    without = _change(True, 5, 3, rank=20, elo=800, srv=0.0)
    assert base - without == 5
    # diff=250 -> +3 ; diff=150 -> +1 ; diff<=100 -> 0
    assert _change(True, 5, 3, rank=20, elo=800, srv=1050.0) - without == 3
    assert _change(True, 5, 3, rank=20, elo=800, srv=950.0) - without == 1
    assert _change(True, 5, 3, rank=20, elo=800, srv=900.0) - without == 0


def test_first_match_no_history():
    """Первый матч: нет предыдущей статистики -> личный бонус = 0."""
    got = rc.calculate_balanced_elo_change(
        position=1, team_won=True, kills=5, deaths=3,
        current_elo=1000, avg_kills=0.0, avg_deaths=0.0,
        avg_server_elo=0.0, leaderboard_rank=999,
    )
    assert got == _change(True, 5, 3, rank=999)


def test_identical_players_identical_change():
    a = _change(True, 6, 4, rank=30, elo=1200, avg_k=5.0, avg_d=4.0, srv=1100.0)
    b = _change(True, 6, 4, rank=30, elo=1200, avg_k=5.0, avg_d=4.0, srv=1100.0)
    assert a == b


# ===========================================================================
# process_match_result — интеграция с подменёнными хранилищами
# ===========================================================================


class _FakeStatsStore:
    def __init__(self, stats_map):
        self.map = dict(stats_map)
        self.set_many_calls: list[list] = []
        self.fail_next_set_many = False

    async def get(self, guild_id, user_id):
        # Как реальное хранилище: каждый get — свежая копия записи,
        # мутации до set_many не «протекают» в БД при сбое.
        stats = self.map.get((guild_id, user_id))
        return copy.deepcopy(stats) if stats is not None else None

    async def get_all(self, guild_id):
        return [
            copy.deepcopy(s)
            for (g, _), s in self.map.items() if g == guild_id
        ]

    async def set(self, stats):
        self.map[(stats.guild_id, stats.user_id)] = stats

    async def set_many(self, stats_list, finalize_match_id=None,
                       guild_id_for_finalize=None):
        if self.fail_next_set_many:
            self.fail_next_set_many = False
            raise RuntimeError("PG down")
        self.set_many_calls.append(list(stats_list))
        for s in stats_list:
            self.map[(s.guild_id, s.user_id)] = s


class _FakeBalanceStore:
    def __init__(self):
        self.grants: dict = {}

    async def add_balance_once(self, guild_id, user_id, amount, key):
        k = (guild_id, user_id, key)
        if k in self.grants:
            return 0
        self.grants[k] = amount
        return amount


class _FakeJsonStore:
    def __init__(self):
        self.saved = []

    def set(self, tournament):
        self.saved.append(tournament)

    def get(self, guild_id, channel_id):
        return None


def _make_tournament(names_a, names_b, uids):
    from models.tournament import Tournament, FormationMode

    t = Tournament(guild_id=1, channel_id=500)
    t.formation_mode = FormationMode.RANDOM_AUTO
    t.teams = [
        {"players": list(names_a), "captain": names_a[0]},
        {"players": list(names_b), "captain": names_b[0]},
    ]
    t.player_user_ids = dict(uids)
    return t


def _match_info(t, kd):
    return {
        "match_type": "grand_final",
        "match_index": 0,
        "winning_team_index": 0,
        "team1_index": 0,
        "team2_index": 1,
        "temp_kd_data": kd,
        "finalize_match_id": f"{t.id}_gf_0",
    }


@pytest.fixture
def elo_env(monkeypatch):
    import storage.player_stats_store as pss
    import storage.user_balance_store as ubs
    import storage.json_store as js

    stats_map = {}
    for i in range(1, 9):
        stats_map[(1, i)] = PlayerStats(
            guild_id=1, user_id=i, name=f"P{i}", elo=1000 + i * 10
        )
    stats_store = _FakeStatsStore(stats_map)
    balance_store = _FakeBalanceStore()
    json_store = _FakeJsonStore()

    monkeypatch.setattr(pss, "player_stats_store", stats_store)
    monkeypatch.setattr(ubs, "user_balance_store", balance_store)
    monkeypatch.setattr(js, "store", json_store)

    return SimpleNamespace(
        stats=stats_store, balance=balance_store, json=json_store
    )


@pytest.mark.asyncio
async def test_process_match_result_applies_classic_elo(elo_env):
    from views.kd_input_view import process_match_result

    names_a = [f"P{i}" for i in range(1, 5)]
    names_b = [f"P{i}" for i in range(5, 9)]
    uids = {f"P{i}": i for i in range(1, 9)}
    t = _make_tournament(names_a, names_b, uids)

    kd = {f"P{i}": {"kills": 6, "deaths": 3} for i in range(1, 9)}
    kd["P8"] = {"kills": 1, "deaths": 9}

    applied = await process_match_result(1, t, _match_info(t, kd))
    assert applied is True

    # Одна транзакционная запись на весь матч
    assert len(elo_env.stats.set_many_calls) == 1
    assert len(elo_env.stats.set_many_calls[0]) == 8

    # Проверка против эталонной формулы для каждого игрока.
    # Ранги до матча: elo = 1000+10i -> P8 rank1 ... P1 rank8
    pre_rank = {i: 9 - i for i in range(1, 9)}
    # catch-up: server avg до матча = mean(1010..1080) = 1045;
    # personal bonus = 0 (games=0 у всех)
    avg_server = sum(1000 + i * 10 for i in range(1, 9)) / 8

    for p in elo_env.stats.set_many_calls[0]:
        uid = p.user_id
        name = f"P{uid}"
        won = uid <= 4
        k, d = kd[name]["kills"], kd[name]["deaths"]
        rank = pre_rank[uid]
        expected = _ref_calculate_balanced_elo_change(
            position=1, team_won=won, kills=k, deaths=d,
            current_elo=1000 + uid * 10,
            avg_kills=0.0, avg_deaths=0.0,
            avg_server_elo=avg_server,
            leaderboard_rank=rank,
        )
        assert p.last_elo_change == expected, (
            f"{name}: got {p.last_elo_change}, want {expected}"
        )
        assert p.elo == 1000 + uid * 10 + expected
        assert p.games == 1

    # Монеты выданы идемпотентно по match_id
    assert len(elo_env.balance.grants) == 8

    # Флаг обработанного матча выставлен, временные данные очищены
    match_id = f"{t.id}_grand_final_0"
    assert match_id in t.processed_elo_matches
    assert match_id in t.temp_match_stats


@pytest.mark.asyncio
async def test_process_match_result_idempotent(elo_env):
    from views.kd_input_view import process_match_result

    names_a = [f"P{i}" for i in range(1, 5)]
    names_b = [f"P{i}" for i in range(5, 9)]
    uids = {f"P{i}": i for i in range(1, 9)}
    t = _make_tournament(names_a, names_b, uids)
    kd = {f"P{i}": {"kills": 5, "deaths": 4} for i in range(1, 9)}
    info = _match_info(t, kd)

    assert await process_match_result(1, t, info) is True
    elos_after_first = {
        uid: s.elo for (g, uid), s in elo_env.stats.map.items()
    }

    # Повторное подтверждение — ELO не начисляется второй раз
    assert await process_match_result(1, t, info) is False
    assert len(elo_env.stats.set_many_calls) == 1
    for (g, uid), s in elo_env.stats.map.items():
        assert s.elo == elos_after_first[uid]
    # Монеты не задвоились
    assert len(elo_env.balance.grants) == 8


@pytest.mark.asyncio
async def test_process_match_result_db_failure_retry(elo_env):
    from views.kd_input_view import process_match_result

    names_a = [f"P{i}" for i in range(1, 5)]
    names_b = [f"P{i}" for i in range(5, 9)]
    uids = {f"P{i}": i for i in range(1, 9)}
    t = _make_tournament(names_a, names_b, uids)
    kd = {f"P{i}": {"kills": 5, "deaths": 4} for i in range(1, 9)}
    info = _match_info(t, kd)

    elo_env.stats.fail_next_set_many = True
    with pytest.raises(RuntimeError):
        await process_match_result(1, t, info)

    # Флаг откачен — повторная финализация возможна
    match_id = f"{t.id}_grand_final_0"
    assert match_id not in t.processed_elo_matches

    assert await process_match_result(1, t, info) is True
    assert len(elo_env.stats.set_many_calls) == 1
    # ELO применился ровно один раз
    for (g, uid), s in elo_env.stats.map.items():
        assert s.games == 1


@pytest.mark.asyncio
async def test_process_match_result_elo_survives_restart(elo_env):
    """ELO, записанный в store, сохраняется — «перезапуск» = новое
    чтение из того же хранилища."""
    from views.kd_input_view import process_match_result

    names_a = [f"P{i}" for i in range(1, 5)]
    names_b = [f"P{i}" for i in range(5, 9)]
    uids = {f"P{i}": i for i in range(1, 9)}
    t = _make_tournament(names_a, names_b, uids)
    kd = {f"P{i}": {"kills": 7, "deaths": 2} for i in range(1, 9)}

    await process_match_result(1, t, _match_info(t, kd))

    # «Рестарт»: store — источник истины, читаем заново
    for (g, uid), s in elo_env.stats.map.items():
        fresh = await elo_env.stats.get(g, uid)
        assert fresh.elo == s.elo
        assert fresh.last_elo_change == s.last_elo_change
        assert fresh.games == 1
        assert fresh.elo != 1000 + uid * 10  # ELO реально изменился
