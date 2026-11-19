"""Opt-in notification roles are separate from participant and staff roles."""
from __future__ import annotations

import asyncio

import discord

from services.shop_role_policy import can_sell_role
from storage.server_layout_store import server_layout_store

FORMATS = {'cs': 'Турниры CS 4×4', 'br': 'Турниры Battle Royale', 'ult': 'Турниры Ultimatum'}
_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


async def toggle(guild, user_id: int, format_key: str) -> bool:
    if format_key not in FORMATS:
        raise ValueError('Неизвестный формат уведомлений.')
    async with _LOCKS.setdefault((guild.id, user_id), asyncio.Lock()):
        rid = server_layout_store.binding(guild.id, f'notify_{format_key}')
        if not rid:
            raise ValueError('Подписки ещё не настроены администрацией.')
        # Fetch current roles/member: stale cache cannot authorize a changed role.
        role = next((r for r in await guild.fetch_roles() if r.id == rid), None)
        if role is None or role.permissions.value != 0 or not await can_sell_role(guild, role):
            raise ValueError('Роль подписки требует проверки администрацией.')
        bot = guild.me
        if bot is None or not bot.guild_permissions.manage_roles or role >= bot.top_role:
            raise ValueError('Бот не может управлять ролью подписки.')
        member = await guild.fetch_member(user_id)
        subscribed = any(r.id == rid for r in member.roles)
        if subscribed:
            await member.remove_roles(role, reason='Самостоятельное отключение уведомлений')
        else:
            await member.add_roles(role, reason='Самостоятельная подписка на уведомления')
        return not subscribed


async def provision(guild) -> dict:
    from services.server_layout import lock
    async with lock(guild.id):
        state = server_layout_store.read(guild.id)
        bindings = state.setdefault('bindings', {})
        if state.get('role_pending'):
            raise ValueError('Предыдущая выдача/создание роли не подтверждено. Сначала сверить роли сервера.')
        roles = await guild.fetch_roles()
        for key, label in FORMATS.items():
            rid = bindings.get(f'notify_{key}')
            if rid:
                role = next((r for r in roles if r.id == rid), None)
                if role is None or role.permissions.value != 0 or not await can_sell_role(guild, role):
                    raise ValueError('Существующая роль подписки небезопасна или удалена.')
                continue
            name = f'Уведомления · {label}'
            if any(r.name == name for r in roles):
                raise ValueError(f'Роль {name} уже существует, но не привязана; нужна ручная сверка.')
            state['role_pending'] = key
            server_layout_store.write(guild.id, state)
            role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                           mentionable=False, hoist=False,
                                           reason='r!z3 добровольные уведомления')
            bindings[f'notify_{key}'] = role.id
            state.pop('role_pending', None)
            server_layout_store.write(guild.id, state)
        return bindings


def role_id(guild_id: int, format_key: str) -> int:
    return server_layout_store.binding(guild_id, f'notify_{format_key}')
