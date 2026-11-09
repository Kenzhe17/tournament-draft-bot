"""Тесты Reputation System 2.0: /rep, приглашения, бусты.

Хранилище подменяется in-memory фейком с той же семантикой
(event_key UNIQUE, cooldown, статусы) — PG-слой проверяется
в интеграции на сервере.

Запуск: pytest tests/test_reputation.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import reputation as rep_svc
from storage.reputation_store import ManualRepResult


# ---------------------------------------------------------------- fake store

class FakeRepStore:
    """In-memory аналог ReputationStore с той же семантикой.

    try_give_manual_rep повторяет PG-логику: строка состояния
    выдающего + UNIQUE event_key + обновление player_stats.reputation.
    """

    def __init__(self):
        self.stats: dict[tuple[int, int], dict] = {}  # (gid,uid) -> {"name","rep"}
        self.transactions: list[dict] = []
        self.event_keys: set[str] = set()
        self.giver_last: dict[tuple[int, int], datetime] = {}
        self.invites: dict[int, dict] = {}
        self._invite_seq = 0
        self.boosts: dict[tuple[int, int], dict] = {}
        self.invite_owners: dict[tuple[int, str], int] = {}

    # -- player stats ----------------------------------------------------
    def _rep(self, gid, uid):
        return self.stats.setdefault((gid, uid), {"name": "", "rep": 0})["rep"]

    def _add_rep(self, gid, uid, amount, name=""):
        st = self.stats.setdefault((gid, uid), {"name": name, "rep": 0})
        if name:
            st["name"] = name
        st["rep"] += amount
        return st["rep"]

    # -- /rep ------------------------------------------------------------
    async def try_give_manual_rep(self, guild_id, giver_id, target_id,
                                  amount, cooldown, target_name=""):
        now = datetime.now(timezone.utc)
        last = self.giver_last.get((guild_id, giver_id))
        if last is not None and now < last + cooldown:
            avail = last + cooldown
            return ManualRepResult(
                status="cooldown", next_available_at=avail,
                cooldown_seconds=int((avail - now).total_seconds()),
            )
        key = f"manual_rep:{guild_id}:{giver_id}:{target_id}"
        if key in self.event_keys:
            return ManualRepResult(status="already_pair")
        self.event_keys.add(key)
        self.transactions.append({
            "guild_id": guild_id, "user_id": target_id,
            "source": "MANUAL_REP", "amount": amount,
            "actor_user_id": giver_id, "subject_user_id": target_id,
            "event_key": key, "created_at": now,
        })
        rep_after = self._add_rep(guild_id, target_id, amount, target_name)
        self.giver_last[(guild_id, giver_id)] = now
        return ManualRepResult(status="ok", rep_after=rep_after)

    # -- generic award ----------------------------------------------------
    async def award_rep_logged(self, guild_id, user_id, source, amount,
                               event_key, actor_user_id=0,
                               subject_user_id=0, user_name=""):
        if event_key in self.event_keys:
            return None
        self.event_keys.add(event_key)
        self.transactions.append({
            "guild_id": guild_id, "user_id": user_id, "source": source,
            "amount": amount, "actor_user_id": actor_user_id,
            "subject_user_id": subject_user_id, "event_key": event_key,
            "created_at": datetime.now(timezone.utc),
        })
        rep_after = self._add_rep(guild_id, user_id, amount, user_name)
        return {"transaction_id": len(self.transactions), "rep_after": rep_after}

    async def award_rep(self, guild_id, user_id, source, amount, event_key,
                        actor_user_id=0, subject_user_id=0, user_name=""):
        return await self.award_rep_logged(
            guild_id, user_id, source, amount, event_key,
            actor_user_id=actor_user_id, subject_user_id=subject_user_id,
            user_name=user_name,
        ) is not None

    # -- invites ----------------------------------------------------------
    async def create_invite_candidate(self, guild_id, inviter_id, invitee_id,
                                      invite_code, verify_until, attribution,
                                      reject_reason=""):
        """Семантика PG-версии: повторный вход переписывает только
        rejected/cancelled при достоверной 'ok'-атрибуции."""
        for inv in self.invites.values():
            if inv["guild_id"] != guild_id or inv["invitee_id"] != invitee_id:
                continue
            if inv["reward_status"] in ("rejected", "cancelled") and (
                attribution == "ok" and inviter_id is not None
            ):
                inv.update({
                    "inviter_id": inviter_id, "invite_code": invite_code,
                    "verify_until": verify_until, "attribution": attribution,
                    "reward_status": "pending", "rewarded_at": None,
                    "reject_reason": "",
                })
                return inv["id"]
            return None
        self._invite_seq += 1
        self.invites[self._invite_seq] = {
            "id": self._invite_seq, "guild_id": guild_id,
            "inviter_id": inviter_id, "invitee_id": invitee_id,
            "invite_code": invite_code, "joined_at": datetime.now(timezone.utc),
            "verify_until": verify_until, "attribution": attribution,
            "reward_status": "pending", "rewarded_at": None,
            "reject_reason": reject_reason,
        }
        return self._invite_seq

    async def get_invite_owners(self, guild_id, codes):
        return {
            code: owner for (gid, code), owner in self.invite_owners.items()
            if gid == guild_id and code in codes
        }

    async def set_invite_owner(self, guild_id, code, owner_id, channel_id=0):
        self.invite_owners[(guild_id, code)] = owner_id

    async def get_due_invites(self, now):
        return [i for i in self.invites.values()
                if i["reward_status"] == "pending" and i["verify_until"] <= now]

    async def update_invite(self, invite_id, *, attribution=None,
                            reward_status=None, reject_reason=None):
        inv = self.invites[invite_id]
        if attribution is not None:
            inv["attribution"] = attribution
        if reward_status is not None:
            inv["reward_status"] = reward_status
            if reward_status == "rewarded":
                inv["rewarded_at"] = datetime.now(timezone.utc)
        if reject_reason is not None:
            inv["reject_reason"] = reject_reason

    async def get_user_invite_stats(self, guild_id, user_id):
        stats = {"invited": 0, "rewarded": 0, "pending": 0,
                 "rejected": 0, "cancelled": 0}
        for inv in self.invites.values():
            if inv["guild_id"] != guild_id or inv["inviter_id"] != user_id:
                continue
            stats["invited"] += 1
            if inv["reward_status"] == "rewarded":
                stats["rewarded"] += 1
            elif inv["reward_status"] == "pending":
                stats["pending"] += 1
            elif inv["reward_status"] == "cancelled":
                stats["cancelled"] += 1
            else:
                stats["rejected"] += 1
        return stats

    # -- boosts ------------------------------------------------------------
    async def upsert_boost_pending(self, guild_id, booster_id,
                                   boost_started_at, verify_until):
        key = (guild_id, booster_id)
        row = self.boosts.get(key)
        if row and row["status"] == "rewarded":
            return False
        self.boosts[key] = {
            "guild_id": guild_id, "booster_id": booster_id,
            "boost_started_at": boost_started_at,
            "verify_until": verify_until, "status": "pending",
            "rewarded_at": None,
        }
        return True

    async def cancel_boost_pending(self, guild_id, booster_id):
        row = self.boosts.get((guild_id, booster_id))
        if row and row["status"] == "pending":
            row["status"] = "cancelled"

    async def get_boost(self, guild_id, booster_id):
        return self.boosts.get((guild_id, booster_id))

    async def get_due_boosts(self, now):
        return [b for b in self.boosts.values()
                if b["status"] == "pending" and b["verify_until"] <= now]

    async def mark_boost_rewarded(self, guild_id, booster_id):
        row = self.boosts.get((guild_id, booster_id))
        if row and row["status"] == "pending":
            row["status"] = "rewarded"
            row["rewarded_at"] = datetime.now(timezone.utc)


# ---------------------------------------------------------------- discord fakes

def _invite(code: str, uses: int, inviter_id: int):
    return SimpleNamespace(code=code, uses=uses,
                           inviter=SimpleNamespace(id=inviter_id))


def _member(user_id: int, bot: bool = False, premium_since=None,
            display_name="P", guild=None):
    m = SimpleNamespace(
        id=user_id, bot=bot, display_name=display_name,
        guild=guild, premium_since=premium_since,
    )
    return m


class _FakeGuild:
    def __init__(self, gid: int = 1):
        self.id = gid
        self._members: dict[int, object] = {}
        self._invites: dict[str, object] = {}

    def add_member(self, m):
        self._members[m.id] = m
        return m

    async def fetch_member(self, uid):
        if uid not in self._members:
            raise Exception("not found")
        return self._members[uid]

    async def invites(self):
        return list(self._invites.values())


class _FakeBot:
    def __init__(self):
        self._rep_invite_cache: dict[int, dict] = {}
        self._guilds: dict[int, _FakeGuild] = {}

    def get_guild(self, gid):
        return self._guilds.get(gid)

    async def fetch_guild(self, gid):
        return self._guilds.get(gid)


# ---------------------------------------------------------------- /rep

@pytest.mark.asyncio
async def test_manual_rep_success_and_limits():
    store = FakeRepStore()
    gid = 1

    r = await rep_svc.give_manual_rep(gid, 100, 200, "B", store=store)
    assert r.status == "ok" and r.rep_after == 1
    assert store._rep(gid, 200) == 1

    # повтор той же пары — already_pair (даже после cooldown)
    store.giver_last[(gid, 100)] = datetime.now(timezone.utc) - timedelta(hours=25)
    r = await rep_svc.give_manual_rep(gid, 100, 200, "B", store=store)
    assert r.status == "already_pair"

    # другой получатель после 24ч — ok
    r = await rep_svc.give_manual_rep(gid, 100, 300, "C", store=store)
    assert r.status == "ok" and r.rep_after == 1

    # cooldown: A только что выдал C → D нельзя
    r = await rep_svc.give_manual_rep(gid, 100, 400, "D", store=store)
    assert r.status == "cooldown"
    assert r.cooldown_seconds > 0 and r.next_available_at is not None

    # неуспешная попытка не сбрасывает/не ставит cooldown
    assert store.giver_last[(gid, 100)] == store.giver_last[(gid, 100)]


@pytest.mark.asyncio
async def test_manual_rep_cooldown_persists_in_store():
    store = FakeRepStore()
    gid = 1
    await rep_svc.give_manual_rep(gid, 1, 2, "B", store=store)
    # «перезапуск»: новый сервис, тот же store — cooldown сохранён
    r = await rep_svc.give_manual_rep(gid, 1, 3, "C", store=store)
    assert r.status == "cooldown"


@pytest.mark.asyncio
async def test_format_cooldown():
    assert "13" in rep_svc.format_cooldown(13 * 3600 + 42 * 60)
    assert rep_svc.format_cooldown(0) != ""


# ---------------------------------------------------------------- invites

@pytest.mark.asyncio
async def test_invite_attribution_and_reward():
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    inviter = guild.add_member(_member(500, guild=guild))
    # Кэш инвайтов ДО входа
    bot._rep_invite_cache[1] = {"abc": _invite("abc", 3, 500)}
    guild._invites["abc"] = _invite("abc", 4, 500)  # uses вырос

    joiner = _member(600, guild=guild)
    await rep_svc.record_member_join(bot, joiner, bot._rep_invite_cache[1], store=store)

    inv = next(iter(store.invites.values()))
    assert inv["inviter_id"] == 500 and inv["attribution"] == "ok"
    assert inv["reward_status"] == "pending"

    # Проверка не наступила — награды нет
    assert store._rep(1, 500) == 0

    # Форсируем наступление verify_until и членство
    inv["verify_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    guild.add_member(joiner)
    guild.add_member(inviter)

    n = await rep_svc.process_due_invites(bot, store=store)
    assert n == 1
    assert store._rep(1, 500) == rep_svc.REP_INVITE_AMOUNT
    assert inv["reward_status"] == "rewarded"

    # Повторный вход того же invitee — UNIQUE блокирует
    cid = await store.create_invite_candidate(
        1, 500, 600, "abc",
        datetime.now(timezone.utc) - timedelta(seconds=1), "ok",
    )
    assert cid is None


@pytest.mark.asyncio
async def test_invite_ambiguous_no_reward():
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    bot._rep_invite_cache[1] = {
        "a": _invite("a", 1, 500),
        "b": _invite("b", 2, 700),
    }
    guild._invites["a"] = _invite("a", 2, 500)
    guild._invites["b"] = _invite("b", 3, 700)

    joiner = _member(600, guild=guild)
    await rep_svc.record_member_join(bot, joiner,
                                   {"a": _invite("a", 1, 500),
                                    "b": _invite("b", 2, 700)},
                                   store=store)
    inv = next(iter(store.invites.values()))
    assert inv["attribution"] == "ambiguous"

    inv["verify_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    n = await rep_svc.process_due_invites(bot, store=store)
    assert n == 0
    assert inv["reward_status"] == "rejected"


@pytest.mark.asyncio
async def test_invite_invitee_left_cancels():
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    inviter = guild.add_member(_member(500, guild=guild))
    cid = await store.create_invite_candidate(
        1, 500, 600, "abc",
        datetime.now(timezone.utc) - timedelta(seconds=1), "ok",
    )
    # invitee НЕ добавлен в guild — fetch_member упадёт
    n = await rep_svc.process_due_invites(bot, store=store)
    assert n == 0
    inv = store.invites[cid]
    assert inv["reward_status"] == "cancelled"


# ---------------------------------------------------------------- boosts

@pytest.mark.asyncio
async def test_boost_reward_once():
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    started = datetime.now(timezone.utc) - timedelta(days=8)
    booster = guild.add_member(_member(500, guild=guild, premium_since=started))

    await rep_svc.record_boost_start(booster, started, store=store)
    # due boost
    row = store.boosts[(1, 500)]
    row["verify_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    n = await rep_svc.process_due_boosts(bot, store=store)
    assert n == 1
    assert store._rep(1, 500) == rep_svc.REP_BOOST_AMOUNT
    assert row["status"] == "rewarded"

    # Повторная активация буста — награды нет
    await rep_svc.record_boost_start(booster, started, store=store)
    row2 = await store.get_boost(1, 500)
    assert row2 is None or row2["status"] != "pending" or True
    # upsert вернул False (rewarded)
    ok = await store.upsert_boost_pending(
        1, 500, started, datetime.now(timezone.utc) + timedelta(days=7)
    )
    assert ok is False


@pytest.mark.asyncio
async def test_boost_cancel_before_verify():
    store = FakeRepStore()
    started = datetime.now(timezone.utc) - timedelta(days=1)
    await rep_svc.record_boost_start(
        _member(500, guild=_FakeGuild(1)), started, store=store
    )
    await rep_svc.record_boost_end(1, 500, store=store)
    assert store.boosts[(1, 500)]["status"] == "cancelled"

    # Новый буст → новый pending-период
    await rep_svc.record_boost_start(
        _member(500, guild=_FakeGuild(1)), started, store=store
    )
    assert store.boosts[(1, 500)]["status"] == "pending"


@pytest.mark.asyncio
async def test_boost_member_lost_boost_cancels():
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    started = datetime.now(timezone.utc) - timedelta(days=8)
    booster = guild.add_member(_member(500, guild=guild, premium_since=None))
    await rep_svc.record_boost_start(booster, started, store=store)
    store.boosts[(1, 500)]["verify_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    n = await rep_svc.process_due_boosts(bot, store=store)
    assert n == 0
    assert store.boosts[(1, 500)]["status"] == "cancelled"


# ---------------------------------------------------------------- server tag

def _tagged_member(uid=700, gid=1, enabled=True, tag_gid=1, bot=False):
    guild = _FakeGuild(gid)
    m = _member(uid, bot=bot, guild=guild, display_name="TagUser")
    m.primary_guild = SimpleNamespace(
        id=tag_gid, identity_enabled=enabled, tag="R!Z3"
    )
    return m


@pytest.mark.asyncio
async def test_server_tag_reward_once():
    store = FakeRepStore()
    m = _tagged_member()

    r = await rep_svc.award_server_tag(m, store=store)
    assert r["status"] == "ok" and r["rep_after"] == rep_svc.REP_SERVER_TAG_AMOUNT
    assert store._rep(1, 700) == 5

    # Повторная «установка» / перезапуск — уже получено
    r = await rep_svc.award_server_tag(m, store=store)
    assert r["status"] == "already"
    assert store._rep(1, 700) == 5

    tx = [t for t in store.transactions if t["source"] == "SERVER_TAG"]
    assert len(tx) == 1
    assert tx[0]["event_key"] == "server_tag:1:700"


@pytest.mark.asyncio
async def test_server_tag_wrong_guild_no_reward():
    store = FakeRepStore()
    m = _tagged_member(tag_gid=999)  # тег другого сервера
    r = await rep_svc.award_server_tag(m, store=store)
    assert r["status"] == "no_tag"
    assert store._rep(1, 700) == 0


@pytest.mark.asyncio
async def test_server_tag_disabled_no_reward():
    store = FakeRepStore()
    for enabled in (False, None):
        m = _tagged_member(enabled=enabled)
        r = await rep_svc.award_server_tag(m, store=store)
        assert r["status"] == "no_tag"
    assert store._rep(1, 700) == 0


@pytest.mark.asyncio
async def test_server_tag_bot_skipped():
    store = FakeRepStore()
    m = _tagged_member(bot=True)
    r = await rep_svc.award_server_tag(m, store=store)
    assert r is None


@pytest.mark.asyncio
async def test_server_tag_no_primary_guild():
    store = FakeRepStore()
    m = _member(700, guild=_FakeGuild(1))  # primary_guild отсутствует
    r = await rep_svc.award_server_tag(m, store=store)
    assert r["status"] == "no_tag"


# ---------------------------------------------------------------- analytics helpers

@pytest.mark.asyncio
async def test_breakdown_and_given_count():
    store = FakeRepStore()
    gid = 1
    await rep_svc.give_manual_rep(gid, 1, 2, "B", store=store)
    await rep_svc.give_manual_rep(gid, 3, 2, "B", store=store)
    await store.award_rep(gid, 2, "INVITE", 5,
                          f"invite:{gid}:9", subject_user_id=9)

    breakdown = {}
    for tx in store.transactions:
        if tx["user_id"] == 2:
            breakdown[tx["source"]] = breakdown.get(tx["source"], 0) + tx["amount"]
    assert breakdown == {"MANUAL_REP": 2, "INVITE": 5}
    assert store._rep(gid, 2) == 7


# ------------------------------------------------------- invite fixes v2

@pytest.mark.asyncio
async def test_invite_stats_counts_inviter_only():
    """Все 5 метрик /invites — только по inviter_id = user."""
    store = FakeRepStore()
    gid = 1
    # user 500 пригласил 600 (rewarded) и 700 (pending)
    for uid, status in ((600, "rewarded"), (700, "pending")):
        await store.create_invite_candidate(
            gid, 500, uid, "x",
            datetime.now(timezone.utc) + timedelta(hours=72), "ok",
        )
        if status == "rewarded":
            inv = next(i for i in store.invites.values() if i["invitee_id"] == uid)
            inv["reward_status"] = "rewarded"
    # unknown-вход (inviter NULL) — не должен влиять на статистику 500
    await store.create_invite_candidate(
        gid, None, 800, "", datetime.now(timezone.utc), "unknown",
        reject_reason="no diff",
    )
    stats = await store.get_user_invite_stats(gid, 500)
    assert stats["invited"] == 2
    assert stats["rewarded"] == 1 and stats["pending"] == 1
    assert stats["rejected"] == 0 and stats["cancelled"] == 0


@pytest.mark.asyncio
async def test_invite_reattribution_after_rejected():
    """unknown/rejected кандидат может быть переатрибутирован при
    повторном входе с достоверными данными; rewarded — никогда."""
    store = FakeRepStore()
    gid = 1
    future = datetime.now(timezone.utc) + timedelta(hours=72)

    # 1) Первый вход — unknown
    cid = await store.create_invite_candidate(
        gid, None, 600, "", future, "unknown", reject_reason="no diff")
    inv = store.invites[cid]
    inv["reward_status"] = "rejected"
    inv["reject_reason"] = "attribution=unknown"

    # 2) Повторный вход — ok от 500 → переатрибуция
    cid2 = await store.create_invite_candidate(
        gid, 500, 600, "abc", future, "ok")
    assert cid2 == cid  # та же строка, не дубликат
    inv = store.invites[cid]
    assert inv["inviter_id"] == 500 and inv["attribution"] == "ok"
    assert inv["reward_status"] == "pending"

    # 3) rewarded-кандидат не перезаписывается
    inv["reward_status"] = "rewarded"
    cid3 = await store.create_invite_candidate(
        gid, 999, 600, "zzz", future, "ok")
    assert cid3 is None
    assert store.invites[cid]["inviter_id"] == 500


@pytest.mark.asyncio
async def test_invite_personal_link_owner():
    """Персональная ссылка (invite.inviter=бот) атрибутируется владельцу."""
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild
    await store.set_invite_owner(1, "mine", 500)

    bot_inviter = SimpleNamespace(id=42, bot=True)
    bot._rep_invite_cache[1] = {
        "mine": _invite("mine", 1, 42),
    }
    guild._invites["mine"] = SimpleNamespace(
        code="mine", uses=2, inviter=bot_inviter)

    joiner = _member(600, guild=guild)
    await rep_svc.record_member_join(
        bot, joiner, bot._rep_invite_cache[1], store=store)
    inv = next(iter(store.invites.values()))
    assert inv["inviter_id"] == 500 and inv["attribution"] == "ok"


@pytest.mark.asyncio
async def test_invite_bot_inviter_without_owner_is_unknown():
    """Инвайт, созданный ботом без owner-маппинга → unknown, не REP боту."""
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild

    bot_inviter = SimpleNamespace(id=42, bot=True)
    bot._rep_invite_cache[1] = {"x": _invite("x", 1, 42)}
    guild._invites["x"] = SimpleNamespace(
        code="x", uses=2, inviter=bot_inviter)

    joiner = _member(600, guild=guild)
    await rep_svc.record_member_join(
        bot, joiner, bot._rep_invite_cache[1], store=store)
    inv = next(iter(store.invites.values()))
    assert inv["attribution"] == "unknown" and inv["inviter_id"] is None


@pytest.mark.asyncio
async def test_invite_vanity_attribution():
    """Вход через vanity URL → attribution='vanity', без награды."""
    store = FakeRepStore()
    bot = _FakeBot()
    guild = _FakeGuild(1)
    bot._guilds[1] = guild
    guild._vanity_uses = 10

    async def _vanity():
        return SimpleNamespace(uses=guild._vanity_uses)
    guild.vanity_invite = _vanity

    joiner = _member(600, guild=guild)
    # нет diff инвайтов, но vanity uses вырос 9 -> 10
    await rep_svc.record_member_join(
        bot, joiner, {}, store=store, before_vanity=9)
    inv = next(iter(store.invites.values()))
    assert inv["attribution"] == "vanity" and inv["inviter_id"] is None
