from __future__ import annotations

from unittest.mock import AsyncMock

import discord
import pytest

from services import server_layout as sl
from storage.server_layout_store import ServerLayoutStore


class Target:
    def __init__(self, uid):
        self.id = uid


class Channel:
    def __init__(self, guild, cid, name, kind='text', category=None, overwrites=None, position=0):
        self.guild, self.id, self.name, self.type = guild, cid, name, kind
        self.category_id = category.id if category else None
        self.overwrites = overwrites or {}
        self.position = position
        self.edits = []
        self.fail = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.fail:
            raise RuntimeError('Unknown API outcome')
        for key in ('name', 'position'):
            if key in kwargs:
                setattr(self, key, kwargs[key])
        if 'category' in kwargs:
            self.category_id = kwargs['category'].id if kwargs['category'] else None
        return self


class Guild:
    id = 123

    def __init__(self):
        self.channels = []
        self.default_role, self.me = Target(123), Target(999)
        self.roles = []
        self.next_id = 1000
        self.created = []
        self.fail_create = False

    async def fetch_channels(self):
        return self.channels

    async def create_category(self, name, **kwargs):
        if self.fail_create:
            raise RuntimeError('Timeout after send')
        return self.add(name, 'category', **kwargs)

    async def create_text_channel(self, name, **kwargs):
        return self.add(name, **kwargs)

    def add(self, name, kind='text', category=None, overwrites=None, **kwargs):
        self.next_id += 1
        channel = Channel(self, self.next_id, name, kind, category, overwrites, position=len(self.channels))
        self.channels.append(channel)
        self.created.append(channel)
        return channel


@pytest.fixture
def layout(tmp_path, monkeypatch):
    store = ServerLayoutStore(tmp_path)
    monkeypatch.setattr(sl, 'state_store', store)
    monkeypatch.setattr(sl, 'configured_bindings', lambda _: {})
    monkeypatch.setattr(sl, 'busy_channels', AsyncMock(return_value=set()))
    return Guild(), store


def snap(cid, name, kind='text', category_id=None):
    return {'id':cid,'name':name,'type':kind,'category_id':category_id,'position':0,'overwrites':[]}


def test_plan_reuses_channels_and_never_guesses_archive():
    plan = sl.plan_layout(123, [snap(1001,'┌💬・чат'),snap(1002,'тест')], {}, set(), set())
    assert plan['bindings']['chat'] == 1001
    assert plan['archive_selected'] == []
    assert not any(o.get('id') == 1002 for o in plan['operations'])
    assert not any(o['action'] == 'delete' for o in plan['operations'])
    assert any(o.get('name') == '🤖・команды-бота' for o in plan['operations'])
    assert any(o.get('name') == '🎮・мини-игры' for o in plan['operations'])


def test_plan_rejects_ambiguous_channels_and_duplicate_categories():
    with pytest.raises(ValueError):
        sl.plan_layout(123,[snap(1,'чат'),snap(2,'общение')],{},set(),set())
    with pytest.raises(ValueError):
        sl.plan_layout(123,[snap(1,'ИНФОРМАЦИЯ','category'),snap(2,'ИНФОРМАЦИЯ','category')],{},set(),set())


@pytest.mark.parametrize('busy,bindings', [({1001},{}),(set(),{'chat':1001})])
def test_plan_cannot_archive_active_or_bound_channel(busy, bindings):
    with pytest.raises(ValueError):
        sl.plan_layout(123,[snap(1001,'other')],bindings,busy,{1001})


def test_configured_missing_channel_does_not_create_duplicate():
    with pytest.raises(ValueError):
        sl.plan_layout(123,[],{'chat':1001},set(),set())


@pytest.mark.asyncio
async def test_apply_and_rollback_preserve_ids_acl_and_new_channels(layout):
    guild, store = layout
    old_category = guild.add('Старое', 'category')
    role = Target(42)
    channel = guild.add('чат', category=old_category,
                        overwrites={role: discord.PermissionOverwrite(view_channel=False)})
    extra = guild.add('старый-тест', category=old_category,
                      overwrites={role: discord.PermissionOverwrite(send_messages=False)})
    original = sl.channel_snapshot(channel)
    plan = await sl.prepare(guild,{extra.id})
    result = await sl.apply(guild,plan['id'])
    assert result['status'] == 'applied'
    assert channel.name == '💬・общение'
    assert all(e.get('sync_permissions') is False for e in channel.edits)
    assert sl.channel_snapshot(channel)['overwrites'] == original['overwrites']
    archive = next(c for c in guild.channels if c.name == 'лишние-каналы')
    assert extra.category_id == archive.id
    assert archive.position == len([c for c in guild.channels if c.type == 'category']) - 1
    count = len(guild.channels)
    await sl.apply(guild,plan['id'])
    assert len(guild.channels) == count
    await sl.rollback(guild,plan['id'])
    assert channel.name == original['name'] and channel.category_id == old_category.id
    assert len(guild.channels) == count  # no deletion on rollback
    assert store.read(guild.id)['bindings'] == {}
    await sl.rollback(guild,plan['id'])


@pytest.mark.asyncio
async def test_apply_rejects_stale_acl_snapshot(layout):
    guild, _ = layout
    channel = guild.add('чат')
    plan = await sl.prepare(guild,set())
    channel.overwrites[Target(33)] = discord.PermissionOverwrite(view_channel=True)
    with pytest.raises(ValueError,match='изменились'):
        await sl.apply(guild,plan['id'])
    assert len(guild.channels) == 1


@pytest.mark.asyncio
async def test_uncertain_create_survives_restart_and_is_not_retried(layout, monkeypatch):
    guild, store = layout
    plan = await sl.prepare(guild,set())
    guild.fail_create = True
    with pytest.raises(RuntimeError):
        await sl.apply(guild,plan['id'])
    restarted = ServerLayoutStore(store.root)
    monkeypatch.setattr(sl,'state_store',restarted)
    assert restarted.read(guild.id)['plan']['status'] == 'uncertain'
    guild.fail_create = False
    with pytest.raises(ValueError):
        await sl.apply(guild,plan['id'])
    assert not guild.channels


@pytest.mark.asyncio
async def test_new_active_tournament_blocks_apply(layout,monkeypatch):
    guild, _ = layout
    channel = guild.add('чат')
    plan = await sl.prepare(guild,set())
    monkeypatch.setattr(sl,'busy_channels',AsyncMock(return_value={channel.id}))
    with pytest.raises(ValueError,match='занят'):
        await sl.apply(guild,plan['id'])


@pytest.mark.asyncio
async def test_rollback_does_not_overwrite_independent_permission_changes(layout):
    guild, _ = layout
    channel = guild.add('чат')
    plan = await sl.prepare(guild,set())
    await sl.apply(guild,plan['id'])
    channel.overwrites[Target(17)] = discord.PermissionOverwrite(view_channel=False)
    with pytest.raises(ValueError,match='Права'):
        await sl.rollback(guild,plan['id'])


def test_corrupt_or_foreign_state_is_not_empty(tmp_path):
    store = ServerLayoutStore(tmp_path)
    (tmp_path/'123.json').write_text('{broken')
    with pytest.raises(ValueError):
        store.read(123)
    with pytest.raises(ValueError):
        store.write(123,{'guild_id':999})


@pytest.mark.asyncio
async def test_new_plan_preserves_previous_snapshot_in_history(layout):
    guild,store=layout
    first=await sl.prepare(guild,set())
    await sl.apply(guild,first['id'])
    second=await sl.prepare(guild,set())
    assert second['id'] != first['id']
    saved=store.root/'history'/str(guild.id)/f"{first['id']}.json"
    assert saved.exists()


def test_plan_moves_unassigned_channel_to_target_category():
    """moves={id: категория} → move_channel op без смены имени/ID."""
    plan = sl.plan_layout(
        123, [snap(1001, 'чат'), snap(2001, 'старые-скрины')],
        {}, set(), set(), moves={2001: 'КОМАНДА'})
    mv = [o for o in plan['operations'] if o['action'] == 'move_channel']
    assert mv == [{'action': 'move_channel', 'id': 2001, 'category': 'КОМАНДА'}]


def test_plan_moves_rejects_busy_bound_voice_and_bad_category():
    base = [snap(1001, 'чат'), snap(2001, 'логи'), snap(2002, 'voice', 'voice')]
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {}, {2001}, set(), moves={2001: 'КОМАНДА'})
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {'chat': 1001}, set(), set(),
                       moves={1001: 'КОМАНДА'})
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {}, set(), set(), moves={2002: 'КОМАНДА'})
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {}, set(), set(), moves={2001: 'нет-такой'})
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {}, set(), set(), moves={2001: 'лишние-каналы'})
    with pytest.raises(ValueError):
        sl.plan_layout(123, base, {}, set(), {2001}, moves={2001: 'КОМАНДА'})


def test_plan_move_is_noop_when_already_in_category():
    item = snap(2001, 'старые-скрины')
    # категория КОМАНДА уже существует с этим id
    item['category_id'] = 900
    cats = [snap(900, 'КОМАНДА', 'category')]
    plan = sl.plan_layout(123, [item] + cats, {}, set(), set(),
                          moves={2001: 'КОМАНДА'})
    assert not any(o['action'] == 'move_channel' for o in plan['operations'])


def test_plan_reuses_existing_category_instead_of_creating():
    """cat_map: целевая категория переиспользует существующую —
    reuse_category op, create_category не эмитится."""
    old_cat = snap(900, '╭──・👋 𝐖𝐄𝐋𝐂𝐎𝐌𝐄', 'category')
    plan = sl.plan_layout(123, [old_cat], {}, set(), set(),
                          cat_map={'ИНФОРМАЦИЯ': '╭──・👋 𝐖𝐄𝐋𝐂𝐎𝐌𝐄'})
    assert not any(o['action'] == 'create_category' and o['name'] == 'ИНФОРМАЦИЯ'
                   for o in plan['operations'])
    ru = [o for o in plan['operations'] if o['action'] == 'reuse_category']
    assert ru == [{'action': 'reuse_category', 'id': 900, 'name': 'ИНФОРМАЦИЯ',
                   'from_name': '╭──・👋 𝐖𝐄𝐋𝐂𝐎𝐌𝐄'}]


def test_plan_cat_map_rejects_missing_and_conflicting():
    with pytest.raises(ValueError):
        sl.plan_layout(123, [], {}, set(), set(),
                       cat_map={'ИНФОРМАЦИЯ': 'несуществует'})
    both = [snap(900, '╭──・👋 X', 'category'),
            snap(901, 'ИНФОРМАЦИЯ', 'category')]
    with pytest.raises(ValueError):
        sl.plan_layout(123, both, {}, set(), set(),
                       cat_map={'ИНФОРМАЦИЯ': '╭──・👋 X'})


def test_plan_move_rename_only_emits_op():
    """Канал уже в целевой категории, но имя требует стиля — op есть."""
    cat = snap(900, 'ИНФОРМАЦИЯ', 'category')
    item = snap(2001, '├🌟・репутация', category_id=900)
    plan = sl.plan_layout(123, [cat, item], {}, set(), set(),
                          moves={2001: 'ИНФОРМАЦИЯ'},
                          move_names={2001: '🌟・репутация'})
    mv = [o for o in plan['operations'] if o['action'] == 'move_channel']
    assert mv == [{'action': 'move_channel', 'id': 2001,
                   'category': 'ИНФОРМАЦИЯ', 'name': '🌟・репутация'}]


def test_plan_move_noop_when_name_and_category_match():
    cat = snap(900, 'ИНФОРМАЦИЯ', 'category')
    item = snap(2001, '🌟・репутация', category_id=900)
    plan = sl.plan_layout(123, [cat, item], {}, set(), set(),
                          moves={2001: 'ИНФОРМАЦИЯ'},
                          move_names={2001: '🌟・репутация'})
    assert not any(o['action'] == 'move_channel' for o in plan['operations'])


def test_access_binding_pinned_channel_id_wins(monkeypatch):
    """Закреплённый ACCESS_ROTATION_CHANNEL_ID: plan резолвит
    'access' строго по ID, имя не важно, дубль по имени-тёзке
    не создаётся и не занимается."""
    import config
    monkeypatch.setattr(config, 'ACCESS_ROTATION_CHANNEL_ID', 424242)
    snapshot = [
        snap(424242, 'совсем-другое-имя'),
        snap(5001, 'доступ-ротация'),
        snap(5002, '🎟・доступ-ротация'),
    ]
    plan = sl.plan_layout(123, snapshot, {}, set(), set())
    assert plan['bindings']['access'] == 424242
    assert not any(o['action'] == 'create_channel'
                   and o.get('binding') == 'access'
                   for o in plan['operations'])


def test_access_binding_pinned_overrides_stale_binding(monkeypatch):
    """Сохранённая привязка на канал-дубликат не перекрывает
    закреплённый ID."""
    import config
    monkeypatch.setattr(config, 'ACCESS_ROTATION_CHANNEL_ID', 424242)
    snapshot = [
        snap(424242, 'custom-name'),
        snap(5001, 'доступ-ротация'),
    ]
    plan = sl.plan_layout(
        123, snapshot, {'access': 5001}, set(), set())
    assert plan['bindings']['access'] == 424242


def test_access_binding_pinned_missing_blocks_plan(monkeypatch):
    """ID закреплён, но канал отсутствует → диагностическая
    ошибка; create_channel для access не предлагается даже при
    наличии канала-тёзки."""
    import config
    monkeypatch.setattr(config, 'ACCESS_ROTATION_CHANNEL_ID', 424242)
    with pytest.raises(ValueError, match='access'):
        sl.plan_layout(
            123, [snap(5001, 'доступ-ротация')], {}, set(), set())
