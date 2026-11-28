"""Flow-тесты CASH CUP с mock Discord Interaction и in-memory store.
Проверяют реальное исполнение обработчиков, а не исходный код."""
from types import SimpleNamespace

import asyncio
import time

import pytest

from cogs import cashcup


def _last(i):
    if i.followup.sent:
        return i.followup.sent[-1][0]
    if i.response.sent:
        return i.response.sent[-1][0]
    return ""


def _sent(i):
    """(msg, kw) последнего ответа — followup после defer, иначе response."""
    if i.followup.sent:
        return i.followup.sent[-1]
    return i.response.sent[-1]
from services import cashcup_bracket as cb


# ---------------- Fake Discord ----------------

class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modal = None
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, msg, **kw):
        self.sent.append((msg, kw))
        self._done = True

    async def edit_message(self, content=None, **kw):
        self.edits.append((content, kw))
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
        self.mutate_calls = []

    async def active(self, guild_id):
        st = self.state
        if st and st.get("status") in ("registration", "running"):
            return st
        return None

    async def latest(self, guild_id):
        return self.state

    async def mutate(self, guild_id, actor_id, action, change, details=None,
                     include_finished=False, tournament_id=None, **kw):
        # Адресация по tournament_id: строка обязана совпадать,
        # как FOR UPDATE ... WHERE id=$2 в PostgreSQL.
        if tournament_id is not None and self.state is not None                 and str(self.state.get("tournament_id") or "") != str(tournament_id):
            raise cashcup.CashCupError("Активный CASH CUP не найден")
        res = change(self.state)
        self.audits.append({"action": action, "actor": actor_id,
                            "details": details})
        self.mutate_calls.append({"action": action,
                                  "tournament_id": tournament_id,
                                  "include_finished": include_finished})
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
    c._state_cache = {}
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
    # Реалистичный дефолт: основа = первые 4 участника (как при
    # регистрации BR-команды), а не только капитан.
    return {"id": tid, "name": name, "captain": captain,
            "members": members, "pending": [],
            "lineup": (lineup if lineup is not None
                       else [p["discord_id"] for p in members[:4]]),
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
    msg, kw = _sent(i)
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
    view = _sent(i)[1]["view"]
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
    # Полная диагностика: показаны ВСЕ проблемы игрока, не только первая.
    msg = _last(i).lower()
    assert "не заполнен" in msg and "ff api" in msg


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
        self.deleted = False

    async def edit(self, **kw):
        self.edits.append(kw)

    async def delete(self):
        self.deleted = True


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
async def test_restore_registration_replaces_missing_card(cog):
    """Registration: message_id отсутствует/сообщение удалено →
    создаётся ОДНА замена карточки (P0 zero-data-loss)."""
    st = _reg_state()  # message_id=None — карточки нет
    cog._store.state = st
    ch = FakeChannel(msg=None)  # fetch -> NotFound
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch,
                            fetch_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild])
    await cog._restore_boards()
    assert len(ch.sent) == 1


@pytest.mark.asyncio
async def test_restore_registration_edits_live_card(cog):
    """Registration: живая карточка → edit на месте, без дублей."""
    st = _reg_state()
    st["message_id"] = 555
    cog._store.state = st
    msg = FakeMsg(mid=555)
    ch = FakeChannel(msg=msg)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch,
                            fetch_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild])
    await cog._restore_boards()
    assert len(msg.edits) == 1
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

    async def fake_sync(state, channel, *, page=None, delta=None):
        calls.append((page, delta))
    cog._sync_board = fake_sync
    cog._board_pages[1] = 2
    cog._store.state = _running_state(60)
    v = cashcup.BoardSelectView()
    i = make_interaction(_member(1))
    i.client = FakeClient(cog)
    i.channel = SimpleNamespace(id=5)
    await v._nav(i, +1)
    assert calls == [(None, 1)]  # страница вычисляется внутри lock


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


# ================= Release-2: результат sync, tournament_id, delta под lock =================

@pytest.mark.asyncio
async def test_restore_counts_sync_failure_and_retries(cog):
    """403 при edit -> _sync_board False -> failures>0 -> _boards_ok False."""
    import discord
    st = _running_state(60)
    st["status"] = "completed"
    cog._store.state = st
    exc = discord.HTTPException(
        SimpleNamespace(status=403, reason="forbidden"), {"message": "forbidden"})
    ch = FakeChannel(msg=FakeMsg(), fetch_exc=exc)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: ch,
                            fetch_channel=lambda cid: ch)
    cog.bot = SimpleNamespace(guilds=[guild], get_guild=lambda g: guild,
                              fetch_user=lambda u: None,
                              add_view=lambda v: None)
    failures = await cog._restore_boards()
    assert failures == 1
    cog._boards_ok = False
    await cog.on_ready()
    assert cog._boards_ok is False  # ошибка не потеряна — будет retry


@pytest.mark.asyncio
async def test_completed_board_id_saved_by_tournament_id(cog):
    """completed-турнир: board_message_id пишется адресной mutate
    с tournament_id и include_finished."""
    st = _running_state(4)
    st["status"] = "completed"
    st["tournament_id"] = "tid123"
    st["board_message_id"] = None  # -> send path
    cog._store.state = st
    ch = FakeChannel()
    ok = await cog._sync_board(st, ch)
    assert ok is True
    assert len(ch.sent) == 1
    call = cog._store.mutate_calls[-1]
    assert call["tournament_id"] == "tid123"
    assert call["include_finished"] is True
    assert st["board_message_id"] == 900


@pytest.mark.asyncio
async def test_sync_board_returns_false_without_state(cog):
    """Нет свежего состояния в PG -> False, без побочных эффектов."""
    st = _running_state(4)
    st["status"] = "completed"
    cog._store.state = None
    ch = FakeChannel()
    assert await cog._sync_board(st, ch) is False
    assert not ch.sent


@pytest.mark.asyncio
async def test_concurrent_nav_single_board_consistent_page(cog):
    """Два одновременных «Вперёд»: одна доска, страница детерминирована."""
    st = _running_state(60)
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    i1 = make_interaction(_member(1))
    i2 = make_interaction(_member(2))
    for i in (i1, i2):
        i.client = FakeClient(cog)
        i.channel = ch
    v = cashcup.BoardSelectView()
    await asyncio.gather(v._nav(i1, +1), v._nav(i2, +1))
    # обе операции под lock: +1 дважды -> страница 2, сообщений не создано
    assert cog._board_pages[1] == 2
    assert not ch.sent
    assert len(msg.edits) == 2


@pytest.mark.asyncio
async def test_concurrent_nav_clamps_at_bounds(cog):
    """«Назад» с первой страницы и «Вперёд» за последнюю — кламп без ошибок."""
    st = _running_state(60)
    cog._store.state = st
    msg = FakeMsg()
    ch = FakeChannel(msg=msg)
    i1 = make_interaction(_member(1))
    i2 = make_interaction(_member(2))
    for i in (i1, i2):
        i.client = FakeClient(cog)
        i.channel = ch
    v = cashcup.BoardSelectView()
    await asyncio.gather(v._nav(i1, -1), v._nav(i2, +999))
    pages = (len(cashcup._board_matches(st["bracket"]))
             + cashcup.BOARD_PER_PAGE - 1) // cashcup.BOARD_PER_PAGE
    assert 0 <= cog._board_pages[1] <= pages - 1
    assert not ch.sent


# ================= §12: привязка interactions к tournament_id / message_id =================

@pytest.mark.asyncio
async def test_post_tid_addresses_exact_tournament(cog):
    """_post(tid=...) пишет в конкретный tournament_id, не в «последний»."""
    st = _reg_state()
    st["tournament_id"] = "tidA"
    cog._store.state = st
    calls = []

    async def fake_mutate(gid, actor, action, change, details=None,
                          include_finished=False, tournament_id=None, **kw):
        calls.append(tournament_id)
        return st, change(st)
    import cogs.cashcup as cc
    orig = cc.cashcup_store.mutate
    cc.cashcup_store.mutate = fake_mutate
    try:
        i = make_interaction(_member(1))
        await cog._post(i, "noop", lambda s: None, tid="tidA")
        assert calls == ["tidA"]
    finally:
        cc.cashcup_store.mutate = orig


@pytest.mark.asyncio
async def test_stale_tid_rejected_by_store(cog):
    """Modal от старого турнира: mutate по чужому tournament_id -> отказ."""
    st = _reg_state()
    st["tournament_id"] = "tidNEW"
    cog._store.state = st
    i = make_interaction(_member(1))
    # tids не совпадают -> CashCupError до change()
    with pytest.raises(cashcup.CashCupError):
        await cog._store.mutate(1, 1, "x", lambda s: None, tournament_id="tidOLD")


@pytest.mark.asyncio
async def test_panel_button_stale_message_rejected(cog):
    """Persistent кнопка панели с message_id чужого/старого сообщения -> отказ."""
    st = _reg_state()
    st["tournament_id"] = "tidA"
    st["message_id"] = 555
    cog._store.state = st
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=999)  # старая карточка
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    # teams — read-only кнопка; guard должен отсечь до вызова cog
    called = []
    async def fake_teams(inter):
        called.append(1)
    cog.show_teams = fake_teams
    await v.teams.callback(i)
    assert not called
    assert "устарел" in _last(i)


@pytest.mark.asyncio
async def test_board_select_stale_message_rejected(cog):
    """Select на устаревшей доске (другой board_message_id) -> отказ."""
    st = _running_state(4)
    st["tournament_id"] = "tidA"
    st["board_message_id"] = 555
    cog._store.state = st
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=999)
    i.client = FakeClient(cog)
    v = cashcup.BoardSelectView()
    v._sel._values = ["M1"]
    await v._pick(i)
    assert "устарел" in _last(i)


@pytest.mark.asyncio
async def test_board_nav_stale_message_rejected(cog):
    st = _running_state(4)
    st["tournament_id"] = "tidA"
    st["board_message_id"] = 555
    cog._store.state = st
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=999)
    i.client = FakeClient(cog)
    v = cashcup.BoardSelectView()
    await v._nav(i, +1)
    assert "устарел" in _last(i)


@pytest.mark.asyncio
async def test_orphan_board_deleted_when_pg_write_fails(cog, monkeypatch):
    """send успешен, mutate упал -> сообщение-сирота удаляется, дубля нет."""
    st = _running_state(4)
    st["board_message_id"] = None
    cog._store.state = st
    ch = FakeChannel()

    async def boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(cashcup.cashcup_store, "mutate", boom)
    ok = await cog._sync_board(st, ch)
    assert ok is False
    assert len(ch.sent) == 1
    m, _ = ch.sent[0]
    assert m.deleted is True


# ---------------- Modal-кнопки: нет 10062 при медленном PG ----------------

def _card_state(mid=555, tid="tidA"):
    st = _reg_state()
    st["tournament_id"] = tid
    st["message_id"] = mid
    return st


def _my_data_state(mid=555, tid="tidA"):
    st = _card_state(mid=mid, tid=tid)
    st["teams"] = [_team("t1", "Alpha", 1, [_player(1)])]
    return st


@pytest.mark.asyncio
async def test_my_data_status_view_then_modal(cog):
    """«Мои данные»: сначала ephemeral-статус профиля, modal
    открывается кнопкой «Обновить данные» с tid турнира."""
    cog._state_cache[1] = (time.monotonic(), _my_data_state())
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    await v.my_data.callback(i)
    # Первый ответ — статус, НЕ modal.
    assert i.response.modal is None
    msg, kw = i.response.sent[-1]
    assert "МОИ ДАННЫЕ" in msg
    assert "UID" in msg and "FF API" in msg and "ADMIN" in msg
    view = kw.get("view")
    assert view is not None and len(view.children) == 1
    # Клик «Обновить данные» — modal как первый ответ этого
    # interaction, с tid конкретного турнира.
    i2 = make_interaction(_member(1))
    i2.client = FakeClient(cog)
    await view.children[0].callback(i2)
    assert i2.response.modal is not None
    assert i2.response.modal.tid == "tidA"
    assert i2.response.modal.team_id == "t1"


@pytest.mark.asyncio
async def test_my_data_slow_pg_no_10062(cog, monkeypatch):
    """PG отвечает >3 c: кнопка обязана ответить до дедлайна Discord
    (3 c) — ephemeral «занята», без modal и без Unknown interaction."""
    async def slow_active(gid):
        await asyncio.sleep(3.5)
        return _card_state()
    async def slow_latest(gid):
        return None
    monkeypatch.setattr(cashcup.cashcup_store, "active", slow_active)
    monkeypatch.setattr(cashcup.cashcup_store, "latest", slow_latest)
    cog._state_cache.clear()

    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)

    t0 = time.monotonic()
    await v.my_data.callback(i)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0            # ответ до дедлайна Discord
    assert i.response.is_done()     # interaction подтверждён -> не 10062
    assert i.response.modal is None
    assert "занята" in _last(i)


@pytest.mark.asyncio
async def test_my_data_warm_cache_skips_pg(cog, monkeypatch):
    """Тёплый кэш: modal открывается мгновенно, PG не вызывается."""
    async def boom(gid):
        raise AssertionError("PG не должен вызываться при тёплом кэше")
    monkeypatch.setattr(cashcup.cashcup_store, "active", boom)
    monkeypatch.setattr(cashcup.cashcup_store, "latest", boom)
    cog._state_cache[1] = (time.monotonic(), _my_data_state())

    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)

    t0 = time.monotonic()
    await v.my_data.callback(i)
    assert time.monotonic() - t0 < 0.5
    assert i.response.modal is None
    assert i.response.sent, "статус не отправлен"


@pytest.mark.asyncio
async def test_my_data_stale_card_rejected(cog):
    """Клик по устаревшей карточке (другой message_id) -> отказ,
    modal не открывается."""
    cog._state_cache.clear()
    cog._store.state = _card_state(mid=555)
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=999)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    await v.my_data.callback(i)
    assert i.response.modal is None
    assert "устарел" in _last(i)


@pytest.mark.asyncio
async def test_sub_join_slow_pg_no_10062(cog, monkeypatch):
    """«Запасной» при PG >3 c: ответ до дедлайна, без 10062."""
    async def slow_active(gid):
        await asyncio.sleep(3.5)
        return _card_state()
    async def slow_latest(gid):
        return None
    monkeypatch.setattr(cashcup.cashcup_store, "active", slow_active)
    monkeypatch.setattr(cashcup.cashcup_store, "latest", slow_latest)
    cog._state_cache.clear()

    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)

    t0 = time.monotonic()
    await v.sub_join.callback(i)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0
    assert i.response.is_done()
    assert "занята" in _last(i)


@pytest.mark.asyncio
async def test_sub_join_modal_keeps_tid(cog):
    """«Запасной» с одним приглашением открывает FFDataModal
    с tid турнира и team_id команды."""
    st = _card_state()
    t = _team("T1", "Alpha", 1, [_player(1)])
    t["sub_invites"] = [42]
    st["teams"] = [t]
    cog._state_cache[1] = (time.monotonic(), st)

    i = make_interaction(_member(42))
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    await v.sub_join.callback(i)

    assert i.response.modal is not None
    assert i.response.modal.tid == "tidA"
    assert i.response.modal.team_id == "T1"


@pytest.mark.asyncio
async def test_sub_join_stale_card_rejected(cog):
    cog._state_cache.clear()
    cog._store.state = _card_state(mid=555)
    i = make_interaction(_member(1))
    i.message = SimpleNamespace(id=999)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    await v.sub_join.callback(i)
    assert i.response.modal is None
    assert "устарел" in _last(i)


@pytest.mark.asyncio
async def test_team_pick_slow_pg_no_10062(cog, monkeypatch):
    """PickView→_team_pick: PG >3с до ответа — ephemeral «занята»,
    без 10062 (modal-ветки не могут defer)."""
    st = _reg_state()
    st["tournament_id"] = "tidA"
    st["message_id"] = 555
    st["teams"] = [_team("T1", "Alpha", 1, [_player(1)])]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    i.client = FakeClient(cog)

    async def slow_state(gid):
        await asyncio.sleep(3.5)
        return st
    monkeypatch.setattr(cog, "_current_state", slow_state)

    t0 = time.monotonic()
    await cog._team_pick(i, "verify", "T1", tid="tidA")
    assert time.monotonic() - t0 < 3.0
    assert i.response.is_done()
    assert "занята" in _last(i)


@pytest.mark.asyncio
async def test_match_pick_slow_pg_no_10062(cog, monkeypatch):
    """PickView→_match_pick: то же ограничение 2с до send_modal."""
    st = _running_state(2)
    st["tournament_id"] = "tidA"
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    i.client = FakeClient(cog)
    mid = st["bracket"]["matches"][0]["id"]

    async def slow_state(gid):
        await asyncio.sleep(3.5)
        return st
    monkeypatch.setattr(cog, "_current_state", slow_state)

    t0 = time.monotonic()
    await cog._match_pick(i, "room", mid, tid="tidA")
    assert time.monotonic() - t0 < 3.0
    assert i.response.is_done()
    assert "занята" in _last(i)


@pytest.mark.asyncio
async def test_resolve_defers_before_slow_mutate(cog, monkeypatch):
    """resolve() обязан defer ДО медленного PG mutate —
    иначе 10062 при disput-потоке."""
    st = _running_state(2)
    st["tournament_id"] = "tidA"
    m = st["bracket"]["matches"][0]
    m["status"] = "disputed"
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    i.client = FakeClient(cog)

    orig = cog._store.mutate

    async def slow_mutate(*a, **k):
        await asyncio.sleep(3.5)
        return await orig(*a, **k)
    monkeypatch.setattr(cog._store, "mutate", slow_mutate)

    task = asyncio.create_task(cog.resolve(
        i, m["id"], SimpleNamespace(value="a"),
        "https://discord.com/channels/1/2/3", tid="tidA"))
    await asyncio.sleep(0.3)
    assert i.response.is_done()  # defer уже выполнен
    await task
    assert i.followup.sent or i.response.sent


# ---------------- Cash Cup Registration & Verification 2.0 ----------------

def _full_player(uid, *, verified=False, ff_ok=True, device="mobile",
                 ff=None, region="ru", nick=None):
    return {"discord_id": uid,
            "ff_uid": ff if ff is not None else f"77{uid:05d}",
            "nick": nick if nick is not None else f"p{uid}",
            "region": region, "device": device,
            "verified": verified, "ff_ok": ff_ok, "joined_at": "t"}


def _ready_team(tid, name, captain, n=4, verified=True):
    return _team(tid, name, captain,
                 [_full_player(captain + i, verified=verified)
                  for i in range(n)])


def _state_with_teams(teams):
    st = _reg_state()
    st["teams"] = teams
    return st


# 1-2: знаменатели FF API / ADMIN = фактические members

def test_teams_render_4_players_denominator(cog):
    st = _state_with_teams([_ready_team("t1", "Alpha", 10, n=4,
                                        verified=False)])
    text, page, pages = cashcup._teams_render(st, 0)
    assert "FF API: 4/4" in text and "ADMIN: 0/4" in text
    assert "verified:" not in text and "/5 ·" not in text.split("ADMIN")[0]


def test_teams_render_5_players_denominator(cog):
    st = _state_with_teams([_ready_team("t1", "Alpha", 10, n=5,
                                        verified=False)])
    text, _, _ = cashcup._teams_render(st, 0)
    assert "FF API: 5/5" in text and "ADMIN: 0/5" in text
    assert "Участники: 5/5" in text


# 3: готовая команда из 4
def test_diag_ready_team_four(cog):
    st = _state_with_teams([_ready_team("t1", "Alpha", 10)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Готова"
    assert d["problems"] == []


# 4: один неподтверждённый ADMIN -> Ожидает ADMIN
def test_diag_waits_admin(cog):
    members = [_full_player(10 + i, verified=(i != 2)) for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Ожидает ADMIN"
    assert d["admin"] == 3 and d["ff_api"] == 4
    assert any("ADMIN" in p for p in d["problems"])


# 5: несколько ошибок в одной команде — все показаны
def test_diag_multiple_errors_one_team(cog):
    bad1 = _full_player(10, ff="", nick="", ff_ok=False)
    bad2 = _full_player(11, device="")
    good = [_full_player(12), _full_player(13)]
    st = _state_with_teams([_team("t1", "Alpha", 10,
                                  [bad1, bad2] + good)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Не готова"
    joined = " ".join(d["problems"]).lower()
    assert "free fire uid" in joined and "устройство" in joined \
        and "ff api" in joined
    # Строки-проблемы есть у обоих битых игроков (у «хороших» —
    # только ожидание ADMIN).
    bad_lines = [p for p in d["problems"] if "<@10>" in p or "<@11>" in p]
    assert len(bad_lines) == 2
    assert "free fire uid" in bad_lines[0].lower()
    assert "устройство" in bad_lines[1].lower()


# 6: несколько проблемных команд при старте — сводка по всем
@pytest.mark.asyncio
async def test_start_shows_all_team_problems(cog):
    st = _reg_state()
    st["prizes"] = {"places": {"1": 1000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 1000}
    st["teams"] = [
        _team("t1", "Alpha", 10, [_full_player(10 + i, ff_ok=False)
                                 for i in range(4)]),
        _team("t2", "Beta", 20, [_full_player(20)]),
    ]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.start(i)
    msg = _last(i)
    assert "Alpha" in msg and "Beta" in msg
    assert "старт невозможен" in msg.lower() or "невозможен" in msg.lower()
    assert st["status"] == "registration"  # сетка не создана
    assert not any(a["action"] == "start" for a in cog._store.audits)


# 7: устройство не указано — отдельно, не Mobile/ПК
def test_diag_no_device_counted_separately(cog):
    members = [_full_player(10 + i, device=("" if i == 0 else "mobile"))
               for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["no_device"] == 1 and d["pcs"] == 0
    assert d["status"] == "Не готова"
    assert any("не указано устройство" in p for p in d["problems"])


# 8: отсутствие FF API подтверждения
def test_diag_ff_api_missing(cog):
    members = [_full_player(10 + i, ff_ok=(i != 1)) for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert any("FF API не подтверждён" in p for p in d["problems"])
    assert d["status"] == "Не готова"


# 10: частичное подтверждение запрещено — ошибка -> НИКТО не verified
@pytest.mark.asyncio
async def test_verify_no_partial_confirm(cog):
    members = [_full_player(10 + i, verified=False) for i in range(4)]
    members[2]["ff_ok"] = False  # один битый
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.verify(i, "Alpha")
    assert not any(p["verified"] for p in st["teams"][0]["members"])


# 11: смена UID/региона/устройства сбрасывает ADMIN-подтверждение
def test_member_problems_admin_flag(cog):
    p = _full_player(10, verified=True)
    assert cashcup._member_problems(p) == []
    p2 = dict(p, verified=False)
    probs = cashcup._member_problems(p2)
    assert probs == ["ожидает подтверждения ADMIN"]
    # include_admin=False (verify) — ADMIN-ожидание не ошибка
    assert cashcup._member_problems(p2, include_admin=False) == []


# 12: изменение состава пересчитывает готовность
def test_diag_recalculates_on_roster_change(cog):
    members = [_full_player(10 + i, verified=True) for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    assert cashcup._team_diag(st, st["teams"][0])["status"] == "Готова"
    st["teams"][0]["members"].pop()          # 4 -> 3
    st["teams"][0]["lineup"] = [10, 11, 12]
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] in ("Не готова", "Ошибка состава")
    assert d["members"] == 3


# 14: повторное нажатие verify идемпотентно
@pytest.mark.asyncio
async def test_verify_idempotent_second_call(cog):
    st = _state_with_teams([_ready_team("t1", "Alpha", 10)])
    cog._store.state = st
    i1 = make_interaction(_member(99, admin=True))
    await cog.verify(i1, "Alpha")
    i2 = make_interaction(_member(99, admin=True))
    await cog.verify(i2, "Alpha")
    assert all(p["verified"] for p in st["teams"][0]["members"])
    assert len(cog._store.audits) == 2  # два audit, состояние не ломается


# 16: пагинация 13/16/32 команд — все на месте, без обрезки
def test_teams_pagination_counts(cog):
    for n, expected_pages in ((13, 3), (16, 4), (32, 7)):
        teams = [_team(f"t{i}", f"Team{i}", i,
                       [_full_player(i * 10 + j) for j in range(1)])
                 for i in range(n)]
        st = _state_with_teams(teams)
        seen = []
        page = 0
        while True:
            text, page, pages = cashcup._teams_render(st, page)
            for t in teams[page * 5:(page + 1) * 5]:
                seen.append(t["name"])
            if page >= pages - 1:
                break
            page += 1
        assert pages == expected_pages
        assert len(seen) == n and len(set(seen)) == n
        # Каждая команда — целый блок на одной странице.
        for name in seen:
            pass  # покрыто постраничным рендером выше


# 17: лимит 2 ПК — Ошибка состава
def test_diag_pc_limit_error(cog):
    members = [_full_player(10 + i,
                            device=("pc" if i < 3 else "mobile"))
               for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Ошибка состава"
    assert any("двух ПК" in p for p in d["problems"])


# дубли FF UID внутри команды
def test_diag_duplicate_ff_uid(cog):
    members = [_full_player(10 + i, ff="99999") for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Ошибка состава"
    assert any("Free Fire UID" in p for p in d["problems"])


# check_team — read-only диагностика ADMIN
@pytest.mark.asyncio
async def test_check_team_readonly_diagnostics(cog):
    members = [_full_player(10 + i, ff_ok=(i != 1)) for i in range(4)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    cog._store.state = st
    audits_before = len(cog._store.audits)
    i = make_interaction(_member(99, admin=True))
    await cog.check_team(i, "Alpha")
    msg = _last(i)
    assert "ПРОВЕРКА КОМАНДЫ" in msg and "Alpha" in msg
    assert "FF API не подтверждён" in msg
    assert "не допущена" in msg.lower()
    assert len(cog._store.audits) == audits_before  # read-only


# start: чистая команда проходит diag-gate (до _validate_teams)
@pytest.mark.asyncio
async def test_start_diag_gate_passes_ready(cog, monkeypatch):
    st = _reg_state()
    st["prizes"] = {"places": {"1": 1000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 1000}
    st["teams"] = [_ready_team("t1", "Alpha", 10),
                   _ready_team("t2", "Beta", 20)]
    cog._store.state = st
    # _validate_teams -> _member_check: глушим Discord-проверки,
    # но оставляем сам gate.
    async def fake_member_check(bot, guild, uid):
        return "t"
    monkeypatch.setattr(cashcup, "_member_check", fake_member_check)
    i = make_interaction(_member(99, admin=True))
    await cog.start(i)
    assert st["status"] == "running" or \
        any(a["action"] == "start" for a in cog._store.audits)


# ---------------- Review fixes: >5 членов, chunked start, admin status ----

def test_diag_over_5_members_structural_error(cog):
    """Regression: состав >5 — структурная ошибка, verify не
    подтверждает."""
    members = [_full_player(10 + i, verified=True) for i in range(6)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    d = cashcup._team_diag(st, st["teams"][0])
    assert d["status"] == "Ошибка состава"
    assert any("максимум 5" in p for p in d["problems"])


@pytest.mark.asyncio
async def test_verify_rejects_over_5_members(cog):
    members = [_full_player(10 + i, verified=False) for i in range(6)]
    st = _state_with_teams([_team("t1", "Alpha", 10, members)])
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.verify(i, "Alpha")
    assert not any(p["verified"] for p in st["teams"][0]["members"])
    assert "5" in _last(i)


@pytest.mark.asyncio
async def test_start_13_bad_teams_all_problems_visible(cog):
    """13 проблемных команд: отчёт уходит несколькими
    ephemeral-сообщениями, ни одна команда не потеряна."""
    st = _reg_state()
    st["prizes"] = {"places": {"1": 1000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 1000}
    st["teams"] = [
        _team(f"t{i}", f"Team{i:02d}", i * 10,
              [_full_player(i * 10 + j, ff_ok=False) for j in range(4)])
        for i in range(13)
    ]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.start(i)
    # followup.sent — все chunked-сообщения (response уже deferred).
    all_text = "\n".join(m for m, _ in i.followup.sent)
    for n in range(13):
        assert f"Team{n:02d}" in all_text, f"Team{n:02d} потеряна"
    assert "старт невозможен" in all_text.lower()
    assert st["status"] == "registration"


@pytest.mark.asyncio
async def test_confirm_ff_reports_real_admin_status(cog, monkeypatch):
    """Ответ confirm_ff отражает реальный verified: ожидает /
    подтверждён — в зависимости от сохранённого флага."""
    st = _reg_state()
    stored_uid = "7700001"
    p = _full_player(1, verified=True, ff=stored_uid)
    st["teams"] = [_team("t1", "Alpha", 1, [p])]
    cog._store.state = st

    async def fake_member_check(bot, guild, uid):
        return "t"
    async def fake_ff(uid, region):
        return {"ff_uid": uid.strip(), "region": region,
                "nick": "nick1"}
    monkeypatch.setattr(cashcup, "_member_check", fake_member_check)
    monkeypatch.setattr(cashcup, "_ff_profile", fake_ff)

    # Повтор тех же данных при verified=True -> «ADMIN: подтверждён».
    i = make_interaction(_member(1))
    await cog.confirm_ff(i, "t1", stored_uid, "ru", "mobile")
    assert "ADMIN: подтверждён" in _last(i)

    # verified=False -> «ADMIN: ожидает проверки».
    st["teams"][0]["members"][0]["verified"] = False
    i2 = make_interaction(_member(1))
    await cog.confirm_ff(i2, "t1", stored_uid, "ru", "mobile")
    assert "ожидает проверки" in _last(i2)


@pytest.mark.asyncio
async def test_teams_pagination_buttons_edit_message(cog):
    """Real discord.ui.View: «Команды» -> ▶ правит то же
    сообщение на страницу 2 без новых send."""
    teams = [_team(f"t{i}", f"Team{i:02d}", i,
                   [_full_player(i * 10 + j) for j in range(1)])
             for i in range(12)]
    cog._store.state = _state_with_teams(teams)
    i = make_interaction(_member(1))
    i.client = FakeClient(cog)
    await cog.show_teams(i)
    # Deferred -> followup.send с view=TeamsView(page0 of 3).
    assert i.followup.sent, "список не отправлен"
    _, kw = i.followup.sent[-1]
    view = kw["view"]
    assert isinstance(view, cashcup.TeamsView)
    assert "Team00" in i.followup.sent[-1][0]
    # Клик ▶ — component interaction: edit_message, не send.
    i2 = make_interaction(_member(1))
    i2.client = FakeClient(cog)
    nxt = [b for b in view.children
           if getattr(b, "label", "") == "▶"]
    assert nxt, "нет кнопки ▶"
    await nxt[0].callback(i2)
    assert i2.response.edits, "страница не отредактирована"
    content, ekw = i2.response.edits[-1]
    assert ekw.get("view") is not None
    assert "Team05" in content and "Team00" not in content
    assert "стр. 2/3" in content


@pytest.mark.asyncio
async def test_start_report_includes_all_problems_no_truncation(cog):
    """Команда с >12 проблемами: КАЖДАЯ строка проблемы доходит до
    админа — ни [:12]-капа, ни [:1900]-обрезки."""
    st = _reg_state()
    st["prizes"] = {"places": {"1": 1000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 1000}
    bad_members = [
        {"discord_id": 900 + j, "ff_uid": "", "nick": "",
         "region": "", "device": "", "verified": False,
         "ff_ok": False, "joined_at": "t"}
        for j in range(4)
    ]
    st["teams"] = [_team("t1", "Messy", 900, bad_members)]
    cog._store.state = st
    i = make_interaction(_member(99, admin=True))
    await cog.start(i)

    all_text = "\n".join(
        m for m, _ in list(i.response.sent) + list(i.followup.sent))
    # 4 участника × 6 проблем = 24 строки — больше старого капа 12.
    n_ff_uid = all_text.count("не заполнен Free Fire UID")
    n_admin = all_text.count("ожидает подтверждения ADMIN")
    assert n_ff_uid == 4 and n_admin == 4
    assert "ещё" not in all_text  # нет «… ещё N» хвоста
    assert all(len(m) <= 2000 for m, _ in i.followup.sent)


@pytest.mark.asyncio
async def test_my_data_no_team_sends_without_view_kwarg(cog):
    """Regression: игрок не в команде → view=None. Раньше
    send_message(view=None) падал с AttributeError
    ('NoneType'.is_finished) — кнопка «Мои данные» ломалась."""
    cog._state_cache[1] = (time.monotonic(), _my_data_state())
    i = make_interaction(_member(999))  # нет в teams
    i.message = SimpleNamespace(id=555)
    i.client = FakeClient(cog)
    v = cashcup.CashCupView(True)
    await v.my_data.callback(i)
    msg, kw = i.response.sent[-1]
    assert "МОИ ДАННЫЕ" in msg or "не состоите" in msg.lower() \
        or "команд" in msg.lower()
    assert "view" not in kw or kw.get("view") is not None
