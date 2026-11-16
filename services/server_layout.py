"""Reviewable Discord layout migration: preserve IDs, ACLs and messages; never delete.

No changes happen on startup. An administrator must request a plan and invoke apply.
Every mutation is journaled before the Discord call. Ambiguous creates stop recovery.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid

import discord

import config
from storage.server_layout_store import server_layout_store as state_store

CATEGORIES = ('ИНФОРМАЦИЯ', 'ТУРНИРЫ', 'СООБЩЕСТВО', 'ГОЛОСОВЫЕ', 'КОМАНДА', 'лишние-каналы')
# Binding key, label, target category, aliases. Existing config wins over name matches.
CHANNELS = (
    ('start', '🏠・старт', 'ИНФОРМАЦИЯ', ('старт',)),
    ('info', '📖・информация', 'ИНФОРМАЦИЯ', ('информация', 'инфо', 'info')),
    ('announcements', '📢・объявления', 'ИНФОРМАЦИЯ', ('объявления', 'новости')),
    ('roles', '🎭・роли', 'ИНФОРМАЦИЯ', ('роли',)),
    ('welcome', '👋・приветствия', 'ИНФОРМАЦИЯ', ('приветствия', 'welcome')),
    ('tournament_1', '🏆・турнир-1', 'ТУРНИРЫ', ('турнир-1',)),
    ('tournament_2', '🏆・турнир-2', 'ТУРНИРЫ', ('турнир-2',)),
    ('tournament_3', '🏆・турнир-3', 'ТУРНИРЫ', ('турнир-3',)),
    ('br', '🪂・battle-royale', 'ТУРНИРЫ', ('battle-royale',)),
    ('journal', '📜・журнал', 'ТУРНИРЫ', ('журнал', 'логи')),
    ('rooms', '🔑・данные-комнаты', 'ТУРНИРЫ', ('данные-комнаты',)),
    ('chat', '💬・общение', 'СООБЩЕСТВО', ('чат', 'общение')),
    ('commands', '🤖・команды-бота', 'СООБЩЕСТВО', ('команды-бота',)),
    ('games', '🎮・мини-игры', 'СООБЩЕСТВО', ('мини-игры',)),
    ('moments', '📸・моменты', 'СООБЩЕСТВО', ('моменты', 'хайлайты')),
)
_LOCKS: dict[int, asyncio.Lock] = {}


def lock(guild_id: int):
    return _LOCKS.setdefault(guild_id, asyncio.Lock())


def normalized(name: str) -> str:
    # Strip decorative prefix, retain the meaningful Cyrillic/Latin channel name.
    parts = re.findall(r'[a-zа-яё0-9][a-zа-яё0-9_-]*', name.lower())
    return '-'.join(parts)


def channel_snapshot(channel) -> dict:
    overwrites = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites.append({'id': target.id, 'type': 'role' if isinstance(target, discord.Role) else 'member',
                           'allow': allow.value, 'deny': deny.value})
    return {'id': channel.id, 'name': channel.name, 'type': str(channel.type),
            'category_id': channel.category_id, 'position': channel.position,
            'overwrites': sorted(overwrites, key=lambda r: (r['type'], r['id']))}


def fingerprint(snapshot: list[dict]) -> str:
    return hashlib.sha256(json.dumps(sorted(snapshot, key=lambda x: x['id']),
                                    sort_keys=True).encode()).hexdigest()


async def busy_channels(guild_id: int) -> set[int]:
    from models.tournament import TournamentPhase
    from storage.br_store import br_store
    from storage.json_store import store
    ids = {t.channel_id for t in store.get_by_guild(guild_id)
           if t.phase != TournamentPhase.COMPLETE}
    ids.update(t.channel_id for t in br_store.get_by_guild(guild_id)
               if str(getattr(t.phase, 'value', t.phase)) != 'complete')
    if config.DATABASE_URL:
        from services.match_channel_pool import list_slots
        from ultimatum.store import ultimatum_store
        # Any read failure aborts planning/apply; unknown live state isn't idle.
        ids.update(t.channel_id for t in await ultimatum_store.list_active_all() if t.guild_id == guild_id)
        ids.update(int(s['channel_id']) for s in await list_slots()
                   if int(s.get('guild_id') or 0) == guild_id and s['state'] != 'free')
    return ids


def configured_bindings(guild_id: int) -> dict[str, int]:
    from services.discord_cfg import channel_id, channel_ids
    result = dict(state_store.read(guild_id).get('bindings', {}))
    candidates = {
        'info': config.INFO_CHANNEL_ID, 'welcome': config.WELCOME_CHANNEL_ID,
        'rooms': config.ROOM_DATA_CHANNEL_ID, 'chat': config.ROOM_OPEN_NOTIFY_CHANNEL_ID,
        'journal': config.TOURNAMENT_AUDIT_CHANNEL_ID,
    }
    candidates['welcome'] = channel_id(guild_id, 'discord.welcome_channel_id', candidates['welcome'])
    candidates['journal'] = channel_id(guild_id, 'discord.tournament_audit_channel_id', candidates['journal'])
    tournament_ids = channel_ids(guild_id, 'discord.tournament_channels', config.TOURNAMENT_CHANNELS)
    for i, cid in enumerate(tournament_ids[:3], 1):
        candidates[f'tournament_{i}'] = cid
    # Persisted bindings take precedence after an explicit layout migration.
    for key, cid in candidates.items():
        if cid and not result.get(key):
            result[key] = int(cid)
    return result


def plan_layout(guild_id: int, snapshot: list[dict], bindings: dict[str, int],
                busy: set[int], archive: set[int]) -> dict:
    by_id = {c['id']: c for c in snapshot}
    categories = {name: [c for c in snapshot if c['type'] == 'category' and c['name'] == name]
                  for name in CATEGORIES}
    if any(len(items) > 1 for items in categories.values()):
        raise ValueError('Найдены категории с одинаковыми целевыми именами. Сначала устраните неоднозначность.')
    ops, used, resolved = [], set(), {}
    for name, items in categories.items():
        if not items:
            ops.append({'action': 'create_category', 'name': name})
    for key, name, category, aliases in CHANNELS:
        cid = int(bindings.get(key) or 0)
        if cid:
            item = by_id.get(cid)
            if item is None:
                raise ValueError(f'{key}: настроенный канал {cid} отсутствует. Исправьте привязку.')
        else:
            matches = [c for c in snapshot if c['type'] in ('text', 'news', 'forum')
                       and normalized(c['name']) in aliases]
            if len(matches) > 1:
                raise ValueError(f'{key}: несколько подходящих каналов. Укажите ID в привязках.')
            item = matches[0] if matches else None
        if item:
            if item['type'] not in ('text', 'news', 'forum'):
                raise ValueError(f'{key}: неподходящий тип канала.')
            if item['id'] in used:
                raise ValueError(f'{key}: канал назначен двум функциям.')
            used.add(item['id']); resolved[key] = item['id']
            if item['id'] in busy:
                continue
            target_id = categories[category][0]['id'] if categories[category] else None
            if item['name'] != name or item['category_id'] != target_id:
                ops.append({'action': 'edit_channel', 'id': item['id'], 'name': name,
                            'category': category, 'binding': key})
        else:
            # Missing tournament slots and integration channels must not be invented.
            if key in ('tournament_1', 'tournament_2', 'tournament_3', 'br', 'journal', 'rooms'):
                continue
            ops.append({'action': 'create_channel', 'name': name, 'category': category, 'binding': key})
    configured_channels = {int(value) for name, value in vars(config).items()
                           if name.endswith("_CHANNEL_ID") and isinstance(value, int) and value > 0}
    configured_channels.update(int(v) for v in config.TOURNAMENT_CHANNELS)
    from services.settings_registry import all_defs
    from services.settings_service import settings_service
    for definition in all_defs():
        if definition.key.startswith('discord.') and definition.key.endswith('_channel_id'):
            value = settings_service.peek(guild_id, definition.key)
            if isinstance(value, int) and value > 0:
                configured_channels.add(value)
    # Explicit selection only. No usage/age guesses or automatic deletion.
    for cid in sorted(archive):
        item = by_id.get(cid)
        if not item or item['type'] == 'category':
            raise ValueError(f'Канал для архива {cid} не найден или является категорией.')
        if cid in used or cid in busy or cid in configured_channels:
            raise ValueError(f'Канал {cid} используется действующей функцией/турниром и не может быть архивирован.')
        ops.append({'action': 'archive', 'id': cid, 'category': 'лишние-каналы'})
    for i, name in enumerate(CATEGORIES):
        ops.append({'action': 'order_category', 'name': name, 'position': i})
    return {'id': uuid.uuid4().hex[:12], 'guild_id': guild_id, 'created_at': int(time.time()),
            'status': 'planned', 'fingerprint': fingerprint(snapshot), 'before': snapshot,
            'bindings_before': bindings, 'bindings': resolved, 'operations': ops,
            'busy_skipped': sorted(busy), 'archive_selected': sorted(archive),
            'unassigned': [c for c in snapshot if c['id'] not in used and c['type'] != 'category']}


async def prepare(guild, archive: set[int]) -> dict:
    async with lock(guild.id):
        state = state_store.read(guild.id)
        old = state.get('plan', {})
        if old.get('status') in ('applying', 'uncertain', 'rolling_back'):
            raise ValueError('Сначала завершите или откатите предыдущую миграцию.')
        if old.get('id'):
            state_store.archive_plan(guild.id, old)
        snapshot = [channel_snapshot(c) for c in await guild.fetch_channels()]
        plan = plan_layout(guild.id, snapshot, configured_bindings(guild.id),
                           await busy_channels(guild.id), archive)
        plan['bindings_before'] = dict(state.get('bindings', {}))
        state_store.archive_plan(guild.id, plan)
        state['plan'] = plan
        state_store.write(guild.id, state)
        return plan


def _save(state: dict):
    state_store.write(state['guild_id'], state)


async def apply(guild, plan_id: str) -> dict:
    async with lock(guild.id):
        state = state_store.read(guild.id); plan = state.get('plan', {})
        if plan.get('id') != plan_id:
            raise ValueError('ID плана не совпадает.')
        if plan.get('status') == 'applied':
            return plan
        if plan.get('status') not in ('planned', 'applying'):
            raise ValueError('План требует проверки/отката. Не повторяйте неопределённую операцию.')
        channels = {c.id: c for c in await guild.fetch_channels()}
        if plan['status'] == 'planned' and fingerprint([channel_snapshot(c) for c in channels.values()]) != plan['fingerprint']:
            raise ValueError('Структура или права изменились после планирования. Сформируйте новый план.')
        busy = await busy_channels(guild.id)
        if any(o.get('id') in busy for o in plan['operations'] if o['action'] in ('edit_channel', 'archive')):
            raise ValueError('Один из каналов теперь занят турниром. Применение остановлено.')
        if any(o.get('state') == 'pending' for o in plan['operations']):
            plan['status'] = 'uncertain'; _save(state)
            raise ValueError('Есть операция с неопределённым исходом. Сверьте Discord и сохранённый план.')
        plan['status'] = 'applying'; _save(state)
        categories = {c.name: c for c in channels.values() if str(c.type) == 'category' and c.name in CATEGORIES}
        for op in plan['operations']:
            if op.get('state') == 'done':
                continue
            op['state'] = 'pending'; _save(state)
            try:
                action = op['action']
                if action == 'create_category':
                    overwrites = None
                    if op['name'] in ('КОМАНДА', 'лишние-каналы'):
                        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
                    created = await guild.create_category(op['name'], overwrites=overwrites or {},
                                                          reason=f'r!z3 layout {plan_id}')
                    op['created_id'] = created.id; categories[created.name] = created
                elif action == 'create_channel':
                    readonly = op['category'] == 'ИНФОРМАЦИЯ'
                    overwrites = {guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, send_messages=not readonly),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                              read_message_history=True, embed_links=True,
                                                              attach_files=True)}
                    created = await guild.create_text_channel(op['name'], category=categories[op['category']],
                                                             overwrites=overwrites, reason=f'r!z3 layout {plan_id}')
                    op['created_id'] = created.id; plan['bindings'][op['binding']] = created.id
                elif action in ('edit_channel', 'archive'):
                    channel = channels[op['id']]
                    kwargs = {'category': categories[op['category']], 'sync_permissions': False,
                              'reason': f'r!z3 layout {plan_id}'}
                    if action == 'edit_channel':
                        kwargs['name'] = op['name']
                    await channel.edit(**kwargs)
                elif action == 'order_category':
                    # All other categories stay intact; archive must be the last category.
                    position = len([c for c in await guild.fetch_channels() if str(c.type) == 'category']) - 1 \
                        if op['name'] == 'лишние-каналы' else op['position']
                    await categories[op['name']].edit(position=position, reason=f'r!z3 layout {plan_id}')
                op['state'] = 'done'; _save(state)
            except Exception:
                # Even a timeout may mean Discord accepted the mutation. Never retry blindly.
                plan['status'] = 'uncertain'; _save(state)
                raise
        state['bindings'].update(plan['bindings'])
        plan['status'] = 'applied'; _save(state)
        return plan


async def rollback(guild, plan_id: str) -> dict:
    async with lock(guild.id):
        state = state_store.read(guild.id); plan = state.get('plan', {})
        if plan.get('id') != plan_id:
            raise ValueError('ID плана не совпадает.')
        if plan.get('status') == 'rolled_back':
            return plan
        if any(o.get('state') == 'pending' for o in plan.get('operations', [])):
            raise ValueError('Неопределённая операция требует ручной сверки до отката.')
        channels = {c.id: c for c in await guild.fetch_channels()}
        busy = await busy_channels(guild.id)
        touched = {o['id'] for o in plan['operations'] if o.get('state') == 'done' and 'id' in o}
        if touched & busy:
            raise ValueError('Каналы заняты действующим турниром. Отложите откат.')
        plan['status'] = 'rolling_back'; _save(state)
        before = {c['id']: c for c in plan['before']}
        for cid in touched:
            channel, old = channels.get(cid), before[cid]
            if channel is None:
                raise ValueError(f'Канал {cid} отсутствует; автоматически не воссоздаётся.')
            # Never undo independent ACL changes; migration itself preserves those ACLs.
            if channel_snapshot(channel)['overwrites'] != old['overwrites']:
                raise ValueError(f'Права канала {cid} изменились. Нужна ручная проверка.')
            category = channels.get(old['category_id']) if old['category_id'] else None
            if old['category_id'] and category is None:
                raise ValueError('Исходная категория отсутствует.')
            await channel.edit(name=old['name'], category=category, position=old['position'],
                               sync_permissions=False, reason=f'r!z3 rollback {plan_id}')
        for old in sorted((c for c in plan['before'] if c['type'] == 'category'), key=lambda c: c['position']):
            category = channels.get(old['id'])
            if category:
                await category.edit(position=old['position'], reason=f'r!z3 rollback {plan_id}')
        # New channels/categories are kept. Never delete their newly accumulated messages.
        state['bindings'] = dict(plan['bindings_before'])
        plan['status'] = 'rolled_back'; _save(state)
        return plan
