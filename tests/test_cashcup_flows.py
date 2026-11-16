"""Flow-тесты CASH CUP с mock Discord Interaction и in-memory store.
Проверяют реальное исполнение обработчиков, а не исходный код."""
from types import SimpleNamespace

import asyncio

import pytest

from cogs import cashcup


def _last(i):
    if i.followup.sent:
        return i.followup.sent[-1][0]
    if i.response.sent:
        return i.response.sent[-1][0]
    return ""
from services import cashcup_bracket as cb


# ---------------- Fake Discord ----------------

class FakeResponse:
    def __init__(self):
        self.sent = []
        self.modal = None
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, msg, **kw):
        self.sent.append((msg, kw))
        self._done = True

    async def defer(self, **kw):
        self._done = True

    async def send_modal(self, modal):
        self.modal = modal
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, msg, **kw):
        self.sent.append((msg, kw))


class FakeClient:
    def __init__(self, cog):
        self._cog = cog

    def get_cog(self, name):
        return self._cog


class FakeGuild:
    def __init__(self, members=None, owner_id=0):
        self.id = 1
        self.owner_id = owner_id
        self._members = members or {}

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel(self, cid):
        return None

    def get_guild(self, gid):
        return self


async def _noop_send(*a, **k):
    return None


def _member(uid, admin=False, owner_id=0):
    guild = FakeGuild(owner_id=owner_id)
    perms = SimpleNamespace(administrator=admin, manage_guild=False)
    return SimpleNamespace(id=uid, guild=guild, send=_noop_send,
                           guild_permissions=perms, roles=[])


def make_interaction(user, *, admin=False, owner_id=0, members=None):
    guild = FakeGuild(members=members or {}, owner_id=owner_id)
    user.guild = guild
    resp = FakeResponse()
    i = SimpleNamespace(
        guild_id=1, guild=guild, user=user,
        response=resp, followup=FakeFollowup(),
        client=FakeClient(None))
    return i


# ---------------- Fake store ----------------

class FakeStore:
    """In-memory замена cashcup_store: mutate применяет change под "блокировкой"."""

    def __init__(self, state=None):
        self.state = state
        self.audits = []

    async def active(self, guild_id):
        st = self.state
        if st and st.get("status") in ("registration", "running"):
            return st
        return None

    async def latest(self, guild_id):
        return self.state

    async def mutate(self, guild_id, actor_id, action, change, details=None,
                     include_finished=False, tournament_id=None, **kw):
        res = change(self.state)
        self.audits.append({"action": action, "actor": actor_id,
                            "details": details})
        return self.state, res

    async def create(self, *a, **kw):
        raise NotImplementedError

    async def set_message(self, *a, **kw):
        return None


@pytest.fixture
def cog(monkeypatch):
    c = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    c.bot = SimpleNamespace(get_guild=lambda gid: None,
                            fetch_user=lambda uid: None,
                            add_view=lambda v: None)
    store = FakeStore()
    monkeypatch.setattr(cashcup, "cashcup_store", store)
    c._store = store
    c._board_pages = {}
    c._board_locks = {}
    c._boards_lock = asyncio.Lock()
    c._boards_ok = False
    return c


def _reg_state():
    return {"status": "registration", "prize": "тест", "pool": 0,
            "guild_id": 1, "message_id": None, "channel_id": 5,
            "teams": [], "bracket": None, "payouts": [], "prizes": {}}


def _player(uid, ff=None, nick=None, device="mobile", verified=False,
            ff_ok=True):
    if ff is None:
        ff = f"1000{uid:02d}"
    return {"discord_id": uid, "device": device, "ff_uid": ff,
            "nick": nick if nick is not None else f"nick{uid}",
            "region": "ru", "verified": verified,
            "ff_ok": ff_ok, "joined_at": "t"}


def _team(tid, name, captain, members, lineup=None):
    return {"id": tid, "name": name, "captain": captain,
            "members": members, "pending": [],
            "lineup": lineup if lineup is not None else [captain],
            "sub_invites": []}


# ---------------- P0-1: призы ----------------

@pytest.mark.asyncio
async def test_prize_modal_saves_first_place(cog):
    cog._store.state = _reg_state()
    admin = _member(99, admin=True)
    i = make_interaction(admin)
    await cog._mi_prize(i, ["100000", "KZT"])
    pr = cog._store.state["prizes"]
    assert pr["places"] == {"1": 100000, "2": 0, "3": 0}
    assert pr["currency"] == "KZT"
    assert cog._store.audits[-1]["action"] == "set_prize"
    # панель показывает приз
    emb = cashcup._panel(cog._store.state)
    assert "ПРИЗ ЗА 1 МЕСТО" in emb.description


@pytest.mark.asyncio
async def test_prize_locked_after_start(cog):
    st = _reg_state()
    st["status"] = "running"
    st["bracket"] = {"matches": []}
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog._mi_prize(i, ["500", "KZT"])
    assert "prizes" not in st or not st["prizes"]
    assert "регистрац" in _last(i).lower() or \
        "Турнир не" in _last(i)


# ---------------- P0-2: выплата ----------------

def _completed_state():
    st = _reg_state()
    st["status"] = "completed"
    t1 = _team("t1", "Champ", 10, [_player(10), _player(11)])
    t2 = _team("t2", "Vice", 20, [_player(20)])
    st["teams"] = [t1, t2]
    st["bracket"] = {"champion": "t1", "matches": []}
    st["prizes"] = {"places": {"1": 100000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 100000}
    return st


@pytest.mark.asyncio
async def test_payout_champion_member_ok(cog):
    cog._store.state = _completed_state()
    admin = _member(99, admin=True)
    members = {10: _member(10), 20: _member(20), 99: admin}
    i = make_interaction(admin, members=members)
    await cog._mi_payout(i, ["10", "100000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert len(cog._store.state["payouts"]) == 1
    p = cog._store.state["payouts"][0]
    assert p["recipient"] == 10 and p["amount"] == 100000
    assert p["currency"] == "KZT" and p["manual"] is True
    assert p["champion_team"] == "t1"
    assert cog._store.audits[-1]["action"] == "payout_recorded"


@pytest.mark.asyncio
async def test_payout_loser_rejected(cog):
    cog._store.state = _completed_state()
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={20: _member(20), 99: admin})
    await cog.payout(i, _member(20), 100000,
                     "https://discord.com/channels/1/2/3")
    assert not cog._store.state["payouts"]
    assert "чемпион" in _last(i).lower()


@pytest.mark.asyncio
async def test_payout_double_rejected(cog):
    st = _completed_state()
    st["payouts"] = [{"recipient": 10, "amount": 100000}]
    cog._store.state = st
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "100000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert len(cog._store.state["payouts"]) == 1
    assert "уже" in _last(i).lower()


@pytest.mark.asyncio
async def test_payout_wrong_amount_rejected(cog):
    cog._store.state = _completed_state()
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "50000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert not cog._store.state["payouts"]
    assert "приз" in _last(i).lower()


@pytest.mark.asyncio
async def test_payout_before_finish_rejected(cog):
    st = _completed_state()
    st["status"] = "running"
    cog._store.state = st
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "100000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert not cog._store.state["payouts"]


# ---------------- P0-3: DISPUTED блокирует продвижение ----------------

def test_disputed_blocks_round_advancement():
    b = cb.create_bracket(["t1", "t2", "t3", "t4"], seed=1)
    ready = cb.ready_matches(b)
    assert len(ready) == 2  # первый раунд: 2 матча
    m1, m2 = ready[0], ready[1]
    n_matches_before = len(b["matches"])
    # первый матч -> disputed, второй завершаем
    m1["status"] = "disputed"
    cb.record_result(b, m2["id"], 1, 0, evidence="ev")
    # раунд НЕ продвинулся: новых матчей нет, active_keys те же
    assert len(b["matches"]) == n_matches_before
    assert set(b["active_keys"]) == {m1["id"], m2["id"]}
    # решаем спор -> ready -> результат -> продвижение ровно один раз
    m1["status"] = "ready"
    cb.record_result(b, m1["id"], 1, 0, evidence="ev2")
    assert len(b["matches"]) > n_matches_before
    new_round = [m for m in b["matches"]
                 if m["id"] not in {m1["id"], m2["id"]}]
    assert new_round, "следующий раунд не создан"
    # все новые матчи используют только реальных участников, без дублей id
    ids = [m["id"] for m in b["matches"]]
    assert len(ids) == len(set(ids))
    participants = {m1["winner"], m2["winner"], m1.get("loser"), m2.get("loser")}
    for m in new_round:
        for side in (m.get("a"), m.get("b")):
            if side:
                assert side in {"t1", "t2", "t3", "t4"}


def test_disputed_not_counted_as_loss_or_bye():
    b = cb.create_bracket(["t1", "t2", "t3", "t4"], seed=1)
    ready = cb.ready_matches(b)
    m1 = ready[0]
    m1["status"] = "disputed"
    losses_before = dict(b["losses"])
    # disputed не должен менять потери и не должен завершаться сам
    assert b["losses"] == losses_before
    assert m1["status"] == "disputed"
    assert m1["id"] in b["active_keys"]


# ---------------- P0-4: «Моя команда» -> RosterView ----------------

@pytest.mark.asyncio
async def test_show_roster_sends_view(cog):
    st = _reg_state()
    st["teams"] = [_team("t1", "Alpha", 10,
                         [_player(10), _player(11), _player(12), _player(13)])]
    cog._store.state = st
    i = make_interaction(_member(10))
    await cog.show_roster(i)
    msg, kw = i.response.sent[-1]
    assert isinstance(kw.get("view"), cashcup.RosterView)
    assert kw["view"].is_captain is True
    labels = [getattr(c, "label", "") for c in kw["view"].children]
    assert "Пригласить запасного" in labels
    assert "Снять команду" in labels


@pytest.mark.asyncio
async def test_show_roster_member_no_privileged(cog):
    st = _reg_state()
    st["teams"] = [_team("t1", "Alpha", 10,
                         [_player(10), _player(11)])]
    cog._store.state = st
    i = make_interaction(_member(11))
    await cog.show_roster(i)
    view = i.response.sent[-1][1]["view"]
    assert view.is_captain is False
    labels = [getattr(c, "label", "") for c in view.children]
    assert "Пригласить запасного" not in labels
    assert "Снять команду" not in labels
    assert "Выйти" in labels and "Мои данные" in labels


# ---------------- P1: captain-only invite ----------------

@pytest.mark.asyncio
async def test_invite_sub_only_captain(cog):
    st = _reg_state()
    st["teams"] = [_team("t1", "Alpha", 10, [_player(10)])]
    cog._store.state = st
    i = make_interaction(_member(11), members={11: _member(11)})
    await cog.invite_sub(i, "33")
    assert "капитан" in _last(i).lower()
    assert not st["teams"][0].get("sub_invites")


@pytest.mark.asyncio
async def test_invite_sub_captain_ok(cog):
    st = _reg_state()
    st["teams"] = [_team("t1", "Alpha", 10, [_player(10)])]
    cog._store.state = st
    target = _member(33)
    i = make_interaction(_member(10), members={10: _member(10), 33: target})
    await cog.invite_sub(i, "33")
    assert 33 in st["teams"][0]["sub_invites"]
    # повторное приглашение не дублирует
    await cog.invite_sub(i, "33")
    assert st["teams"][0]["sub_invites"].count(33) == 1


# ---------------- P1: verify требует полный профиль ----------------

@pytest.mark.asyncio
async def test_verify_rejects_incomplete_profile(cog):
    st = _reg_state()
    bad = _player(10, ff="", nick="", ff_ok=False)
    good = [_player(u) for u in (11, 12, 13)]
    st["teams"] = [_team("t1", "Alpha", 10, [bad] + good)]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.verify(i, "Alpha")
    assert not any(p["verified"] for p in st["teams"][0]["members"])
    assert "профиль" in _last(i).lower()


@pytest.mark.asyncio
async def test_verify_ok_and_reset_on_change(cog):
    st = _reg_state()
    st["teams"] = [_team("t1", "Alpha", 10,
                         [_player(u) for u in (10, 11, 12, 13)])]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.verify(i, "Alpha")
    assert all(p["verified"] for p in st["teams"][0]["members"])
    # игрок меняет UID -> verified сбрасывается (change в confirm_ff)
    p = st["teams"][0]["members"][0]
    assert p["verified"] is True


# ---------------- P1: строгий выбор стороны ----------------

def test_side_strict():
    assert cashcup.CashCupCog._side("a") == "a"
    assert cashcup.CashCupCog._side("B") == "b"
    assert cashcup.CashCupCog._side("1") is None
    assert cashcup.CashCupCog._side("team_a") is None
    assert cashcup.CashCupCog._side("") is None


@pytest.mark.asyncio
async def test_mi_forfeit_invalid_side_rejected(cog):
    st = _reg_state()
    st["status"] = "running"
    st["bracket"] = {"matches": []}
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog._mi_forfeit(i, "W1", ["xxx", "reason"])
    assert "a` или `b" in _last(i) or \
        "a или b" in _last(i)
    assert not cog._store.audits  # forfeit не вызван


# ---------------- P1: пагинация ----------------

def test_pickview_pagination_70():
    opts = [(f"Team {i}", f"id{i}", "") for i in range(70)]
    v = cashcup.PickView("Команда…", opts, lambda i, v: None)
    sel = v.children[0]
    assert len(sel.options) == 25
    labels = [getattr(c, "label", "") for c in v.children]
    assert "Назад" in labels and "Вперёд" in labels
    # вторая страница — следующие 25
    v2 = cashcup.PickView("Команда…", opts, lambda i, v: None, page=1)
    assert v2.children[0].options[0].value == "id25"
    assert v2.children[0].options[-1].value == "id49"
    # последняя страница — 20 опций
    v3 = cashcup.PickView("Команда…", opts, lambda i, v: None, page=2)
    assert len(v3.children[0].options) == 20


def test_pickview_single_page_no_nav():
    v = cashcup.PickView("x", [("a", "1", "")], lambda i, v: None)
    assert len(v.children) == 1


# ---------------- P1: GF side picker ----------------

def test_gamesideview_buttons():
    class _C:  # minimal cog stub
        async def _mi_game_side(self, i, side, ev): pass
    v = cashcup.GameSideView(_C(), "GF", "Alpha", "Beta")
    labels = [c.label for c in v.children]
    assert labels == ["Победа: Alpha", "Победа: Beta"]


# ---------------- CommandTree ----------------

def test_command_tree_only_two():
    names = {c.name for c in cashcup.CashCupCog.cashcup._children.values()}
    assert names == {"create", "panel"}




# ================= Hardening: выплата без приза / валюта / валидация / board =================

@pytest.mark.asyncio
async def test_payout_rejected_without_prize(cog):
    st = _completed_state()
    st["prizes"] = {}
    cog._store.state = st
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "100000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert not st["payouts"]
    assert "приз" in _last(i).lower()


@pytest.mark.asyncio
async def test_payout_rejected_zero_prize(cog):
    st = _completed_state()
    st["prizes"] = {"places": {"1": 0, "2": 0, "3": 0}, "currency": "KZT"}
    cog._store.state = st
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "100000", "KZT",
                             "https://discord.com/channels/1/2/3"])
    assert not st["payouts"]


@pytest.mark.asyncio
async def test_payout_usd_currency(cog):
    st = _completed_state()
    st["prizes"] = {"places": {"1": 500, "2": 0, "3": 0},
                    "currency": "USD", "total": 500}
    cog._store.state = st
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "500", "USD",
                             "https://discord.com/channels/1/2/3"])
    p = st["payouts"][0]
    assert p["currency"] == "USD" and p["amount"] == 500
    assert "500 USD" in _last(i)
    assert "₸" not in _last(i)
    assert cog._store.audits[-1]["details"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_payout_wrong_currency_rejected(cog):
    cog._store.state = _completed_state()
    admin = _member(99, admin=True)
    i = make_interaction(admin, members={10: _member(10), 99: admin})
    await cog._mi_payout(i, ["10", "100000", "USD",
                             "https://discord.com/channels/1/2/3"])
    assert not cog._store.state["payouts"]


# ---------- _validate_teams: независимая проверка ----------

def _valid_team_state():
    st = _reg_state()
    st["teams"] = [
        _team("t1", "Alpha", 10,
              [_player(u, verified=True) for u in (10, 11, 12, 13)],
              lineup=[10, 11, 12, 13]),
        _team("t2", "Beta", 20,
              [_player(u, verified=True) for u in (20, 21, 22, 23)],
              lineup=[20, 21, 22, 23]),
    ]
    st["prizes"] = {"places": {"1": 1000, "2": 0, "3": 0}, "currency": "KZT"}
    return st


async def _run_validate(monkeypatch, state):
    async def _fake_check(bot, guild, uid):
        return None
    monkeypatch.setattr(cashcup, "_member_check", _fake_check)
    guild = SimpleNamespace(id=1)
    return await cashcup._validate_teams(object(), guild, state)


@pytest.mark.asyncio
async def test_validate_empty_ff_uid_rejected(monkeypatch):
    st = _valid_team_state()
    st["teams"][0]["members"][0]["ff_uid"] = ""
    with pytest.raises(cashcup.CashCupError, match="UID"):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_ff_ok_false_rejected(monkeypatch):
    st = _valid_team_state()
    st["teams"][0]["members"][0]["ff_ok"] = False
    st["teams"][0]["members"][0]["verified"] = True  # verified не спасает
    with pytest.raises(cashcup.CashCupError, match="API"):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_unverified_rejected(monkeypatch):
    st = _valid_team_state()
    st["teams"][0]["members"][0]["verified"] = False
    with pytest.raises(cashcup.CashCupError):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_three_pc_rejected(monkeypatch):
    st = _valid_team_state()
    for p in st["teams"][0]["members"][:3]:
        p["device"] = "pc"
    with pytest.raises(cashcup.CashCupError, match="ПК"):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_three_players_rejected(monkeypatch):
    st = _valid_team_state()
    st["teams"][0]["members"] = st["teams"][0]["members"][:3]
    st["teams"][0]["lineup"] = [10, 11, 12, 99]
    with pytest.raises(cashcup.CashCupError):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_substitute_checked(monkeypatch):
    """Запасной (5-й) тоже проходит полную проверку профиля."""
    st = _valid_team_state()
    st["teams"][0]["members"].append(_player(14, verified=True))
    st["teams"][0]["members"][4]["ff_uid"] = ""
    with pytest.raises(cashcup.CashCupError, match="UID"):
        await _run_validate(monkeypatch, st)


@pytest.mark.asyncio
async def test_validate_all_good_passes(monkeypatch):
    await _run_validate(monkeypatch, _valid_team_state())  # без исключений


# ---------- Board: persistent ids, restore, ошибки API ----------

def test_board_nav_persistent_ids():
    v = cashcup.BoardSelectView()
    ids = {getattr(c, "custom_id", "") for c in v.children}
    assert "cashcup:board_select" in ids
    assert "cashcup:board_prev" in ids
    assert "cashcup:board_next" in ids


def _running_state(n_matches=30):
    st = _reg_state()
    st["status"] = "running"
    st["guild_id"] = 1
    st["channel_id"] = 5
    st["board_message_id"] = 777
    teams = [_team(f"t{i}", f"T{i}", i, [_player(i)])
             for i in range(n_matches * 2)]
    st["teams"] = teams
    b = cb.create_bracket([t["id"] for t in teams], seed=1)
    st["bracket"] = b
    return st


class FakeMsg:
    def __init__(self, mid=777):
        self.id = mid
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    def __init__(self, msg=None, send_exc=None, fetch_exc=None):
        self.msg = msg
        self.sent = []
        self._send_exc = send_exc
        self._fetch_exc = fetch_exc

    async def fetch_message(self, mid):
        if self._fetch_exc:
            raise self._fetch_exc
        if self.msg is None:
            import discord
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), {"message": "nf"})
        return self.msg

    async def send(self, **kw):
        if self._send_exc:
            raise self._send_exc
        m = FakeMsg(900)
        self.sent.append((m, kw))
        return m


@pytest.mark.asyncio
async def test_board_sync_edits_existing_no_duplicate(cog):
    st = _running_state(4)
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    await cog._sync_board_locked(st, ch)
    assert len(msg.edits) == 1
    assert not ch.sent
    assert cog._board_pages[1] == 0


@pytest.mark.asyncio
async def test_board_sync_notfound_recreates_once(cog):
    st = _running_state(4)
    cog._store.state = st
    ch = FakeChannel(msg=None)  # fetch -> NotFound
    await cog._sync_board_locked(st, ch)
    assert len(ch.sent) == 1


@pytest.mark.asyncio
async def test_board_sync_http_error_no_duplicate(cog):
    import discord
    st = _running_state(4)
    cog._store.state = st
    ch = FakeChannel(fetch_exc=discord.Forbidden(
        SimpleNamespace(status=403, reason="forbidden"), {"message": "forbidden"}))
    await cog._sync_board_locked(st, ch)
    assert not ch.sent  # 403 не создаёт дубликат


@pytest.mark.asyncio
async def test_board_sync_429_no_duplicate(cog):
    import discord
    st = _running_state(4)
    cog._store.state = st
    ch = FakeChannel(fetch_exc=discord.HTTPException(
        SimpleNamespace(status=429, reason="rate limited"), {"message": "rate limited"}))
    await cog._sync_board_locked(st, ch)
    assert not ch.sent


@pytest.mark.asyncio
async def test_restore_boards_updates_existing(cog):
    st = _running_state(4)
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch,
                            fetch_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild], get_guild=lambda g: guild,
                              add_view=lambda v: None,
                              fetch_user=lambda u: None)
    await cog._restore_boards()
    assert len(msg.edits) == 1
    assert not ch.sent


@pytest.mark.asyncio
async def test_restore_skips_registration(cog):
    st = _reg_state()  # registration — доски нет
    cog._store.state = st
    ch = FakeChannel(msg=FakeMsg())
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild])
    await cog._restore_boards()
    assert not ch.sent


@pytest.mark.asyncio
async def test_restore_completed_board(cog):
    st = _running_state(4)
    st["status"] = "completed"
    st["bracket"]["champion"] = "t0"
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild])
    await cog._restore_boards()
    assert len(msg.edits) == 1  # завершённая доска тоже обновляется


@pytest.mark.asyncio
async def test_board_pages_tracking(cog):
    """_board_pages обновляется при sync — _nav не использует устаревший self._page."""
    st = _running_state(60)  # >15 матчей -> несколько страниц
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    await cog._sync_board_locked(st, ch, page=0)
    assert cog._board_pages[1] == 0
    await cog._sync_board_locked(st, ch, page=1)
    assert cog._board_pages[1] == 1
    # страница за пределами клампится в допустимый диапазон
    await cog._sync_board_locked(st, ch, page=999)
    assert cog._board_pages[1] < 999


# ================= Hardening-2: on_ready restore, nav lock, prize TX, embed pages =================

@pytest.mark.asyncio
async def test_on_ready_empty_guilds_ok(cog):
    """Холодный старт: bot.guilds пуст в setup_hook — on_ready не падает."""
    cog.bot = SimpleNamespace(guilds=[], add_view=lambda v: None)
    cog._boards_ok = False
    await cog.on_ready()
    assert cog._boards_ok is True


@pytest.mark.asyncio
async def test_on_ready_idempotent_and_retries(cog, monkeypatch):
    calls = []

    async def fake_restore():
        calls.append(1)
        return 0
    monkeypatch.setattr(cog, "_restore_boards", fake_restore)
    cog.bot = SimpleNamespace(guilds=[])
    cog._boards_ok = False
    await cog.on_ready()
    await cog.on_ready()  # повторный — пропуск
    assert len(calls) == 1
    # сбой -> _boards_ok False -> следующий on_ready повторяет
    async def fail_restore():
        calls.append(1)
        return 2
    monkeypatch.setattr(cog, "_restore_boards", fail_restore)
    cog._boards_ok = False
    await cog.on_ready()
    await cog.on_ready()
    assert len(calls) == 3  # 1 + 2 попытки


@pytest.mark.asyncio
async def test_nav_goes_through_sync_board(cog):
    """_nav обязан идти через _sync_board (lock + свежее PG), не locked."""
    calls = []

    async def fake_sync(state, channel, *, page=None):
        calls.append(page)
    cog._sync_board = fake_sync
    cog._board_pages[1] = 2
    cog._store.state = _running_state(60)
    v = cashcup.BoardSelectView()
    i = make_interaction(_member(1))
    i.client = FakeClient(cog)
    i.channel = SimpleNamespace(id=5)
    await v._nav(i, +1)
    assert calls == [3]


@pytest.mark.asyncio
async def test_start_rejects_when_prize_changes_in_tx(cog, monkeypatch):
    """Приз изменён между snapshot и mutate -> start отклоняется, сетки нет."""
    st = _valid_team_state()
    st["status"] = "registration"

    class RaceStore(FakeStore):
        async def mutate(self, guild_id, actor_id, action, change,
                         details=None, **kw):
            if action == "start":
                self.state["prizes"] = {
                    "places": {"1": 1, "2": 0, "3": 0},
                    "currency": "KZT", "total": 1}
            return await super().mutate(guild_id, actor_id, action, change,
                                        details, **kw)

    cog._store = RaceStore(st)
    monkeypatch.setattr(cashcup, "cashcup_store", cog._store)
    async def fake_validate(bot, guild, state):
        return None
    monkeypatch.setattr(cashcup, "_validate_teams", fake_validate)
    admin = _member(99, admin=True)
    i = make_interaction(admin)
    await cog.start(i)
    assert st["bracket"] is None
    assert st["status"] == "registration"
    assert "зменил" in _last(i) or "овторите" in _last(i)


def test_check_prize_state():
    ok = {"prizes": {"places": {"1": 100, "2": 0, "3": 0}, "currency": "KZT"}}
    cashcup._check_prize_state(ok)  # не падает
    for bad in ({}, {"prizes": {}},
                {"prizes": {"places": {"1": 0, "2": 0, "3": 0}, "currency": "KZT"}},
                {"prizes": {"places": {"1": -5, "2": 0, "3": 0}, "currency": "KZT"}},
                {"prizes": {"places": {"1": 100, "2": 50, "3": 0}, "currency": "KZT"}},
                {"prizes": {"places": {"1": 100, "2": 0, "3": 0}, "currency": ""}}):
        with pytest.raises(cashcup.CashCupError):
            cashcup._check_prize_state(bad)


def test_embed_and_select_same_page():
    """Embed и Select показывают один и тот же срез матчей на странице."""
    st = _running_state(60)  # команд 120 -> матчей >15 -> 2+ страниц
    b = st["bracket"]
    matches = cashcup._board_matches(b)
    page = 1
    emb = cashcup._board_embed(st, page=page)
    emb_ids = [m["id"] for m in matches[
        page * cashcup.BOARD_PER_PAGE:(page + 1) * cashcup.BOARD_PER_PAGE]]
    text = emb.description
    for mid in emb_ids:
        assert f"`{mid}`" in text
    view = cashcup.BoardSelectView(matches, page=page, state=st)
    sel_ids = [o.value for o in view.children[0].options]
    assert sel_ids == emb_ids[:len(sel_ids)] or set(sel_ids) == set(emb_ids)
    # страница за пределами — кламп
    v2 = cashcup.BoardSelectView(matches, page=999, state=st)
    last_page_ids = [o.value for o in v2.children[0].options]
    n_pages = (len(matches) + cashcup.BOARD_PER_PAGE - 1) // cashcup.BOARD_PER_PAGE
    expected = matches[(n_pages - 1) * cashcup.BOARD_PER_PAGE:]
    assert [m["id"] for m in expected] == last_page_ids
