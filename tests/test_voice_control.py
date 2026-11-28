"""Unit-тесты Voice Control: durable lifecycle ограничений,
модерационный координатор, overwrites, режим ведущего, таймеры,
закрытие канала, синхронизация, сессии панелей.

Без реального Discord/PostgreSQL — store подменяется in-memory
реализацией с той же семантикой, Discord-объекты — duck-typed
фейки + настоящие discord.PermissionOverwrite.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

import config
from services import voice_control as vc

GUILD_ID = 1000
OWNER_ID = 1
ACTOR_ID = 2
BOT_ID = 999

ACTIVE_STATES = ("active", "pending_apply")
TERMINAL_STATES = ("released", "expired", "conflict", "failed")


# ----------------------------------------------------------------------
# In-memory store с семантикой storage/voice_control_store (P0)
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
        self.event_rows = []
        self._next_id = 1
        self._claimed = set()

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
                   "name_override", "user_limit",
                   "name_before", "user_limit_before")
        cur = self._modes.setdefault(
            (guild_id, channel_id),
            {"speaker_mode": False, "auto_mute": False, "closed": False,
             "name_override": None, "user_limit": None,
             "name_before": None, "user_limit_before": None})
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
                              request_key=None, created_by=0,
                              state="pending_apply"):
        if request_key:
            for r in self.restrictions_rows:
                if r["request_key"] == request_key:
                    return dict(r) if r["state"] == "active" else None
        row = {
            "id": self._next_id, "guild_id": guild_id,
            "channel_id": channel_id, "user_id": user_id, "kind": kind,
            "origin": origin, "reason": reason, "expires_at": expires_at,
            "state": state, "request_key": request_key,
            "created_by": created_by,
            "created_at": datetime.now(UTC),
            "released_at": None, "released_by": None,
            "claim_token": None, "claim_until": None,
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
            if active_only and r["state"] not in ACTIVE_STATES:
                continue
            out.append(dict(r))
        out.sort(key=lambda r: -r["id"])
        return out[:limit]

    async def set_state(self, guild_id, restriction_id, state, *,
                        actor_id=0, from_states=None):
        for r in self.restrictions_rows:
            if r["guild_id"] != guild_id or r["id"] != restriction_id:
                continue
            if from_states and r["state"] not in from_states:
                return None
            r["state"] = state
            if state in TERMINAL_STATES:
                r["released_at"] = datetime.now(UTC)
                r["released_by"] = actor_id
            r["claim_token"] = None
            r["claim_until"] = None
            return dict(r)
        return None

    async def claim_pending(self, state, worker, *, limit=50,
                            lease_seconds=120, after_id=0):
        out = []
        for r in sorted(self.restrictions_rows, key=lambda x: x["id"]):
            if r["state"] != state or r["id"] <= after_id:
                continue
            if r["id"] in self._claimed:
                continue
            r["claim_token"] = worker
            r["claim_until"] = datetime.now(UTC) + timedelta(
                seconds=lease_seconds)
            self._claimed.add(r["id"])
            out.append(dict(r))
            if len(out) >= limit:
                break
        return out

    async def release_claim(self, restriction_id):
        self._claimed.discard(restriction_id)
        for r in self.restrictions_rows:
            if r["id"] == restriction_id:
                r["claim_token"] = None
                r["claim_until"] = None

    async def pending_states(self):
        counts = {}
        for r in self.restrictions_rows:
            if r["state"] in ("pending_apply", "pending_release",
                              "active", "conflict"):
                counts[r["state"]] = counts.get(r["state"], 0) + 1
        return counts

    async def release_restriction(self, guild_id, restriction_id, *,
                                  actor_id=0, final_state="released"):
        for r in self.restrictions_rows:
            if (r["guild_id"] == guild_id and r["id"] == restriction_id
                    and r["state"] in ACTIVE_STATES + ("pending_release",)):
                r["state"] = final_state
                r["released_at"] = datetime.now(UTC)
                r["released_by"] = actor_id
                r["claim_token"] = None
                r["claim_until"] = None
                return dict(r)
        return None

    async def expire_due_restrictions(self, limit=200):
        now = datetime.now(UTC)
        out = []
        for r in sorted(self.restrictions_rows, key=lambda x: x["id"]):
            if (r["state"] == "active" and r["expires_at"]
                    and r["expires_at"] <= now):
                r["state"] = "pending_release"
                r["claim_token"] = None
                r["claim_until"] = None
                out.append(dict(r))
                if len(out) >= limit:
                    break
        return out

    async def has_other_active(self, guild_id, user_id, kind, *,
                               exclude_id=0):
        return any(
            r["guild_id"] == guild_id and r["user_id"] == user_id
            and r["kind"] == kind and r["state"] in ACTIVE_STATES
            and r["id"] != exclude_id
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
                "before": e.get("before"), "applied": e.get("applied"),
                "state": e.get("state", "applied"),
                "created_by": actor_id,
                "created_at": datetime.now(UTC),
                "restored_at": None})
        return ids

    async def active_overwrites(self, guild_id, channel_id):
        return [dict(o) for o in self.overwrites
                if o["guild_id"] == guild_id and o["channel_id"] == channel_id
                and o["state"] in ("saved", "applied", "conflict")]

    async def all_active_overwrites(self, limit=1000):
        return [dict(o) for o in self.overwrites
                if o["state"] in ("saved", "applied", "conflict")][:limit]

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
        self.event_rows.append({"guild_id": guild_id, "actor_id": actor_id,
                            "action": action, "channel_id": channel_id,
                            "detail": dict(detail or {}),
                            "created_at": datetime.now(UTC)})

    async def events(self, guild_id, *, limit=15):
        rows = [e for e in self.event_rows if e["guild_id"] == guild_id]
        return list(reversed(rows))[:limit]

    async def scan_active(self, *, after_id=0, limit=500):
        out = [dict(r) for r in sorted(self.restrictions_rows, key=lambda x: x["id"])
               if r["state"] in ACTIVE_STATES and r["id"] > after_id]
        return out[:limit]

    async def all_active_restrictions(self, limit=5000):
        return [dict(r) for r in self.restrictions_rows
                if r["state"] in ACTIVE_STATES][:limit]


class FakeModStore:
    """In-memory замена moderation_store.voice_state."""

    def __init__(self):
        self.state = {}   # (guild_id, user_id, field) -> {'status','at'}

    async def voice_state(self, guild_id, target_id, field):
        return dict(self.state.get((guild_id, target_id, field),
                                   {"status": "none", "at": None}))

    def set(self, guild_id, user_id, field, status, at=None):
        self.state[(guild_id, user_id, field)] = {
            "status": status, "at": at or datetime.now(UTC)}


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

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_role(self, rid):
        if rid == self.id:
            return self.default_role
        return NS(id=rid, name=f"role{rid}")

    async def fetch_member(self, uid):
        m = self.members.get(uid)
        if m is None:
            raise discord.NotFound(MagicMockResponse(), "not found")
        return m

    @property
    def me(self):
        return self.members.get(BOT_ID)


def make_role(rid, position=1):
    return NS(id=rid, position=position, name=f"role{rid}")


def make_member(uid, guild, channel=None, *, muted=False, deaf=False,
                self_muted=False, self_deafed=False, bot=False,
                roles=(), top_role=1, edit_side_effect=None):
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
        if edit_side_effect is not None:
            await edit_side_effect(**kwargs)
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
            guild._voice[uid] = None
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


class _OWMap(dict):
    """channel.overwrites с ключами по target.id — NS-объекты
    тестов нехешируемы, а discord.py ключует по Snowflake."""

    @staticmethod
    def _k(t):
        return getattr(t, "id", t)

    def __setitem__(self, k, v):
        super().__setitem__(self._k(k), (k, v))

    def __getitem__(self, k):
        return super().__getitem__(self._k(k))[1]

    def get(self, k, d=None):
        e = super().get(self._k(k))
        return e[1] if e else d

    def pop(self, k, d=None):
        e = super().pop(self._k(k), None)
        return e[1] if e is not None else d

    def items(self):
        return [(k, v) for k, v in super().values()]

    def __contains__(self, k):
        return self._k(k) in self


class FakeChannel:
    """Voice-канал с НАСТОЯЩИМИ PermissionOverwrite в .overwrites —
    как у discord.py (channel.overwrites: target -> PermissionOverwrite)."""

    type = discord.ChannelType.voice

    def __init__(self, cid, guild, name="General", members=None,
                 user_limit=0, position=0):
        self.id = cid
        self.name = name
        self.guild = guild
        self.members = list(members or [])
        self.user_limit = user_limit
        self.position = position
        self.mention = f"<#{cid}>"
        self.overwrites = _OWMap()
        self.edit_calls = []
        self.set_permissions = AsyncMock(side_effect=self._set_permissions)
        self.edit = AsyncMock(side_effect=self._edit)
        self._connect_perms = {}   # member/role id -> bool (effective)

    async def _set_permissions(self, target, overwrite=None, reason=""):
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def _edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "user_limit" in kwargs:
            self.user_limit = kwargs["user_limit"]

    def overwrites_for(self, target):
        ow = self.overwrites.get(target)
        if ow is None:
            return NS(connect=True, view_channel=True)
        allow, deny = ow.pair()
        return NS(connect=bool(allow.connect or not deny.connect),
                  view_channel=bool(allow.view_channel or not deny.view_channel))

    def permissions_for(self, member):
        return NS(connect=self._connect_perms.get(member.id, True))


def ow(connect=None, allow_bits=0, deny_bits=0):
    """PermissionOverwrite с заданным connect и произвольными битами."""
    a = discord.Permissions(allow_bits)
    d = discord.Permissions(deny_bits)
    if connect is True:
        a |= discord.Permissions(connect=True)
    elif connect is False:
        d |= discord.Permissions(connect=True)
    return discord.PermissionOverwrite.from_pair(a, d)


def make_channel(cid, guild, name="General", members=None, user_limit=0,
                 position=0):
    ch = FakeChannel(cid, guild, name, members, user_limit, position)
    guild.channels.append(ch)
    return ch


def make_bot_member(guild):
    return make_member(BOT_ID, guild, top_role=99,
                       roles=[make_role(50, position=50)])


class MagicMockResponse:
    def __init__(self, status=404):
        self.status = status
        self.reason = "mock"


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    mod = FakeModStore()
    monkeypatch.setattr(vc, "db", store)
    monkeypatch.setattr(vc, "mod_db", mod)
    monkeypatch.setattr(config, "VOICE_CONTROL_ENABLED", True)
    guild = FakeGuild()
    bot_member = make_bot_member(guild)
    vc.set_bot(NS(get_guild=lambda gid: guild if gid == GUILD_ID else None))
    return NS(store=store, mod=mod, guild=guild, bot_member=bot_member)


def voice_of(member):
    return member.voice


# ----------------------------------------------------------------------
# Базовые тесты (портированы)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_active_channels_sorted_and_stage_excluded(env):
    g = env.guild
    ch_small = make_channel(10, g, "Small")
    ch_big = make_channel(11, g, "Big")
    make_member(10, g, ch_small)
    for i in range(12):
        make_member(100 + i, g, ch_big)
    make_member(200, g, ch_big, bot=True)
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
    assert big["count"] == 12

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
    foreign_muted = make_member(72, g, ch, muted=True)
    await vc.db.put_settings(GUILD_ID, protected_user_ids=[60])

    result = await vc.bulk_mute(ch, actor)
    assert result["done"] == 2
    assert normal1.voice.mute and normal2.voice.mute
    assert not actor.voice.mute and not owner.voice.mute
    assert not bot.voice.mute and not protected.voice.mute
    assert foreign_muted.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, channel_id=ch.id)
    assert {r["user_id"] for r in rows} == {70, 71}
    assert all(r["state"] == "active" for r in rows)


@pytest.mark.asyncio
async def test_apply_restriction_never_claims_foreign_mute(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch, muted=True)

    result = await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_MANUAL, actor_id=ACTOR_ID)
    assert result["skipped"] == "foreign_mute"
    assert target.voice.mute
    assert await vc.db.restrictions(GUILD_ID) == []


@pytest.mark.asyncio
async def test_bulk_unmute_only_releases_own_restrictions(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    ours = make_member(70, g, ch, muted=True)
    foreign = make_member(71, g, ch, muted=True)
    rec = await vc.db.add_restriction(
        GUILD_ID, ch.id, ours.id, "server_mute", vc.ORIGIN_BULK_MUTE,
        created_by=ACTOR_ID, state="active")
    assert rec is not None

    result = await vc.bulk_unmute(ch, actor)
    assert result["done"] == 1
    assert not ours.voice.mute
    assert foreign.voice.mute


@pytest.mark.asyncio
async def test_speaker_mode_mutes_and_releases_only_own(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    speaker = make_member(80, g, ch)
    listener = make_member(81, g, ch)
    bulk_muted = make_member(82, g, ch)

    await vc.apply_restriction(bulk_muted, ch, "server_mute",
                               vc.ORIGIN_BULK_MUTE, actor_id=ACTOR_ID)

    result = await vc.start_speaker_mode(ch, actor, speakers=[speaker])
    assert listener.voice.mute and not speaker.voice.mute
    assert result["muted"] >= 1

    newcomer = make_member(83, g, ch)
    await vc.reconcile_member(newcomer)
    assert newcomer.voice.mute

    result = await vc.stop_speaker_mode(ch, actor)
    assert not listener.voice.mute
    assert bulk_muted.voice.mute
    assert result["kept"] >= 1


@pytest.mark.asyncio
async def test_timer_restriction_expires(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    past = datetime.now(UTC) - timedelta(seconds=5)
    result = await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_TIMER, actor_id=ACTOR_ID,
        expires_at=past)
    assert result["restriction"]["state"] == "active"
    assert target.voice.mute

    bot = NS(get_guild=lambda gid: g)
    stats = await vc.sweep_timers(bot)
    assert stats["expired"] >= 1
    assert not target.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=False)
    assert rows[0]["state"] == "expired"


@pytest.mark.asyncio
async def test_close_open_preserves_other_bits(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch, top_role=50)
    everyone = g.default_role
    view_bit = discord.Permissions(view_channel=True).value
    send_bit = discord.Permissions(send_messages=True).value
    ch.overwrites[everyone] = discord.PermissionOverwrite.from_pair(
        discord.Permissions(view_bit | send_bit), discord.Permissions(0))

    await vc.close_voice(ch, actor)
    ow_obj = ch.overwrites[everyone]
    allow, deny = ow_obj.pair()
    assert deny.connect
    assert allow.view_channel and allow.send_messages
    member_exc = make_member(90, g, ch)
    exc_ow = ow(connect=True)
    ch.overwrites[member_exc] = exc_ow
    snap_before = ch.overwrites[member_exc]

    result = await vc.open_voice(ch, actor)
    assert result["conflicts"] == 0
    ow2 = ch.overwrites.get(everyone)
    if ow2 is not None:
        a2, d2 = ow2.pair()
        assert not d2.connect and not a2.connect
        assert a2.view_channel and a2.send_messages
    assert ch.overwrites.get(member_exc) is snap_before
    mode = await vc.db.mode(GUILD_ID, ch.id)
    assert not mode or not mode.get("closed")


@pytest.mark.asyncio
async def test_close_idempotent_and_open_conflict(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch, top_role=50)
    everyone = g.default_role

    r1 = await vc.close_voice(ch, actor)
    assert not r1["already"]
    calls = ch.set_permissions.call_count
    r2 = await vc.close_voice(ch, actor)
    assert r2["already"]
    assert ch.set_permissions.call_count == calls

    ch.overwrites[everyone] = ow(connect=True)
    result = await vc.open_voice(ch, actor)
    assert result["conflicts"] >= 1
    mode = await vc.db.mode(GUILD_ID, ch.id)
    assert mode and mode.get("closed")


@pytest.mark.asyncio
async def test_crash_between_api_and_commit_recovers(env):
    """Крэш после Discord API, до PG commit: overwrite 'saved',
    connect уже deny → open_voice корректно восстанавливает."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch, top_role=50)
    everyone = g.default_role

    applied = vc._ow_snapshot(ow(connect=False))
    await vc.db.save_overwrites(
        GUILD_ID, ch.id,
        [{"target_type": "role", "target_id": everyone.id,
          "before": None, "applied": applied, "state": "saved"}],
        actor.id)
    ch.overwrites[everyone] = ow(connect=False)

    result = await vc.open_voice(ch, actor)
    assert result["restored"] >= 1
    ow_obj = ch.overwrites.get(everyone)
    if ow_obj is not None:
        assert not ow_obj.pair()[1].connect


@pytest.mark.asyncio
async def test_move_stale_channel_rejected(env):
    g = env.guild
    ch_a = make_channel(10, g, "A")
    ch_b = make_channel(11, g, "B")
    actor = make_member(ACTOR_ID, g, ch_a)
    target = make_member(70, g, ch_a)
    dest = make_channel(12, g, "Dest")
    dest._connect_perms[target.id] = True

    await target.move_to(ch_b)
    with pytest.raises(vc.StaleTargetError):
        await vc.move_member(target, dest, actor, expected_channel_id=ch_a.id)
    assert target.voice.channel is ch_b

    await vc.move_member(target, dest, actor)
    assert target.voice.channel is dest


@pytest.mark.asyncio
async def test_disconnect_stale_channel_rejected(env):
    g = env.guild
    ch_a = make_channel(10, g, "A")
    ch_b = make_channel(11, g, "B")
    actor = make_member(ACTOR_ID, g, ch_a)
    target = make_member(70, g, ch_a)
    await target.move_to(ch_b)
    with pytest.raises(vc.StaleTargetError):
        await vc.disconnect_member(target, actor, expected_channel_id=ch_a.id)
    assert target.voice.channel is ch_b


@pytest.mark.asyncio
async def test_guild_isolation(env):
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_MANUAL, actor_id=ACTOR_ID)
    assert await vc.db.restrictions(9999) == []


# ----------------------------------------------------------------------
# P0.4 — Unified Moderation Ownership
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mod_mute_after_vc_release_is_preserved(env):
    """1. VC mute → release → новый модерационный mute → reconcile.
    Модерационный мут сохраняется."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    rec = await vc.apply_restriction(target, ch, "server_mute",
                                     vc.ORIGIN_MANUAL, actor=actor)
    rid = rec["restriction"]["id"]
    await vc.release_restriction(rec["restriction"], actor=actor)
    assert not target.voice.mute

    target.voice.mute = True
    env.mod.set(GUILD_ID, target.id, "mute", "active")

    await vc.reconcile_member(target)
    assert target.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=False)
    ours = next(r for r in rows if r["id"] == rid)
    assert ours["state"] in TERMINAL_STATES


@pytest.mark.asyncio
async def test_mod_mute_during_vc_restriction_keeps_flag(env):
    """2. VC mute → модерационный mute → release Voice Control.
    Флаг остаётся — он принадлежит модерации."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    rec = await vc.apply_restriction(target, ch, "server_mute",
                                     vc.ORIGIN_MANUAL, actor=actor)
    assert target.voice.mute

    env.mod.set(GUILD_ID, target.id, "mute", "active")

    await vc.release_restriction(rec["restriction"], actor=actor)
    assert target.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=False)
    ours = next(r for r in rows if r["id"] == rec["restriction"]["id"])
    assert ours["state"] in ("released", "expired")
    actions = [e["action"] for e in
               await env.store.events(GUILD_ID, limit=100)]
    assert "voice.moderation_kept" in actions


@pytest.mark.asyncio
async def test_mod_unmute_after_vc_mute_not_restored(env):
    """Модерация сняла мут после VC-ограничения → reconcile НЕ
    восстанавливает флаг, помечает конфликт."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    rec = await vc.apply_restriction(target, ch, "server_mute",
                                     vc.ORIGIN_MANUAL, actor=actor)
    assert target.voice.mute

    env.mod.set(GUILD_ID, target.id, "mute", "inactive")
    target.voice.mute = False

    await vc.reconcile_member(target)
    assert not target.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=False)
    ours = next(r for r in rows if r["id"] == rec["restriction"]["id"])
    assert ours["state"] == "conflict"


@pytest.mark.asyncio
async def test_vc_mute_then_mod_mute_then_vc_release_keeps(env):
    """Массовое размьют не снимает модерационный мут."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    other = make_member(71, g, ch)

    await vc.bulk_mute(ch, actor)
    assert target.voice.mute and other.voice.mute

    env.mod.set(GUILD_ID, target.id, "mute", "active")
    result = await vc.bulk_unmute(ch, actor)
    assert result["done"] >= 1
    assert target.voice.mute
    assert not other.voice.mute


# ----------------------------------------------------------------------
# P0.5 — Durable Restriction Lifecycle
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_403_retries_via_pending_release(env):
    """3. Ошибка 403 при release → pending_release, retry успешен."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    calls = {"failed": False}

    async def flaky(**kwargs):
        if kwargs.get("mute") is False and not calls["failed"]:
            calls["failed"] = True
            raise discord.Forbidden(MagicMockResponse(status=403), "no")
        if target.voice is not None and "mute" in kwargs:
            target.voice.mute = kwargs["mute"]

    target.edit = AsyncMock(side_effect=flaky)
    rec = await vc.apply_restriction(target, ch, "server_mute",
                                     vc.ORIGIN_MANUAL, actor=actor)
    assert target.voice.mute

    await vc.release_restriction(rec["restriction"], actor=actor)
    row = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                   active_only=False)
    ours = next(r for r in row if r["id"] == rec["restriction"]["id"])
    assert ours["state"] == "pending_release"
    assert target.voice.mute

    bot = NS(get_guild=lambda gid: g)
    stats = await vc.sweep_timers(bot)
    assert stats["pending_release"] >= 1
    assert not target.voice.mute
    row = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                   active_only=False)
    ours = next(r for r in row if r["id"] == rec["restriction"]["id"])
    assert ours["state"] == "released"


@pytest.mark.asyncio
async def test_release_after_leaving_and_reconnect(env):
    """4. Выход из войса до release → reconnect в другой канал:
    очистка не зависит от channel_id."""
    g = env.guild
    ch_a = make_channel(10, g, "A")
    ch_b = make_channel(11, g, "B")
    actor = make_member(ACTOR_ID, g, ch_a)
    target = make_member(70, g, ch_a)

    rec = await vc.apply_restriction(target, ch_a, "server_mute",
                                     vc.ORIGIN_MANUAL, actor=actor)
    await target.move_to(None)
    await target.move_to(ch_b)
    await vc.release_restriction(rec["restriction"], actor=actor)
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_restart_after_apply_before_commit(env):
    """5. Перезапуск после Discord apply, но до PG commit:
    pending_apply + флаг уже стоит → resolve → active."""
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch, muted=True)

    rec = await vc.db.add_restriction(
        GUILD_ID, ch.id, target.id, "server_mute", vc.ORIGIN_MANUAL,
        created_by=ACTOR_ID, state="pending_apply")
    bot = NS(get_guild=lambda gid: g)
    stats = await vc.sweep_timers(bot)
    assert stats["pending_apply"] >= 1
    row = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                   active_only=False)
    ours = next(r for r in row if r["id"] == rec["id"])
    assert ours["state"] == "active"
    assert target.voice.mute


@pytest.mark.asyncio
async def test_restart_at_pending_release(env):
    """6. Перезапуск при pending_release → sweeper завершает очистку."""
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch, muted=True)

    rec = await vc.db.add_restriction(
        GUILD_ID, ch.id, target.id, "server_mute", vc.ORIGIN_MANUAL,
        created_by=ACTOR_ID, state="pending_release")
    bot = NS(get_guild=lambda gid: g)
    await vc.sweep_timers(bot)
    row = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                   active_only=False)
    ours = next(r for r in row if r["id"] == rec["id"])
    assert ours["state"] == "released"
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_deafen_lifecycle_same_as_mute(env):
    """Deafen проходит тот же durable lifecycle."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    rec = await vc.apply_restriction(target, ch, "server_deafen",
                                     vc.ORIGIN_MANUAL, actor=actor)
    assert target.voice.deaf
    await vc.release_restriction(rec["restriction"], actor=actor)
    assert not target.voice.deaf


# ----------------------------------------------------------------------
# P0.6 — Restriction Stacking
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timer_plus_speaker_mode_stacking(env):
    """7. Временный мут + speaker mode: истечение таймера не
    размучивает при активном режиме ведущего."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)
    speaker = make_member(80, g, ch)

    past = datetime.now(UTC) - timedelta(seconds=1)
    await vc.apply_restriction(
        target, ch, "server_mute", vc.ORIGIN_TIMER, actor_id=ACTOR_ID,
        expires_at=past)
    assert target.voice.mute
    await vc.start_speaker_mode(ch, actor, speakers=[speaker])
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=True)
    assert len([r for r in rows if r["kind"] == "server_mute"]) >= 2

    bot = NS(get_guild=lambda gid: g)
    stats = await vc.sweep_timers(bot)
    assert stats["expired"] >= 1
    assert target.voice.mute

    await vc.stop_speaker_mode(ch, actor)
    await vc.sweep_timers(bot)
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_transition_between_two_managed_channels(env):
    """8. Переход между двумя управляемыми войсами: флаг —
    guild-level, ограничение канала A остаётся валидным."""
    g = env.guild
    ch_a = make_channel(10, g, "A")
    ch_b = make_channel(11, g, "B")
    actor = make_member(ACTOR_ID, g, ch_a)
    target = make_member(70, g, ch_a)

    await vc.apply_restriction(target, ch_a, "server_mute",
                               vc.ORIGIN_MANUAL, actor=actor)
    await target.move_to(ch_b)
    assert target.voice.mute
    await vc.reconcile_member(target)
    assert target.voice.mute
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=True)
    assert any(r["channel_id"] == ch_a.id for r in rows)

    await target.move_to(ch_a)
    await vc.reconcile_member(target)
    assert target.voice.mute


# ----------------------------------------------------------------------
# P0.7 — Feature flag & emergency cleanup
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flag_off_blocks_new_ops_but_sweep_safe(env, monkeypatch):
    """18/19. Feature flag OFF: bulk/apply/auto-mute запрещены,
    автомут новых участников не создаётся, sweep не восстанавливает
    принудительно снятые флаги."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_MANUAL, actor=actor)
    assert target.voice.mute

    monkeypatch.setattr(config, "VOICE_CONTROL_ENABLED", False)

    assert (await vc.bulk_mute(ch, actor)).get("error") == "disabled"
    assert (await vc.start_speaker_mode(ch, actor)).get("error") == "disabled"

    await vc.db.put_mode(GUILD_ID, ch.id, auto_mute=True)
    newcomer = make_member(71, g, ch)
    assert await vc.apply_auto_mute(newcomer, ch) is None
    assert not newcomer.voice.mute

    target.voice.mute = False
    bot = NS(get_guild=lambda gid: g)
    await vc.sweep_timers(bot)
    assert not target.voice.mute


@pytest.mark.asyncio
async def test_emergency_cleanup_dry_run_and_execute(env):
    """P0.7: owner emergency cleanup — dry-run отчёт, затем
    выполнение завершает режимы и снимает ограничения."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    listener = make_member(80, g, ch)
    speaker = make_member(81, g, ch)

    await vc.start_speaker_mode(ch, actor, speakers=[speaker])
    assert listener.voice.mute
    await vc.close_voice(ch, actor)

    dry = await vc.emergency_cleanup(g, actor, dry_run=True)
    assert dry["dry_run"] and dry["channels_with_modes"] >= 1
    assert dry["active_restrictions"] >= 1
    assert listener.voice.mute

    await vc.emergency_cleanup(g, actor, dry_run=False)
    assert not listener.voice.mute
    mode = await vc.db.mode(GUILD_ID, ch.id)
    assert not mode or not (mode.get("speaker_mode") or mode.get("closed"))
    states = await vc.db.pending_states()
    assert states.get("pending_release", 0) == 0


# ----------------------------------------------------------------------
# P0.9 — Concurrency & reconciliation
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scan_active_paginates_over_500(env):
    """20. Более 500 записей: курсорная пагинация без starvation."""
    g = env.guild
    ch = make_channel(10, g, "Big")
    make_member(ACTOR_ID, g, ch)
    for i in range(550):
        uid = 5000 + i
        m = make_member(uid, g, ch)
        await vc.db.add_restriction(
            GUILD_ID, ch.id, uid, "server_mute", vc.ORIGIN_MANUAL,
            created_by=ACTOR_ID, state="active")
        m.voice.mute = False

    bot = NS(get_guild=lambda gid: g)
    # Каждый sweep обрабатывает партию SWEEP_BATCH=100 по курсору —
    # несколько проходов покрывают все 550 без starvation.
    for _ in range(8):
        await vc.sweep_timers(bot)
    total = sum(1 for i in range(550)
                if g.members[5000 + i].voice.mute)
    assert total == 550


@pytest.mark.asyncio
async def test_claim_pending_exclusive(env):
    """Durable claims: две «машины» не берут одну запись."""
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch, muted=True)
    await vc.db.add_restriction(
        GUILD_ID, ch.id, target.id, "server_mute", vc.ORIGIN_MANUAL,
        created_by=ACTOR_ID, state="pending_apply")

    batch1 = await vc.db.claim_pending("pending_apply", "w1", limit=10)
    assert len(batch1) == 1
    batch2 = await vc.db.claim_pending("pending_apply", "w2", limit=10)
    assert batch2 == []
    await vc.db.release_claim(batch1[0]["id"])
    batch3 = await vc.db.claim_pending("pending_apply", "w2", limit=10)
    assert len(batch3) == 1


@pytest.mark.asyncio
async def test_bulk_mute_100_members(env):
    """17. Массовый мут 100 участников."""
    g = env.guild
    ch = make_channel(10, g, "Big")
    actor = make_member(ACTOR_ID, g, ch)
    members = [make_member(6000 + i, g, ch) for i in range(100)]

    result = await vc.bulk_mute(ch, actor)
    assert result["done"] == 100
    assert all(m.voice.mute for m in members)
    rows = await vc.db.restrictions(GUILD_ID, channel_id=ch.id)
    assert len(rows) == 100


# ----------------------------------------------------------------------
# P1 — protected users/roles на индивидуальных действиях
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_protected_user_and_role_blocked(env):
    """21. Защищённые участники при индивидуальных действиях."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    prot_user = make_member(70, g, ch)
    prot_role = make_member(71, g, ch, roles=[make_role(33)])
    await vc.db.put_settings(GUILD_ID, protected_user_ids=[70],
                             protected_role_ids=[33])

    with pytest.raises(vc.VoiceControlError):
        await vc.apply_restriction(prot_user, ch, "server_mute",
                                   vc.ORIGIN_MANUAL, actor=actor)
    with pytest.raises(vc.VoiceControlError):
        await vc.apply_restriction(prot_role, ch, "server_mute",
                                   vc.ORIGIN_MANUAL, actor=actor)
    with pytest.raises(vc.VoiceControlError):
        await vc.disconnect_member(prot_user, actor)
    with pytest.raises(vc.VoiceControlError):
        await vc.move_member(prot_role, make_channel(12, g, "D"), actor)


@pytest.mark.asyncio
async def test_apply_restriction_dedup_and_stack(env):
    """Одинаковый origin — дубль; разные origin — стек."""
    g = env.guild
    ch = make_channel(10, g, "General")
    actor = make_member(ACTOR_ID, g, ch)
    target = make_member(70, g, ch)

    await vc.apply_restriction(target, ch, "server_mute",
                               vc.ORIGIN_MANUAL, actor=actor)
    r2 = await vc.apply_restriction(target, ch, "server_mute",
                                    vc.ORIGIN_MANUAL, actor=actor)
    assert r2.get("skipped") == "already_restricted"
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=True)
    assert len(rows) == 1

    r3 = await vc.apply_restriction(target, ch, "server_mute",
                                    vc.ORIGIN_TIMER, actor=actor)
    assert not r3.get("skipped")
    rows = await vc.db.restrictions(GUILD_ID, user_id=target.id,
                                    active_only=True)
    assert len(rows) == 2


# ----------------------------------------------------------------------
# P0.3 — Panel session integrity (cog-уровень, без Discord)
# ----------------------------------------------------------------------
def _fake_interaction(guild, user, message_id=None):
    return NS(guild=guild, user=user,
              message=NS(id=message_id) if message_id else None,
              response=NS(is_done=lambda: False),
              type=None)


@pytest.mark.asyncio
async def test_panel_invalidation_on_rerun(env):
    """12. Старая кнопка после повторного /voice не работает."""
    from cogs.voice_control import PanelState, VoiceControlCog
    cog = VoiceControlCog(NS())
    g = env.guild
    user = make_member(ACTOR_ID, g)
    p1 = PanelState(GUILD_ID, ACTOR_ID)
    p1.message_id = 111
    p2 = PanelState(GUILD_ID, ACTOR_ID)
    p2.message_id = 222
    cog.panels[(GUILD_ID, ACTOR_ID)] = p2
    p1.invalidate()

    assert cog._panel_for(_fake_interaction(g, user, 111)) is None
    assert cog._panel_for(_fake_interaction(g, user, 222)) is p2


@pytest.mark.asyncio
async def test_panel_ttl_expiry(env):
    """14. Истечение TTL панели."""
    from cogs.voice_control import PANEL_TTL, PanelState
    p = PanelState(GUILD_ID, ACTOR_ID)
    p.last_touch = time.monotonic() - PANEL_TTL - 1
    assert p.expired()
    p.touch()
    assert not p.expired()


@pytest.mark.asyncio
async def test_confirm_snapshot_consumed_once(env):
    """16. Повторное нажатие подтверждения не срабатывает."""
    from cogs.voice_control import PanelState, VoiceControlCog
    cog = VoiceControlCog(NS())
    p = PanelState(GUILD_ID, ACTOR_ID)
    cog._begin_confirm(p, "move", {"channel_id": 1, "user_ids": [2]})
    snap = cog._consume_confirm(p, "move")
    assert snap == {"channel_id": 1, "user_ids": [2]}
    assert cog._consume_confirm(p, "move") is None
    cog._begin_confirm(p, "close", {"channel_id": 1})
    assert cog._consume_confirm(p, "move") is None


@pytest.mark.asyncio
async def test_modal_binding_rejects_stale_panel(env):
    """13. Устаревшая modal после смены канала/панели."""
    from cogs.voice_control import PanelState, RenameModal, VoiceControlCog
    cog = VoiceControlCog(NS())
    g = env.guild
    user = make_member(ACTOR_ID, g)
    panel = PanelState(GUILD_ID, ACTOR_ID)
    panel.channel_id = 10
    cog.panels[(GUILD_ID, ACTOR_ID)] = panel
    modal = RenameModal(panel, cog)

    inter = _fake_interaction(g, user)
    inter.response = NS(is_done=lambda: False,
                        send_message=AsyncMock())
    ch = make_channel(10, g, "General")
    cog.bot = NS(get_channel=lambda cid: ch if cid == 10 else None,
                 get_guild=lambda gid: g)
    got = await modal._channel_still_valid(inter, panel)
    assert got is ch
    panel.channel_id = 11
    got = await modal._channel_still_valid(inter, panel)
    assert got is None
    panel.invalidate()
    assert not await modal._panel_alive(inter)


@pytest.mark.asyncio
async def test_real_discord_components(env):
    """23. Реальные discord.py LayoutView/Button/Select/Modal —
    не duck-typed: проверяем лимиты и callback'и."""
    from cogs.voice_control import (
        PanelState,
        ProtectModal,
        RenameModal,
        VoiceControlCog,
    )
    from utils.design.layouts import DesignView
    cog = VoiceControlCog(NS())
    g = env.guild
    ch = make_channel(10, g, "General")
    make_member(ACTOR_ID, g, ch)
    cog.bot = NS(get_guild=lambda gid: g,
                 get_channel=lambda cid: next(
                     (c for c in g.channels if c.id == cid), None))
    panel = PanelState(GUILD_ID, ACTOR_ID)
    panel.channel_id = ch.id
    panel.guild_id = GUILD_ID

    view = await cog._render(panel)
    assert isinstance(view, DesignView)
    flat = list(view.walk_children())
    assert len(flat) <= 40
    buttons = [c for c in flat if isinstance(c, discord.ui.Button)]
    assert buttons and all(b.callback for b in buttons)

    rm = RenameModal(panel, cog)
    assert rm.title and isinstance(rm, discord.ui.Modal)
    pm = ProtectModal(panel, cog, "user")
    assert pm.title and len(pm.children) == 1
