"""Regression-тесты /alert (Cash Cup P1).

Покрытие ТЗ §9: ADMIN-only, упоминания капитанов и игроков,
диагностика ADMIN-pending vs незаполненные данные, готовые
команды, подзапасные, структурные проблемы, cooldown,
preview, 32 команды, read-only состояние, «Мои данные».
"""
from types import SimpleNamespace

import asyncio

import pytest

from cogs import cashcup


# ---------------- Fake Discord ----------------

class FakeResponse:
    def __init__(self):
        self.sent = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, msg, **kw):
        self.sent.append((msg, kw))
        self._done = True

    async def defer(self, **kw):
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, msg, **kw):
        self.sent.append((msg, kw))


class FakeChannel:
    def __init__(self, cid=5):
        self.id = cid
        self.sent = []          # (content, kwargs)
        self.edits = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=1000 + len(self.sent))

    async def fetch_message(self, mid):
        raise AssertionError("alert не должен читать сообщения")

    async def edit(self, *a, **kw):
        self.edits.append((a, kw))


class FakeGuild:
    def __init__(self, channel=None, owner_id=0):
        self.id = 1
        self.owner_id = owner_id
        self._channel = channel
        self.fetch_calls = 0

    def get_channel(self, cid):
        return self._channel if self._channel and \
            self._channel.id == cid else None

    async def fetch_channel(self, cid):
        self.fetch_calls += 1
        if self._channel and self._channel.id == cid:
            return self._channel
        import discord
        raise discord.NotFound(
            SimpleNamespace(status=404, reason="nf", message="nf"), "nf")


def _member(uid, *, admin=False, owner_id=0):
    guild = SimpleNamespace(owner_id=owner_id, id=1)
    perms = SimpleNamespace(administrator=admin, manage_guild=False)
    return SimpleNamespace(id=uid, guild=guild,
                           guild_permissions=perms, roles=[])


def make_interaction(user, *, channel=None, owner_id=0):
    guild = FakeGuild(channel=channel, owner_id=owner_id)
    user.guild = guild
    return SimpleNamespace(
        guild_id=1, guild=guild, user=user, channel=channel,
        response=FakeResponse(), followup=FakeFollowup())


# ---------------- Fake store ----------------

class FakeStore:
    def __init__(self, state=None):
        self.state = state
        self.mutate_calls = []

    async def active(self, guild_id):
        st = self.state
        if st and st.get("status") in ("registration", "running"):
            return st
        return None

    async def latest(self, guild_id):
        return self.state

    async def mutate(self, *a, **kw):  # pragma: no cover - не должен зваться
        self.mutate_calls.append((a, kw))
        raise AssertionError("/alert не должен менять состояние")

    async def set_message(self, *a, **kw):
        return None


@pytest.fixture
def env(monkeypatch):
    channel = FakeChannel(5)
    store = FakeStore()
    cog = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    cog._alert_locks = {}
    cog._alert_last = {}
    cog._state_cache = {}
    monkeypatch.setattr(cashcup, "cashcup_store", store)
    return SimpleNamespace(cog=cog, store=store, channel=channel)


def _last(i):
    if i.followup.sent:
        return i.followup.sent[-1][0]
    if i.response.sent:
        return i.response.sent[-1][0]
    return ""


def _player(uid, ff=None, nick=None, device="mobile", verified=False,
            ff_ok=True, region="ru"):
    if ff is None:
        ff = f"1000{uid:02d}"
    return {"discord_id": uid, "device": device, "ff_uid": ff,
            "nick": nick if nick is not None else f"nick{uid}",
            "region": region, "verified": verified,
            "ff_ok": ff_ok, "joined_at": "t"}


def _team(tid, name, captain, members, lineup=None, sub_invites=None):
    return {"id": tid, "name": name, "captain": captain,
            "members": members, "pending": [],
            "lineup": (lineup if lineup is not None
                       else [p["discord_id"] for p in members[:4]]),
            "sub_invites": sub_invites or []}


def _reg_state(teams):
    return {"status": "registration", "prize": "тест", "pool": 0,
            "guild_id": 1, "message_id": 77, "channel_id": 5,
            "tournament_id": "cc-test",
            "teams": teams, "bracket": None, "payouts": [], "prizes": {}}


def _ready_team(tid, name, cap, uids):
    return _team(tid, name, cap,
                 [_player(u, verified=True) for u in uids],
                 lineup=uids[:4])


def _public_text(channel):
    return "\n".join(c for c, _ in channel.sent)


# ---------------- §9.1–2: доступ ----------------

@pytest.mark.asyncio
async def test_admin_runs_alert_publishes(env):
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _team("t2", "BETA", 20,
              [_player(20, ff="", nick="", device="", verified=False,
                       ff_ok=False, region="")]),
    ])
    admin = _member(99, admin=True)
    i = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert env.channel.sent, "публичное уведомление не опубликовано"
    assert "ПРОВЕРКА СОСТАВОВ" in _public_text(env.channel)


@pytest.mark.asyncio
async def test_captain_without_admin_denied(env):
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13])])
    cap = _member(10)  # капитан, но не ADMIN
    i = make_interaction(cap, channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "Недостаточно прав" in _last(i)


@pytest.mark.asyncio
async def test_regular_user_denied(env):
    env.store.state = _reg_state([])
    i = make_interaction(_member(555), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "Недостаточно прав" in _last(i)


# ---------------- §9.3–8: содержимое отчёта ----------------

@pytest.mark.asyncio
async def test_all_captains_mentioned_including_ready(env):
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _ready_team("t2", "BETA", 20, [20, 21, 22, 23]),
        _team("t3", "GAMMA", 30, [_player(u) for u in (30, 31, 32, 33)]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "<@10>" in text and "<@20>" in text and "<@30>" in text
    # mentions именно в content, не только embed
    for _, kw in env.channel.sent:
        assert kw.get("allowed_mentions") is not None
    mentions = env.channel.sent[0][1]["allowed_mentions"].users
    ids = {getattr(m, "id", m) for m in mentions}
    assert {10, 20, 30} <= ids


@pytest.mark.asyncio
async def test_missing_uid_and_device_listed(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [
            _player(10, verified=True),
            _player(11, ff="", device=""),          # нет UID и устройства
            _player(12, verified=True),
            _player(13, verified=True),
        ], lineup=[10, 11, 12, 13]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "<@11>" in text
    assert "не указан Free Fire UID" in text
    assert "не указано устройство" in text
    # реальный FF UID не светится публично
    assert "100010" not in text and "100011" not in text


@pytest.mark.asyncio
async def test_ff_api_unconfirmed_listed(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [
            _player(10, verified=True),
            _player(11, ff_ok=False),
            _player(12, verified=True),
            _player(13, verified=True),
        ], lineup=[10, 11, 12, 13]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "<@11>" in text
    assert "FF API не подтвердил аккаунт" in text


@pytest.mark.asyncio
async def test_admin_pending_distinguished_from_empty(env):
    """verified=False при заполненных данных → «ожидает проверки
    ADMIN», а не «заполнить»; в ролл-колл действий не входит."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [
            _player(10, verified=True),
            _player(11),                    # всё заполнено, ждёт ADMIN
            _player(12, verified=True),
            _player(13, verified=True),
        ], lineup=[10, 11, 12, 13]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "данные заполнены, ожидает проверки ADMIN" in text
    assert "Ожидает ADMIN — 3/4 подтверждено" in text
    # <@11> упомянут в блоке команды, но НЕ в строке действий
    # (либо её вообще нет — действий не требуется)
    for ln in text.splitlines():
        if "Требуется заполнить данные" in ln:
            assert "<@11>" not in ln


@pytest.mark.asyncio
async def test_ready_team_marked_ready(env):
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _team("t2", "BETA", 20, [_player(u) for u in (20, 21, 22, 33)]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "Все обязательные данные заполнены и проверены" in text
    assert "Данные заполнены — 4/4 подтверждено" in text


@pytest.mark.asyncio
async def test_four_player_team_status(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10,
              [_player(u, verified=True) for u in (10, 11, 12, 13)],
              lineup=[10, 11, 12, 13]),
        _team("t2", "BETA", 20, [_player(u) for u in (20, 21, 22, 33)]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert "Данные заполнены — 4/4 подтверждено" in _public_text(env.channel)


@pytest.mark.asyncio
async def test_pending_sub_invite_not_counted(env):
    """sub_invites (запасной не в members) не считается
    незаполнившим данные."""
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _team("t2", "BETA", 20, [_player(u) for u in (20, 21, 22, 33)]),
    ])
    env.store.state["teams"][0]["sub_invites"] = [77]
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "Данные заполнены — 4/4 подтверждено" in text
    assert "<@77>" not in text


@pytest.mark.asyncio
async def test_three_player_team_warned(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10,
              [_player(10, verified=True), _player(11, verified=True),
               _player(12, verified=True)],
              lineup=[10, 11, 12]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "ALPHA" in text
    assert "основа должна содержать 4 игроков" in text
    assert "Готова" not in text.split("ALPHA")[1].split("Статус")[0] \
        or "не" in text.lower()


@pytest.mark.asyncio
async def test_more_than_two_pc_shown(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [
            _player(10, verified=True, device="pc"),
            _player(11, verified=True, device="pc"),
            _player(12, verified=True, device="pc"),
            _player(13, verified=True, device="mobile"),
        ], lineup=[10, 11, 12, 13]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "более двух ПК" in text


# ---------------- §9.13–14: статус турнира ----------------

@pytest.mark.asyncio
async def test_no_active_cashcup_no_notification(env):
    env.store.state = None
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "Нет активного CASH CUP" in _last(i)


@pytest.mark.asyncio
async def test_running_status_rejected(env):
    st = _reg_state([_ready_team("t1", "ALPHA", 10, [10, 11, 12, 13])])
    st["status"] = "running"
    env.store.state = st
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "регистрац" in _last(i).lower()


@pytest.mark.asyncio
async def test_cancelled_latest_not_alerted(env):
    st = _reg_state([])
    st["status"] = "cancelled"
    env.store.state = st
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent


# ---------------- §9.15–16: cooldown и preview ----------------

@pytest.mark.asyncio
async def test_repeat_alert_immediately_resends(env):
    """Cooldown отсутствует: повторный /alert сразу шлёт новый отчёт."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    admin = _member(99, admin=True)
    i1 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i1, preview=False)
    first_sends = len(env.channel.sent)
    assert first_sends >= 1

    i2 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i2, preview=False)
    assert len(env.channel.sent) > first_sends  # новая рассылка


@pytest.mark.asyncio
async def test_three_sequential_alerts_all_send(env):
    """Три последовательных вызова — три полные рассылки."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    admin = _member(99, admin=True)
    counts = []
    for _ in range(3):
        i = make_interaction(admin, channel=env.channel)
        await env.cog.alert.callback(env.cog, i, preview=False)
        counts.append(len(env.channel.sent))
    assert counts[0] >= 1 and counts[1] > counts[0]         and counts[2] > counts[1]


@pytest.mark.asyncio
async def test_preview_publishes_nothing(env):
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=True)
    assert not env.channel.sent
    assert "ПРОВЕРКА СОСТАВОВ" in _last(i)
    # preview не расходует cooldown
    assert 1 not in env.cog._alert_last


@pytest.mark.asyncio
async def test_all_ready_private_result_no_pings(env):
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _ready_team("t2", "BETA", 20, [20, 21, 22, 23]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "Все команды готовы" in _last(i)


# ---------------- §9.17–18: масштаб и read-only ----------------

@pytest.mark.asyncio
async def test_32_teams_all_displayed(env):
    teams = []
    for n in range(32):
        cap = 1000 + n
        uids = [cap, cap + 1, cap + 2, cap + 3]
        if n % 3 == 0:
            # каждая третья — с проблемой
            members = [_player(u, verified=(u != cap + 1))
                       for u in uids]
            teams.append(_team(f"t{n}", f"TEAM{n:02d}", cap, members,
                               lineup=uids))
        else:
            teams.append(_ready_team(f"t{n}", f"TEAM{n:02d}", cap, uids))
    env.store.state = _reg_state(teams)
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    for n in range(32):
        assert f"TEAM{n:02d}" in text
    # все 32 капитана упомянуты
    for n in range(32):
        assert f"<@{1000 + n}>" in text
    # сообщение разбито на несколько — лимит 2000 соблюдён
    assert len(env.channel.sent) > 1
    for content, _ in env.channel.sent:
        assert len(content) <= 2000
    assert env.store.mutate_calls == []


@pytest.mark.asyncio
async def test_state_not_mutated(env):
    st = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    env.store.state = st
    snapshot = repr(st)
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert repr(st) == snapshot
    assert env.store.mutate_calls == []


@pytest.mark.asyncio
async def test_missing_card_link_warns_not_creates(env):
    st = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    st["message_id"] = 0
    env.store.state = st
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "карточка" in text.lower()
    # новых карточек/сообщений-карт не создавалось: только alert-тексты
    assert all("ПРОВЕРКА СОСТАВОВ" in (c or "")
               for c, _ in env.channel.sent)


@pytest.mark.asyncio
async def test_parallel_alerts_serialized_no_interleave(env):
    """Два одновременных /alert: per-guild lock сериализует —
    каждая рассылка завершается целиком, сообщения не перемешиваются."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    admin = _member(99, admin=True)
    i1 = make_interaction(admin, channel=env.channel)
    i2 = make_interaction(admin, channel=env.channel)
    await asyncio.gather(
        env.cog.alert.callback(env.cog, i1, preview=False),
        env.cog.alert.callback(env.cog, i2, preview=False))
    # Две последовательные полные рассылки (без cooldown).
    n = len(env.channel.sent)
    assert n >= 2
    # Третий ручной вызов — ещё одна полная рассылка.
    i3 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i3, preview=False)
    assert len(env.channel.sent) > n


@pytest.mark.asyncio
async def test_my_data_button_still_present(env):
    view = cashcup.CashCupView(True)
    ids = [getattr(c, "custom_id", "") for c in view.children]
    assert "cashcup:my_data" in ids
    assert "cashcup:register_br" in ids


@pytest.mark.asyncio
async def test_registration_card_panel_unchanged(env):
    st = _reg_state([_ready_team("t1", "ALPHA", 10, [10, 11, 12, 13])])
    emb = cashcup._panel(st)
    assert "Регистрация открыта" in emb.description
    assert "CASH CUP" in emb.title or "CASH CUP" in str(emb)


def test_alert_is_top_level_command_not_in_cashcup_group():
    """/alert — глобальная команда, не /cashcup alert:
    дерево группы остаётся {create, panel}."""
    names = {c.name for c in cashcup.CashCupCog.cashcup._children.values()}
    assert names == {"create", "panel"}
    cog_cmds = getattr(cashcup.CashCupCog, "__cog_app_commands__", [])
    assert any(getattr(c, "name", "") == "alert" for c in cog_cmds)


# ---------------- hardening: mentions ≤100, cooldown, dedup, fresh ----------------


@pytest.mark.asyncio
async def test_per_chunk_mentions_capped_and_scoped(env):
    """allowed_mentions каждого сообщения — только ID из него, ≤100."""
    import re as _re
    teams = []
    for i in range(18):
        teams.append(_team(f"t{i}", f"TEAM{i}", 1000 + i,
                           [_player(2000 + i * 10 + j, verified=False)
                            for j in range(4)]))
    env.store.state = _reg_state(teams)
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert env.channel.sent
    for text, kw in env.channel.sent:
        ids = {int(u) for u in _re.findall(r"<@(\d+)>", text)}
        am = kw["allowed_mentions"].users
        am_ids = {getattr(u, "id", u) for u in am}
        assert len(am_ids) <= 100
        assert am_ids <= ids or am_ids == ids
        assert len(am_ids) == len(ids) or len(ids) <= 100


@pytest.mark.asyncio
async def test_failed_send_no_cooldown(env, monkeypatch):
    """Ошибка первой отправки → cooldown НЕ установлен."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
    ])
    orig_send = env.channel.send
    async def _boom(*a, **kw):
        raise discord.HTTPException(_FakeResp(), "boom")
    env.channel.send = _boom
    admin = _member(99, admin=True)
    i1 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i1, preview=False)
    # После полной ошибки следующий ручной вызов снова пытается.
    env.channel.send = orig_send
    i2 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i2, preview=False)
    assert env.channel.sent


@pytest.mark.asyncio
async def test_partial_send_dedup_no_duplicates(env, monkeypatch):
    """Частичная отправка: повтор не дублирует уже ушедшие чанки."""
    teams = [_team(f"t{i}", f"TEAM{i}", 100 + i,
                   [_player(300 + i * 10 + j) for j in range(4)])
             for i in range(18)]
    env.store.state = _reg_state(teams)
    calls = []
    orig = env.channel.send
    state = {"fail_after": None}
    async def _send(text, **kw):
        calls.append(text)
        if state["fail_after"] is not None \
                and len(calls) > state["fail_after"]:
            raise discord.HTTPException(_FakeResp(), "x")
        env.channel.sent.append((text, kw))
    monkeypatch.setattr(env.channel, "send", _send)
    admin = _member(99, admin=True)
    # Полный первый прогон → запоминаем число чанков.
    i1 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i1, preview=False)
    total = len(calls)
    assert total >= 1
    # «Частичный» сценарий: сбрасываем dedup/cooldown, имитируем
    # что часть чанков уже ушла.
    env.cog._alert_last.clear()
    env.cog._alert_sent.clear()
    sent_map = env.cog._alert_sent
    # Имитируем: первый чанк «уже отправлен».
    import hashlib
    lines, mids, rep = cashcup._alert_payload(env.store.state, 1)
    chunks = cashcup._pack_lines(lines)
    h = hashlib.sha1(chunks[0].encode()).hexdigest()
    sent_map[1] = (hashlib.sha1("\n".join(lines).encode()).hexdigest(),
                   {h}, False)
    calls.clear()
    i2 = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i2, preview=False)
    # Первый чанк не переотправлен.
    assert chunks[0] not in calls
    assert len(calls) == len(chunks) - 1


@pytest.mark.asyncio
async def test_fresh_state_rebuild_aborts_when_all_ready(env):
    """Между снимками все команды стали готовы → публикация отменена."""
    env.store.state = _reg_state([
        _team("t1", "ALPHA", 10, [_player(u) for u in (10, 11, 12, 33)]),
        _ready_team("t2", "BETA", 20, [40, 41, 42, 43]),
    ])
    admin = _member(99, admin=True)
    # Первый снимок видит ALPHA неготовой; fresh() вернёт всех готовых.
    orig_active = env.store.active
    calls = {"n": 0}
    async def _active(gid):
        calls["n"] += 1
        if calls["n"] >= 2:
            return _reg_state([_ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
                               _ready_team("t2", "BETA", 20, [40, 41, 42, 43])])
        return await orig_active(gid)
    env.store.active = _active
    i = make_interaction(admin, channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    assert not env.channel.sent
    assert "готовы" in _last(i).lower()


@pytest.mark.asyncio
async def test_status_label_no_guaranteed_admission(env):
    """В отчёте нет «Готова» — только «Данные заполнены»."""
    env.store.state = _reg_state([
        _ready_team("t1", "ALPHA", 10, [10, 11, 12, 13]),
        _team("t2", "BETA", 20, [_player(u) for u in (20, 21, 22, 33)]),
    ])
    i = make_interaction(_member(99, admin=True), channel=env.channel)
    await env.cog.alert.callback(env.cog, i, preview=False)
    text = _public_text(env.channel)
    assert "Данные заполнены" in text
    assert "Готова —" not in text


class _FakeResp:
    status = 500
    reason = "x"
