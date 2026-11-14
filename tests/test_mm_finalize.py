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
        # имитация успешной записи: флаг + stats_done
        tournament.processed_elo_matches.add(
            f"{tournament.id}_{match_info['match_type']}_"
            f"{match_info['match_index']}"
        )
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
