"""Тесты utils.message_refresher — debounce/coalescing message.edit.

Запуск: pytest tests/test_message_refresher.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.message_refresher import (
    EditRequest,
    MessageRefresher,
    payload_fingerprint,
)


class FakeMessage:
    """Минимальный дубль discord.Message для edit()."""

    def __init__(self, fail_with: Exception | None = None):
        self.edits: list[dict] = []
        self.fail_with = fail_with
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return self

    async def delete(self):
        self.deleted = True


def _builder_for(msg: FakeMessage, state: dict):
    """Builder, который читает актуальное состояние на момент вызова."""

    async def build():
        return EditRequest(
            message=msg,
            kwargs={"content": state["text"]},
        )

    return build


@pytest.mark.asyncio
async def test_coalesces_burst_into_single_edit():
    ref = MessageRefresher(default_delay=0.05)
    msg = FakeMessage()
    state = {"text": "v0"}
    for i in range(1, 6):
        state["text"] = f"v{i}"
        ref.schedule("k1", _builder_for(msg, state))
    await asyncio.sleep(0.3)
    assert len(msg.edits) == 1
    # Опубликовано последнее состояние, а не промежуточное
    assert msg.edits[0]["content"] == "v5"
    await ref.shutdown()


@pytest.mark.asyncio
async def test_skips_unchanged_content():
    ref = MessageRefresher(default_delay=0.05)
    msg = FakeMessage()
    state = {"text": "same"}
    ref.schedule("k2", _builder_for(msg, state))
    await asyncio.sleep(0.2)
    assert len(msg.edits) == 1
    # Повторное событие без изменения состояния → без PATCH
    ref.schedule("k2", _builder_for(msg, state))
    await asyncio.sleep(0.2)
    assert len(msg.edits) == 1
    await ref.shutdown()


@pytest.mark.asyncio
async def test_edit_during_active_edit_reruns():
    ref = MessageRefresher(default_delay=0.05)
    msg = FakeMessage()
    state = {"text": "first"}
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowMsg(FakeMessage):
        async def edit(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().edit(**kwargs)

    slow = SlowMsg()
    state2 = {"text": "first"}
    ref.schedule("k3", _builder_for(slow, state2))
    await asyncio.wait_for(entered.wait(), 1)
    # Пока edit «в полёте», приходит новая правка
    state2["text"] = "second"
    ref.schedule("k3", _builder_for(slow, state2))
    release.set()
    await asyncio.sleep(0.3)
    assert [e["content"] for e in slow.edits] == ["first", "second"]
    await ref.shutdown()


@pytest.mark.asyncio
async def test_not_found_calls_on_missing_once():
    ref = MessageRefresher(default_delay=0.05)
    resp = SimpleNamespace(
        status=404, reason="Not Found", text="Unknown Message"
    )
    gone = discord.NotFound(resp, "Unknown Message")
    msg = FakeMessage(fail_with=gone)
    calls = []

    async def on_missing():
        calls.append(1)

    async def build():
        return EditRequest(
            message=msg, kwargs={"content": "x"}, on_missing=on_missing
        )

    ref.schedule("k4", build)
    await asyncio.sleep(0.2)
    ref.schedule("k4", build)  # ключ «умер» — повторного builder нет
    await asyncio.sleep(0.2)
    assert calls == [1]
    # builder вызывался только для первого schedule
    await ref.shutdown()


@pytest.mark.asyncio
async def test_none_builder_skips_edit():
    ref = MessageRefresher(default_delay=0.05)
    msg = FakeMessage()

    async def build():
        return None

    ref.schedule("k5", build)
    await asyncio.sleep(0.2)
    assert msg.edits == []
    await ref.shutdown()


@pytest.mark.asyncio
async def test_payload_fingerprint_stable():
    class FakeView:
        def __init__(self, label):
            self.children = [
                SimpleNamespace(
                    label=label, custom_id="c1", disabled=False,
                    style="primary", content=None, emoji=None,
                    placeholder=None, options=None, children=None,
                )
            ]

    e1 = discord.Embed(title="T", description="D")
    kw1 = {"embed": e1, "view": FakeView("A")}
    kw2 = {"view": FakeView("A"), "embed": discord.Embed(title="T", description="D")}
    assert payload_fingerprint(kw1) == payload_fingerprint(kw2)
    kw3 = {"view": FakeView("B"), "embed": discord.Embed(title="T", description="D")}
    assert payload_fingerprint(kw1) != payload_fingerprint(kw3)


@pytest.mark.asyncio
async def test_queue_bounded():
    ref = MessageRefresher(default_delay=60.0, max_keys=3)

    async def build():
        return None

    for i in range(10):
        ref.schedule(f"key{i}", build)
    assert len(ref._wanted) <= 3
    await ref.shutdown()
