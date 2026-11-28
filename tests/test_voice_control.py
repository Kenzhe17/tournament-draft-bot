"""Unit-тесты Voice Control: ограничения, режим ведущего, таймеры,
закрытие канала, синхронизация. Без реального Discord/PostgreSQL —
store подменяется in-memory реализацией с той же семантикой.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from services import voice_control as vc

GUILD_ID = 1000
OWNER_ID = 1
ACTOR_ID = 2


# ----------------------------------------------------------------------
# In-memory store с семантикой storage/voice_control_store
# ----------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self.settings_map = {}
        self._modes = {}
        self._speakers = {}
        self._exceptions = {}
        self.restrictions_rows = []
        self.overwrites = []
        self.ops = {}
        self.events = []
        self._next_id = 1

    # settings ---------------------------------------------------------
    async def settings(self, guild_id):
        return dict(self.settings_map.get(guild_id, {
            "allowed_role_ids": [], "protected_user_ids": [],
            "protected_role_ids": [], "protected_channel_ids": [],
            "log_channel_id": None,
        }))

    async def put_settings(self, guild_id, **values):
        cur = await self.settings(guild_id)
        for k in ("allowed_role_ids", "protected_user_ids",
                  "protected_role_ids", "protected_channel_ids"):
            if k in values:
                cur[k] = sorted({int(v) for v in values[k]})
        if "log_channel_id" in values:
            cur["log_channel_id"] = values["log_channel_id"]
        self.settings_map[guild_id] = cur
        return cur

    # modes ------------------------------------------------------------
    async def mode(self, guild_id, channel_id):
        cur = self._modes.get((guild_id, channel_id))
        return {**dict(cur), "guild_id": guild_id,
                "channel_id": channel_id} if cur else None

    async def modes(self, guild_id):
        return [{**dict(v), "guild_id": g, "channel_id": c}
                for (g, c), v in self._modes.items() if g == guild_id]

    async def put_mode(self, guild_id, channel_id, **fields):
        allowed = ("speaker_mode", "auto_mute", "closed",
                   "name_override", "user_limit")
        cur = self._modes.setdefault(
            (guild_id, channel_id),
            {"speaker_mode": False, "auto_mute": False, "closed": False,
             "name_override": None, "user_limit": None})
        for k, v in fields.items():
            if k in allowed:
                cur[k] = v
        return dict(cur)

    async def delete_mode_if_idle(self, guild_id, channel_id):
        cur = self._modes.get((guild_id, channel_id))
        if cur and not any(cur.get(k) for k in
                           ("speaker_mode", "auto_mute", "closed",
                            "name_override", "user_limit")):
            del self._modes[(guild_id, channel_id)]
        key = (guild_id, channel_id)
        if key in self._speakers and not self._speakers[key]:
            del self._speakers[key]

    # speakers / exceptions ---------------------------------------------
    async def speakers(self, guild_id, channel_id):
        return sorted(self._speakers.get((guild_id, channel_id), set()))

    async def add_speaker(self, guild_id, channel_id, user_id, actor_id):
        self._speakers.setdefault((guild_id, channel_id), set()).add(user_id)

    async def remove_speaker(self, guild_id, channel_id, user_id):
        self._speakers.get((guild_id, channel_id), set()).discard(user_id)

    async def clear_speakers(self, guild_id, channel_id):
        self._speakers.pop((guild_id, channel_id), None)

    async def exceptions(self, guild_id, channel_id):
        return [dict(e) for e in
                self._exceptions.get((guild_id, channel_id), [])]

    async def add_exception(self, guild_id, channel_id, target_type,
                            target_id, actor_id):
        rows = self._exceptions.setdefault((guild_id, channel_id), [])
        if not any(e["target_type"] == target_type
                   and e["target_id"] == target_id for e in rows):
            rows.append({"target_type": target_type,
                         "target_id": target_id, "added_by": actor_id})

    async def remove_exception(self, guild_id, channel_id, target_type,
                               target_id):
        rows = self._exceptions.get((guild_id, channel_id), [])
        self._exceptions[(guild_id, channel_id)] = [
            e for e in rows
            if not (e["target_type"] == target_type
                    and e["target_id"] == target_id)]

    # restrictions ------------------------------------------------------
    async def add_restriction(self, guild_id, channel_id, user_id, kind,
                              origin, *, reason="", expires_at=None,
                              request_key=None, created_by=0):
        if request_key:
            for r in self.restrictions_rows:
                if r["request_key"] == request_key:
                    return dict(r) if r["state"] == "active" else None
        row = {
            "id": self._next_id, "guild_id": guild_id,
            "channel_id": channel_id, "user_id": user_id, "kind": kind,
            "origin": origin, "reason": reason, "expires_at": expires_at,
            "state": "active", "request_key": request_key,
            "created_by": created_by,
            "created_at": datetime.now(UTC),
            "released_at": None, "released_by": None,
        }
        self._next_id += 1
        self.restrictions_rows.append(row)
        return dict(row)

    async def restrictions(self, guild_id, *, channel_id=None, user_id=None,
                           active_only=True, limit=500):
        out = []
        for r in self.restrictions_rows:
            if r["guild_id"] != guild_id:
                continue
            if channel_id is not None and r["channel_id"] != channel_id:
                continue
            if user_id is not None and r["user_id"] != user_id:
                continue
            if active_only and r["state"] != "active":
                continue
            out.append(dict(r))
        out.sort(key=lambda r: -r["id"])
        return out[:limit]

    async def release_restriction(self, guild_id, restriction_id, *,
                                  actor_id=0):
        for r in self.restrictions_rows:
            if (r["guild_id"] == guild_id and r["id"] == restriction_id
                    and r["state"] == "active"):
                r["state"] = "released"
                r["released_at"] = datetime.now(UTC)
                r["released_by"] = actor_id
                return dict(r)
        return None

    async def expire_due_restrictions(self, limit=200):
        now = datetime.now(UTC)
        out = []
        for r in self.restrictions_rows:
            if (r["state"] == "active" and r["expires_at"]
                    and r["expires_at"] <= now):
                r["state"] = "expired"
                r["released_at"] = now
                out.append(dict(r))
                if len(out) >= limit:
                    break
        return out

    async def has_other_active(self, guild_id, channel_id, user_id, kind, *,
                               exclude_id=0):
        return any(
            r["guild_id"] == guild_id and r["channel_id"] == channel_id
            and r["user_id"] == user_id and r["kind"] == kind
            and r["state"] == "active" and r["id"] != exclude_id
            for r in self.restrictions_rows)

    # overwrites ---------------------------------------------------------
    async def save_overwrites(self, guild_id, channel_id, entries, actor_id):
        ids = []
        for e in entries:
            self._next_id += 1
            ids.append(self._next_id)
            self.overwrites.append({
                "id": self._next_id, "guild_id": guild_id,
                "channel_id": channel_id, "target_type": e["target_type"],
                "target_id": e["target_id"],
                "before": e.get("before"), "applied": e["applied"],
                "state": e.get("state", "applied"),
                "created_by": actor_id,
                "created_at": datetime.now(UTC),
                "restored_at": None})
        return ids

    async def active_overwrites(self, guild_id, channel_id):
        return [dict(o) for o in self.overwrites
                if o["guild_id"] == guild_id and o["channel_id"] == channel_id
                and o["state"] in ("saved", "applied", "conflict")]

    async def resolve_overwrite(self, guild_id, overwrite_id, *, state,
                                actor_id=0):
        for o in self.overwrites:
            if o["guild_id"] == guild_id and o["id"] == overwrite_id \
                    and o["state"] != "restored":
                o["state"] = state
                if state == "restored":
                    o["restored_at"] = datetime.now(UTC)
                return dict(o)
        return None

    # ops / events --------------------------------------------------------
    async def op_create(self, op_id, guild_id, channel_id, kind, actor_id,
                        detail=None):
        self.ops[op_id] = {"op_id": op_id, "guild_id": guild_id,
                           "channel_id": channel_id, "kind": kind,
                           "status": "pending",
                           "detail": dict(detail or {}),
                           "actor_id": actor_id,
                           "created_at": datetime.now(UTC),
                           "finished_at": None}

    async def op_finish(self, op_id, status, detail=None):
        if op_id in self.ops:
            self.ops[op_id]["status"] = status
            self.ops[op_id]["finished_at"] = datetime.now(UTC)
            self.ops[op_id]["detail"].update(detail or {})

    async def last_reversible_op(self, guild_id, channel_id):
        kinds = {"bulk_mute", "bulk_unmute", "speaker_start", "speaker_stop",
                 "mute_member", "unmute_member", "close_voice", "open_voice"}
        cands = [o for o in self.ops.values()
                 if o["guild_id"] == guild_id and o["channel_id"] == channel_id
                 and o["status"] == "done" and o["kind"] in kinds]
        return max(cands, key=lambda o: o["created_at"]) if cands else None

    async def event(self, guild_id, actor_id, action, *, channel_id=None,
                    detail=None):
        self.events.append({"guild_id": guild_id, "actor_id": actor_id,
                            "action": action, "channel_id": channel_id,
                            "detail": dict(detail or {}),
                            "created_at": datetime.now(UTC)})

    async def events(self, guild_id, *, limit=15):
        rows = [e for e in self.events if e["guild_id"] == guild_id]
        return list(reversed(rows))[:limit]

    async def all_active_restrictions(self, limit=5000):
        return [dict(r) for r in self.restrictions_rows
                if r["state"] == "active"][:limit]


# ----------------------------------------------------------------------
# Фейки Discord-объектов
# ----------------------------------------------------------------------
class FakeGuild:
    def __init__(self, gid=GUILD_ID):
        self.id = gid
        self.owner_id = OWNER_ID
        self.channels = []
        self.members = {}
        self._voice = {}
        self.default_role = NS(id=gid, name="@everyone")

    def get_member(self, uid):
        return self.members.get(uid)

    def get_role(self, rid):
        if rid == self.id:
            return self.default_role
        return NS(id=rid, name=f"role{rid}")

    async def fetch_member(self, uid):
        m = self.members.get(uid)
        if m is None:
            raise discord.HTTPException(MagicMockResponse(), "not found")
        return m

    def _voice_state_for(self, uid):
        return self._voice.get(uid)

    @property
    def me(self):
        return self.members.get(BOT_ID)


BOT_ID = 999


def make_role(rid, position=1):
    return NS(id=rid, position=position, name=f"role{rid}")


def make_member(uid, guild, channel=None, *, muted=False, deaf=False,
                self_muted=False, self_deafed=False, bot=False,
                roles=(), top_role=1):
    m = NS(
        id=uid, bot=bot, guild=guild, top_role=top_role,
        roles=list(roles), display_name=f"User{uid}", name=f"user{uid}",
        mention=f"<@{uid}>", voice=None,
        guild_permissions=NS(administrator=False, mute_members=True,
                             deafen_members=True, move_members=True,
                             manage_roles=True, manage_channels=True),
    )

    async def edit(**kwargs):
        kwargs.pop("reason", None)
        if m.voice is not None:
            if "mute" in kwargs:
                m.voice.mute = kwargs["mute"]
            if "deaf" in kwargs:
                m.voice.deaf = kwargs["deaf"]
        return m

    async def move_to(target=None, reason=""):
        old = m.voice.channel if m.voice else None
        if old is not None and hasattr(old, "members") \
                and m in getattr(old, "members", []):
            old.members.remove(m)
        if target is None:
            m.voice = None
        else:
            if m.voice is None:
                m.voice = NS(channel=None, mute=False, deaf=False,
                             self_mute=False, self_deaf=False)
            m.voice.channel = target
            if hasattr(target, "members") and m not in target.members:
                target.members.append(m)
        return m

    m.edit = AsyncMock(side_effect=edit)
    m.move_to = AsyncMock(side_effect=move_to)
    if channel is not None:
        m.voice = NS(channel=channel, mute=muted, deaf=deaf,
                     self_mute=self_muted, self_deaf=self_deafed)
        if hasattr(channel, "members") and m not in channel.members:
            channel.members.append(m)
    guild.members[uid] = m
    guild._voice[uid] = m.voice
    return m


def make_channel(cid, guild, name="General", members=None, user_limit=0):
    ch = NS(
        id=cid, name=name, guild=guild, members=list(members or []),
        user_limit=user_limit, position=0,
        mention=f"<#{cid}>",
    )
    ch._perm = {}

    def _overwrites_for(t):
        tid = getattr(t, "id", None)
        connect, view = ch._perm.get(tid, (True, True))
        return NS(connect=connect, view_channel=view)

    async def _set_permissions(target, overwrite=None, reason=""):
        tid = getattr(target, "id", None)
        if overwrite is None:
            ch._perm.pop(tid, None)
        else:
            ch._perm[tid] = (getattr(overwrite, "connect", None),
                             getattr(overwrite, "view_channel", None))

    ch.overwrites_for = _overwrites_for
    ch.set_permissions = AsyncMock(side_effect=_set_permissions)
    ch.edit = AsyncMock()
    ch.permissions_for = lambda m: NS(connect=True)
    guild.channels.append(ch)
    return ch


def make_bot_member(guild):
    return make_member(BOT_ID, guild, top_role=99,
                       roles=[make_role(50, position=50)])


class MagicMockResponse:
    status = 404


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(vc, "db", store)
    guild = FakeGuild()
    bot_member = make_bot_member(guild)
    vc.set_bot(NS(get_guild=lambda gid: guild if gid == GUILD_ID else None))
    return NS(store=store, guild=guild, bot_member=bot_member)


def voice_of(member):
    return member.voice


# ----------------------------------------------------------------------
# Тесты
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_active_channels_sorted_and_stage_excluded(env):
    g = env.guild
    ch_small = make_channel(10, g, "Small")
    ch_big = make_channel(11, g, "Big")
    make_member(10, g, ch_small)
    for i in range(12):
        make_member(100 + i, g, ch_big)
    make_member(200, g, ch_big, bot=True)  # бот не считается
    stage = NS(id=12, name="Stage", guild=g, members=[NS(id=1, bot=False)],
               type=discord.ChannelType.stage_voice)
    g.channels.append(stage)
    empty = make_channel(13, g, "Empty")

    conf = await vc.db.settings(GUILD_ID)
    out = await vc.active_channels(g, conf)
    names = [i["channel"].name for i in out]
    assert names[0] == "Big" and names[1] == "Small"
    assert all(i["channel"].name != "Stage" for i in out)
    assert all(i["channel"].name != "Empty" for i in out)
    big = next(i for i in out if i["channel"].name == "Big")
    assert big["count"] == 12  # бот исключён

    # Пустой канал с активным режимом остаётся доступным.
    await vc.db.put_mode(GUILD_ID, empty.id, auto_mute=True)
    out = await vc.active_channels(g, await vc.db.settings(GUILD_ID))
    assert any(i["channel"].id == empty.id and i["mode_active"]
               for i in out)


@pytest.mark.asyncio
async def test_bulk_mute_excludes_actor_owner_protected_bots(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    owner = make_member(OWNER_ID, g, ch)
    bot = make_member(50, g, ch, bot=True)
    protected = make_member(60, g, ch)
    normal1 = make_member(70, g, ch)
    normal2 = make_member(71, g, ch)
    foreign_muted = make_member(72, g, ch, muted=True)  # мут не наш
    await vc.db.put_settings(GUILD_ID, protected_user_ids=[60])

    result = await vc.bulk_mute(ch, actor)
    assert result["done"] == 2
    assert normal1.voice.mute and normal2.voice.mute
    assert not actor.voice.mute and not owner.voice.mute
    assert not bot.voice.mute and not protected.voice.mute
    assert foreign_muted.voice.mute  # чужой мут не тронут
    # Наши ограничения записаны.
    rows = await vc.db.restrictions(GUILD_ID, channel_id=ch.id)
    assert {r["user_id"] for r in rows} == {70, 71}


@pytest.mark.asyncio
async def test_apply_restriction_never_claims_foreign_mute(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch, muted=True)  # серверный мут неизвестного origin

    result = await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_MANUAL, actor_id=ACTOR_ID)
    assert result["skipped"] == "foreign_mute"
    assert target.voice.mute  # не снят и не перезаписан
    assert await vc.db.restrictions(GUILD_ID) == []


@pytest.mark.asyncio
async def test_bulk_unmute_only_releases_own_restrictions(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    ours = make_member(70, g, ch, muted=True)
    foreign = make_member(71, g, ch, muted=True)
    # Только для ours создаём запись Voice Control.
    await vc.db.add_restriction(GUILD_ID, ch.id, ours.id, "server_mute",
                                vc.ORIGIN_BULK_MUTE, created_by=ACTOR_ID)

    result = await vc.bulk_unmute(ch, actor)
    assert result["done"] == 1
    assert not ours.voice.mute
    assert foreign.voice.mute  # чужой мут сохранён


@pytest.mark.asyncio
async def test_speaker_mode_mutes_and_releases_only_own(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    speaker = make_member(80, g, ch)
    listener = make_member(81, g, ch)
    bulk_muted = make_member(82, g, ch)

    # bulk-ограничение поверх — должно пережить завершение режима.
    await vc.apply_restriction(bulk_muted, ch, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)

    result = await vc.start_speaker_mode(ch, actor, speakers=[speaker])
    assert listener.voice.mute and not speaker.voice.mute
    assert result["muted"] >= 1

    # Новый участник при активном режиме получает мут через reconcile.
    newbie = make_member(83, g, ch)
    await vc.reconcile_member(newbie)
    assert newbie.voice.mute

    # Добавление ведущего снимает его speaker-мут.
    await vc.speaker_add(ch, newbie, actor)
    assert not newbie.voice.mute

    stop = await vc.stop_speaker_mode(ch, actor)
    assert not listener.voice.mute
    # bulk_muted остаётся замученным — ограничение другого origin.
    assert bulk_muted.voice.mute
    assert stop["kept"] >= 1


@pytest.mark.asyncio
async def test_timer_expiry_keeps_other_restrictions(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    past = datetime.now(UTC) - timedelta(seconds=1)
    await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_TIMER, actor_id=ACTOR_ID,
        expires_at=past)
    # Второе (бессрочное) ограничение того же kind — стек origins.
    await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)
    assert target.voice.mute

    # Истёкший таймер помечается expired, но флаг остаётся:
    # действует другое активное ограничение.
    expired = await vc.db.expire_due_restrictions()
    assert len(expired) == 1
    assert await vc.db.has_other_active(
        GUILD_ID, ch.id, target.id, "server_mute",
        exclude_id=expired[0]["id"])
    assert target.voice.mute

    # Снимаем второе ограничение → отложенная очистка через reconcile.
    other = next(r for r in await vc.db.restrictions(GUILD_ID)
                 if r["state"] == "active")
    await vc.db.release_restriction(GUILD_ID, other["id"])
    await vc.reconcile_member(target)
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_reconcile_releases_restrictions_of_old_channel(env):
    g = env.guild
    ch_a = make_channel(10, g, "A")
    ch_b = make_channel(11, g, "B")
    make_member(ACTOR_ID, g, ch_a)
    target = make_member(70, g, ch_a)
    await vc.apply_restriction(target, ch_a, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)
    assert target.voice.mute

    # Участник перешёл в другой канал — mute-флаг сохраняется в
    # voice state, но ограничение старого канала освобождается.
    target.voice.channel = ch_b
    await vc.reconcile_member(target)
    rows = await vc.db.restrictions(GUILD_ID, channel_id=ch_a.id,
                                    user_id=target.id)
    assert rows == []
    # Флаг снят, т.к. других активных ограничений нет.
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_reconcile_remutes_when_flag_cleared_externally(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)
    assert target.voice.mute
    target.voice.mute = False  # стороннее снятие

    await vc.reconcile_member(target)
    assert target.voice.mute  # восстановлено


@pytest.mark.asyncio
async def test_close_and_open_voice_restore_overwrites(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)

    await vc.close_voice(ch, actor)
    # set_permissions вызван с connect=False для @everyone.
    args, kwargs = ch.set_permissions.call_args
    overwrite = kwargs.get("overwrite") or (args[1] if len(args) > 1 else None)
    assert overwrite is not None and overwrite.connect is False
    mode = await vc.db.mode(GUILD_ID, ch.id)
    assert mode and mode["closed"]

    # Открытие восстанавливает исходный connect=True.
    result = await vc.open_voice(ch, actor)
    assert result["restored"] == 1 and result["conflicts"] == 0
    mode = await vc.db.mode(GUILD_ID, ch.id)
    assert not mode or not mode.get("closed")


@pytest.mark.asyncio
async def test_open_voice_detects_third_party_conflict(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)

    await vc.close_voice(ch, actor)
    # Сторонний админ вернул connect=True до нашего open.
    ch._perm[GUILD_ID] = (True, True)
    result = await vc.open_voice(ch, actor)
    assert result["conflicts"] == 1
    rows = await vc.db.active_overwrites(GUILD_ID, ch.id)
    assert all(o["state"] == "conflict" for o in rows)


@pytest.mark.asyncio
async def test_guild_isolation(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)
    # Чужая гильдия не видит и не снимает ограничения.
    other = await vc.db.restrictions(GUILD_ID + 1)
    assert other == []
    rows = await vc.db.restrictions(GUILD_ID)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_bulk_mute_idempotent_double_click(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)
    result = await vc.bulk_mute(ch, actor)
    assert result["done"] == 0  # уже ограничен — пропущен


@pytest.mark.asyncio
async def test_channel_lock_serializes_bulk(env):
    """Двойное нажатие: второй bulk_mute видит уже наложенные мьюты."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    for i in range(5):
        make_member(100 + i, g, ch)
    r1, r2 = await asyncio.gather(
        vc.bulk_mute(ch, actor), vc.bulk_mute(ch, actor))
    assert r1["done"] + r2["done"] == 5
    assert r1["done"] + r2["done"] + r1["skipped"] + r2["skipped"] >= 5


@pytest.mark.asyncio
async def test_move_member_checks_target_and_capacity(env):
    g = env.guild
    ch = make_channel(10, g, "A")
    dest = make_channel(11, g, "B", user_limit=1)
    actor = make_member(ACTOR_ID, g, ch)
    make_member(90, g, dest)
    target = make_member(70, g, ch)

    with pytest.raises(vc.VoiceControlError):
        await vc.move_member(target, dest, actor)  # канал заполнен

    dest.user_limit = 0
    await vc.move_member(target, dest, actor)
    assert target.voice.channel is dest


@pytest.mark.asyncio
async def test_disconnect_member(env):
    g = env.guild
    ch = make_channel(10, g, "A")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    await vc.disconnect_member(target, actor)
    assert target.voice is None


@pytest.mark.asyncio
async def test_auto_mute_on_reconcile_when_enabled(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    await vc.db.put_mode(GUILD_ID, ch.id, auto_mute=True)
    newbie = make_member(70, g, ch)
    await vc.reconcile_member(newbie)
    assert newbie.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, channel_id=ch.id,
                                    user_id=newbie.id)
    assert rows and rows[0]["origin"] == vc.ORIGIN_AUTO_MUTE
