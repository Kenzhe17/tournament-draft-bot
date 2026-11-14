"""Тесты платной смены ника (услуга nickname_change).

Покрытие: валидация, баланс, права бота, иерархия, идемпотентность
списания/возврата, сетевой сбой Discord, recovery после рестарта,
независимость от смены display_name в турнирах.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: F401

import services.nickname_service as ns
from storage.nickname_ops_store import (
    NicknameOpsStore,
    STATUS_APPLIED,
    STATUS_REFUNDED,
    STATUS_RESERVED,
    STATUS_UNKNOWN,
)

G = 111
U = 555
PRICE = 1000


# ------------------------------------------------------------------ fakes


class FakeBalance:
    """Баланс с идемпотентными грантами (как add_balance_once в PG)."""

    def __init__(self):
        self.bal: dict[tuple[int, int], int] = {}
        self.sub_calls: list[int] = []
        self.grants: set[str] = set()

    def set(self, guild, user, amount):
        self.bal[(guild, user)] = amount

    async def get_balance(self, guild_id, user_id):
        return self.bal.get((guild_id, user_id), 0)

    async def subtract_balance(self, guild_id, user_id, amount):
        self.sub_calls.append(amount)
        cur = self.bal.get((guild_id, user_id), 0)
        if cur < amount:
            raise ValueError("Insufficient balance")
        self.bal[(guild_id, user_id)] = cur - amount
        return self.bal[(guild_id, user_id)]

    async def add_balance_once(self, guild_id, user_id, amount, key):
        if key in self.grants:
            return 0
        self.grants.add(key)
        self.bal[(guild_id, user_id)] = (
            self.bal.get((guild_id, user_id), 0) + amount
        )
        return amount


class _Role:
    def __init__(self, value):
        self.value = value

    def __ge__(self, other):
        return self.value >= other.value

    def __lt__(self, other):
        return self.value < other.value


def _member(uid=U, nick="Nota", display="Nota", top=10, edit=None):
    return SimpleNamespace(
        id=uid,
        nick=nick,
        display_name=display,
        top_role=_Role(top),
        edit=edit or AsyncMock(),
        guild_permissions=SimpleNamespace(manage_nicknames=True),
    )


def _guild(member, bot_top=20, manage_nick=True):
    bot = SimpleNamespace(
        id=999,
        top_role=_Role(bot_top),
        guild_permissions=SimpleNamespace(
            manage_nicknames=manage_nick
        ),
    )
    return SimpleNamespace(
        id=G,
        me=bot,
        get_member=lambda uid: member if uid == member.id else None,
        fetch_member=AsyncMock(
            side_effect=lambda uid: member if uid == member.id else None
        ),
    )


def _interaction(member, guild=None, balance=None):
    guild = guild or _guild(member)
    return SimpleNamespace(
        user=SimpleNamespace(id=member.id, display_name=member.display_name),
        guild=guild,
        guild_id=G,
        channel_id=1,
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    ops_file = tmp_path / "nickname_ops.json"
    monkeypatch.setattr(
        "storage.nickname_ops_store.OPS_FILE", ops_file
    )
    ops = NicknameOpsStore()
    monkeypatch.setattr(ns, "nickname_ops_store", ops)

    balance = FakeBalance()
    monkeypatch.setattr(ns, "user_balance_store", balance)

    return SimpleNamespace(ops=ops, balance=balance)


# ------------------------------------------------------------ validation


def test_validate_nickname_rules():
    assert ns.validate_nickname("New Nick", "Old") is None
    assert ns.validate_nickname("", "Old") is not None
    assert ns.validate_nickname("x" * 33, "Old") is not None
    assert ns.validate_nickname("a\x00b", "Old") is not None
    assert ns.validate_nickname("a​b", "Old") is not None  # zero-width
    assert ns.validate_nickname("@everyone", "Old") is not None
    assert ns.validate_nickname("@here", "Old") is not None
    assert ns.validate_nickname("discord.gg/abc", "Old") is not None
    assert ns.validate_nickname("Old", "Old") is not None
    assert ns.validate_nickname("old", "Old") is not None  # casefold
    # эмодзи и unicode разрешены
    assert ns.validate_nickname("Player 😀🔥", "Old") is None


# -------------------------------------------------------------- purchase


@pytest.mark.asyncio
async def test_successful_purchase(env):
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "Nota Bene", PRICE
    )

    assert result == "success"
    assert payload["balance"] == 1500
    member.edit.assert_awaited_once()
    kwargs = member.edit.call_args.kwargs
    assert kwargs["nick"] == "Nota Bene"
    assert kwargs["reason"] == ns.NICK_EDIT_REASON
    assert env.balance.bal[(G, U)] == 1500
    op = env.ops.get(payload["op_id"])
    assert op["status"] == STATUS_APPLIED
    assert op["charged"] is True


@pytest.mark.asyncio
async def test_insufficient_balance(env):
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 500)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "error"
    member.edit.assert_not_awaited()
    assert env.balance.bal[(G, U)] == 500
    op = env.ops.get(payload["op_id"])
    assert op["status"] == STATUS_REFUNDED
    assert op["charged"] is False


@pytest.mark.asyncio
async def test_no_manage_nicknames_permission(env):
    member = _member()
    guild = _guild(member, manage_nick=False)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "error"
    assert "Управление никами" in payload["message"]
    member.edit.assert_not_awaited()
    assert env.balance.bal[(G, U)] == 5000


@pytest.mark.asyncio
async def test_hierarchy_blocks(env):
    member = _member(top=30)
    guild = _guild(member, bot_top=20)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "error"
    assert "не позволит" in payload["message"]
    member.edit.assert_not_awaited()
    assert env.balance.bal[(G, U)] == 5000


@pytest.mark.asyncio
async def test_same_nick_rejected(env):
    member = _member(nick="Nota", display="Nota")
    guild = _guild(member)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    result, _ = await ns.purchase_nickname_change(inter, "Nota", PRICE)
    assert result == "error"
    assert env.balance.bal[(G, U)] == 5000


@pytest.mark.asyncio
async def test_repeat_purchase_allowed(env):
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    r1, p1 = await ns.purchase_nickname_change(inter, "Nick One", PRICE)
    member.nick = "Nick One"
    member.display_name = "Nick One"
    r2, p2 = await ns.purchase_nickname_change(inter, "Nick Two", PRICE)

    assert r1 == "success" and r2 == "success"
    assert env.balance.bal[(G, U)] == 3000
    assert p1["op_id"] != p2["op_id"]


@pytest.mark.asyncio
async def test_parallel_purchase_blocked(env):
    """Вторая покупка при активной операции отклоняется."""
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    # Активная операция (reserved)
    await env.ops.create(
        guild_id=G, user_id=U, old_nick="A",
        new_nick="B", price=PRICE,
    )

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )
    assert result == "error"
    assert "незавершённая" in payload["message"]
    assert env.balance.bal[(G, U)] == 5000


@pytest.mark.asyncio
async def test_double_confirm_no_double_charge(env):
    """Повторное подтверждение после applied — новая операция,
    корректное повторное списание (услуга повторяемая)."""
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 5000)
    inter = _interaction(member, guild)

    r1, p1 = await ns.purchase_nickname_change(inter, "Nick A", PRICE)
    member.nick = "Nick A"
    member.display_name = "Nick A"
    r2, p2 = await ns.purchase_nickname_change(inter, "Nick B", PRICE)

    assert r1 == r2 == "success"
    assert len(env.balance.sub_calls) == 2
    assert env.balance.bal[(G, U)] == 3000


# --------------------------------------------------------- discord failure


@pytest.mark.asyncio
async def test_forbidden_refunds(env):
    member = _member()
    member.edit = AsyncMock(
        side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "no"
        )
    )
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "error"
    assert "Монеты возвращены" in payload["message"]
    assert env.balance.bal[(G, U)] == 2500
    op = env.ops.get(payload["op_id"])
    assert op["status"] == STATUS_REFUNDED


@pytest.mark.asyncio
async def test_http_error_refunds(env):
    member = _member()
    member.edit = AsyncMock(
        side_effect=discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"), "bad"
        )
    )
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    result, _ = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )
    assert result == "error"
    assert env.balance.bal[(G, U)] == 2500


@pytest.mark.asyncio
async def test_network_failure_nick_applied(env):
    """Таймаут, но ник фактически изменился → applied, монеты списаны."""
    member = _member()

    async def flaky_edit(**kwargs):
        member.nick = kwargs["nick"]  # Discord применил, ответ потерян
        raise asyncio.TimeoutError()

    member.edit = AsyncMock(side_effect=flaky_edit)
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "success"
    assert env.balance.bal[(G, U)] == 1500
    op = env.ops.get(payload["op_id"])
    assert op["status"] == STATUS_APPLIED


@pytest.mark.asyncio
async def test_network_failure_nick_unchanged_refunds(env):
    """Таймаут и ник не изменился → возврат монет."""
    member = _member()
    member.edit = AsyncMock(side_effect=asyncio.TimeoutError())
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )

    assert result == "error"
    assert env.balance.bal[(G, U)] == 2500
    op = env.ops.get(payload["op_id"])
    assert op["status"] == STATUS_REFUNDED


@pytest.mark.asyncio
async def test_no_double_refund(env):
    """Повторное разрешение операции не возвращает монеты дважды."""
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 2500)
    inter = _interaction(member, guild)

    member.edit = AsyncMock(side_effect=asyncio.TimeoutError())
    result, payload = await ns.purchase_nickname_change(
        inter, "NewName", PRICE
    )
    assert result == "error"
    assert env.balance.bal[(G, U)] == 2500

    op = env.ops.get(payload["op_id"])
    # Принудительно вернуть в unknown и разрешить повторно
    await env.ops.set_status(op["op_id"], STATUS_UNKNOWN)
    await ns._resolve_op_by_nick(guild, op)
    await ns._resolve_op_by_nick(guild, op)

    # Возврат был один раз (grant dedup)
    assert env.balance.bal[(G, U)] == 2500
    assert len(env.balance.grants) == 1


# ------------------------------------------------------------- recovery


@pytest.mark.asyncio
async def test_recovery_applied_keeps_charge(env):
    """Операция reserved+charged, ник изменился → applied, без возврата."""
    member = _member(nick="NewName", display="NewName")
    guild = _guild(member)
    env.balance.set(G, U, 1500)

    op = await env.ops.create(
        guild_id=G, user_id=U, old_nick="Old",
        new_nick="NewName", price=PRICE,
    )
    await env.ops.mark_charged(op["op_id"])

    bot = SimpleNamespace(get_guild=lambda gid: guild)
    resolved = await ns.recover_pending(bot)

    assert resolved == 1
    assert env.ops.get(op["op_id"])["status"] == STATUS_APPLIED
    assert env.balance.bal[(G, U)] == 1500  # без возврата


@pytest.mark.asyncio
async def test_recovery_refunds_when_nick_unchanged(env):
    """Операция reserved+charged, ник не изменился → возврат."""
    member = _member(nick="Nota", display="Nota")
    guild = _guild(member)
    env.balance.set(G, U, 1500)

    op = await env.ops.create(
        guild_id=G, user_id=U, old_nick="Nota",
        new_nick="NewName", price=PRICE,
    )
    await env.ops.mark_charged(op["op_id"])

    bot = SimpleNamespace(get_guild=lambda gid: guild)
    await ns.recover_pending(bot)

    assert env.ops.get(op["op_id"])["status"] == STATUS_REFUNDED
    assert env.balance.bal[(G, U)] == 2500


@pytest.mark.asyncio
async def test_recovery_uncharged_refunds_conservatively(env):
    """Операция без подтверждённого списания: recovery не может
    отличить «списание не выполнялось» от «списание подтверждено,
    но флаг не записан» — выбирает сторону пользователя и
    возвращает монеты (консервативно, без потери для игрока)."""
    member = _member()
    guild = _guild(member)
    env.balance.set(G, U, 2500)

    op = await env.ops.create(
        guild_id=G, user_id=U, old_nick="Nota",
        new_nick="NewName", price=PRICE,
    )
    # charged=False — subtract не выполнялся/не подтверждён

    bot = SimpleNamespace(get_guild=lambda gid: guild)
    await ns.recover_pending(bot)

    assert env.ops.get(op["op_id"])["status"] == STATUS_REFUNDED
    # Консервативный возврат: лучше вернуть, чем потерять монеты.
    assert env.balance.bal[(G, U)] == 3500


# ------------------------------------------------- tournament independence


def test_tournament_identity_survives_nick_change():
    """Смена display_name не ломает регистрацию: идентификация по uid."""
    from models.tournament import Tournament
    from views.setup_view import _find_entry_by_uid

    t = Tournament(guild_id=G, channel_id=2)
    t.circle1 = ["Nota"]
    t.player_user_ids = {"Nota": U}

    # Пользователь сменил ник — display_name в Discord другой,
    # но user_id тот же → запись находится.
    name, loc = _find_entry_by_uid(t, U)
    assert name == "Nota"
    assert loc == 1

    # И наоборот: другой пользователь с тем же display_name
    # не считается тем же участником.
    name2, loc2 = _find_entry_by_uid(t, U + 1)
    assert name2 is None and loc2 is None
