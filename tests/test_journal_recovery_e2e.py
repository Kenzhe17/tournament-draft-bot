"""E2E восстановления журнала: реальный PostgreSQL (pgserver)
+ fake Discord API.

Сценарии, приближенные к production-инцидентам:
1. pending-запись после «рестарта» → recover() → worker →
   synced, ID сохранены в PG.
2. pending_components='main' + карточка уже в канале
   (потерянный ответ send) → recovery принимает существующую,
   дубль НЕ создаётся.
3. pending_components='main' + карточки нет в истории →
   новый send ЗАПРЕЩЁН → retry_wait, канал пуст.
4. retry_wait с next_attempt_at в будущем → recover()
   планирует с задержкой, а не выполняет сразу.
5. Событийный notify + recover одновременно → одна карточка.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from storage import tournament_journal_store as jstore
from services import tournament_journal as jn
from services import tournament_audit_service as audit_svc
from models.tournament import Tournament, TournamentPhase

GID = 1196165488565289012
CID = 1571226820593061980
TID = "1_e2e"


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_journal_e2e")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev_url, prev_pool = dbmod.DATABASE_URL, dbmod._pool
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    jstore._table_ready = False
    async with pool.acquire() as conn:
        await jstore.ensure_table(conn)
        await conn.execute("DELETE FROM tournament_journal")
    try:
        yield pool
    finally:
        dbmod.DATABASE_URL = prev_url
        dbmod._pool = prev_pool
        jstore._table_ready = False
        await pool.close()


# --- fake Discord ---------------------------------------------------------


class _Msg:
    def __init__(self, mid, view=None, channel=None):
        self.id = mid
        self.view = view
        self.content = ""
        self.components = list(getattr(view, "children", []) or [])
        # Карточки журнала авторства бота — _find_card_message
        # фильтрует history по bot.user.id.
        self.author = SimpleNamespace(id=42)
        self.edits = []
        self.channel = channel
        self.guild = None
        self._channels = getattr(channel, "_channels", None)

    async def edit(self, view=None, **kw):
        self.edits.append(view)
        return self

    async def create_thread(self, name=None, **kw):
        # Discord-модель: thread.id == message.id,
        # thread.parent_id == message.channel.id.
        th = _Thread(self.id, name=name or "")
        th.parent_id = int(getattr(self.channel, "id", 0) or 0)
        th._channels = self._channels
        if self._channels is not None:
            self._channels[th.id] = th
        return th


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self._next = 1000  # маленькие id — int64-safe для PG
        self.parent_id = None
        self.archived = False
        self.name = "journal"

    async def send(self, view=None, **kw):
        self._next += 1
        m = _Msg(self._next, view, channel=self)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        import discord as _d
        for m in self.sent:
            if m.id == mid:
                return m
        raise _d.NotFound(SimpleNamespace(status=404), "nf")

    async def edit(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        return self

    def history(self, limit=100):
        async def _gen():
            for m in reversed(self.sent[-limit:]):
                yield m
        return _gen()

    def archived_threads(self, limit=100, **kw):
        async def _gen():
            return
            yield
        return _gen()


class _Thread(_Chan):
    def __init__(self, tid, name=""):
        super().__init__(tid)
        self.name = name


class _Bot:
    def __init__(self, channels):
        self._channels = channels
        self.user = SimpleNamespace(id=42)

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError("no channel")
        return ch

    def get_guild(self, gid):
        return None


def _marker_msg(mid: int, tid: str) -> _Msg:
    m = _Msg(mid)
    m.components = [SimpleNamespace(children=[
        SimpleNamespace(content=f"**ID:** `{tid}`")])]
    return m


def _tournament() -> Tournament:
    t = Tournament(guild_id=GID, channel_id=2)
    t.id = TID
    t.message_id = 555
    t.phase = TournamentPhase.SEMIFINALS
    return t


@pytest.fixture
def discord_env(monkeypatch):
    ch = _Chan(555)   # id канала маленький; ключ реестра — CID
    bot = _Bot({CID: ch})
    ch._channels = bot._channels
    # fetch_message возвращает сообщения с channel/_channels.
    _orig_fetch = ch.fetch_message

    async def _fetch(mid):
        m = await _orig_fetch(mid)
        m.channel = ch
        m._channels = bot._channels
        return m
    ch.fetch_message = _fetch
    monkeypatch.setattr(jn, "journal_store", jstore)
    monkeypatch.setattr(jn, "_bot", lambda: bot)
    monkeypatch.setattr(jn, "_audit_channel_id", lambda: CID)
    monkeypatch.setattr(jn, "_EDIT_TS", {})
    monkeypatch.setattr(jn, "_SIG_CACHE", {})
    monkeypatch.setattr(jn, "_VERIFY_TS", {})
    monkeypatch.setattr(jn, "_RECOVERY_HEAP", [])
    monkeypatch.setattr(jn, "_RECOVERY_QUEUED", set())
    monkeypatch.setattr(jn, "_PENDING_LAST_SCHEDULE", {})
    monkeypatch.setattr(jn, "_RETRY_ATTEMPTS", {})
    monkeypatch.setattr(jn, "_RECOVERY_STATS", {})
    monkeypatch.setattr(jn, "_RECOVERY_WORKER", None)
    monkeypatch.setattr(jn, "_PG_POLL_LAST", 0.0)
    monkeypatch.setattr(jn, "_FLUSH_LOCKS", {})
    monkeypatch.setattr(jn, "_DIRTY", {})
    monkeypatch.setattr(jn, "_URL_CACHE", {})
    monkeypatch.setattr(jn.tournament_store, "all", lambda: [])
    t = _tournament()

    async def _load(tid):
        return t if str(tid) == TID else None
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret([]))
    jn._RECOVERED = False
    try:
        yield ch, bot
    finally:
        jn._RECOVERED = False
        # Остановить worker, чтобы не утекал между тестами.
        w = jn._RECOVERY_WORKER
        if w is not None and not w.done():
            w.cancel()


async def _ret(events):
    return events


async def _settle(max_iter=600):
    """Дождаться завершения recovery-очереди.

    Heap пустеет в момент pop — flush ещё выполняется.
    Поэтому ждём: очередь пуста И счётчики _RECOVERY_STATS
    перестали расти (flush завершён и статус записан).
    """
    stable = 0
    last_total = -1
    for _ in range(max_iter):
        total = sum(jn._RECOVERY_STATS.values())
        busy = any(l.locked() for l in jn._FLUSH_LOCKS.values())
        quiet = (not jn._RECOVERY_HEAP
                 and not jn._RECOVERY_QUEUED
                 and not busy)
        if quiet and total == last_total and total >= 0:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        last_total = total
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_pending_record_recovers_to_synced(pg, discord_env):
    """Запись pending после «рестарта» → recover → worker →
    synced; все ID сохранены в PostgreSQL."""
    ch, bot = discord_env
    main = await jstore.upsert_main(
        guild_id=GID, channel_id=2, tournament_id=TID)
    assert main["sync_status"] == "pending"

    stats = await jn.recover(bot)
    assert stats["scheduled"] >= 1
    await _settle()

    row = await jstore.get_main(TID)
    assert row["sync_status"] == "synced"
    assert row["audit_message_id"]
    assert row["thread_id"]
    assert row["summary_message_id"]
    assert row["pending_components"] == ""
    # Основная карточка — ровно одна в канале.
    assert len(ch.sent) == 1


@pytest.mark.asyncio
async def test_e2e_lost_send_response_adopts_no_duplicate(
        pg, discord_env):
    """send прошёл, ответ потерян, PG не записал id
    (pending_components='main') → recovery находит карточку
    в истории и НЕ создаёт дубль."""
    ch, bot = discord_env
    # «Уже отправленная» карточка — как будто Discord её создал.
    ch.sent.append(_marker_msg(900001, TID))
    main = await jstore.upsert_main(
        guild_id=GID, channel_id=2, tournament_id=TID)
    await jstore.mark_send_pending(int(main["id"]), "main")
    await jstore.set_sync(int(main["id"]), "processing")
    # «Зависший» processing: обновлён давно — list_pending
    # подбирает такие записи (свежий processing пропускается).
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE tournament_journal SET updated_at = "
            "now() - interval '20 minutes' WHERE id = $1",
            int(main["id"]))

    await jn.recover(bot)
    await _settle()

    row = await jstore.get_main(TID)
    assert row["audit_message_id"] == 900001
    assert row["sync_status"] == "synced"
    # Дубля основной карточки нет — в канале только маркер.
    assert len(ch.sent) == 1


@pytest.mark.asyncio
async def test_e2e_inconclusive_lookup_never_sends(
        pg, discord_env):
    """pending_components='main', но карточки в истории нет →
    новый send ЗАПРЕЩЁН: запись в retry_wait, канал пуст."""
    ch, bot = discord_env
    main = await jstore.upsert_main(
        guild_id=GID, channel_id=2, tournament_id=TID)
    await jstore.mark_send_pending(int(main["id"]), "main")
    await jstore.set_sync(int(main["id"]), "processing")
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE tournament_journal SET updated_at = "
            "now() - interval '20 minutes' WHERE id = $1",
            int(main["id"]))

    await jn.recover(bot)
    await _settle(max_iter=200)

    row = await jstore.get_main(TID)
    # Хотя бы одна попытка выполнена и ушла в retry_wait —
    # но новых сообщений в канале НЕТ.
    assert ch.sent == []
    assert row["sync_status"] in ("retry_wait", "error",
                                  "processing")
    assert not row["audit_message_id"]


@pytest.mark.asyncio
async def test_e2e_deferred_retry_not_run_early(pg, discord_env):
    """retry_wait с next_attempt_at в будущем после рестарта:
    recover() ставит в очередь с задержкой — flush НЕ
    выполняется раньше времени."""
    ch, bot = discord_env
    main = await jstore.upsert_main(
        guild_id=GID, channel_id=2, tournament_id=TID)
    await jstore.set_retry(
        int(main["id"]), 600.0, "Discord 429: slow down")

    stats = await jn.recover(bot)
    assert stats["deferred"] == 1
    # Куча содержит tid с run_at в будущем — worker ждёт.
    assert jn._RECOVERY_HEAP
    run_at = jn._RECOVERY_HEAP[0][0]
    assert run_at - time.monotonic() > 60.0
    await asyncio.sleep(1.0)
    # За секунду flush не выполнился — канал пуст.
    assert ch.sent == []
    # Чистим кучу, чтобы worker не ждал 10 минут.
    jn._RECOVERY_HEAP.clear()
    jn._RECOVERY_QUEUED.clear()
    row = await jstore.get_main(TID)
    assert row["sync_status"] == "retry_wait"


@pytest.mark.asyncio
async def test_e2e_event_and_recovery_single_card(
        pg, discord_env):
    """notify_event (событийный путь через worker) и recover()
    для одного турнира → ровно одна основная карточка."""
    ch, bot = discord_env
    await jstore.upsert_main(
        guild_id=GID, channel_id=2, tournament_id=TID)
    jn.notify_event({
        "persisted": "pg", "source": "tournament",
        "action": "tournament.start", "tournament_id": TID,
        "actor_user_id": 1, "created_at": time.time(),
    })
    await asyncio.sleep(0.2)   # debounce ещё не прошёл
    await jn.recover(bot)
    await _settle()
    assert len(ch.sent) == 1
    row = await jstore.get_main(TID)
    assert row["sync_status"] == "synced"
