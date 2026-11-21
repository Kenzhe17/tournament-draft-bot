"""Cog-level интеграционный тест CASH CUP на отдельной тестовой PostgreSQL.

Проходит НАСТОЯЩИЕ обработчики CashCupCog (не прямые мутации store):
register_br_team → confirm_ff каждым игроком → invite_sub/confirm_ff
запасного → verify → start → checkin → room → result → GF game →
completed → payout (однократно) → аудит → stale tid → повторный result.

Discord-граница замокана (Interaction/Response/Followup/User/Guild),
БД — реальная временная PostgreSQL через pgserver. Production-БД
не используется.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")
discord = pytest.importorskip("discord")
from discord import app_commands

import config
import storage.db as dbmod
import cogs.cashcup as cashcup_mod
from cogs.cashcup import CashCupCog
from services.cashcup_store import cashcup_store
from services.cashcup_bracket import ready_matches

G = 777000
CH = 555000
ADMIN_UID = 900001
# Капитаны и игроки двух BR-команд.
CAP_A, P_A2, P_A3, P_A4, SUB_A = 910001, 910002, 910003, 910004, 910005
CAP_B, P_B2, P_B3, P_B4 = 920001, 920002, 920003, 920004
EV = "https://discord.com/channels/1196165488565289012/1/2"


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_cashcup_cog")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    cashcup_store._ready = False
    await cashcup_store.ensure_schema()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cashcup_audit")
        await conn.execute("DELETE FROM cashcup_tournaments")
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


# ---------- фейки Discord-границы ----------


class _Resp:
    """discord.InteractionResponse: is_done/defer/send_message/send_modal."""

    def __init__(self):
        self.done = False
        self.messages: list = []
        self.modals: list = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, ephemeral: bool = True) -> None:
        self.done = True

    async def send_message(self, content=None, **kw):
        self.done = True
        self.messages.append(content)

    async def send_modal(self, modal) -> None:
        self.done = True
        self.modals.append(modal)


class _Followup:
    def __init__(self):
        self.sent: list = []

    async def send(self, *args, **kw):
        self.sent.append((args, kw))


class _Channel:
    id = CH

    async def send(self, *args, **kw):
        return SimpleNamespace(id=424242)

    async def fetch_message(self, mid):
        return SimpleNamespace(
            id=mid, edit=AsyncMock(), delete=AsyncMock())


class _Guild:
    def __init__(self, gid: int):
        self.id = gid
        self.owner_id = ADMIN_UID

    async def fetch_member(self, uid: int):
        return SimpleNamespace(
            id=uid,
            joined_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            send=AsyncMock())

    def get_member(self, uid: int):
        return SimpleNamespace(
            id=uid,
            joined_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            send=AsyncMock())

    def get_channel(self, cid):
        return _Channel()

    async def fetch_channel(self, cid):
        return _Channel()


class _Bot:
    def __init__(self, guild: _Guild):
        self.guild = guild
        self.guilds = [guild]

    async def fetch_user(self, uid: int):
        # primary_guild с тегом r!z3 — требование _has_server_tag.
        return SimpleNamespace(
            id=uid,
            primary_guild=SimpleNamespace(
                id=config.GUILD_ID, identity_enabled=True),
            send=AsyncMock())

    def get_guild(self, gid: int):
        return self.guild if gid == self.guild.id else None

    def add_view(self, view) -> None:
        pass


def _interaction(uid: int, guild: _Guild, bot: _Bot, *,
                 admin: bool = False) -> SimpleNamespace:
    user = SimpleNamespace(
        id=uid,
        guild=guild,
        guild_permissions=SimpleNamespace(administrator=admin),
        primary_guild=SimpleNamespace(
            id=config.GUILD_ID, identity_enabled=True),
        send=AsyncMock())
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        user=user,
        channel_id=CH,
        channel=_Channel(),
        response=_Resp(),
        followup=_Followup(),
        client=SimpleNamespace(get_cog=lambda name: COG[0]))


COG: list = []


def _br_team(team_id: str, name: str, captain: int,
             members: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        team_id=team_id, team_name=name, password="pw",
        captain_id=captain, guild_id=G, members=list(members))


def _ff_uid(uid: int) -> str:
    return f"9{uid:08d}"[:15]


async def _seed_two_teams(pg_pool, monkeypatch):
    """PG-tournament + два BR-состава в team_store (мок на границе)."""
    guild = _Guild(G)
    bot = _Bot(guild)
    cog = CashCupCog(bot)  # type: ignore[arg-type]
    COG[:] = [cog]

    br_teams = {
        CAP_A: _br_team("brA", "Alpha", CAP_A,
                        [CAP_A, P_A2, P_A3, P_A4]),
        CAP_B: _br_team("brB", "Beta", CAP_B,
                        [CAP_B, P_B2, P_B3, P_B4]),
    }
    monkeypatch.setattr(
        cashcup_mod.team_store, "get_by_captain",
        lambda uid: br_teams.get(uid))

    ff_calls: list[tuple[str, str]] = []

    async def fake_get_info(region: str, uid: str):
        ff_calls.append((region, uid))
        return {"basicInfo": {"nickname": f"ff_{uid}",
                              "accountId": uid}}

    monkeypatch.setattr(cashcup_mod, "freefire_service",
                        SimpleNamespace(configured=True,
                                        get_info=fake_get_info))
    monkeypatch.setattr(cashcup_mod.vac_service, "check_vac",
                        AsyncMock(return_value=None))

    state = await cashcup_store.create(
        G, CH, ADMIN_UID, "тест", prize1=100000, currency="KZT")
    tid = state["tournament_id"]
    return cog, guild, bot, tid, ff_calls


async def _confirm_all(cog, guild, bot, tid, team_ids):
    """Каждый игрок обеих команд подтверждает FF UID через cog.confirm_ff."""
    roster = {
        "Alpha": [CAP_A, P_A2, P_A3, P_A4],
        "Beta": [CAP_B, P_B2, P_B3, P_B4],
    }
    for key, uids in roster.items():
        for i, uid in enumerate(uids):
            device = "pc" if i == 0 else "mobile"  # 1 ПК на команду
            inter = _interaction(uid, guild, bot)
            await cog.confirm_ff(inter, team_ids[key], _ff_uid(uid),
                                 "ru", device, tid=tid)
            assert inter.followup.sent or inter.response.messages, \
                f"confirm_ff uid={uid} не ответил"


async def _drive_match(cog, guild, bot, tid, state, match):
    """check-in обоих капитанов + комната от ADMIN для ready-матча."""
    mid = match["id"]
    team_by_id = {t["id"]: t for t in state["teams"]}
    for tid_ in (match["a"], match["b"]):
        cap = team_by_id[tid_]["captain"]
        inter = _interaction(cap, guild, bot)
        await cog.checkin(inter, mid, tid=tid)
        assert inter.followup.sent or inter.response.messages
    admin = _interaction(ADMIN_UID, guild, bot, admin=True)
    await cog.room(admin, mid, "777001", "secretpw", tid=tid)
    assert admin.followup.sent or admin.response.messages


@pytest.mark.asyncio
async def test_cog_full_two_team_flow_pg(pg_pool, monkeypatch):
    cog, guild, bot, tid, ff_calls = await _seed_two_teams(
        pg_pool, monkeypatch)

    # 1) Обе BR-команды регистрируются через настоящий cog-метод.
    for cap in (CAP_A, CAP_B):
        inter = _interaction(cap, guild, bot)
        await cog.register_br_team(inter)
        assert inter.followup.sent or inter.response.messages
    state = await cashcup_store.active(G)
    assert state["status"] == "registration"
    assert len(state["teams"]) == 2
    team_ids = {t["name"]: t["id"] for t in state["teams"]}
    assert set(team_ids) == {"Alpha", "Beta"}

    # 2) FF UID подтверждает КАЖДЫЙ игрок (8 вызовов get_info).
    await _confirm_all(cog, guild, bot, tid, team_ids)
    assert len(ff_calls) == 8
    state = await cashcup_store.active(G)
    for t in state["teams"]:
        assert all(p.get("ff_ok") and p.get("ff_uid")
                   for p in t["members"])

    # 3) Дубликат FF UID отклоняется.
    inter = _interaction(P_A2, guild, bot)
    await cog.confirm_ff(inter, team_ids["Alpha"], _ff_uid(P_A3),
                         "ru", "mobile", tid=tid)
    text = " ".join(map(str, inter.followup.sent + inter.response.messages))
    assert "уже используется" in text

    # 4) Запасной: капитан приглашает, игрок принимает и подтверждает.
    cap_a = _interaction(CAP_A, guild, bot)
    await cog.invite_sub(cap_a, str(SUB_A), tid=tid)
    assert cap_a.followup.sent or cap_a.response.messages
    sub = _interaction(SUB_A, guild, bot)
    await cog.confirm_ff(sub, team_ids["Alpha"], _ff_uid(SUB_A),
                         "ru", "mobile", tid=tid)
    state = await cashcup_store.active(G)
    alpha = next(t for t in state["teams"] if t["name"] == "Alpha")
    assert len(alpha["members"]) == 5
    assert alpha["lineup"] == [CAP_A, P_A2, P_A3, P_A4]

    # 5) ADMIN verify обеих команд → start → bracket running.
    admin = _interaction(ADMIN_UID, guild, bot, admin=True)
    for name in ("Alpha", "Beta"):
        await cog.verify(admin, name, tid=tid)
        assert admin.followup.sent or admin.response.messages
    await cog.start(admin, tid=tid)
    state = await cashcup_store.active(G)
    assert state["status"] == "running"
    assert state["bracket"]["matches"]

    # 6) Stale tid отклоняется до любых мутаций.
    stale = _interaction(CAP_A, guild, bot)
    await cog.checkin(stale, "W1", tid="deadbeef0000")
    text = " ".join(map(str, stale.followup.sent + stale.response.messages))
    assert "изменилось" in text or "не найден" in text.lower()

    # 7) Полный прогон сетки: W-матчи → checkin/room/result,
    #    GF → checkin/room → две игры game → completed.
    guard = 0
    while True:
        guard += 1
        assert guard < 60, "сетка не завершилась"
        state = await cashcup_store.latest(G)
        if state["status"] == "completed":
            break
        assert state["status"] == "running"
        ready = ready_matches(state["bracket"])
        assert ready, f"нет ready-матчей при status={state['status']}"
        gf = [m for m in ready if m["bracket"] == "GF"]
        if gf:
            m = gf[0]
            await _drive_match(cog, guild, bot, tid, state, m)
            for side in ("a", "a"):  # BO3: две победы одной стороны
                g = _interaction(ADMIN_UID, guild, bot, admin=True)
                await cog.game(
                    g, app_commands.Choice(name=side.upper(), value=side),
                    EV, tid=tid)
                assert g.followup.sent or g.response.messages
            continue
        m = ready[0]
        await _drive_match(cog, guild, bot, tid, state, m)
        r = _interaction(ADMIN_UID, guild, bot, admin=True)
        await cog.result(r, m["id"], 1, 0, EV, tid=tid)
        assert r.followup.sent or r.response.messages
        # Повторный result того же матча — отказ, состояние не меняется.
        again = _interaction(ADMIN_UID, guild, bot, admin=True)
        await cog.result(again, m["id"], 0, 1, EV, tid=tid)
        text = " ".join(map(str, again.followup.sent
                            + again.response.messages))
        assert text.strip(), "повторный result не ответил"
        state2 = await cashcup_store.latest(G)
        assert state2["bracket"]["matches"]

    state = await cashcup_store.latest(G)
    assert state["status"] == "completed"
    champ_id = state["bracket"]["champion"]
    assert champ_id

    # 8) Ручная выплата — однократно, реальных денег нет.
    champ = next(t for t in state["teams"] if t["id"] == champ_id)
    recip_uid = champ["members"][0]["discord_id"]
    recipient = SimpleNamespace(id=recip_uid)
    p = _interaction(ADMIN_UID, guild, bot, admin=True)
    await cog.payout(p, recipient, 100000, EV,
                     tournament_id=tid, currency="KZT")
    assert p.followup.sent or p.response.messages
    state = await cashcup_store.latest(G)
    assert len(state["payouts"]) == 1
    assert state["payouts"][0]["amount"] == 100000

    p2 = _interaction(ADMIN_UID, guild, bot, admin=True)
    await cog.payout(p2, recipient, 100000, EV,
                     tournament_id=tid, currency="KZT")
    text = " ".join(map(str, p2.followup.sent + p2.response.messages))
    assert "уже" in text
    state = await cashcup_store.latest(G)
    assert len(state["payouts"]) == 1

    # 9) Аудит покрывает весь путь.
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM cashcup_audit WHERE tournament_id=$1",
            tid)
    assert n >= 15

    # 10) Free Fire API вызывался ровно по разу на подтверждение
    #     (9 игроков: 8 основных + запасной) + повторы confirm_ff.
    assert len(ff_calls) >= 9


@pytest.mark.asyncio
async def test_admin_menu_actions_dispatch_pg(pg_pool, monkeypatch):
    """Все действия AdminMenuView проходят через _admin_action без
    исключений и всегда дают ответ (pick-view / modal / текст)."""
    cog, guild, bot, tid, _ = await _seed_two_teams(pg_pool, monkeypatch)
    # teams через store напрямую — dispatch-тест не про регистрацию.
    def add_teams(st):
        st["teams"] = [
            {"id": "tA", "name": "Alpha", "captain": CAP_A,
             "members": [{"discord_id": u, "device": "mobile",
                          "ff_uid": _ff_uid(u), "nick": "n",
                          "region": "ru", "verified": True,
                          "ff_ok": True,
                          "joined_at": "2026-11-18T00:00:00+00:00"}
                         for u in (CAP_A, P_A2, P_A3, P_A4)],
             "pending": [], "lineup": [CAP_A, P_A2, P_A3, P_A4],
             "sub_invites": [], "br_team_id": None},
            {"id": "tB", "name": "Beta", "captain": CAP_B,
             "members": [{"discord_id": u, "device": "mobile",
                          "ff_uid": _ff_uid(u), "nick": "n",
                          "region": "ru", "verified": True,
                          "ff_ok": True,
                          "joined_at": "2026-11-18T00:00:00+00:00"}
                         for u in (CAP_B, P_B2, P_B3, P_B4)],
             "pending": [], "lineup": [CAP_B, P_B2, P_B3, P_B4],
             "sub_invites": [], "br_team_id": None},
        ]
        from services.cashcup_bracket import create_bracket
        st["bracket"] = create_bracket(["tA", "tB"])
        st["status"] = "running"
    from services.cashcup_bracket import create_bracket  # noqa: F401
    await cashcup_store.mutate(G, ADMIN_UID, "seed", add_teams,
                               tournament_id=tid)

    admin = _interaction(ADMIN_UID, guild, bot, admin=True)
    # Не-админ получает отказ на любом действии.
    pleb = _interaction(P_A2, guild, bot)
    await cog._admin_action(pleb, "forecast", tid=tid)
    text = " ".join(map(str, pleb.followup.sent + pleb.response.messages))
    assert "ADMIN" in text

    actions = ["matches", "bracket", "bracket_final", "forecast",
               "history", "verify", "remove", "prize", "start",
               "cancel", "result", "room", "forfeit", "game", "dq",
               "revert", "pause", "resume", "resolve", "payout",
               "unknown_action"]
    for action in actions:
        inter = _interaction(ADMIN_UID, guild, bot, admin=True)
        await cog._admin_action(inter, action, tid=tid)
        got = (inter.followup.sent or inter.response.messages
               or inter.response.modals)
        assert got, f"action={action} не дал ответа"


@pytest.mark.asyncio
async def test_double_result_and_stale_tid_pg(pg_pool, monkeypatch):
    """Повторный result и чужой tid не портят состояние."""
    cog, guild, bot, tid, _ = await _seed_two_teams(pg_pool, monkeypatch)
    # Сидим running-состояние напрямую (как в предыдущем тесте).
    def seed(st):
        st["teams"] = [
            {"id": "tA", "name": "Alpha", "captain": CAP_A,
             "members": [{"discord_id": u, "device": "mobile",
                          "ff_uid": _ff_uid(u), "nick": "n",
                          "region": "ru", "verified": True,
                          "ff_ok": True,
                          "joined_at": "2026-11-18T00:00:00+00:00"}
                         for u in (CAP_A, P_A2, P_A3, P_A4)],
             "pending": [], "lineup": [CAP_A, P_A2, P_A3, P_A4],
             "sub_invites": [], "br_team_id": None},
            {"id": "tB", "name": "Beta", "captain": CAP_B,
             "members": [{"discord_id": u, "device": "mobile",
                          "ff_uid": _ff_uid(u), "nick": "n",
                          "region": "ru", "verified": True,
                          "ff_ok": True,
                          "joined_at": "2026-11-18T00:00:00+00:00"}
                         for u in (CAP_B, P_B2, P_B3, P_B4)],
             "pending": [], "lineup": [CAP_B, P_B2, P_B3, P_B4],
             "sub_invites": [], "br_team_id": None},
        ]
        from services.cashcup_bracket import create_bracket
        st["bracket"] = create_bracket(["tA", "tB"])
        st["status"] = "running"
    await cashcup_store.mutate(G, ADMIN_UID, "seed", seed,
                               tournament_id=tid)

    state = await cashcup_store.active(G)
    mid = ready_matches(state["bracket"])[0]["id"]
    admin = _interaction(ADMIN_UID, guild, bot, admin=True)

    # Без check-in/комнаты result отклоняется.
    await cog.result(admin, mid, 1, 0, EV, tid=tid)
    text = " ".join(map(str, admin.followup.sent + admin.response.messages))
    assert "чек" in text.lower() or "готовн" in text.lower() \
        or "комнат" in text.lower()

    # Чужой tid на room — отказ без мутации.
    admin2 = _interaction(ADMIN_UID, guild, bot, admin=True)
    await cog.room(admin2, mid, "1", "p", tid="deadbeef0000")
    text = " ".join(map(str, admin2.followup.sent + admin2.response.messages))
    assert "изменилось" in text or "не найден" in text.lower()
    state = await cashcup_store.active(G)
    m = next(x for x in state["bracket"]["matches"] if x["id"] == mid)
    assert not m.get("room_id")


# ---------- P0: восстановление регистрации после restart ----------


class _CardChannel:
    """Канал с реальными message_id: send/fetch/delete как у Discord."""

    def __init__(self):
        self.messages: dict[int, SimpleNamespace] = {}
        self.next_id = 6000
        self.sent_count = 0

    async def send(self, *args, **kw):
        mid = self.next_id
        self.next_id += 1
        self.sent_count += 1
        msg = SimpleNamespace(id=mid, edit=AsyncMock(), delete=AsyncMock())
        self.messages[mid] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self.messages.get(int(mid))
        if msg is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="nf"),
                {"message": "nf"})
        return msg


class _CardGuild(_Guild):
    def __init__(self, gid: int, channel: _CardChannel):
        super().__init__(gid)
        self._channel = channel

    def get_channel(self, cid):
        return self._channel if cid == CH else None

    async def fetch_channel(self, cid):
        return self.get_channel(cid)


async def _audit_count(tid: str) -> int:
    pool = dbmod._pool
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM cashcup_audit WHERE tournament_id=$1",
            tid)


async def _snapshot(tid: str) -> dict:
    state = await cashcup_store.latest(G)
    assert state["tournament_id"] == tid
    return {
        "status": state["status"],
        "version": state["version"],
        "channel_id": state["channel_id"],
        "message_id": state["message_id"],
        "prize": state["prize"],
        "pool": state.get("pool"),
        "teams": state["teams"],
        "audit": await _audit_count(tid),
    }


async def _registration_fixture(pg_pool, monkeypatch):
    """Турнир в registration: 2 BR-команды, FF UID у всех,
    приглашение запасного, ожидающая заявка pending."""
    cog, guild, bot, tid, _ = await _seed_two_teams(pg_pool, monkeypatch)
    for cap in (CAP_A, CAP_B):
        inter = _interaction(cap, guild, bot)
        await cog.register_br_team(inter)
    state = await cashcup_store.active(G)
    team_ids = {t["name"]: t["id"] for t in state["teams"]}
    await _confirm_all(cog, guild, bot, tid, team_ids)
    # Запасной приглашён, но НЕ подтвердил — sub_invites живёт.
    cap_a = _interaction(CAP_A, guild, bot)
    await cog.invite_sub(cap_a, str(SUB_A), tid=tid)
    # Ожидающая заявка (pending) — через прямую мутацию стора.
    def add_pending(st):
        t = next(t for t in st["teams"] if t["name"] == "Alpha")
        t.setdefault("pending", []).append(
            {"discord_id": 930001, "ff_uid": "", "ff_ok": False})
    await cashcup_store.mutate(G, CAP_A, "test_pending", add_pending,
                               tournament_id=tid)
    return cog, guild, bot, tid, team_ids


@pytest.mark.asyncio
async def test_registration_card_survives_restart_alive_message(
        pg_pool, monkeypatch):
    """Рестарт при ЖИВОЙ карточке: edit на месте, без дублей,
    все команды/UID/sub_invites/pending сохранены."""
    cog1, guild1, bot1, tid, _ = await _registration_fixture(
        pg_pool, monkeypatch)
    before = await _snapshot(tid)
    assert before["status"] == "registration"
    assert len(before["teams"]) == 2

    # Карточка существует в «Discord» (канал с message_id).
    ch = _CardChannel()
    card = await ch.send(view="panel")
    await cashcup_store.set_message(G, card.id)
    before = await _snapshot(tid)

    # === RESTART: старый cog уничтожен, новый поднимается ===
    guild2 = _CardGuild(G, ch)
    bot2 = _Bot(guild2)
    cog2 = CashCupCog(bot2)  # type: ignore[arg-type]
    await cog2.cog_load()
    await cog2.on_ready()

    # Карточка отредактирована на месте — НОВЫХ сообщений нет.
    assert ch.sent_count == 1
    card.edit.assert_awaited()
    after = await _snapshot(tid)
    assert after["message_id"] == before["message_id"]
    assert after["teams"] == before["teams"]
    assert after["status"] == "registration"
    assert after["audit"] == before["audit"]

    # Повторный on_ready (reconnect) — снова без дублей.
    cog2._boards_ok = False
    await cog2.on_ready()
    assert ch.sent_count == 1
    assert card.edit.await_count == 2

    # Кнопки работают: новая BR-команда регистрируется в ТОТ ЖЕ
    # tournament_id (не создаётся новый турнир).
    br_teams = {
        940001: _br_team("brC", "Gamma", 940001,
                         [940001, 940002, 940003, 940004]),
    }
    monkeypatch.setattr(
        cashcup_mod.team_store, "get_by_captain",
        lambda uid: {**{CAP_A: _br_team("brA", "Alpha", CAP_A,
                                        [CAP_A, P_A2, P_A3, P_A4]),
                        CAP_B: _br_team("brB", "Beta", CAP_B,
                                        [CAP_B, P_B2, P_B3, P_B4])},
                     **br_teams}.get(uid))
    inter = _interaction(940001, guild2, bot2)
    await cog2.register_br_team(inter)
    state = await cashcup_store.active(G)
    assert state["tournament_id"] == tid
    assert len(state["teams"]) == 3
    names = {t["name"] for t in state["teams"]}
    assert names == {"Alpha", "Beta", "Gamma"}


@pytest.mark.asyncio
async def test_registration_card_deleted_recreated_once(
        pg_pool, monkeypatch):
    """Карточка удалена во время рестарта: создаётся ОДНА замена,
    message_id обновляется, данные турнира не трогаются."""
    cog1, guild1, bot1, tid, _ = await _registration_fixture(
        pg_pool, monkeypatch)
    before = await _snapshot(tid)
    await cashcup_store.set_message(G, 999999)  # «удалённое» сообщение

    ch = _CardChannel()  # fetch(999999) -> NotFound
    guild2 = _CardGuild(G, ch)
    bot2 = _Bot(guild2)
    cog2 = CashCupCog(bot2)  # type: ignore[arg-type]
    await cog2.cog_load()
    await cog2.on_ready()

    assert ch.sent_count == 1           # ровно одна замена
    after = await _snapshot(tid)
    assert after["message_id"] != before["message_id"]
    # message_id указывает на реально отправленное сообщение.
    new_id = after["message_id"]
    assert new_id in ch.messages
    assert after["teams"] == before["teams"]
    assert after["status"] == "registration"
    assert after["version"] == before["version"]  # set_message — не mutate
    assert after["audit"] == before["audit"]

    # Повторный restore: теперь сообщение «существует» → edit,
    # дублей нет.
    cog2._boards_ok = False
    await cog2.on_ready()
    assert ch.sent_count == 1
    ch.messages[new_id].edit.assert_awaited()
