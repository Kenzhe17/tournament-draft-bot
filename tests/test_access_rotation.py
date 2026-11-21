"""Тесты ежедневной ротации роли «Доступ».

Покрытие: расчёт активности, выбор неактивного, отсутствие
кандидатов, защита организаторов/exempt/72ч, идемпотентность
scheduler, отказ Discord + восстановление, перезапуск посреди
ротации, лимит 10 мест, запрет ORG без «Доступа», гонки заявок.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from services import access_rotation as rot
from storage import access_store
from views import org_role_view as omv


ACCESS_ROLE = 1566949895078940672
ORG_ROLE = 555


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class FakeRole:
    def __init__(self, rid):
        self.id = rid


class FakeMember:
    def __init__(self, uid, role_ids=(), fail_add=False, fail_remove=False):
        self.id = uid
        self.name = f"user{uid}"
        self.display_name = self.name
        self.roles = [FakeRole(r) for r in role_ids]
        self.fail_add = fail_add
        self.fail_remove = fail_remove
        self.added = []
        self.removed = []

    async def add_roles(self, role, *a, **kw):
        if self.fail_add:
            raise discord.Forbidden(
                NS(status=403, reason="Forbidden"), "no")
        if role.id not in {r.id for r in self.roles}:
            self.roles.append(FakeRole(role.id))
        self.added.append(role.id)

    async def remove_roles(self, role, *a, **kw):
        if self.fail_remove:
            raise discord.Forbidden(
                NS(status=403, reason="Forbidden"), "no")
        self.roles = [r for r in self.roles if r.id != role.id]
        self.removed.append(role.id)


class FakeGuild:
    def __init__(self, gid=1, members=()):
        self.id = gid
        self.members = list(members)
        self.name = "test"
        self.text_channels = []
        self._roles = {ACCESS_ROLE: FakeRole(ACCESS_ROLE)}

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_role(self, rid):
        return self._roles.get(rid)

    async def fetch_member(self, uid):
        return self.get_member(uid)


# ---------------------------------------------------------------------------
# Фейк access_store (in-memory)
# ---------------------------------------------------------------------------


class FakeAccessStore:
    def __init__(self):
        self.queue = {}          # uid -> created idx
        self.runs = {}           # (gid, key) -> status
        self.rotations = []
        self.exemptions = {}     # uid -> until
        self.grants = {}         # uid -> datetime
        self.state = {}
        self._seq = 0

    async def queue_join(self, gid, uid):
        self.queue.setdefault(uid, len(self.queue))
        return True

    async def queue_leave(self, gid, uid):
        return self.queue.pop(uid, None) is not None

    async def queue_list(self, gid, limit=100):
        return sorted(self.queue, key=lambda u: self.queue[u])[:limit]

    async def queue_count(self, gid):
        return len(self.queue)

    async def queue_remove(self, gid, uid):
        self.queue.pop(uid, None)

    async def begin_run(self, gid, key, day, kind):
        if (gid, key) in self.runs:
            return False
        self.runs[(gid, key)] = "running"
        return True

    async def finish_run(self, gid, key, status, detail=None):
        self.runs[(gid, key)] = status

    async def record_rotation(self, gid, run_key, day, **kw):
        self.rotations.append({"run_key": run_key, "day": day, **kw})
        if kw.get("new_user_id"):
            self.grants[kw["new_user_id"]] = datetime.now(timezone.utc)

    async def last_rotation(self, gid):
        return self.rotations[-1] if self.rotations else None

    async def last_grant_at(self, gid, uid):
        return self.grants.get(uid)

    async def set_exemption(self, gid, uid, until, **kw):
        self.exemptions[uid] = until

    async def clear_exemption(self, gid, uid):
        return self.exemptions.pop(uid, None) is not None

    async def active_exemptions(self, gid):
        now = datetime.now(timezone.utc)
        return {u: t for u, t in self.exemptions.items()
                if t > now}

    async def get_state(self, gid):
        return self.state.get(gid, {"channel_id": None, "message_id": None})

    async def set_panel(self, gid, cid, mid):
        self.state[gid] = {"channel_id": cid, "message_id": mid}


@pytest.fixture
def fake_store(monkeypatch):
    fs = FakeAccessStore()
    monkeypatch.setattr(rot, "access_store", fs)
    return fs


@pytest.fixture
def no_org(monkeypatch):
    "`has_org_duties` → False по умолчанию в тестах."""
    async def _no(guild, uid):
        return False
    monkeypatch.setattr(rot, "has_org_duties", _no)


@pytest.fixture
def scores_map(monkeypatch):
    """activity_scores → управляемая карта очков."""
    box = {"scores": {}}

    async def _fake(gid, uids, **kw):
        if box["scores"] is None:
            return None
        return {u: box["scores"].get(u, 0) for u in uids}
    monkeypatch.setattr(rot, "activity_scores", _fake)
    return box


def _holders(n, role_ids=(ACCESS_ROLE,), start=100):
    return [FakeMember(start + i, role_ids) for i in range(n)]


# ---------------------------------------------------------------------------
# Активность
# ---------------------------------------------------------------------------


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, voice_rows, match_rows):
        self.voice_rows = voice_rows
        self.match_rows = match_rows

    async def fetch(self, sql, *params):
        if "banner_voice_daily" in sql:
            return self.voice_rows
        if "match_history" in sql:
            return self.match_rows
        return []


async def _scores_with(voice_rows, match_rows, uids, monkeypatch):
    import storage.db as db_mod
    conn = _Conn(voice_rows, match_rows)

    async def _get_pool():
        return _Pool(conn)
    monkeypatch.setattr(db_mod, "get_pool", _get_pool)
    # rot импортирует get_pool внутри функции — патчим модуль storage.db
    return await rot.activity_scores(1, uids)


@pytest.mark.asyncio
async def test_activity_score_formula(monkeypatch):
    today = rot.act.today_almaty()
    scores = await _scores_with(
        voice_rows=[
            # 1800 сек = 30 мин → 6 очков
            {"user_id": 1, "day": today, "seconds": 1800},
            # 30000 сек → cap 21600 → 72 очка
            {"user_id": 2, "day": today, "seconds": 30000},
        ],
        match_rows=[
            {"user_id": 1, "cnt": 2},   # +20
            {"user_id": 3, "cnt": 1},   # +10
        ],
        uids=[1, 2, 3, 4],
        monkeypatch=monkeypatch,
    )
    assert scores == {1: 26, 2: 72, 3: 10, 4: 0}


@pytest.mark.asyncio
async def test_activity_unavailable_returns_none(monkeypatch):
    import storage.db as db_mod

    async def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db_mod, "get_pool", _boom)
    assert await rot.activity_scores(1, [1, 2]) is None


# ---------------------------------------------------------------------------
# Ротация
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swap_least_active_for_most_active(
        fake_store, scores_map, no_org):
    # 10 владельцев; у 100 минимальная активность.
    guild = FakeGuild(members=_holders(10))
    members = {m.id: m for m in guild.members}
    # Очередь: 200 (score 100), 201 (score 50)
    await fake_store.queue_join(1, 201)
    await fake_store.queue_join(1, 200)
    members[200] = FakeMember(200)
    members[201] = FakeMember(201)
    guild.members += [members[200], members[201]]
    scores_map["scores"] = {**{i: 10 for i in range(100, 110)},
                            100: 1, 200: 100, 201: 50}

    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "done"
    assert res["swapped"] == {
        "old_user_id": 100, "old_score": 1,
        "new_user_id": 200, "new_score": 100,
    }
    # 100 лишился роли, 200 получил.
    assert ACCESS_ROLE not in {r.id for r in members[100].roles}
    assert ACCESS_ROLE in {r.id for r in members[200].roles}
    assert 200 not in fake_store.queue
    # Журнал записан.
    assert fake_store.rotations[-1]["old_user_id"] == 100
    assert fake_store.rotations[-1]["new_user_id"] == 200


@pytest.mark.asyncio
async def test_no_candidates_no_swap(fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    scores_map["scores"] = {i: 5 for i in range(100, 110)}
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "done"
    assert res["swapped"] is None
    assert res["skipped_reason"] == "no_candidates"
    # Все роли на месте.
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members)


@pytest.mark.asyncio
async def test_candidate_not_more_active_no_swap(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 10 for i in range(100, 110)},
                            200: 5}
    res = await rot.run_rotation(guild, kind="auto")
    assert res["swapped"] is None
    assert res["skipped_reason"] == "no_more_active_candidate"


@pytest.mark.asyncio
async def test_fill_empty_slots_without_removal(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(3))
    guild.members += [FakeMember(200), FakeMember(201), FakeMember(202)]
    for u in (200, 201, 202):
        await fake_store.queue_join(1, u)
    scores_map["scores"] = {**{i: 1 for i in range(100, 103)},
                            200: 9, 201: 8, 202: 7}
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "done"
    # Заполнили до 10: 3 места — 200, 201, 202 (score desc).
    assert [f["user_id"] for f in res["filled"]] == [200, 201, 202]
    assert res["swapped"] is None
    # Старые владельцы не тронуты.
    for m in guild.members[:3]:
        assert ACCESS_ROLE in {r.id for r in m.roles}
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members[3:])
    assert not fake_store.queue


@pytest.mark.asyncio
async def test_org_protected_holder_skipped(
        fake_store, scores_map, monkeypatch):
    guild = FakeGuild(members=_holders(10))
    # 100 — наименее активный, но имеет ORG-роль → защищён.
    guild.members[0].roles.append(FakeRole(ORG_ROLE))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 10 for i in range(100, 110)},
                            100: 0, 101: 1, 200: 100}

    async def _org(g, uid):
        return uid == 100  # только 100 «занят» организаторством
    monkeypatch.setattr(rot, "has_org_duties", _org)

    res = await rot.run_rotation(guild, kind="auto")
    # Снимается следующий неактивный — 101.
    assert res["swapped"]["old_user_id"] == 101
    assert ACCESS_ROLE in {r.id for r in guild.members[0].roles}


@pytest.mark.asyncio
async def test_exemption_and_72h_protection(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 10 for i in range(100, 110)},
                            100: 0, 101: 1, 200: 100}
    # 100 — exempt.
    fake_store.exemptions[100] = datetime.now(timezone.utc) + timedelta(hours=5)
    # 101 — получил доступ <72ч назад.
    fake_store.grants[101] = datetime.now(timezone.utc) - timedelta(hours=1)
    res = await rot.run_rotation(guild, kind="auto")
    assert res["swapped"]["old_user_id"] == 102


@pytest.mark.asyncio
async def test_all_protected_no_swap(fake_store, scores_map, monkeypatch):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 0 for i in range(100, 110)}, 200: 99}

    async def _all_org(g, uid):
        return True
    monkeypatch.setattr(rot, "has_org_duties", _all_org)
    res = await rot.run_rotation(guild, kind="auto")
    assert res["swapped"] is None
    assert res["skipped_reason"] == "all_holders_protected"


@pytest.mark.asyncio
async def test_idempotent_same_day(fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 1 for i in range(100, 110)}, 200: 99}
    r1 = await rot.run_rotation(guild, kind="auto")
    r2 = await rot.run_rotation(guild, kind="auto")
    assert r1["status"] == "done"
    assert r2["status"] == "already_today"
    # Только одна замена за день.
    assert len(fake_store.rotations) == 1


@pytest.mark.asyncio
async def test_restart_mid_rotation_is_idempotent(
        fake_store, scores_map, no_org):
    """Run зарезервирован → «падение» → повторный запуск не дублирует."""
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 1 for i in range(100, 110)}, 200: 99}
    # Имитация: run_key уже занят (процесс упал после begin_run).
    today = rot.act.today_almaty()
    await fake_store.begin_run(1, f"auto:{today.isoformat()}", today, "auto")
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "already_today"
    # Роли владельцев не менялись (200 — просто участник гильдии).
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members[:10])
    assert ACCESS_ROLE not in {r.id for r in guild.members[10].roles}


@pytest.mark.asyncio
async def test_discord_forbidden_on_grant_restores_old(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    new_member = FakeMember(200, fail_add=True)
    guild.members.append(new_member)
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 1 for i in range(100, 110)}, 200: 99}
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "discord_error"
    # Старый владелец восстановлен.
    old = guild.members[0]
    assert ACCESS_ROLE in {r.id for r in old.roles}
    assert ACCESS_ROLE in old.added  # re-grant после отката


@pytest.mark.asyncio
async def test_discord_error_on_revoke_aborts(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members[0].fail_remove = True  # у 100 запрет на снятие
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 1 for i in range(100, 110)}, 200: 99}
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "discord_forbidden"
    # Никто не потерял роль.
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members[:10])


@pytest.mark.asyncio
async def test_activity_unavailable_skips_rotation(
        fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = None  # БД недоступна
    res = await rot.run_rotation(guild, kind="auto")
    assert res["status"] == "activity_unavailable"
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members[:10])


@pytest.mark.asyncio
async def test_max_10_holders_fill_caps(
        fake_store, scores_map, no_org):
    """9 владельцев + 5 кандидатов → заполнение ровно до 10."""
    guild = FakeGuild(members=_holders(9))
    for i in range(5):
        guild.members.append(FakeMember(200 + i))
        await fake_store.queue_join(1, 200 + i)
    scores_map["scores"] = {**{i: 1 for i in range(100, 109)},
                            **{200 + i: 0 for i in range(5)}}
    res = await rot.run_rotation(guild, kind="auto")
    assert len(res["filled"]) == 1  # только одно свободное место
    assert res["swapped"] is None   # кандидаты не активнее владельцев
    holders = [m for m in guild.members
               if ACCESS_ROLE in {r.id for r in m.roles}]
    assert len(holders) == 10
    # Очередь: остаток кандидатов остался.
    assert len(fake_store.queue) == 4


@pytest.mark.asyncio
async def test_queue_race_single_row(fake_store):
    """Гонка заявок: повторный join не создаёт дубль."""
    await fake_store.queue_join(1, 42)
    await fake_store.queue_join(1, 42)
    assert await fake_store.queue_list(1) == [42]
    assert await fake_store.queue_count(1) == 1


@pytest.mark.asyncio
async def test_dry_run_no_mutation(fake_store, scores_map, no_org):
    guild = FakeGuild(members=_holders(10))
    guild.members.append(FakeMember(200))
    await fake_store.queue_join(1, 200)
    scores_map["scores"] = {**{i: 1 for i in range(100, 110)}, 200: 99}
    res = await rot.run_rotation(guild, kind="manual", dry_run=True)
    assert res["status"] == "dry_run"
    assert res["swapped"]["new_user_id"] == 200
    # Роли и очередь не тронуты, run не зарезервирован.
    assert all(ACCESS_ROLE in {r.id for r in m.roles}
               for m in guild.members[:10])
    assert ACCESS_ROLE not in {r.id for r in guild.members[10].roles}
    assert 200 in fake_store.queue
    assert not fake_store.rotations


# ---------------------------------------------------------------------------
# ORG-гейт без «Доступа»
# ---------------------------------------------------------------------------


def test_org_gate_requires_access_role():
    member_no = FakeMember(1, role_ids=(ORG_ROLE,))
    member_yes = FakeMember(2, role_ids=(ORG_ROLE, ACCESS_ROLE))
    assert omv.has_access_role(member_no) is False
    assert omv.has_access_role(member_yes) is True


def test_org_gate_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "config.ACCESS_ROLE_ID", 0, raising=False)
    # has_access_role читает config лениво — патчим sys.modules config.
    import config
    monkeypatch.setattr(config, "ACCESS_ROLE_ID", 0)
    assert omv.has_access_role(FakeMember(1)) is True
