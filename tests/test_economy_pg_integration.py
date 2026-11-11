"""Integration tests economy-атомарности на РЕАЛЬНОЙ PostgreSQL.

pgserver поднимает изолированный PostgreSQL (bundled binaries) —
никакие рабочие базы не затрагиваются. Проверяется именно
транзакционная семантика PG: FOR UPDATE, rollback, уникальность
bet_settlements, конкурентные соединения.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

G, U = 1, 100
TID, MID = "tourn1", "tourn1_final_0"

# Минимальная схема для economy-тестов (полный init_db проверяется
# отдельным тестом test_init_db_creates_bet_settlements).
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS user_balance (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INTEGER DEFAULT 100,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS bets (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    tournament_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    team_name TEXT NOT NULL,
    team_index INTEGER NOT NULL DEFAULT 0,
    amount INTEGER NOT NULL,
    odds REAL NOT NULL DEFAULT 1.9,
    PRIMARY KEY (guild_id, user_id, tournament_id, match_id)
);
CREATE TABLE IF NOT EXISTS match_odds (
    match_id TEXT PRIMARY KEY,
    team_a_name TEXT NOT NULL DEFAULT '',
    team_b_name TEXT NOT NULL DEFAULT '',
    team_a_odds REAL NOT NULL DEFAULT 1.9,
    team_b_odds REAL NOT NULL DEFAULT 1.9,
    team_a_buffer INTEGER NOT NULL DEFAULT 0,
    team_b_buffer INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bet_settlements (
    settlement_key TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    tournament_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    winning_team_name TEXT NOT NULL DEFAULT '',
    winning_team_index INTEGER NOT NULL DEFAULT 0,
    payouts JSONB NOT NULL DEFAULT '{}'::jsonb,
    payouts_by_name JSONB NOT NULL DEFAULT '{}'::jsonb,
    settled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS betting_stats (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    total_won INTEGER DEFAULT 0,
    total_lost INTEGER DEFAULT 0,
    total_bets INTEGER DEFAULT 0,
    successful_bets INTEGER DEFAULT 0,
    best_win INTEGER DEFAULT 0,
    worst_loss INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
"""


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    """Поднять реальный PostgreSQL (изолированный экземпляр)."""
    pgdata = tmp_path_factory.mktemp("pgdata")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    """Пул к тестовой БД со схемой; подменяет storage.db.get_pool."""
    import storage.db as dbmod

    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)

    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(pg_pool):
    async with pg_pool.acquire() as conn:
        for table in ("bets", "bet_settlements", "match_odds",
                      "user_balance", "betting_stats"):
            await conn.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def balance_store(monkeypatch, tmp_path):
    import storage.user_balance_store as ubs

    monkeypatch.setattr(ubs, "BALANCE_FILE", tmp_path / "ub.json")
    monkeypatch.setattr(ubs, "DECAY_FILE", tmp_path / "decay.json")
    monkeypatch.setattr(
        ubs.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs.decay_tracker, "mark_processed", lambda g, u: None
    )
    store = ubs.UserBalanceStore()
    store._use_db = True
    store._balances = {}
    return store


@pytest.fixture
def bet_store():
    import storage.bet_store as bs_mod

    store = bs_mod.BetStore()
    store._use_db = True
    return store


async def _set_balance(pool, g, u, value):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = $3
            """,
            g, u, value,
        )


async def _get_balance(pool, g, u):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2",
            g, u,
        )


async def _insert_bet(pool, user_id=U, amount=100, team="Team A",
                      guild=G, tid=TID, mid=MID):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bets
                (guild_id, user_id, user_name, tournament_id, match_id,
                 team_name, team_index, amount, odds)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            guild, user_id, f"u{user_id}", tid, mid, team, 0, amount, 1.9,
        )


async def _set_odds(pool, mid, a=2.0, b=1.8):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO match_odds (match_id, team_a_odds, team_b_odds)
            VALUES ($1, $2, $3)
            ON CONFLICT (match_id) DO UPDATE
            SET team_a_odds=$2, team_b_odds=$3
            """,
            mid, a, b,
        )


# ---------------------------------------------------------------------------
# user_balance_store на реальной PG
# ---------------------------------------------------------------------------

class TestBalanceRealPG:
    @pytest.mark.asyncio
    async def test_1000_concurrent_subtracts(self, pg_pool, balance_store):
        await _set_balance(pg_pool, G, U, 1000)

        async def attempt():
            try:
                await balance_store.subtract_balance(G, U, 10)
                return True
            except ValueError:
                return False

        results = await asyncio.gather(*[attempt() for _ in range(1000)])
        assert sum(results) == 100
        assert await _get_balance(pg_pool, G, U) == 0

    @pytest.mark.asyncio
    async def test_concurrent_add_subtract(self, pg_pool, balance_store):
        await _set_balance(pg_pool, G, U, 100)

        async def add():
            await balance_store.add_balance(G, U, 10)

        async def sub():
            try:
                await balance_store.subtract_balance(G, U, 10)
                return True
            except ValueError:
                return False

        await asyncio.gather(
            *[add() for _ in range(50)],
            *[sub() for _ in range(5)],
        )
        assert await _get_balance(pg_pool, G, U) == 550

    @pytest.mark.asyncio
    async def test_insufficient_and_negative(self, pg_pool, balance_store):
        await _set_balance(pg_pool, G, U, 50)
        with pytest.raises(ValueError, match="Insufficient"):
            await balance_store.subtract_balance(G, U, 100)
        assert await _get_balance(pg_pool, G, U) == 50
        with pytest.raises(ValueError):
            await balance_store.subtract_balance(G, U, -1)

    @pytest.mark.asyncio
    async def test_transfer_atomic(self, pg_pool, balance_store):
        await _set_balance(pg_pool, G, U, 1000)
        result = await balance_store.transfer_balance(G, U, G, 200, 100)
        assert result["from_balance"] == 890
        assert result["to_balance"] == 200
        assert await _get_balance(pg_pool, G, U) == 890
        assert await _get_balance(pg_pool, G, 200) == 200

    @pytest.mark.asyncio
    async def test_transfer_insufficient_no_partial(self, pg_pool, balance_store):
        await _set_balance(pg_pool, G, U, 50)
        await _set_balance(pg_pool, G, 200, 500)
        with pytest.raises(ValueError, match="Insufficient"):
            await balance_store.transfer_balance(G, U, G, 200, 100)
        assert await _get_balance(pg_pool, G, U) == 50
        assert await _get_balance(pg_pool, G, 200) == 500

    @pytest.mark.asyncio
    async def test_max_balance_cap(self, pg_pool, balance_store):
        from config import MAX_BALANCE
        await _set_balance(pg_pool, G, U, MAX_BALANCE - 30)
        new_bal = await balance_store.add_balance(G, U, 100)
        assert new_bal == MAX_BALANCE


# ---------------------------------------------------------------------------
# bet_store.resolve_match_bets на реальной PG
# ---------------------------------------------------------------------------

class TestBetSettlementRealPG:
    @pytest.mark.asyncio
    async def test_payout_credited_once(self, pg_pool, bet_store, balance_store):
        await _set_balance(pg_pool, G, U, 100)
        await _set_odds(pg_pool, MID, 2.0, 1.8)
        await _insert_bet(pg_pool, amount=100, team="Team A")

        payouts, _ = await bet_store.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert payouts[U] == 200
        assert await _get_balance(pg_pool, G, U) == 300

        async with pg_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM bets WHERE match_id=$1", MID
            ) == 0
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM bet_settlements WHERE settlement_key=$1",
                f"{TID}:{MID}",
            ) == 1

    @pytest.mark.asyncio
    async def test_repeat_and_restart_no_double(self, pg_pool, bet_store,
                                              balance_store):
        await _set_balance(pg_pool, G, U, 100)
        await _set_odds(pg_pool, MID, 2.0, 1.8)
        await _insert_bet(pg_pool, amount=100, team="Team A")

        await bet_store.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert await _get_balance(pg_pool, G, U) == 300

        # «Перезапуск»: новый экземпляр BetStore на той же БД
        import storage.bet_store as bs_mod
        store2 = bs_mod.BetStore()
        store2._use_db = True
        payouts, _ = await store2.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert payouts == {}
        assert await _get_balance(pg_pool, G, U) == 300

    @pytest.mark.asyncio
    async def test_concurrent_resolve_single_payout(self, pg_pool, bet_store,
                                                    balance_store):
        await _set_balance(pg_pool, G, U, 100)
        await _set_odds(pg_pool, MID, 2.0, 1.8)
        await _insert_bet(pg_pool, amount=100, team="Team A")

        results = await asyncio.gather(
            bet_store.resolve_match_bets(G, TID, MID, "Team A", 0),
            bet_store.resolve_match_bets(G, TID, MID, "Team A", 0),
            bet_store.resolve_match_bets(G, TID, MID, "Team A", 0),
            return_exceptions=True,
        )
        paid = [r for r in results
                if not isinstance(r, Exception) and r[0]]
        assert len(paid) == 1
        assert await _get_balance(pg_pool, G, U) == 300

    @pytest.mark.asyncio
    async def test_mid_tx_failure_full_rollback(self, pg_pool, bet_store,
                                                balance_store):
        """Сбой внутри транзакции → ставки, балансы и settlement
        откачены полностью; повторный запуск успешен."""
        await _set_balance(pg_pool, G, U, 100)
        await _set_balance(pg_pool, G, 999, 50)
        await _set_odds(pg_pool, MID, 2.0, 1.8)
        await _insert_bet(pg_pool, user_id=U, amount=100, team="Team A")
        await _insert_bet(pg_pool, user_id=999, amount=100, team="Team A")

        # Триггер отклоняет операции для user_id=999 → кредит упадёт
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE OR REPLACE FUNCTION reject_user999()
                RETURNS trigger AS $$
                BEGIN
                    IF NEW.user_id = 999 THEN
                        RAISE EXCEPTION 'injected credit failure';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """)
            await conn.execute("""
                CREATE TRIGGER trg_reject999
                BEFORE INSERT OR UPDATE ON user_balance
                FOR EACH ROW EXECUTE FUNCTION reject_user999()
            """)

        try:
            with pytest.raises(Exception, match="injected credit failure"):
                await bet_store.resolve_match_bets(G, TID, MID, "Team A", 0)
        finally:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DROP TRIGGER IF EXISTS trg_reject999 ON user_balance"
                )
                await conn.execute(
                    "DROP FUNCTION IF EXISTS reject_user999()"
                )

        # Полный rollback: обе ставки на месте, балансы не изменены,
        # settlement не создан
        async with pg_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM bets WHERE match_id=$1", MID
            ) == 2
            assert await conn.fetchval(
                "SELECT COUNT(*) FROM bet_settlements WHERE settlement_key=$1",
                f"{TID}:{MID}",
            ) == 0
        assert await _get_balance(pg_pool, G, U) == 100
        assert await _get_balance(pg_pool, G, 999) == 50

        # После устранения сбоя расчёт проходит штатно
        payouts, _ = await bet_store.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert payouts[U] == 200
        assert payouts[999] == 200
        assert await _get_balance(pg_pool, G, U) == 300
        assert await _get_balance(pg_pool, G, 999) == 250

    @pytest.mark.asyncio
    async def test_max_balance_cap_on_payout(self, pg_pool, bet_store,
                                             balance_store):
        from config import MAX_BALANCE
        await _set_balance(pg_pool, G, U, MAX_BALANCE - 50)
        await _set_odds(pg_pool, MID, 2.0, 1.8)
        await _insert_bet(pg_pool, amount=1000, team="Team A")

        payouts, _ = await bet_store.resolve_match_bets(G, TID, MID, "Team A", 0)
        assert payouts[U] == 2000  # расчётная выплата
        # Но баланс ограничен MAX_BALANCE
        assert await _get_balance(pg_pool, G, U) == MAX_BALANCE


# ---------------------------------------------------------------------------
# init_db: миграция bet_settlements на реальной PG
# ---------------------------------------------------------------------------

class TestInitDbMigration:
    @pytest.mark.asyncio
    async def test_init_db_creates_bet_settlements(self, pg_uri):
        """Полный init_db() на тестовой БД создаёт нужные таблицы."""
        import storage.db as dbmod

        prev_url = dbmod.DATABASE_URL
        dbmod.DATABASE_URL = pg_uri
        dbmod._pool = None
        try:
            await dbmod.init_db()
            pool = await dbmod.get_pool()
            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name='bet_settlements')"
                )
                assert exists is True
        finally:
            await dbmod.close_pool()
            dbmod._pool = None
            dbmod.DATABASE_URL = prev_url
