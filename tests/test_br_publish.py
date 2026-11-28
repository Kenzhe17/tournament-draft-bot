"""Публикация итогов BR: снимок, финальные кнопки, сбой Discord, рестарт.

Проверяет фактический payload message.edit (финальный embed + кнопки
«По картам»/«Мой сквад» без «Комната»/«Управление»), идемпотентность
и восстановление отложенной публикации после «рестарта».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import discord

from models.br_tournament import (BRFormat, BRTournament, BRTournamentFormation,
                                  BRTournamentPhase)
from storage.br_store import br_store
from views import br_view
from views.br_view import (BRCardView, PublishResultsButton,
                           resume_pending_publish)
from utils.design import compose


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeMessage:
    def __init__(self, message_id=555):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, fail=False):
        self.message = FakeMessage()
        self.fail = fail

    async def fetch_message(self, message_id):
        if self.fail:
            raise discord.HTTPException(None, "boom")
        return self.message


class FakeGuild:
    def __init__(self, channels):
        self.id = 1
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return None

    def get_member(self, user_id):
        return None


class FakeClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild


def _interaction(guild, user_id=999):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=guild,
        channel=guild.get_channel(2),
        response=FakeResponse(),
    )


def _walk(view):
    for child in getattr(view, "children", []):
        yield child
        yield from _walk(child)


def _buttons(view):
    return [c for c in _walk(view) if isinstance(c, discord.ui.Button)]


def _collect_tournament(fmt="squad", n_slots=5, maps=2, channel_id=2):
    t = BRTournament(
        id="tid-1", guild_id=1, channel_id=channel_id,
        formation=BRTournamentFormation.RANDOM,
        format=BRFormat(fmt), map_count=maps,
        message_id=555, is_test=True)
    for i in range(n_slots * t.slot_size):
        ok, err = t.register_player(f"P{i}", 1000 + i)
        assert ok, err
    t.draw()
    if t.phase == BRTournamentPhase.DRAFT:
        t.begin_games()
    t.finish_games()
    for si in range(n_slots):
        for m in range(maps):
            ok, err = t.set_result_entry(m, si, place=si + 1, kills=m)
            assert ok, err
    assert t.results_complete()
    return t


@pytest.fixture
def patched_store(monkeypatch):
    """br_store → один турнир в памяти, set() только записывает."""
    state = {"t": None, "sets": []}

    def _get(gid, cid):
        t = state["t"]
        return t if t and t.guild_id == gid and t.channel_id == cid else None

    monkeypatch.setattr(br_store, "get", _get)
    monkeypatch.setattr(br_store, "get_any", _get)
    monkeypatch.setattr(br_store, "history", lambda: [])
    monkeypatch.setattr(br_store, "set", lambda t: state["sets"].append(t))
    monkeypatch.setattr(br_view, "is_org", lambda uid: True)
    return state


# ---------------------------------------------------------------------------
# Финальные кнопки в payload message.edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_edit_payload_has_final_buttons(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    assert len(channel.message.edits) == 1
    view = channel.message.edits[0]["view"]
    buttons = _buttons(view)
    labels = [b.label for b in buttons]
    cids = [b.custom_id for b in buttons]
    # финальные кнопки: пагинация по картам + «Мой сквад»
    assert "Мой сквад" in labels
    assert "◀" in labels and "▶" in labels
    assert any("br_rpage" in c for c in cids)
    # «Комната» и «Управление» в публичном финале отсутствуют
    assert "Комната" not in labels
    assert "Управление" not in labels
    assert not any("br_room" in c or "br_org_menu" in c for c in cids)
    # состояние закоммичено: RESULTS + снимок в архивном объекте
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_published and t.results_snapshot is not None
    assert patched_store["sets"]


@pytest.mark.asyncio
async def test_publish_rejects_stale_tournament_id(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, "other-tid")
    await btn.callback(inter)
    assert channel.message.edits == []
    assert t.phase == BRTournamentPhase.COLLECT
    assert any("устарела" in str(m[0][0]) for m in inter.response.messages)


# ---------------------------------------------------------------------------
# Сбой Discord → повтор с тем же снимком
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_discord_failure_keeps_pending_and_retry_same_snapshot(
        patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel(fail=True)
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    # Discord упал: карточка не тронута, состояние COLLECT+pending
    assert channel.message.edits == []
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is not None
    assert any("НЕ опубликованы" in str(m[0][0]) for m in inter.response.messages)
    snap1 = [dict(r) for r in t.publish_pending]

    # повтор: Discord починился — тот же снимок, без второго сообщения
    channel.fail = False
    await btn.callback(inter)
    assert len(channel.message.edits) == 1  # edit того же message_id
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_snapshot == snap1


@pytest.mark.asyncio
async def test_double_publish_is_idempotent(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)
    btn = PublishResultsButton(1, 2, t.id)

    await btn.callback(inter)
    await btn.callback(inter)  # повторный клик
    # вторая публикация не делает второй edit и не падает
    assert len(channel.message.edits) == 1
    assert t.phase == BRTournamentPhase.RESULTS


# ---------------------------------------------------------------------------
# Рестарт посередине публикации
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_pending_publish_after_restart(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    assert t.freeze_results()[0]
    br_store.set(t)  # «сбой» после заморозки, до commit
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is not None

    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    client = FakeClient(guild)

    ok = await resume_pending_publish(t, client)
    assert ok
    assert t.phase == BRTournamentPhase.RESULTS
    assert len(channel.message.edits) == 1
    # payload — финальные кнопки
    buttons = _buttons(channel.message.edits[0]["view"])
    labels = [b.label for b in buttons]
    assert "Мой сквад" in labels and "Комната" not in labels
    assert "Управление" not in labels


@pytest.mark.asyncio
async def test_resume_without_pending_is_noop(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    ok = await resume_pending_publish(t, FakeClient(FakeGuild({})))
    assert not ok
    assert t.phase == BRTournamentPhase.COLLECT
