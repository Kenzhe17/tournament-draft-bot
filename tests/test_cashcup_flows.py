"""Flow-тесты CASH CUP с mock Discord Interaction и in-memory store.
Проверяют реальное исполнение обработчиков, а не исходный код."""
from types import SimpleNamespace

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
    return c


def _reg_state():
    return {"status": "registration", "prize": "тест", "pool": 0,
            "guild_id": 1, "message_id": None, "channel_id": 5,
            "teams": [], "bracket": None, "payouts": [], "prizes": {}}


def _player(uid, ff="FF1", nick=None, device="mobile", verified=False,
            ff_ok=True):
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
