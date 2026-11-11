"""Concurrency & atomicity tests for economy stores.

PostgreSQL в тестовом окружении недоступна, поэтому используется
FakePG — минимальная in-memory реализация asyncpg-интерфейса
(pool.acquire / conn.transaction / execute / fetchrow / fetch),
которая исполняет ровно те SQL-паттерны, что генерируют
user_balance_store и bet_store. Транзакция сериализуется общим
asyncio.Lock и поддерживает rollback по snapshot — это позволяет
проверить атомарность и идемпотентность логики без реальной БД.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from unittest.mock import AsyncMock

G, U = 1, 100
TID, MID = "tourn1", "tourn1_final_0"


# ---------------------------------------------------------------------------
# Fake asyncpg
# ---------------------------------------------------------------------------

class _Tx:
    """Транзакция: глобальная сериализация + snapshot rollback."""

    def __init__(self, db: "FakeDB") -> None:
        self._db = db
        self._snap = None

    async def __aenter__(self):
        await self._db.mu.acquire()
        self._snap = (
            copy.deepcopy(self._db.balances),
            copy.deepcopy(self._db.bets),
            copy.deepcopy(self._db.settlements),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            (self._db.balances, self._db.bets,
             self._db.settlements) = self._snap
        self._db.mu.release()
        return False


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, db: "FakeDB") -> None:
        self._db = db

    def transaction(self) -> _Tx:
        return _Tx(self._db)

    async def execute(self, sql: str, *params):
        db = self._db
        if db.fail_once and db.fail_once in sql:
            marker = db.fail_once
            db.fail_once = None
            raise RuntimeError(f"injected failure: {marker}")
        s = " ".join(sql.split())

        if "INSERT INTO user_balance" in s and "LEAST" in s:
            # Кредит от bet settlement: (g, u, payout, MAX_BALANCE)
            g, u, payout, mx = params
            cur = db.balances.get((g, u), 100)
            db.balances[(g, u)] = min(cur + payout, mx)
        elif "INSERT INTO user_balance" in s and "DO NOTHING" in s:
            # ensure_row: (g, u) -> дефолт 100
            db.balances.setdefault((params[0], params[1]), 100)
        elif s.startswith("UPDATE user_balance SET balance = $1"):
            db.balances[(params[1], params[2])] = params[0]
        elif "DELETE FROM bets" in s:
            db.bets.pop((params[0], params[1]), None)
        elif "INSERT INTO bet_settlements" in s:
            db.settlements[params[0]] = {
                "guild_id": params[1], "payouts": json.loads(params[6]),
            }
        else:
            raise AssertionError(f"unexpected execute: {s[:100]}")
        return "OK"

    async def fetchrow(self, sql: str, *params):
        db = self._db
        s = " ".join(sql.split())
        if "FROM user_balance" in s:
            key = (params[0], params[1])
            if key not in db.balances:
                return None
            return {"balance": db.balances[key]}
        if "FROM bet_settlements" in s:
            if params[0] in db.settlements:
                return {"settlement_key": params[0]}
            return None
        if "FROM match_odds" in s:
            return db.odds.get(params[0])
        raise AssertionError(f"unexpected fetchrow: {s[:100]}")

    async def fetch(self, sql: str, *params):
        db = self._db
        s = " ".join(sql.split())
        if "FROM bets" in s:
            return [dict(b) for b in db.bets.get((params[0], params[1]), [])]
        raise AssertionError(f"unexpected fetch: {s[:100]}")


class FakePool:
    def __init__(self, db: "FakeDB") -> None:
        self._conn = FakeConn(db)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class FakeDB:
    def __init__(self) -> None:
        self.mu = asyncio.Lock()
        self.balances: dict[tuple[int, int], int] = {}
        self.bets: dict[tuple[str, str], list[dict]] = {}
        self.odds: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.fail_once: str | None = None

    def balance(self, g=G, u=U) -> int:
        return self.balances.get((g, u), 100)


@pytest.fixture
def fake_pg(monkeypatch, tmp_path):
    db = FakeDB()
    pool = FakePool(db)

    async def get_pool():
        return pool

    monkeypatch.setattr("storage.db.get_pool", get_pool)

    import storage.user_balance_store as ubs
    monkeypatch.setattr(ubs, "BALANCE_FILE", tmp_path / "ub.json")
    monkeypatch.setattr(ubs, "DECAY_FILE", tmp_path / "decay.json")
    monkeypatch.setattr(
        ubs.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs.decay_tracker, "mark_processed", lambda g, u: None
    )

    import storage.betting_stats_store as bss
    monkeypatch.setattr(
        bss.betting_stats_store, "record_bet_result", AsyncMock()
    )

    return db


def _balance_store(ubs):
    store = ubs.UserBalanceStore()
    store._use_db = True
    return store


def _bet_store(monkeypatch, tmp_path):
    import storage.bet_store as bs_mod
    store = bs_mod.BetStore()
    store._use_db = True
    return store


def _add_bet(db, user_id=U, amount=100, team="Team A", guild=G):
    db.bets.setdefault((TID, MID), []).append({
        "guild_id": guild, "user_id": user_id, "user_name": f"u{user_id}",
        "tournament_id": TID, "match_id": MID, "team_name": team,
        "team_index": 0, "amount": amount, "odds": 1.9,
    })


# ---------------------------------------------------------------------------
# user_balance_store: атомарное списание
# ---------------------------------------------------------------------------

class TestSubtractBalance:
    @pytest.mark.asyncio
    async def test_1000_concurrent_subtracts_no_double_spend(
        self, fake_pg, monkeypatch, tmp_path
    ):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = 1000

        async def attempt():
            try:
                await store.subtract_balance(G, U, 10)
                return True
            except ValueError:
                return False

        results = await asyncio.gather(*[attempt() for _ in range(1000)])
        succeeded = sum(results)

        # Ровно 100 операций по 10 монет из 1000 — без двойного списания
        assert succeeded == 100
        assert fake_pg.balance() == 0
        assert fake_pg.balance() >= 0

    @pytest.mark.asyncio
    async def test_insufficient_balance_rejected(self, fake_pg, monkeypatch, tmp_path):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = 50

        with pytest.raises(ValueError, match="Insufficient"):
            await store.subtract_balance(G, U, 100)
        assert fake_pg.balance() == 50

    @pytest.mark.asyncio
    async def test_negative_and_invalid_amounts(self, fake_pg, monkeypatch, tmp_path):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)

        with pytest.raises(ValueError):
            await store.subtract_balance(G, U, -5)
        with pytest.raises(ValueError):
            await store.add_balance(G, U, -5)
        with pytest.raises(ValueError):
            await store.set_balance(G, U, -1)

    @pytest.mark.asyncio
    async def test_concurrent_add_and_subtract(self, fake_pg, monkeypatch, tmp_path):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = 100

        async def add():
            await store.add_balance(G, U, 10)

        async def sub():
            try:
                await store.subtract_balance(G, U, 10)
                return True
            except ValueError:
                return False

        await asyncio.gather(
            *[add() for _ in range(50)],
            *[sub() for _ in range(5)],
        )
        # 100 + 50*10 - 5*10 = 550, все операции успешны
        assert fake_pg.balance() == 550

    @pytest.mark.asyncio
    async def test_add_respects_max_balance(self, fake_pg, monkeypatch, tmp_path):
        from config import MAX_BALANCE
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = MAX_BALANCE - 30

        new_bal = await store.add_balance(G, U, 100)
        assert new_bal == MAX_BALANCE
        assert fake_pg.balance() == MAX_BALANCE


class TestTransferBalance:
    @pytest.mark.asyncio
    async def test_transfer_atomic_success(self, fake_pg, monkeypatch, tmp_path):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = 1000

        result = await store.transfer_balance(G, U, G, 200, 100)
        # fee 10%: total=110, sender=890, receiver=200 (100+100)
        assert result["total_deducted"] == 110
        assert result["from_balance"] == 890
        assert result["to_balance"] == 200
        assert fake_pg.balance(G, U) == 890
        assert fake_pg.balance(G, 200) == 200

    @pytest.mark.asyncio
    async def test_transfer_insufficient_rolls_back(self, fake_pg, monkeypatch, tmp_path):
        import storage.user_balance_store as ubs
        store = _balance_store(ubs)
        fake_pg.balances[(G, U)] = 50
        fake_pg.balances[(G, 200)] = 500

        with pytest.raises(ValueError, match="Insufficient"):
            await store.transfer_balance(G, U, G, 200, 100)
        # Получатель не должен получить деньги при сбое списания
        assert fake_pg.balance(G, U) == 50
        assert fake_pg.balance(G, 200) == 500


# ---------------------------------------------------------------------------
# bet_store: атомарная идемпотентная выплата
# ---------------------------------------------------------------------------

class TestResolveMatchBets:
    @pytest.mark.asyncio
    async def test_payout_credited_once(self, fake_pg, monkeypatch, tmp_path):
        store = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {
            "team_a_odds": 2.0, "team_b_odds": 1.8,
        }
        _add_bet(fake_pg, amount=100, team="Team A")

        payouts, _ = await store.resolve_match_bets(
            G, TID, MID, "Team A", 0
        )
        assert payouts[U] == 200  # 100 * 2.0
        assert fake_pg.balance(G, U) == 300  # 100 + 200
        assert (TID, MID) not in fake_pg.bets  # ставки удалены
        assert f"{TID}:{MID}" in fake_pg.settlements

    @pytest.mark.asyncio
    async def test_repeat_call_no_double_payout(self, fake_pg, monkeypatch, tmp_path):
        store = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {"team_a_odds": 2.0, "team_b_odds": 1.8}
        _add_bet(fake_pg, amount=100, team="Team A")

        await store.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert fake_pg.balance(G, U) == 300

        # Повторный вызов — идемпотентный no-op
        payouts2, _ = await store.resolve_match_bets(
            G, TID, MID, "Team A", 0
        )
        assert payouts2 == {}
        assert fake_pg.balance(G, U) == 300

    @pytest.mark.asyncio
    async def test_concurrent_resolve_single_payout(
        self, fake_pg, monkeypatch, tmp_path
    ):
        store = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {"team_a_odds": 2.0, "team_b_odds": 1.8}
        _add_bet(fake_pg, amount=100, team="Team A")

        results = await asyncio.gather(
            store.resolve_match_bets(G, TID, MID, "Team A", 0),
            store.resolve_match_bets(G, TID, MID, "Team A", 0),
            return_exceptions=True,
        )
        # Ровно один вызов рассчитан, второй — идемпотентный пропуск
        paid = [r for r in results
                if not isinstance(r, Exception) and r[0]]
        assert len(paid) == 1
        assert fake_pg.balance(G, U) == 300

    @pytest.mark.asyncio
    async def test_restart_between_commit_and_payout(
        self, fake_pg, monkeypatch, tmp_path
    ):
        """«Перезапуск»: новый экземпляр BetStore на той же БД."""
        store1 = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {"team_a_odds": 2.0, "team_b_odds": 1.8}
        _add_bet(fake_pg, amount=100, team="Team A")

        await store1.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert fake_pg.balance(G, U) == 300

        # Бот перезапустился — новый store, то же хранилище
        store2 = _bet_store(monkeypatch, tmp_path)
        payouts, _ = await store2.resolve_match_bets(
            G, TID, MID, "Team A", 0
        )
        assert payouts == {}
        assert fake_pg.balance(G, U) == 300  # двойного начисления нет

    @pytest.mark.asyncio
    async def test_failure_rolls_back_everything(
        self, fake_pg, monkeypatch, tmp_path
    ):
        """Сбой при записи settlement → ставки и балансы откачены."""
        store = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {"team_a_odds": 2.0, "team_b_odds": 1.8}
        _add_bet(fake_pg, amount=100, team="Team A")
        fake_pg.fail_once = "INSERT INTO bet_settlements"

        with pytest.raises(RuntimeError, match="injected"):
            await store.resolve_match_bets(G, TID, MID, "Team A", 0)

        # Полный rollback: ставка на месте, баланс не изменён
        assert (TID, MID) in fake_pg.bets
        assert fake_pg.balance(G, U) == 100
        assert f"{TID}:{MID}" not in fake_pg.settlements

        # Повторная обработка после устранения сбоя — успешна
        payouts, _ = await store.resolve_match_bets(
            G, TID, MID, "Team A", 0
        )
        assert payouts[U] == 200
        assert fake_pg.balance(G, U) == 300

    @pytest.mark.asyncio
    async def test_losing_bet_no_payout(self, fake_pg, monkeypatch, tmp_path):
        store = _bet_store(monkeypatch, tmp_path)
        fake_pg.balances[(G, U)] = 100
        fake_pg.odds[MID] = {"team_a_odds": 2.0, "team_b_odds": 1.8}
        _add_bet(fake_pg, amount=100, team="Team B")  # ставка на проигравшую

        payouts, _ = await store.resolve_match_bets(
            G, TID, MID, "Team A", 0
        )
        assert payouts[U] == 0
        assert fake_pg.balance(G, U) == 100
        assert f"{TID}:{MID}" in fake_pg.settlements
