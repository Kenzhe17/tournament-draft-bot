"""Интеграционные тесты публичных публикаций CASH CUP.

pgserver (временный PostgreSQL) + fake Discord-каналы: проверяют
идемпотентность публикаций, отсутствие дублей, пинг капитанов
ровно один раз, обновление на месте и восстановление удалённых
сообщений — без единого реального Discord-вызова.
"""
from __future__ import annotations

import discord
import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import cogs.cashcup_public as pubmod
import storage.db as dbmod
from services.cashcup_bracket import create_bracket, ready_matches, record_result
from services.cashcup_publication_store import cashcup_publication_store as pubstore
from services.cashcup_store import cashcup_store

pytestmark = pytest.mark.asyncio

G = 556000
BRACKET_CH = 700001
MATCH_CH = 700002


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_ccpub")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    cashcup_store._ready = False
    pubstore._ready = False
    await cashcup_store.ensure_schema()
    await pubstore.ensure_schema()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cashcup_audit")
        await conn.execute("DELETE FROM cashcup_tournaments")
        await conn.execute("DELETE FROM cashcup_publications")
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


# ---------- fake Discord ----------

class _Resp:
    status = 500
    reason = "fake"


class _Perms:
    view_channel = True
    send_messages = True
    embed_links = True
    read_message_history = True


class FakeMessage:
    _next = 100000

    def __init__(self, channel, **kwargs):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel = channel
        self.kwargs = kwargs
        self.content = kwargs.get("content") or ""
        self.embeds = list(kwargs.get("embeds") or
                           ([kwargs["embed"]] if kwargs.get("embed") else []))
        self.deleted = False
        self.edit_calls = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"] or ""
        if "embeds" in kwargs:
            self.embeds = list(kwargs["embeds"])
        elif "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] else []
        self.kwargs.update(kwargs)
        return self

    async def delete(self):
        self.deleted = True
        self.channel._messages.pop(self.id, None)


class FakeChannel:
    def __init__(self, cid, guild):
        self.id = cid
        self.guild = guild
        self._messages = {}
        self.send_calls = []
        self.fail_next_send = 0
        self.fail_next_edit = 0

    def permissions_for(self, member):
        return _Perms()

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        if self.fail_next_send > 0:
            self.fail_next_send -= 1
            raise discord.HTTPException(_Resp(), "fake discord error")
        msg = FakeMessage(self, **kwargs)
        self._messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self._messages.get(mid)
        if msg is None or msg.deleted:
            raise discord.NotFound(_Resp(), "gone")
        return msg

    async def history(self, limit=100):
        msgs = sorted(self._messages.values(), key=lambda m: -m.id)
        for m in msgs[:limit]:
            yield m


class FakeGuild:
    def __init__(self, gid, channels):
        self.id = gid
        self._channels = channels
        self.me = object()

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise discord.NotFound(_Resp(), "no channel")
        return ch


class FakeBot:
    def __init__(self, guild):
        self._guild = guild
        self.guilds = [guild]

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None


def _teams(n: int = 18) -> list[dict]:
    teams = []
    for i in range(n):
        dids = [9200 + i * 10 + j for j in range(5)]
        members = [{"discord_id": d, "ff_uid": str(820000 + i * 10 + j),
                    "nick": f"Pub{i}-{j}", "region": "ru",
                    "device": "pc", "ff_ok": True, "verified": True}
                   for j, d in enumerate(dids)]
        teams.append({"id": f"tm{i:02d}", "name": f"PubTeam{i:02d}",
                      "captain": dids[0], "members": members,
                      "pending": [], "lineup": dids[:4]})
    return teams


async def _running_tournament(teams_n: int = 18) -> dict:
    await cashcup_store.create(G, 12345, 1, "TEST PRIZE")

    def change(state):
        state["teams"] = _teams(teams_n)
        state["bracket"] = create_bracket(
            [t["id"] for t in state["teams"]], seed=11)
        state["status"] = "running"
    state, _ = await cashcup_store.mutate(G, 1, "test_start", change)
    return state


@pytest.fixture
def discord_env(monkeypatch):
    """Fake guild/bot + сконфигурированные каналы публикаций."""
    guild = FakeGuild(G, {})
    bch = FakeChannel(BRACKET_CH, guild)
    mch = FakeChannel(MATCH_CH, guild)
    guild._channels = {BRACKET_CH: bch, MATCH_CH: mch}
    bot = FakeBot(guild)
    monkeypatch.setattr(pubmod, "CASHCUP_BRACKET_CHANNEL_ID", BRACKET_CH)
    monkeypatch.setattr(pubmod, "CASHCUP_MATCH_CHANNEL_ID", MATCH_CH)
    cog = pubmod.CashCupPublicCog(bot)
    return cog, guild, bch, mch


class TestPublicationStore:
    async def test_ensure_row_idempotent(self, pg_pool):
        r1 = await pubstore.ensure_row("t1", G, "bracket")
        r2 = await pubstore.ensure_row("t1", G, "bracket")
        assert r1["id"] == r2["id"]
        rows = await pubstore.list_for_tournament("t1")
        assert len(rows) == 1

    async def test_unique_key(self, pg_pool):
        await pubstore.ensure_row("t2", G, "match:W1-1")
        with pytest.raises(asyncpg.UniqueViolationError):
            async with dbmod._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO cashcup_publications "
                    "(tournament_id,guild_id,publication_key) "
                    "VALUES ('t2',$1,'match:W1-1')", G)

    async def test_get_by_message(self, pg_pool):
        row = await pubstore.ensure_row("t3", G, "match:W1-2")
        await pubstore.update(row["id"], channel_id=MATCH_CH,
                              message_id=555001, status="ok")
        found = await pubstore.get_by_message(G, 555001)
        assert found and found["publication_key"] == "match:W1-2"
        assert await pubstore.get_by_message(G, 999) is None


class TestSync:
    async def test_initial_publish_and_dedup(self, pg_pool, discord_env):
        cog, _guild, bch, mch = discord_env
        state = await _running_tournament(18)
        tid = state["tournament_id"]
        n_matches = len(state["bracket"]["matches"])

        assert await cog.sync_guild(G) is True
        assert len(bch.send_calls) == 1           # одна сетка
        assert len(mch.send_calls) == n_matches   # по сообщению на матч
        rows = await pubstore.list_for_tournament(tid)
        assert len(rows) == n_matches + 1
        assert all(r["status"] == "ok" for r in rows)
        assert all(r["message_id"] for r in rows)

        # Пинг капитанов — ровно при создании, только два id.
        ping_calls = [c for c in mch.send_calls if c.get("content")]
        assert len(ping_calls) == n_matches
        for call in ping_calls:
            users = call["allowed_mentions"].users
            assert len(users) == 2
            assert "@everyone" not in call["content"]
            assert "@here" not in call["content"]

        # Повторный sync — ни одного нового сообщения и ни одного edit.
        bch.send_calls.clear(); mch.send_calls.clear()
        edits_before = sum(len(m.edit_calls) for m in bch._messages.values())
        edits_before += sum(len(m.edit_calls) for m in mch._messages.values())
        assert await cog.sync_guild(G) is True
        assert not bch.send_calls and not mch.send_calls
        edits_after = sum(len(m.edit_calls) for m in bch._messages.values())
        edits_after += sum(len(m.edit_calls) for m in mch._messages.values())
        assert edits_after == edits_before

    async def test_result_updates_in_place_and_new_pairs_appear(
            self, pg_pool, discord_env):
        cog, _guild, _bch, mch = discord_env
        state = await _running_tournament(18)
        tid = state["tournament_id"]
        await cog.sync_guild(G)

        # Сыграть все готовые матчи раунда → sync → новые пары.
        def change(st):
            ready = ready_matches(st["bracket"])
            for m in ready:
                record_result(st["bracket"], m["id"], 1, 0,
                              evidence="test")
            return len(ready)
        await cashcup_store.mutate(G, 1, "test_results", change,
                                   tournament_id=tid)
        assert await cog.sync_guild(G) is True

        rows = await pubstore.list_for_tournament(tid)
        total_matches = len((await cashcup_store.get(G, tid))
                            ["bracket"]["matches"])
        assert len(rows) == total_matches + 1
        # Новые сообщения только для вновь открытых пар.
        assert len(mch._messages) == total_matches
        # Все message_id уникальны — дублей нет.
        ids = [r["message_id"] for r in rows if r["message_id"]]
        assert len(ids) == len(set(ids))

    async def test_deleted_message_recovered_without_ping(
            self, pg_pool, discord_env):
        cog, _guild, _bch, mch = discord_env
        state = await _running_tournament(18)
        tid = state["tournament_id"]
        await cog.sync_guild(G)

        row = next(r for r in await pubstore.list_for_tournament(tid)
                   if r["publication_key"].startswith("match:"))
        old_mid = row["message_id"]
        mch._messages.pop(old_mid)  # «ручное» удаление
        # Удаление обнаруживается на verify-тике (next_attempt_at)
        # или при изменении хэша — форсируем наступление проверки.
        async with dbmod._pool.acquire() as conn:
            await conn.execute(
                "UPDATE cashcup_publications SET next_attempt_at=NULL "
                "WHERE tournament_id=$1 AND publication_key=$2",
                tid, row["publication_key"])

        mch.send_calls.clear()
        assert await cog.sync_guild(G) is True
        new_row = await pubstore.get(tid, row["publication_key"])
        assert new_row["message_id"] != old_mid
        # Повторного пинга нет: сообщение восстановлено молча.
        assert all(not c.get("content") for c in mch.send_calls)

    async def test_send_error_retries(self, pg_pool, discord_env):
        cog, _guild, _bch, mch = discord_env
        state = await _running_tournament(18)
        tid = state["tournament_id"]
        mch.fail_next_send = 1
        assert await cog.sync_guild(G) is False
        row = next(r for r in await pubstore.list_for_tournament(tid)
                   if r["status"] == "error")
        assert row["retry_count"] >= 1
        # Ближайшая попытка отложена backoff'ом — повторный sync
        # не шлёт ничего в Discord.
        mch.send_calls.clear()
        await cog.sync_guild(G)
        assert not mch.send_calls
        # Принудительно делаем retry due и проверяем восстановление.
        async with dbmod._pool.acquire() as conn:
            await conn.execute(
                "UPDATE cashcup_publications SET next_attempt_at=NULL "
                "WHERE tournament_id=$1", tid)
        assert await cog.sync_guild(G) is True
        rows = await pubstore.list_for_tournament(tid)
        assert all(r["status"] == "ok" for r in rows)

    async def test_history_scan_failure_blocks_send(
            self, pg_pool, discord_env):
        """Ошибка чтения истории ≠ «публикации нет»: без надёжного
        подтверждения отсутствие send запрещён (fail-closed)."""
        cog, _guild, bch, mch = discord_env
        state = await _running_tournament(8)
        tid = state["tournament_id"]
        # Строки публикаций без message_id — состояние «send был,
        # ответ потерян». Канал содержит «сироту», но history
        # недоступна → sync обязан отказать, а не создать дубль.
        for m in state["bracket"]["matches"]:
            await pubstore.ensure_row(tid, G, f"match:{m['id']}")
        await pubstore.ensure_row(tid, G, "bracket")

        async def _boom(limit=100):
            raise discord.HTTPException(_Resp(), "history down")
            yield  # pragma: no cover
        mch.history = _boom
        bch.history = _boom

        assert await cog.sync_guild(G) is False
        assert not mch.send_calls and not bch.send_calls
        rows = await pubstore.list_for_tournament(tid)
        assert all(r["status"] == "error" for r in rows)

    async def test_lost_send_response_adopted_by_marker(
            self, pg_pool, discord_env):
        """Потерянный ответ channel.send: сообщение опубликовано,
        message_id не сохранён → находится по footer-marker,
        фактический id записывается, дубль не создаётся, pinged
        выставляется по уже доставленному упоминанию."""
        import services.cashcup_public_render as render
        cog, _guild, _bch, mch = discord_env
        state = await _running_tournament(8)
        tid = state["tournament_id"]
        m0 = state["bracket"]["matches"][0]
        key = f"match:{m0['id']}"
        card = render.match_card(state, m0)
        # «Сирота»: send прошёл с пингом, ответ/запись потеряны.
        orph = FakeMessage(mch, embed=card["embed"],
                           content=card["content"] or "<@1> <@2>")
        mch._messages[orph.id] = orph
        await pubstore.ensure_row(tid, G, key)

        assert await cog.sync_guild(G) is True
        row = await pubstore.get(tid, key)
        assert row["message_id"] == orph.id
        assert row["channel_id"] == MATCH_CH
        assert row["pinged"] is True
        # Карточка m0 не отправлялась повторно — только edit сироты.
        for call in mch.send_calls:
            for emb in (call.get("embeds") or
                        ([call["embed"]] if call.get("embed") else [])):
                ft = getattr(getattr(emb, "footer", None), "text", "") or ""
                assert f":{key}" not in ft
        assert orph.edit_calls  # сообщение отредактировано на месте

    async def test_backfill_completed_tournament_no_pings(
            self, pg_pool, discord_env):
        """Включение функции на уже идущем/завершённом турнире:
        backfill публикует все матчи, но без пингов капитанов."""
        from services.cashcup_sim import simulate_to_final
        cog, _guild, _bch, mch = discord_env
        await cashcup_store.create(G, 12345, 1, "BACKFILL")

        def change(st):
            st["teams"] = _teams(8)
            st["bracket"] = create_bracket(
                [t["id"] for t in st["teams"]], seed=5)
            st["status"] = "running"
            simulate_to_final(st, seed=5)
            st["status"] = "completed"
        state, _ = await cashcup_store.mutate(G, 1, "backfill", change)
        tid = state["tournament_id"]

        assert await cog.sync_guild(G) is True
        rows = await pubstore.list_for_tournament(tid)
        assert len(rows) == len(state["bracket"]["matches"]) + 1
        # Все карточки созданы, но ни одна не содержит пинга.
        assert mch.send_calls
        assert all(not c.get("content") for c in mch.send_calls)

    async def test_disabled_without_channels(self, pg_pool, monkeypatch):
        guild = FakeGuild(G, {})
        bot = FakeBot(guild)
        monkeypatch.setattr(pubmod, "CASHCUP_BRACKET_CHANNEL_ID", 0)
        monkeypatch.setattr(pubmod, "CASHCUP_MATCH_CHANNEL_ID", 0)
        cog = pubmod.CashCupPublicCog(bot)
        await _running_tournament(6)
        assert await cog.sync_guild(G) is True
        assert await pubstore.list_for_tournament(
            (await cashcup_store.latest(G))["tournament_id"]) == []
