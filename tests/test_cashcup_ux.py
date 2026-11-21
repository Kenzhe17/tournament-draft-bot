"""Регрессии кнопочного UX CASH CUP: BR-команда → снапшот, self-service
UID, запасной, ADMIN-меню, приз только за 1 место, минимум slash-команд."""
from types import SimpleNamespace

import pytest

from cogs import cashcup


def _state():
    return {"status": "registration", "teams": [], "bracket": None,
            "payouts": [], "prize": "тест"}


def _player(uid, ff=""):
    return {"discord_id": uid, "device": "", "ff_uid": ff, "nick": "",
            "region": "", "verified": False, "joined_at": "t"}


def _team(tid, name, captain, members):
    return {"id": tid, "name": name, "captain": captain,
            "members": members, "pending": [], "lineup": [captain]}


# ---------- CommandTree: только create + panel ----------

def test_command_tree_minimal():
    names = {c.name for c in cashcup.CashCupCog.cashcup._children.values()}
    assert names == {"create", "panel"}
    for gone in ("team", "join", "approve", "leave", "kick", "verify",
                 "roster", "withdraw", "remove", "start", "checkin",
                 "result", "game", "room", "roominfo", "swap", "payout",
                 "cancel", "revert", "forfeit", "dq", "pause", "resume"):
        assert gone not in names
    # Группа /cashcup info удалена из дерева.
    assert not any(getattr(c, "_children", None) for c in
                   cashcup.CashCupCog.cashcup._children.values())


def test_no_cashcup_info_group():
    assert not hasattr(cashcup.CashCupCog, "info")


# ---------- Уникальность игроков ----------

def test_unique_player_empty_ff_uid_allowed():
    """Заглушки BR-регистрации (ff_uid="") не считаются дублем UID."""
    st = _state()
    st["teams"].append(_team("t1", "A", 1, [_player(1, "12345"), _player(2)]))
    cashcup._unique_player(st, 3, "")   # другой Discord, пустой UID — ок
    cashcup._unique_player(st, 4, "")
    with pytest.raises(cashcup.CashCupError):
        cashcup._unique_player(st, 1, "")          # тот же Discord
    with pytest.raises(cashcup.CashCupError):
        cashcup._unique_player(st, 9, "12345")     # чужой UID


def test_unique_player_pending_checked():
    st = _state()
    t = _team("t1", "A", 1, [_player(1)])
    t["pending"] = [_player(7, "999")]
    st["teams"].append(t)
    with pytest.raises(cashcup.CashCupError):
        cashcup._unique_player(st, 8, "999")


def test_find_cc_team_members_and_pending():
    st = _state()
    t1 = _team("t1", "A", 1, [_player(1)])
    t2 = _team("t2", "B", 2, [_player(2)])
    t2["pending"] = [_player(3)]
    st["teams"] = [t1, t2]
    assert cashcup._find_cc_team(st, 1)["id"] == "t1"
    assert cashcup._find_cc_team(st, 3)["id"] == "t2"
    assert cashcup._find_cc_team(st, 9) is None


# ---------- Публичная панель ----------

def test_panel_view_buttons():
    v = cashcup.CashCupView(True)
    ids = {getattr(c, "custom_id", "") for c in v.children}
    assert "cashcup:register_br" in ids
    assert "cashcup:my_data" in ids
    assert "cashcup:sub_join" in ids
    assert "cashcup:admin" in ids
    # Старых кнопок создания/join-команды нет.
    assert "cashcup:create_team" not in ids
    assert "cashcup:join_team" not in ids


def test_panel_shows_first_place_prize():
    st = _state()
    st["prizes"] = {"places": {"1": 100000, "2": 0, "3": 0},
                    "currency": "KZT", "total": 100000}
    emb = cashcup._panel(st)
    text = emb.description
    assert "ПРИЗ ЗА 1 МЕСТО" in text
    assert "100 000" in text.replace(",", " ") or "100000" in text.replace(" ", "")
    assert "2 —" not in text and "3 —" not in text


# ---------- ADMIN-меню ----------

def _menu_options(view):
    sel = view.children[0]
    return {o.value for o in sel.options}


def test_admin_menu_registration_actions():
    v = cashcup.AdminMenuView({"status": "registration", "bracket": None})
    opts = _menu_options(v)
    assert {"verify", "remove", "prize", "start", "cancel"} <= opts
    assert "result" not in opts and "payout" not in opts


def test_admin_menu_running_actions():
    st = {"status": "running",
          "bracket": {"matches": [{"id": "W1", "status": "disputed",
                                   "score": [0, 0]}]}}
    v = cashcup.AdminMenuView(st)
    opts = _menu_options(v)
    assert {"result", "room", "forfeit", "game", "dq", "revert",
            "pause", "resume", "resolve"} <= opts
    assert "start" not in opts and "verify" not in opts


def test_admin_menu_completed_actions():
    v = cashcup.AdminMenuView({"status": "completed", "bracket": {}})
    opts = _menu_options(v)
    assert "payout" in opts
    assert "result" not in opts and "start" not in opts


def test_admin_menu_under_25_options():
    for st in ({"status": "registration"}, {"status": "running"},
               {"status": "completed"}, None):
        assert len(_menu_options(cashcup.AdminMenuView(st))) <= 25


# ---------- MatchCardView: разделение player/admin ----------

def _member(user_id=1, admin=False, owner_id=0):
    guild = SimpleNamespace(owner_id=owner_id, id=1)
    perms = SimpleNamespace(administrator=admin, manage_guild=False)
    return SimpleNamespace(id=user_id, guild=guild, guild_permissions=perms,
                           roles=[])


def _match_state():
    return {"status": "running",
            "teams": [_team("t1", "A", 10, [_player(10)]),
                      _team("t2", "B", 20, [_player(20)])],
            "bracket": None}


def _match():
    return {"id": "W1", "a": "t1", "b": "t2", "bracket": "W", "bo": 1,
            "status": "ready", "score": [0, 0], "checkins": [],
            "games": [], "dispute": None}


def test_match_card_player_buttons_for_captain():
    cog = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    v = cashcup.MatchCardView(cog, _match_state(), _match(), _member(10))
    labels = [getattr(c, "label", "") for c in v.children]
    assert "Подтвердить готовность" in labels
    assert "Комната" in labels
    assert "Результат" not in labels  # не admin


def test_match_card_admin_buttons():
    cog = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    v = cashcup.MatchCardView(cog, _match_state(), _match(),
                              _member(99, admin=True))
    labels = [getattr(c, "label", "") for c in v.children]
    assert "Результат" in labels and "Тех. победа" in labels
    assert "Подтвердить готовность" not in labels  # не капитан


def test_match_card_disputed_shows_resolve():
    cog = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    m = _match()
    m["status"] = "disputed"
    st = _match_state()
    v = cashcup.MatchCardView(cog, st, m, _member(99, admin=True))
    labels = [getattr(c, "label", "") for c in v.children]
    assert "Решить спор" in labels


def test_match_card_gf_shows_game_button():
    cog = cashcup.CashCupCog.__new__(cashcup.CashCupCog)
    m = _match()
    m["bracket"] = "GF"
    v = cashcup.MatchCardView(cog, _match_state(), m, _member(99, admin=True))
    labels = [getattr(c, "label", "") for c in v.children]
    assert "Игра GF" in labels


# ---------- Модалки ----------

def test_ff_modal_fields():
    modal = cashcup.FFDataModal("t1")
    kinds = [type(c).__name__ for c in modal.children]
    assert kinds == ["TextInput"] * 3
    labels = [c.label for c in modal.children]
    assert any("UID" in l for l in labels)
    assert any("тройств" in l for l in labels)


def test_cb_modal_dynamic_fields():
    modal = cashcup._CbModal("t", [("A", "", True, 5), ("B", "", False, 5)],
                             lambda i, v: None)
    assert len(modal.children) == 2


# ---------- BR-интеграция (source-level, без Discord E2E) ----------

def test_register_br_uses_team_store_snapshot():
    src = open(cashcup.__file__, encoding="utf-8").read()
    idx = src.find("async def register_br_team")
    body = src[idx:idx + 3500]
    assert "team_store.get_by_captain" in body
    assert "br_team_id" in body          # независимый снапшот
    assert "needs_ff" in body            # игроки подтверждают UID сами
    assert "_unique_player" in body
    assert "_assert_registration" in body


def test_confirm_ff_guards():
    src = open(cashcup.__file__, encoding="utf-8").read()
    idx = src.find("async def confirm_ff")
    body = src[idx:idx + 4500]
    assert "sub_invites" in body          # запасной только по приглашению
    assert "_ff_profile" in body          # ник из API
    assert "_guard" in body               # членство/тег/VAC
    assert "pc" in body and "> 2" in body  # лимит 2 ПК
    assert "_assert_registration" in body


def test_sub_invite_flow_exists():
    src = open(cashcup.__file__, encoding="utf-8").read()
    assert "async def invite_sub" in src
    assert "async def sub_offer_teams" in src
    assert "sub_invites" in src


def test_prize_first_place_only_source():
    src = open(cashcup.__file__, encoding="utf-8").read()
    assert '"2": p2' not in src and '"3": p3' not in src
    assert "prize1" in src or "places" in src
