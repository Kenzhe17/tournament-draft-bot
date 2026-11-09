"""Тесты фазы Ready Check («Найден матч») для Ranked Matchmaking.

Запуск: pytest tests/test_ready_check.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.mm_search import MMSearch
from models.ready_check import ReadyCheck, ReadyCheckPlayer, ReadyCheckStatus
from storage import ready_check_store as rc_store_mod
from storage.ready_check_store import ReadyCheckStore


# ---------------------------------------------------------------- helpers

def _rc(tmp_path, monkeypatch, match_id="rc_test_1") -> ReadyCheckStore:
    """Чистый JSON-backed стор на tmp-файле."""
    monkeypatch.setattr(rc_store_mod, "RC_FILE", tmp_path / "rc.json")
    monkeypatch.setattr(rc_store_mod, "DATABASE_URL", "")
    return ReadyCheckStore()


def _sample_rc(match_id="rc_test_1", n=8, deadline=None) -> ReadyCheck:
    players = [
        ReadyCheckPlayer(user_id=1000 + i, name=f"Player{i}", team="A" if i < 4 else "B")
        for i in range(n)
    ]
    return ReadyCheck(
        match_id=match_id,
        guild_id=1,
        search_channel_id=10,
        tournament_channel_id=20,
        players=players,
        deadline=deadline if deadline is not None else time.time() + 90,
        match_num=42,
    )


class _FakeResponse:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def defer(self, *args, **kwargs):
        pass

    async def edit_message(self, *args, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeChannel:
    def __init__(self, guild=None):
        self.id = 10
        self.guild = guild
        self.sent: list[dict] = []

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=555)


class _FakeGuild:
    def __init__(self):
        self.id = 1

    def get_member(self, user_id):
        return None

    def get_role(self, role_id):
        return None


class _FakeInteraction:
    def __init__(self, user_id: int, guild=None, channel=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild or _FakeGuild()
        self.channel = channel or _FakeChannel(self.guild)
        self.channel_id = self.channel.id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = SimpleNamespace(edit=AsyncMock())


class _FakeBot:
    def __init__(self):
        self.user = SimpleNamespace(id=999)
        self.views: list = []
        self._guild = _FakeGuild()
        self._channels: dict[int, _FakeChannel] = {}

    def get_channel(self, channel_id):
        return self._channels.setdefault(channel_id, _FakeChannel(self._guild))

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    def add_view(self, view, message_id=None):
        self.views.append((view, message_id))


class _FakePlayerStatsStore:
    async def get(self, guild_id, user_id):
        return None


class _FakeTournamentStore:
    def get_by_guild(self, guild_id):
        return []


def _make_cog(monkeypatch, tmp_path):
    """MMCog со стабами хранилищ."""
    import cogs.mm as mm_mod

    store = _rc(tmp_path, monkeypatch)
    monkeypatch.setattr(mm_mod, "ready_check_store", store)

    names = [f"P{i}" for i in range(8)]
    search = MMSearch(
        guild_id=1,
        channel_id=10,
        format=__import__("models.mm_search", fromlist=["MMFormat"]).MMFormat.SQUAD,
    )
    search.players = list(names)
    search.player_user_ids = {n: 1000 + i for i, n in enumerate(names)}
    search.tournament_channel_id = 20
    search.message_id = 77

    class _SearchStore:
        def get(self, guild_id, channel_id, queue_type="normal"):
            return search

        def set(self, s):
            pass

    monkeypatch.setattr(mm_mod, "mm_search_store", _SearchStore())
    monkeypatch.setattr(mm_mod, "store", _FakeTournamentStore())
    monkeypatch.setattr(mm_mod, "player_stats_store", _FakePlayerStatsStore())

    cog = mm_mod.MMCog(_FakeBot())
    monkeypatch.setattr(cog, "_start_search_timer", lambda *a, **k: None)
    monkeypatch.setattr(cog, "_cancel_search_timer", lambda *a, **k: None)
    return cog, store, search


# ---------------------------------------------------------------- model

def test_model_counts_and_lookup():
    rc = _sample_rc()
    assert len(rc.players) == 8
    assert len(rc.team_a) == 4 and len(rc.team_b) == 4
    assert rc.ready_count == 0
    p = rc.player_by_id(1003)
    assert p is not None and p.team == "A"
    assert rc.player_by_id(9999) is None
    p.is_ready = True
    assert rc.ready_count == 1
    assert not rc.is_active or rc.status == ReadyCheckStatus.READY_CHECK


def test_model_roundtrip():
    rc = _sample_rc()
    rc.players[0].is_ready = True
    rc.players[0].ready_at = 123.0
    rc.status = ReadyCheckStatus.TIMEOUT
    rc2 = ReadyCheck.from_dict(rc.to_dict())
    assert rc2.match_id == rc.match_id
    assert rc2.status == ReadyCheckStatus.TIMEOUT
    assert rc2.players[0].is_ready and rc2.players[0].ready_at == 123.0
    assert rc2.ready_count == 1


# ---------------------------------------------------------------- store

@pytest.mark.asyncio
async def test_store_create_get_transition(tmp_path, monkeypatch):
    store = _rc(tmp_path, monkeypatch)
    rc = _sample_rc()
    assert await store.create(rc)
    # дубликат match_id
    assert not await store.create(_sample_rc())
    # активная проверка для того же поиска
    other = _sample_rc(match_id="rc_other")
    other.search_channel_id = 10
    assert not await store.create(other)

    got = await store.get("rc_test_1")
    assert got is not None and got.ready_count == 0

    # атомарный переход
    assert await store.try_transition("rc_test_1", ReadyCheckStatus.READY_CHECK, ReadyCheckStatus.ALL_READY)
    # повторный переход из старого статуса — False
    assert not await store.try_transition("rc_test_1", ReadyCheckStatus.READY_CHECK, ReadyCheckStatus.TIMEOUT)

    actives = await store.list_active()
    assert any(r.match_id == "rc_test_1" for r in actives)  # ALL_READY ещё активен

    rc = await store.get("rc_test_1")
    rc.status = ReadyCheckStatus.LAUNCHED
    await store.save(rc)
    assert all(r.match_id != "rc_test_1" for r in await store.list_active())


@pytest.mark.asyncio
async def test_store_persistence_reload(tmp_path, monkeypatch):
    store = _rc(tmp_path, monkeypatch)
    rc = _sample_rc()
    await store.create(rc)
    rc.players[0].is_ready = True
    await store.save(rc)

    # «перезапуск»: новый стор читает тот же файл
    store2 = ReadyCheckStore()
    got = await store2.get("rc_test_1")
    assert got is not None
    assert got.players[0].is_ready
    assert got.ready_count == 1


# ---------------------------------------------------------------- renderer

def test_renderer_produces_1920x1080():
    from utils.match_found_renderer import (
        MatchFoundData,
        PlayerCard,
        TeamAverages,
        render_match_found_sync,
    )

    data = MatchFoundData(match_num=777)
    for i in range(4):
        data.team_a.append(PlayerCard(
            user_id=100 + i, name=f"ИгрокА{i}", team="A",
            elo=2000 + i, sr=7.5, kd=1.5, winrate=55.0,
        ))
        data.team_b.append(PlayerCard(
            user_id=200 + i, name=f"PlayerB{i}", team="B",
            elo=1900 + i, sr=6.5, kd=1.2, winrate=50.0,
        ))
    data.avg_a = TeamAverages(elo=2100, sr=7.5, kd=1.5, win_chance=55)
    data.avg_b = TeamAverages(elo=1950, sr=6.5, kd=1.2, win_chance=45)

    png = render_match_found_sync(data, {})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png))
    assert img.size == (1920, 1080)


def test_renderer_long_and_cyrillic_names():
    from utils.match_found_renderer import (
        MatchFoundData,
        PlayerCard,
        TeamAverages,
        render_match_found_sync,
    )

    data = MatchFoundData(match_num=1)
    long_name = "ОченьДлинныйНикнеймИгрокаКоторыйНеВлезаетВШаблон"
    for i in range(4):
        data.team_a.append(PlayerCard(
            user_id=i, name=long_name if i == 0 else f"P{i}", team="A",
        ))
    for i in range(4):
        data.team_b.append(PlayerCard(user_id=10 + i, name=f"B{i}", team="B"))
    data.avg_a = TeamAverages()
    data.avg_b = TeamAverages()
    png = render_match_found_sync(data, {})
    assert len(png) > 10000


# ---------------------------------------------------------------- cog flow

@pytest.mark.asyncio
async def test_full_ready_launches_once(tmp_path, monkeypatch):
    """8/8 → запуск ровно один раз, повторные клики не дублируют."""
    cog, store, search = _make_cog(monkeypatch, tmp_path)

    launched: list[str] = []

    async def fake_launch(search_arg, channel):
        launched.append(search_arg.guild_id)

    monkeypatch.setattr(cog, "_create_tournament_from_search", fake_launch)
    monkeypatch.setattr(cog, "_update_ready_message", AsyncMock())

    # создаём RC напрямую
    rc = _sample_rc()
    await store.create(rc)

    async def click(uid):
        inter = _FakeInteraction(uid)
        await cog.handle_ready_click(inter, rc.match_id)
        return inter

    # посторонний не может
    outsider = await click(7777)
    assert "не участвуете" in outsider.followup.sent[0][0][0].lower()

    # 8 готовых параллельно
    await asyncio.gather(*[click(1000 + i) for i in range(8)])

    rc = await store.get(rc.match_id)
    assert rc.status == ReadyCheckStatus.LAUNCHED
    assert len(launched) == 1

    # повторный клик после завершения — не запускает повторно
    await click(1000)
    assert len(launched) == 1


@pytest.mark.asyncio
async def test_ready_idempotent(tmp_path, monkeypatch):
    cog, store, search = _make_cog(monkeypatch, tmp_path)
    monkeypatch.setattr(cog, "_update_ready_message", AsyncMock())

    rc = _sample_rc()
    await store.create(rc)

    inter = _FakeInteraction(1000)
    await cog.handle_ready_click(inter, rc.match_id)
    await cog.handle_ready_click(inter, rc.match_id)  # повторно

    rc = await store.get(rc.match_id)
    assert rc.ready_count == 1  # не удвоилось
    assert "уже подтвердили" in inter.followup.sent[-1][0][0].lower()


@pytest.mark.asyncio
async def test_decline_cancels_and_requeues(tmp_path, monkeypatch):
    cog, store, search = _make_cog(monkeypatch, tmp_path)
    monkeypatch.setattr(cog, "_update_ready_message", AsyncMock())

    rc = _sample_rc()
    await store.create(rc)

    inter = _FakeInteraction(1002)  # team A player
    await cog.handle_decline_click(inter, rc.match_id)

    rc = await store.get(rc.match_id)
    assert rc.status == ReadyCheckStatus.DECLINED
    # остальные 7 вернулись в очередь
    assert len(search.players) == 7
    assert "P2" not in search.players
    assert set(search.players) == {f"P{i}" for i in range(8)} - {"P2"}


@pytest.mark.asyncio
async def test_timeout_requeues_only_ready(tmp_path, monkeypatch):
    cog, store, search = _make_cog(monkeypatch, tmp_path)
    monkeypatch.setattr(cog, "_update_ready_message", AsyncMock())

    rc = _sample_rc(deadline=time.time() - 1)
    await store.create(rc)
    # 5 готовых, 3 нет
    for i in range(5):
        rc.players[i].is_ready = True
    await store.save(rc)

    await cog._expire_ready_check(rc.match_id)

    rc = await store.get(rc.match_id)
    assert rc.status == ReadyCheckStatus.TIMEOUT
    # в очередь вернулись только подтвердившие (5)
    assert len(search.players) == 5
    assert set(search.players) == {"P0", "P1", "P2", "P3", "P4"}


@pytest.mark.asyncio
async def test_start_ready_check_creates_card(tmp_path, monkeypatch):
    """8 игроков → ReadyCheck + PNG + сообщение с кнопками."""
    cog, store, search = _make_cog(monkeypatch, tmp_path)

    channel = _FakeChannel(_FakeGuild())
    inter = _FakeInteraction(1000, guild=channel.guild, channel=channel)

    await cog._start_ready_check(search, inter)

    actives = await store.list_active()
    assert len(actives) == 1
    rc = actives[0]
    assert rc.status == ReadyCheckStatus.READY_CHECK
    assert len(rc.players) == 8
    assert len(rc.team_a) == 4 and len(rc.team_b) == 4
    assert rc.deadline - time.time() <= 90.5

    # Карточка «Найден матч» уходит в фиксированный канал, а не в канал вызова
    from cogs.mm import MATCH_FOUND_CHANNEL_ID

    card_channel = cog.bot.get_channel(MATCH_FOUND_CHANNEL_ID)
    assert len(card_channel.sent) == 1
    assert len(channel.sent) == 0
    kwargs = card_channel.sent[0]["kwargs"]
    assert "file" in kwargs
    assert kwargs["file"].fp.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"

    # persistent view зарегистрирован
    assert len(cog.bot.views) >= 1
