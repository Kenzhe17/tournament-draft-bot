"""Тесты удаления игровой REP (r!z3 reputation cleanup).

Проверяет:
- update_player_stats_from_match НЕ меняет reputation (win/loss);
- player_stats_store.set/set_many не перезаписывают reputation
  устаревшим объектом (JSON-fallback и PG-путь);
- services.rep_cleanup: план списания, disputed-список,
  идемпотентность применения.

Запуск: pytest tests/test_rep_game_cleanup.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.player_stats import PlayerStats
from services import rep_cleanup
from utils.rating_calculator import update_player_stats_from_match


def _stats(rep: int = 0, **kw) -> PlayerStats:
    return PlayerStats(guild_id=1, user_id=42, name="P", reputation=rep, **kw)


# ------------------------------------------------------------------ матч

class TestMatchDoesNotTouchRep:
    def test_win_no_rep_change(self):
        stats = _stats(rep=17)
        update_player_stats_from_match(
            stats, kills=5, deaths=1, elo_change=12, team_won=True
        )
        assert stats.reputation == 17
        assert stats.wins == 1
        assert stats.elo == 1012

    def test_loss_no_rep_change(self):
        stats = _stats(rep=17)
        update_player_stats_from_match(
            stats, kills=1, deaths=5, elo_change=-10, team_won=False
        )
        assert stats.reputation == 17
        assert stats.wins == 0

    def test_repeat_finalize_no_rep_change(self):
        stats = _stats(rep=3)
        for _ in range(3):
            update_player_stats_from_match(
                stats, kills=2, deaths=2, elo_change=5, team_won=True
            )
        assert stats.reputation == 3
        assert stats.wins == 3


# ------------------------------------------------- store: JSON fallback

class TestStorePreservesRepJson:
    @pytest.mark.asyncio
    async def test_set_preserves_existing_reputation(self):
        from storage.player_stats_store import PlayerStatsStore

        store = PlayerStatsStore.__new__(PlayerStatsStore)
        store._use_db = False
        store._stats = {}
        store.save = lambda: None  # не писать data/player_stats.json

        fresh = _stats(rep=0)
        await store.set(fresh)
        # «Чужое» начисление REP 2.0 — репутация выросла в БД/кэше
        store._stats["1:42"].reputation = 23

        stale = _stats(rep=0)
        stale.elo = 1200
        await store.set(stale)
        assert store._stats["1:42"].reputation == 23
        assert store._stats["1:42"].elo == 1200

    @pytest.mark.asyncio
    async def test_set_many_preserves_existing_reputation(self):
        from storage.player_stats_store import PlayerStatsStore

        store = PlayerStatsStore.__new__(PlayerStatsStore)
        store._use_db = False
        store._stats = {}
        store.save = lambda: None

        await store.set(_stats(rep=0))
        store._stats["1:42"].reputation = 30

        await store.set_many([_stats(rep=0)])
        assert store._stats["1:42"].reputation == 30


# ------------------------------------------------------- store: PG путь

class _FakePgConn:
    """Минимальный asyncpg-conn: считает execute, отдаёт queued fetchval."""

    def __init__(self, fetchval_results=None):
        self.queries: list[str] = []
        self.fetchval_results = list(fetchval_results or [])

    async def execute(self, query, *args):
        self.queries.append(query)

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if self.fetchval_results:
            return self.fetchval_results.pop(0)
        return None

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    def transaction(self):
        return _FakeTx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


async def _async_ret(value):
    return value


def _pg_store(monkeypatch, conn):
    from storage.player_stats_store import PlayerStatsStore
    import storage.db as db

    store = PlayerStatsStore.__new__(PlayerStatsStore)
    store._use_db = True
    monkeypatch.setattr(db, "get_pool", lambda: _async_ret(_FakePool(conn)))
    return store


class TestStorePreservesRepPg:
    @pytest.mark.asyncio
    async def test_set_update_sql_omits_reputation(self, monkeypatch):
        conn = _FakePgConn()
        store = _pg_store(monkeypatch, conn)
        await store.set(_stats(rep=99))
        assert len(conn.queries) == 1
        q = conn.queries[0]
        update_part = q.split("DO UPDATE SET", 1)[1]
        assert "reputation" not in update_part
        # INSERT по-прежнему принимает reputation для новых строк
        assert "reputation" in q.split("DO UPDATE SET", 1)[0]

    @pytest.mark.asyncio
    async def test_set_many_update_sql_omits_reputation(self, monkeypatch):
        conn = _FakePgConn()
        store = _pg_store(monkeypatch, conn)
        await store.set_many([_stats(rep=99), _stats(rep=5)])
        stats_queries = [
            q for q in conn.queries if "player_stats" in q
        ]
        assert len(stats_queries) == 2
        for q in stats_queries:
            update_part = q.split("DO UPDATE SET", 1)[1]
            assert "reputation" not in update_part


# ------------------------------------------------------- rep_cleanup plan

def _row(gid, uid, rep, name="P"):
    return (gid, uid, name, rep)


class TestCleanupPlan:
    def test_deducts_only_confirmed_game_rep(self):
        # rep=35: 12 journaled + 10 legacy manual + 13 game
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 35)],
            tx_sums={(1, 1): 12},
            game_est={(1, 1): 4 * 1 + 3 * 2},  # 4 матча, 3 победы = 10
            legacy_manual={(1, 1): 10},
        )
        p = plan[0]
        assert p["remainder"] == 13
        assert p["deduct"] == 10
        assert p["rep_after"] == 25
        assert p["disputed"] == 3  # 13 - 10 неподтверждённых

    def test_example_from_spec(self):
        # 35 REP: 12 игры + 10 буст + 10 два инвайта + 3 /rep -> 23
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 35)],
            tx_sums={(1, 1): 23},   # 10 boost + 10 invites + 3 manual
            game_est={(1, 1): 12},
            legacy_manual={},
        )
        p = plan[0]
        assert p["deduct"] == 12
        assert p["rep_after"] == 23
        assert p["disputed"] == 0

    def test_estimate_capped_by_remainder(self):
        # История длиннее, чем неучтённый остаток — списываем только остаток.
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 10)],
            tx_sums={(1, 1): 0},
            game_est={(1, 1): 50},
            legacy_manual={},
        )
        p = plan[0]
        assert p["deduct"] == 10
        assert p["rep_after"] == 0
        assert p["disputed"] == 0

    def test_no_unexplained_rep_no_deduction(self):
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 20)],
            tx_sums={(1, 1): 20},
            game_est={(1, 1): 99},
            legacy_manual={},
        )
        assert plan[0]["deduct"] == 0
        assert plan[0]["rep_after"] == 20

    def test_legacy_unknown_blocks_deduction(self):
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 30)],
            tx_sums={(1, 1): 5},
            game_est={(1, 1): 25},
            legacy_manual=None,  # rep_given.json недоступен
        )
        p = plan[0]
        assert p["deduct"] == 0
        assert p["disputed"] == 25

    def test_no_match_history_everything_disputed(self):
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 15)],
            tx_sums={(1, 1): 5},
            game_est={},
            legacy_manual={},
            match_history_available=False,
        )
        p = plan[0]
        assert p["deduct"] == 0
        assert p["disputed"] == 10

    def test_disputed_players_filter(self):
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 10), _row(1, 2, 8)],
            tx_sums={(1, 1): 0, (1, 2): 8},
            game_est={(1, 1): 4},
            legacy_manual={},
        )
        disputed = rep_cleanup.disputed_players(plan)
        # user1: remainder 10, est 4 -> disputed 6; user2: disputed 0
        assert [d["user_id"] for d in disputed] == [1]


class TestLoadLegacyManualRep:
    def test_parses_rep_given(self, tmp_path):
        f = tmp_path / "rep_given.json"
        f.write_text(
            '{"1:100": {"targets": ["200", "300"], "day": "", "count": 2},'
            ' "1:101": ["200"]}',
            encoding="utf-8",
        )
        out = rep_cleanup.load_legacy_manual_rep(f)
        assert out == {(1, 200): 2, (1, 300): 1}

    def test_missing_file_returns_none(self, tmp_path):
        assert rep_cleanup.load_legacy_manual_rep(tmp_path / "nope.json") is None


# ------------------------------------------------------- apply idempotency

class TestApplyCleanup:
    @pytest.mark.asyncio
    async def test_second_run_is_noop(self):
        conn = _FakePgConn(fetchval_results=[1])  # op уже существует
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 10)],
            tx_sums={}, game_est={(1, 1): 5}, legacy_manual={},
        )
        result = await rep_cleanup.apply_cleanup_plan(conn, plan, op_id="test_op")
        assert result["applied"] is False
        assert result["reason"] == "already_ran"
        # Ни одного UPDATE player_stats
        assert not any("UPDATE player_stats" in q for q in conn.queries)

    @pytest.mark.asyncio
    async def test_apply_writes_items_and_deducts(self):
        conn = _FakePgConn(fetchval_results=[None, 7])  # no run; rep->7
        plan = rep_cleanup.build_cleanup_plan(
            stats_rows=[_row(1, 1, 10, "Hero")],
            tx_sums={}, game_est={(1, 1): 4}, legacy_manual={},
        )
        result = await rep_cleanup.apply_cleanup_plan(conn, plan, op_id="test_op2")
        assert result["applied"] is True
        assert result["total_deducted"] == 4
        updates = [q for q in conn.queries if "UPDATE player_stats" in q]
        assert len(updates) == 1
        assert "GREATEST(reputation - $3, 0)" in updates[0]
        item_inserts = [q for q in conn.queries if "INSERT INTO rep_cleanup_items" in q]
        assert len(item_inserts) == 1
