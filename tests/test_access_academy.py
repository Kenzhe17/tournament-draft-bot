"""Тесты Academy организатора: руководство, кнопки, ЛС."""
from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discord
from services import access_academy as acad
from views import access_panel


def test_guide_mentions_only_real_features():
    text = "\n".join(acad.guide_lines())
    assert "/tournament create" in text
    assert "draft" in text and "balanced-sr" in text
    # никаких несуществующих для ORG форматов как инструкций
    assert "cashcup" not in text.lower() or "не даёт" in text.lower()


def test_guide_view_is_cv2():
    v = acad.build_guide_view()
    assert isinstance(v, discord.ui.LayoutView)
    c = v.children[0]
    assert isinstance(c, discord.ui.Container)


def test_panel_has_academy_buttons():
    view = access_panel.render_panel_view(None, {
        "holders": [], "scores": {}, "queue_count": 0,
        "queue": [], "next_rotation": None, "last": None})
    row = view.children[0].children[-1]
    ids = [getattr(c, "custom_id", None) for c in row.children]
    assert "access:join" in ids and "access:leave" in ids
    assert "access:guide" in ids and "access:orgpanel" in ids


def test_persistent_view_registers_all_ids():
    v = access_panel.AccessPanelView()
    ids = [getattr(i, "custom_id", None) for i in v.children]
    assert {"access:join", "access:leave",
            "access:guide", "access:orgpanel"} <= set(ids)


class _Member:
    def __init__(self, uid):
        self.id = uid
        self.sent = []
    async def send(self, *a, **kw):
        self.sent.append((a, kw))


@pytest.mark.asyncio
async def test_welcome_dm_once_and_closed_ok(monkeypatch):
    store = {}
    async def was(g, u): return store.get((g, u), False)
    async def mark(g, u): store[(g, u)] = True
    monkeypatch.setattr("storage.access_store.was_welcomed", was)
    monkeypatch.setattr("storage.access_store.mark_welcomed", mark)
    m = _Member(42)
    assert await acad.send_welcome_dm(m, guild_id=1) is True
    assert len(m.sent) == 1
    # повтор — не шлём
    assert await acad.send_welcome_dm(m, guild_id=1) is False
    assert len(m.sent) == 1


@pytest.mark.asyncio
async def test_welcome_dm_closed_dm_still_marks(monkeypatch):
    async def was(g, u): return False
    async def mark(g, u): pass
    monkeypatch.setattr("storage.access_store.was_welcomed", was)
    monkeypatch.setattr("storage.access_store.mark_welcomed", mark)
    class Closed(_Member):
        async def send(self, *a, **kw):
            raise discord.Forbidden(NS(status=403, reason="x"), "x")
    m = Closed(7)
    assert await acad.send_welcome_dm(m, guild_id=1) is False
