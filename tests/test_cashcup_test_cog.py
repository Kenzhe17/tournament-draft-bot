"""Регрессионные тесты cog cogs/cashcup_test.py.

Покрывают дефекты, найденные на staging:
- _reply() не должен передавать view=None в followup.send
  (TypeError: expected view parameter to be of type View or LayoutView).
- on_ready обязан быть @commands.Cog.listener(), иначе persistent
  views не восстанавливаются после рестарта.
- _find_match — синхронный метод (async без await возвращал coroutine).
- GF-кнопки проверяют ADMIN/guild/session в момент клика.
"""

from __future__ import annotations

import inspect

import pytest

import discord
from discord.ext import commands

from cogs.cashcup_test import CashCupTestCog, TestPanelView, _cid


class _FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


class _FakeResponse:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, *a, **kw):
        self.kwargs = kw

    async def defer(self, **kw):
        pass

    async def send_modal(self, modal):
        pass


class _FakeInteraction:
    def __init__(self):
        self.followup = _FakeFollowup()
        self.response = _FakeResponse()
        self.guild = None
        self.user = None
        self.channel = None
        self.data = {}
        self.client = _FakeBot()


class _FakeBot:
    def __init__(self, cog=None):
        self.views = []
        self._cog = cog

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self._cog


def _cog() -> CashCupTestCog:
    cog = CashCupTestCog(_FakeBot())
    cog.bot._cog = cog
    return cog


@pytest.mark.asyncio
async def test_reply_without_view_has_no_view_kwarg():
    """P0: view=None в followup.send → TypeError в Discord."""
    cog = _cog()
    inter = _FakeInteraction()
    await cog._reply(inter, "hello")
    assert inter.followup.calls, "followup.send не вызван"
    _, kwargs = inter.followup.calls[0]
    assert "view" not in kwargs, "view=None не должен передаваться"
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_reply_with_view_passes_view():
    cog = _cog()
    inter = _FakeInteraction()
    view = discord.ui.View()
    await cog._reply(inter, "hi", view=view)
    _, kwargs = inter.followup.calls[0]
    assert kwargs.get("view") is view


def test_on_ready_is_cog_listener():
    """P1: без @commands.Cog.listener() on_ready не вызывается —
    persistent views не восстанавливаются после рестарта."""
    func = CashCupTestCog.on_ready
    assert getattr(func, "__cog_listener__", False) is True, \
        "on_ready не помечен как cog listener"


def test_find_match_is_sync():
    """P0: async _find_match без await → coroutine вместо dict."""
    assert not inspect.iscoroutinefunction(CashCupTestCog._find_match)


def test_panel_view_custom_ids_contain_session():
    """Кнопки привязаны к session id — старая кнопка не трогает
    новую сессию."""
    view = TestPanelView("abc123def456")
    ids = [getattr(i, "custom_id", "") for i in view.children]
    assert ids and all(i.startswith("cctest:abc123def456:") for i in ids)
    assert len(view.children) <= 25
    assert max(i.row for i in view.children) <= 4


def test_cid_length_limit():
    assert len(_cid("x" * 12, "pick:result")) <= 100


@pytest.mark.asyncio
async def test_gf_game_denies_non_admin():
    """P1: GF-действие повторно проверяет ADMIN на входе —
    отзыв прав после открытия кнопок блокирует запись."""
    cog = _cog()
    inter = _FakeInteraction()
    inter.guild = discord.Object(id=1)
    inter.user = discord.Object(id=2)  # не Member → _is_admin False
    await cog._gf_game(inter, "sid", "GF", "a")
    # Должен быть отказ, а не запись/исключение AttributeError.
    assert inter.followup.calls
    text = str(inter.followup.calls[0][0][0])
    assert "только ADMIN" in text or "ADMIN" in text


@pytest.mark.asyncio
async def test_gf_cb_denies_without_guild():
    cog = _cog()
    inter = _FakeInteraction()
    inter.guild = None
    inter.user = discord.Object(id=2)
    inter.data = {"custom_id": "cctest:sid:side:gf_game:GF:a"}
    inter.client = cog.bot
    cb = cog._gf_cb("sid", "GF")
    await cb(inter)
    assert inter.response.kwargs is not None  # send_message вызван
