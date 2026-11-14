"""Финализация MM-матча: exactly-once ELO, per-match lock, resume.

Проверяем finalize_mm_match без Discord API:
- два одновременных подтверждения → process_match_result вызван 1 раз;
- исключение посередине финализации → матч НЕ завершён, повтор возможен;
- stats_done в match_finalizations → ELO не начисляется повторно.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament  # noqa: E402
import views.mm_match_view as mmv  # noqa: E402
import views.kd_input_view as kdv  # noqa: E402


def _check_tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=500)
    t.teams = [
        {"players": ["P1", "P2", "P3", "P4"], "captain": "P1"},
        {"players": ["P5", "P6", "P7", "P8"], "captain": "P5"},
    ]
    t.player_user_ids = {f"P{i}": i for i in range(1, 9)}
    t.final_teams = [0, 1]
    mid = mmv.match_id_of(t)
    t.temp_match_stats[mid] = {
        f"P{i}": {"kills": 5, "deaths": 2} for i in range(1, 9)
    }
    t.match_channels = {
        "500": {
            "state": "check", "match_num": 3,
            "roster": [f"P{i}" for i in range(1, 9)],
            "card_message_id": 0,
        }
    }
    return t


def _interaction():
    guild = SimpleNamespace(
        id=1,
        get_channel=lambda cid: None,
        fetch_channel=lambda cid: None,
    )
    return SimpleNamespace(guild=guild, user=SimpleNamespace(id=1))


@pytest.fixture
def finalize_env(monkeypatch):
    calls = {"process": 0, "histories": 0, "confirm": 0}
    state = {"fail_once": False, "db_status": None}

    async def fake_process(gid, tournament, match_info, interaction=None):
        calls["process"] += 1
        if state["fail_once"]:
            state["fail_once"] = False
            raise RuntimeError("PG down")
        # имитация успешной записи: флаг + stats_done в той же tx
        tournament.processed_elo_matches.add(
            f"{tournament.id}_{match_info['match_type']}_"
            f"{match_info['match_index']}"
        )
        state["db_status"] = "stats_done"
        return True

    async def fake_histories(*a, **kw):
        calls["histories"] += 1

    async def fake_elo(*a, **kw):
        return []

    async def fake_status(gid, mid):
        return state["db_status"]

    async def fake_mark_done(gid, mid):
        state["db_status"] = "done"

    async def fake_refresh(*a, **kw):
        return None

    async def fake_release(*a, **kw):
        return None

    monkeypatch.setattr(kdv, "process_match_result", fake_process)
    monkeypatch.setattr(mmv, "_save_match_histories", fake_histories)
    monkeypatch.setattr(mmv, "_elo_lines", fake_elo)
    monkeypatch.setattr(mmv, "_refresh_card", fake_refresh)
    monkeypatch.setattr(mmv, "_release_match_channel", fake_release)

    import storage.finalize_store as fs
    monkeypatch.setattr(fs, "get_status", fake_status)
    monkeypatch.setattr(fs, "mark_done", fake_mark_done)

    return SimpleNamespace(calls=calls, state=state)


@pytest.mark.asyncio
async def test_double_confirm_applies_elo_once(finalize_env):
    t = _check_tournament()
    ix = _interaction()

    results = await asyncio.gather(
        mmv.finalize_mm_match(ix, t, "a", (5, 3)),
        mmv.finalize_mm_match(ix, t, "a", (5, 3)),
    )
    oks = [r for r in results]
    # Ровно один успех; process_match_result вызван один раз
    assert finalize_env.calls["process"] == 1
    assert sum(1 for ok, _ in oks if ok) == 1
    meta = mmv.meta_of(t)
    assert meta["state"] == "done"
    assert meta["winner"] == "a"


@pytest.mark.asyncio
async def test_failure_mid_finalize_not_completed(finalize_env):
    t = _check_tournament()
    ix = _interaction()
    finalize_env.state["fail_once"] = True

    ok, err = await mmv.finalize_mm_match(ix, t, "b", (5, 4))
    assert not ok
    meta = mmv.meta_of(t)
    assert meta["state"] == "check"  # матч НЕ завершён
    # processed-флаг откачен — повторная попытка возможна
    mid = mmv.match_id_of(t)
    assert mid not in t.processed_elo_matches

    ok2, _ = await mmv.finalize_mm_match(ix, t, "b", (5, 4))
    assert ok2
    assert finalize_env.calls["process"] == 2  # вторая попытка прошла


@pytest.mark.asyncio
async def test_stats_done_resume_skips_elo(finalize_env):
    """stats_done в БД → статистика уже применена: ELO не дублируется,
    финализация доводится до конца."""
    t = _check_tournament()
    ix = _interaction()
    finalize_env.state["db_status"] = "stats_done"

    ok, _ = await mmv.finalize_mm_match(ix, t, "a", (5, 2))
    assert ok
    assert finalize_env.calls["process"] == 0  # ELO не начислялся
    assert finalize_env.calls["histories"] == 1
    assert finalize_env.state["db_status"] == "done"
    assert mmv.meta_of(t)["state"] == "done"


@pytest.mark.asyncio
async def test_elo_applied_history_failed_retry_completes(finalize_env, monkeypatch):
    """ELO применён (stats_done + флаг), история упала → повторное
    подтверждение завершает матч БЕЗ нового ELO и БЕЗ блокировки
    processed-флагом."""
    t = _check_tournament()
    ix = _interaction()

    calls = {"hist": 0}

    async def flaky_histories(*a, **kw):
        calls["hist"] += 1
        if calls["hist"] == 1:
            raise RuntimeError("history store down")

    monkeypatch.setattr(mmv, "_save_match_histories", flaky_histories)

    ok, _ = await mmv.finalize_mm_match(ix, t, "a", (5, 1))
    assert not ok
    assert mmv.meta_of(t)["state"] == "check"  # не завершён
    mid = mmv.match_id_of(t)
    assert mid in t.processed_elo_matches  # флаг выставлен
    assert finalize_env.state["db_status"] == "stats_done"

    # Повтор: DB-маркерstats_done → ELO не трогаем, история доводится
    ok2, _ = await mmv.finalize_mm_match(ix, t, "a", (5, 1))
    assert ok2
    assert finalize_env.calls["process"] == 1   # ELO ровно один раз
    assert calls["hist"] == 2                    # история повторена
    assert finalize_env.state["db_status"] == "done"
    assert mmv.meta_of(t)["state"] == "done"


@pytest.mark.asyncio
async def test_pg_down_during_status_check_no_rewards(finalize_env, monkeypatch):
    """Недоступная PostgreSQL при проверке финализации → отказ без
    каких-либо начислений (ни ELO, ни монет, ни истории)."""
    import storage.finalize_store as fs

    async def boom(gid, mid):
        raise ConnectionError("PG unreachable")

    monkeypatch.setattr(fs, "get_status", boom)

    t = _check_tournament()
    ok, err = await mmv.finalize_mm_match(_interaction(), t, "a", (5, 0))
    assert not ok
    assert "PG unreachable" in err or "Ошибка" in err
    assert finalize_env.calls["process"] == 0
    assert finalize_env.calls["histories"] == 0
    assert mmv.meta_of(t)["state"] == "check"


@pytest.mark.asyncio
async def test_mark_done_failure_keeps_match_complete(finalize_env, monkeypatch):
    """Сбой mark_done после всех этапов: матч завершён, канал
    освобождён, ошибка не скрыта (False), статус остаётся stats_done."""
    import storage.finalize_store as fs

    async def boom(gid, mid):
        raise RuntimeError("mark_done failed")

    monkeypatch.setattr(fs, "mark_done", boom)

    t = _check_tournament()
    ok, err = await mmv.finalize_mm_match(_interaction(), t, "b", (5, 2))
    assert not ok  # ошибка не скрыта
    assert mmv.meta_of(t)["state"] == "done"   # матч завершён
    assert finalize_env.calls["process"] == 1
    assert finalize_env.state["db_status"] == "stats_done"  # не done


@pytest.mark.asyncio
async def test_mark_done_retry_after_failure_completes(finalize_env, monkeypatch):
    """mark_done упал → матч done, статус stats_done. Повторное
    подтверждение дозаписывает маркер БЕЗ повторного ELO/истории."""
    import storage.finalize_store as fs

    state = {"fail": True}

    async def flaky_mark_done(gid, mid):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("mark_done failed once")
        finalize_env.state["db_status"] = "done"

    monkeypatch.setattr(fs, "mark_done", flaky_mark_done)

    t = _check_tournament()
    ix = _interaction()

    ok, _ = await mmv.finalize_mm_match(ix, t, "a", (5, 3))
    assert not ok
    assert mmv.meta_of(t)["state"] == "done"
    assert finalize_env.state["db_status"] == "stats_done"

    # Retry: meta done + stats_done → только mark_done, ничего больше
    ok2, _ = await mmv.finalize_mm_match(ix, t, "a", (5, 3))
    assert ok2
    assert finalize_env.calls["process"] == 1      # ELO не повторялся
    assert finalize_env.calls["histories"] == 1    # история не дублировалась
    assert finalize_env.state["db_status"] == "done"
    assert mmv.meta_of(t)["finalize_marker_done"] is True


# ------------------------------------------------------------------
# Освобождение канала: проверка state/owner, retry через sweeper
# ------------------------------------------------------------------

import discord  # noqa: E402
import cogs.mm as mm  # noqa: E402
from models.tournament import TournamentPhase  # noqa: E402


def _released_tournament(match_num: int = 3, search_cid: int = 10) -> Tournament:
    t = _check_tournament()  # channel_id=500, match_channels["500"]
    t.guild_id = 1
    t.mm_search_channel_id = search_cid
    t.phase = TournamentPhase.COMPLETE
    meta = mmv.meta_of(t)
    meta["state"] = "done"
    meta["match_num"] = match_num
    return t


class _FakeSlotPool:
    """Имитация match_channel_pool.slot_info/release_channel."""

    def __init__(self, state="active", owner="mm:1:10:3", release_ok=True):
        self.state = state
        self.owner = owner
        self.release_ok = release_ok
        self.release_calls = 0

    async def slot_info(self, cid):
        return {
            "channel_id": cid, "state": self.state, "owner": self.owner,
        }

    async def release_channel(self, channel):
        self.release_calls += 1
        if self.release_ok:
            self.state = "free"
            self.owner = ""
        return self.release_ok


def _guild_with_channel(chan=None, fetch_fails=False):
    async def _fetch(cid):
        if fetch_fails or chan is None:
            raise RuntimeError("Discord API down")
        return chan

    return SimpleNamespace(
        id=1,
        get_channel=lambda cid: chan,
        fetch_channel=_fetch,
    )


@pytest.mark.asyncio
async def test_release_verified_free_sets_flag(monkeypatch):
    """Успешный release: слот FREE по факту → channel_released."""
    import services.match_channel_pool as mcp

    t = _released_tournament(match_num=3)
    pool = _FakeSlotPool(state="active", owner="mm:1:10:3")
    monkeypatch.setattr(mcp, "slot_info", pool.slot_info)
    monkeypatch.setattr(mcp, "release_channel", pool.release_channel)

    # Discord-канала нет → release_channel не вызывается, но слот
    # уже free? Нет — state active → флаг НЕ ставится.
    guild = _guild_with_channel(fetch_fails=True)
    await mmv._release_match_channel(guild, t)
    assert pool.release_calls == 0
    assert mmv.meta_of(t).get("channel_released") is not True

    # Слот уже free → флаг без Discord-вызовов
    pool.state = "free"
    await mmv._release_match_channel(guild, t)
    assert mmv.meta_of(t)["channel_released"] is True


@pytest.mark.asyncio
async def test_release_never_touches_foreign_slot(monkeypatch):
    """Слот с owner другого поколения — НЕ освобождается."""
    import services.match_channel_pool as mcp

    t = _released_tournament(match_num=3)
    # Тот же search channel, но match_num=9 — чужой матч
    pool = _FakeSlotPool(state="active", owner="mm:1:10:9")
    monkeypatch.setattr(mcp, "slot_info", pool.slot_info)
    monkeypatch.setattr(mcp, "release_channel", pool.release_channel)

    await mmv._release_match_channel(_guild_with_channel(), t)
    assert pool.release_calls == 0       # release_channel не вызывался
    assert pool.state == "active"        # слот чужого матча цел
    assert mmv.meta_of(t)["channel_released"] is True  # для нас — ушёл


@pytest.mark.asyncio
async def test_release_failure_leaves_retry_flag_unset(monkeypatch):
    """Discord API недоступен + слот не free → channel_released
    не выставляется, sweeper повторит."""
    import services.match_channel_pool as mcp

    t = _released_tournament(match_num=3)
    pool = _FakeSlotPool(state="active", owner="mm:1:10:3",
                         release_ok=False)
    monkeypatch.setattr(mcp, "slot_info", pool.slot_info)
    monkeypatch.setattr(mcp, "release_channel", pool.release_channel)

    # Канал не резолвится → Discord-часть пропускается,
    # slot всё ещё active → флаг не ставится.
    await mmv._release_match_channel(
        _guild_with_channel(fetch_fails=True), t)
    assert mmv.meta_of(t).get("channel_released") is not True


@pytest.mark.asyncio
async def test_sweeper_retries_pending_release(monkeypatch):
    """_retry_pending_channel_releases вызывает release только для
    завершённых MM-матчей без channel_released."""
    t = _released_tournament(match_num=3)
    store_fake = SimpleNamespace(
        get_by_guild=lambda gid: [t] if gid == 1 else []
    )
    monkeypatch.setattr(mm, "store", store_fake)

    cog = mm.MMCog.__new__(mm.MMCog)
    cog.bot = SimpleNamespace(guilds=[SimpleNamespace(id=1)])

    called = []

    async def fake_release(guild, tournament):
        called.append(tournament.id)
        mmv.meta_of(tournament)["channel_released"] = True

    monkeypatch.setattr(mmv, "_release_match_channel", fake_release)

    await cog._retry_pending_channel_releases()
    assert called == [t.id]

    # Флаг выставлен → повторных вызовов нет
    await cog._retry_pending_channel_releases()
    assert called == [t.id]


@pytest.mark.asyncio
async def test_sweeper_repairs_finalize_marker(monkeypatch):
    """_reconcile_mm_finalizations: stats_done → mark_done для
    завершённого матча; ELO/история не трогаются."""
    t = _released_tournament(match_num=3)
    mmv.meta_of(t).pop("channel_released", None)
    store_fake = SimpleNamespace(
        get_by_guild=lambda gid: [t] if gid == 1 else []
    )
    monkeypatch.setattr(mm, "store", store_fake)

    cog = mm.MMCog.__new__(mm.MMCog)
    cog.bot = SimpleNamespace(guilds=[SimpleNamespace(id=1)])

    import storage.finalize_store as fs
    marked = []

    async def fake_status(gid, mid):
        return "stats_done"

    async def fake_mark(gid, mid):
        marked.append(mid)

    monkeypatch.setattr(fs, "get_status", fake_status)
    monkeypatch.setattr(fs, "mark_done", fake_mark)

    await cog._reconcile_mm_finalizations()
    assert len(marked) == 1
    assert mmv.meta_of(t)["finalize_marker_done"] is True

    # Повторный прогон — маркер уже починен, mark_done не вызывается
    await cog._reconcile_mm_finalizations()
    assert len(marked) == 1
