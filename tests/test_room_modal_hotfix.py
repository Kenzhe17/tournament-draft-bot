"""P0 hotfix: перезапись публичных карточек турниров.

Root cause: defer() без thinking=True на modal_submit/component —
DEFERRED_MESSAGE_UPDATE целится в сообщение-источник (публичная
карточка при единственном матче), edit_original_response
перезаписывал её служебным UI («Изменение отменено.»).

Покрывает:
- defer(..., thinking=True) в RoomModal/_apply_room_edit;
- «Отмена» никогда не редактирует публичную карточку;
- успешное сохранение комнаты — приватный ответ + штатное
  обновление карточки через update_tournament_message;
- ошибки GenerateMatchesButton — ephemeral, без перезаписи;
- force=True в update_tournament_message;
- card_repair scan/repair.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_spec = importlib.util.spec_from_file_location(
    "root_bot", Path(__file__).resolve().parent.parent / "bot.py"
)
bot_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot_module)

from models.tournament import Tournament, TournamentPhase
from views import room_view as rv


# ---------------------------------------------------------------------------
# Фейки Interaction
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, message=None):
        self.calls: list[tuple] = []
        self._done = False
        self._message = message

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kw):
        self.calls.append(("defer", kw))
        self._done = True

    async def edit_message(self, **kw):
        self.calls.append(("edit_message", kw))
        # Как в discord.py: response.edit_message() правит
        # сообщение-источник interaction.
        if self._message is not None:
            await self._message.edit(**kw)

    async def send_message(self, *a, **kw):
        self.calls.append(("send_message", (a, kw)))
        self._done = True


class _Followup:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, *a, **kw):
        self.sent.append((a, kw))


class _Client:
    def __init__(self):
        self.updates: list = []

    async def update_tournament_message(self, guild, tournament, **kw):
        self.updates.append((guild, tournament, kw))
        return True


class _IC:
    def __init__(self, *, message=None, user_id=101, client=None):
        self.message = message
        self.response = _Resp(message)
        self.followup = _Followup()
        self.original_edits: list[dict] = []
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.guild = SimpleNamespace(id=1)
        self.guild_id = 1
        self.channel_id = 2
        self.client = client or _Client()
        self.id = 1
        self.token = "tok"

    async def edit_original_response(self, **kw):
        self.original_edits.append(kw)

    async def send_modal(self, modal):
        self.modal = modal


class _PubMessage:
    """Публичная карточка турнира — фиксирует edit-вызовы."""

    def __init__(self, mid: int):
        self.id = mid
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)
        return self


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = "1_1000"
    t.message_id = 555
    t.phase = TournamentPhase.FINAL
    t.organizer_id = 42
    t.teams = [
        {"captain": "Cap0",
         "players": ["P0a", "P0b", "P0c", "P0d"]},
        {"captain": "Cap1",
         "players": ["P1a", "P1b", "P1c", "P1d"]},
    ]
    t.team_names = {0: "Team0", 1: "Team1"}
    t.player_user_ids = {"P0a": 101, "Cap0": 201}
    t.final_teams = [0, 1]
    return t


def _match(t):
    return rv.iter_tournament_matches(t)[0]


class _RoomStore:
    def __init__(self, existing=None):
        self.existing = existing
        self.upserts: list[tuple] = []

    def get_active_room(self, gid, cid, key):
        return self.existing

    def upsert_room(self, gid, cid, key, room_id, password, uid,
                    tournament_id=None):
        self.upserts.append((key, room_id, password, uid))
        return {"room": {"room_id": room_id, "password": password},
                "created": self.existing is None}

    def has_room(self, gid, cid, key):
        return self.existing is not None


@pytest.fixture
def room_env(monkeypatch):
    t = _tournament()
    m = _match(t)
    monkeypatch.setattr(rv, "store",
                        SimpleNamespace(get=lambda g, c: t))
    rs = _RoomStore(existing={"room_id": "old", "password": "pw"})
    monkeypatch.setattr(rv, "room_store", rs)
    return t, m, rs


def _modal(t, m):
    modal = rv.RoomModal(1, 2, t, m)
    modal.room_input._value = "999"
    modal.pass_input._value = "secret"
    return modal


def _defer_calls(ic):
    return [kw for name, kw in ic.response.calls if name == "defer"]


def _find_button(view, label):
    stack = list(getattr(view, "children", []) or [])
    while stack:
        item = stack.pop()
        if getattr(item, "label", None) == label and hasattr(
                item, "callback"):
            return item
        stack.extend(getattr(item, "children", []) or [])
    return None


# ---------------------------------------------------------------------------
# 1-2. Modal submit: thinking-defer + confirm view в приватный ответ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_submit_defers_thinking_ephemeral(room_env):
    """Критично: defer должен быть ephemeral+thinking — иначе
    DEFERRED_MESSAGE_UPDATE перезапишет публичную карточку."""
    t, m, rs = room_env
    modal = _modal(t, m)
    pub = _PubMessage(t.message_id)
    ic = _IC(message=pub)

    await modal.on_submit(ic)

    defers = _defer_calls(ic)
    assert defers and defers[0].get("thinking") is True
    assert defers[0].get("ephemeral") is True
    # Confirm-UI ушёл в edit_original_response (приватный ответ),
    # публичное сообщение НЕ редактировалось.
    assert ic.original_edits
    assert pub.edits == []
    # Публичная карточка не получила edit_message.
    assert not any(c[0] == "edit_message"
                   for c in ic.response.calls)


@pytest.mark.asyncio
async def test_cancel_never_edits_public_card(room_env):
    """Точный сценарий инцидента: кнопка «Отмена» на сообщении,
    совпадающем с tournament.message_id → только ephemeral."""
    t, m, rs = room_env
    modal = _modal(t, m)
    pub = _PubMessage(t.message_id)
    ic = _IC(message=pub)
    await modal.on_submit(ic)

    # Достаём confirm-view из original response.
    view = ic.original_edits[-1].get("view")
    cancel = _find_button(view, "Отмена")
    assert cancel is not None

    # «Отмена» нажата на сообщении = публичная карточка.
    ic2 = _IC(message=pub, user_id=101)
    await cancel.callback(ic2)

    assert pub.edits == []                       # публичное цело
    sent = [c for c in ic2.response.calls
            if c[0] == "send_message"]
    assert sent and sent[0][1][1].get("ephemeral") is True
    # Pending-запись удалена.
    assert (1, 2, 101) not in rv._PENDING_EDITS


@pytest.mark.asyncio
async def test_cancel_on_private_message_still_edits(room_env):
    """На обычном (не публичном) сообщении «Отмена» редактирует
    свой ephemeral-ответ как раньше."""
    t, m, rs = room_env
    modal = _modal(t, m)
    ic = _IC(message=_PubMessage(999))  # не message_id турнира
    await modal.on_submit(ic)
    view = ic.original_edits[-1].get("view")
    cancel = _find_button(view, "Отмена")

    priv = _PubMessage(777)
    ic2 = _IC(message=priv, user_id=101)
    await cancel.callback(ic2)
    # edit_message записан в response calls (это и есть правка
    # сообщения-источника на стороне Discord).
    assert any(c[0] == "edit_message"
               for c in ic2.response.calls)


@pytest.mark.asyncio
async def test_apply_room_edit_defers_thinking(room_env):
    """Кнопка «Подтвердить изменение» — тоже приватный defer."""
    t, m, rs = room_env
    modal = _modal(t, m)
    ic = _IC(message=_PubMessage(999))
    await modal.on_submit(ic)
    view = ic.original_edits[-1].get("view")
    ok = _find_button(view, "Подтвердить изменение")
    assert ok is not None

    ic2 = _IC(message=_PubMessage(777), user_id=101)
    # Подтверждение: комнату «заменяем» — upsert вызовется.
    import services.room_publisher as rp
    async def _pub(*a, **kw):
        return {"card": True, "notified": True}
    import pytest as _pt  # noqa
    # Патчим publish_room — локальный импорт внутри _save_room.
    orig = rp.publish_room
    rp.publish_room = _pub
    try:
        await ok.callback(ic2)
    finally:
        rp.publish_room = orig

    defers = _defer_calls(ic2)
    assert defers and defers[0].get("thinking") is True
    # Комната обновлена (не создана заново).
    assert rs.upserts and rs.upserts[0][1] == "999"


# ---------------------------------------------------------------------------
# 3-4. Успешное сохранение комнаты (новая комната)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_room_new_private_reply_and_refresh(
        monkeypatch, room_env):
    t, m, _rs = room_env
    rs = _RoomStore(existing=None)  # комнаты ещё нет
    monkeypatch.setattr(rv, "room_store", rs)

    import services.room_publisher as rp
    async def _pub(*a, **kw):
        return {"card": True, "notified": True}
    monkeypatch.setattr(rp, "publish_room", _pub)

    modal = _modal(t, m)
    pub = _PubMessage(t.message_id)
    ic = _IC(message=pub)
    await modal.on_submit(ic)

    # Приватный ответ с подтверждением (edit_original_response —
    # безопасно, т.к. defer был thinking=True → отдельное
    # ephemeral-сообщение, не публичная карточка).
    assert ic.original_edits
    # Публичная карточка не редактировалась.
    assert pub.edits == []
    # update_tournament_message вызван для статуса «Комната готова».
    assert ic.client.updates


# ---------------------------------------------------------------------------
# 5. GenerateMatchesButton: ошибки не трогают публичную карточку
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_matches_errors_ephemeral(monkeypatch):
    import views.matches_view as mv

    monkeypatch.setattr(
        mv, "can_manage_channel_tournament",
        lambda *a, **kw: True)
    monkeypatch.setattr(
        mv, "store", SimpleNamespace(get=lambda g, c: None))

    btn = mv.GenerateMatchesButton(1, 2)
    ic = _IC()
    await btn.callback(ic)

    # Ошибка ушла ephemeral followup, публичное сообщение
    # (deferred update) не редактировалось.
    assert ic.followup.sent
    assert ic.followup.sent[0][1].get("ephemeral") is True
    assert ic.original_edits == []


# ---------------------------------------------------------------------------
# 6. force=True в update_tournament_message
# ---------------------------------------------------------------------------


class _FMsg:
    def __init__(self, mid):
        self.id = mid
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        return self


class _FChan:
    def __init__(self):
        self.msgs = {}

    async def fetch_message(self, mid):
        if mid not in self.msgs:
            raise RuntimeError("nf")
        return self.msgs[mid]


def _make_bot(monkeypatch):
    bot = object.__new__(bot_module.TournamentBot)
    bot._registered_view_keys = set()
    bot._register_view = lambda v: None
    bot.build_view_for_tournament = lambda t, **kw: SimpleNamespace(
        children=[])

    async def _emb(t, g):
        return SimpleNamespace(to_dict=lambda: {"d": "v"})
    monkeypatch.setattr(bot_module, "build_embed_for_phase", _emb)
    monkeypatch.setattr(
        bot_module, "compose",
        lambda e, v: SimpleNamespace(children=[]))
    return bot


@pytest.fixture(autouse=True)
def _clean_caches():
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()
    yield
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()


class _FT:
    id = "t1"
    guild_id = 1
    channel_id = 2
    message_id = 42
    phase = "SETUP"

    def to_dict(self):
        return {"id": self.id}


@pytest.mark.asyncio
async def test_force_rewrites_cached_signature(monkeypatch):
    """force=True редактирует сообщение даже при совпадающей
    подписи контента (кэш _EDIT_CACHE инвалидируется)."""
    t = _FT()
    fake_store = SimpleNamespace(get=lambda g, c: t)
    import storage.json_store as js
    monkeypatch.setattr(js, "store", fake_store)

    bot = _make_bot(monkeypatch)
    chan = _FChan()
    msg = _FMsg(42)
    chan.msgs[42] = msg
    guild = SimpleNamespace(id=1, get_channel=lambda c: chan)

    try:
        import services.match_announcer as ma
        monkeypatch.setattr(
            ma, "announce_phase_matches",
            lambda *a, **kw: _noop())
    except Exception:
        pass

    # Обычный вызов — edit #1.
    assert await bot.update_tournament_message(guild, t) is True
    assert len(msg.edits) == 1
    # Повторный без изменений — edit пропущен (sig совпал).
    assert await bot.update_tournament_message(guild, t) is True
    assert len(msg.edits) == 1
    # force=True — edit выполняется повторно.
    assert await bot.update_tournament_message(
        guild, t, force=True) is True
    assert len(msg.edits) == 2


async def _noop():
    return None


# ---------------------------------------------------------------------------
# 7. card_repair: диагностика и repair
# ---------------------------------------------------------------------------


def test_diagnose_message_markers():
    from services import card_repair as cr

    class _C:
        def __init__(self, content):
            self.content = content
            self.children = []

    class _M:
        content = ""
        components = [_C("-# Изменение отменено.")]

    assert cr.diagnose_message(_M()) is not None

    class _OK:
        content = ""
        components = [_C("## Турнир\nВойти (3/8)")]

    assert cr.diagnose_message(_OK()) is None


@pytest.mark.asyncio
async def test_repair_only_damaged(monkeypatch):
    from services import card_repair as cr

    damaged_t = _FT()
    damaged_t.id = "damaged"
    ok_t = _FT()
    ok_t.id = "ok"
    ok_t.message_id = 44
    missing_t = _FT()
    missing_t.id = "missing"
    missing_t.message_id = 43

    class _Guild:
        def __init__(self, chan):
            self._c = chan

        def get_channel(self, cid):
            return self._c

        async def fetch_channel(self, cid):
            return self._c

    class _Chan:
        def __init__(self):
            self.msgs = {
                42: _damaged_msg(), 43: None, 44: _ok_msg()}

        async def fetch_message(self, mid):
            import discord as _d
            m = self.msgs.get(mid)
            if m is None:
                raise _d.NotFound(_FakeResp2(), "nf")
            return m

    class _GuildObj:
        id = 1

        def __init__(self, chan):
            self._chan = chan

        def get_channel(self, cid):
            return self._chan

    chan = _Chan()
    guild = _GuildObj(chan)

    class _Bot:
        def get_guild(self, gid):
            return guild

        async def fetch_guild(self, gid):
            return guild

        async def update_tournament_message(
                self, g, t, **kw):
            self.last = (t.id, kw)
            return True

    bot = _Bot()
    ts = [damaged_t, ok_t, missing_t]
    monkeypatch.setattr(
        cr.tournament_store, "all", lambda: iter(ts))

    result = await cr.repair(bot)
    assert result["damaged"] == 1
    assert result["missing"] == 1
    assert result["repaired"] == ["damaged"]
    assert bot.last[0] == "damaged"
    assert bot.last[1].get("force") is True


def _damaged_msg():
    class _C:
        content = "-# Изменение отменено."
        children = []

    class _M:
        content = ""
        components = [_C()]

        def __init__(self):
            self.id = 42

    return _M()


def _ok_msg():
    class _C:
        content = "## Турнир\nРегистрация открыта"
        children = []

    class _M:
        content = ""
        components = [_C()]

        def __init__(self):
            self.id = 44

    return _M()


class _FakeResp2:
    status = 404
    reason = "nf"


# ---------------------------------------------------------------------------
# 8-10. card_repair v2: статусы FORBIDDEN/UNKNOWN, Cash Cup (PG), аудит
# ---------------------------------------------------------------------------


class _ForbiddenChan:
    async def fetch_message(self, mid):
        import discord as _d
        raise _d.Forbidden(_FakeResp2(), "forbidden")


class _GuildObj2:
    id = 1
    def __init__(self, chan):
        self._chan = chan
    def get_channel(self, cid):
        return self._chan


@pytest.mark.asyncio
async def test_scan_forbidden_not_healthy(monkeypatch):
    """403 → FORBIDDEN, никогда не HEALTHY."""
    from services import card_repair as cr
    t = _FT(); t.id = "fb"; t.message_id = 42
    monkeypatch.setattr(cr.tournament_store, "all", lambda: iter([t]))

    class _Bot:
        def get_guild(self, gid): return _GuildObj2(_ForbiddenChan())
        async def fetch_guild(self, gid): return _GuildObj2(_ForbiddenChan())
        guilds = []

    report = await cr.scan(_Bot())
    assert report[0]["status"] == cr.FORBIDDEN
    assert report[0]["status"] != cr.HEALTHY


@pytest.mark.asyncio
async def test_scan_cashcup_pg_error_is_unknown(monkeypatch):
    """PG недоступна → UNKNOWN, не ложный HEALTHY."""
    from services import card_repair as cr
    import services.cashcup_store as cs

    monkeypatch.setattr(cr.tournament_store, "all", lambda: iter([]))

    class _G:
        id = 5
    class _Bot:
        guilds = [_G()]

    async def _boom(gid):
        raise RuntimeError("pg down")
    monkeypatch.setattr(cs.cashcup_store, "active", _boom)

    report = await cr.scan(_Bot())
    assert len(report) == 1
    assert report[0]["kind"] == "cashcup"
    assert report[0]["status"] == cr.UNKNOWN


@pytest.mark.asyncio
async def test_repair_skips_forbidden_and_audits(monkeypatch):
    """Repair не трогает FORBIDDEN; аудит-ран записывается."""
    from services import card_repair as cr

    monkeypatch.setattr(cr.tournament_store, "all", lambda: iter([]))
    monkeypatch.setattr(cr, "_scan_cashcup", lambda bot: _coro([]))
    audited = []
    async def _audit(bot, result):
        audited.append(result)
    monkeypatch.setattr(cr, "_audit_run", _audit)

    class _Bot:
        guilds = []
        async def update_tournament_message(self, *a, **kw):
            raise AssertionError("must not repair non-DAMAGED")

    # Прямой вызов repair на пустом scan → 0 repaired, аудит вызван.
    result = await cr.repair(_Bot())
    assert result["scanned"] == 0 and result["repaired"] == []
    assert audited and audited[0]["scanned"] == 0


async def _coro(v):
    return v


async def _nosleep(*a, **kw):
    return None


# ---------------------------------------------------------------------------
# 11-13. card_repair v2: recheck перед правкой, backup, идемпотентность
# ---------------------------------------------------------------------------


def _entry(tid="t1", mid=42, status="DAMAGED"):
    return {
        "kind": "tournament", "tournament_id": tid,
        "guild_id": 1, "channel_id": 2, "message_id": mid,
        "phase": "final", "status": status,
        "detail": f"{status}:x",
    }


class _ChanV2:
    def __init__(self, msg):
        self._msg = msg

    async def fetch_message(self, mid):
        if self._msg is None:
            import discord as _d
            raise _d.NotFound(_FakeResp2(), "nf")
        return self._msg


class _GuildV2:
    id = 1

    def __init__(self, chan):
        self._chan = chan

    def get_channel(self, cid):
        return self._chan


class _BotV2:
    def __init__(self, msg):
        self._chan = _ChanV2(msg)
        self.updates = []
        self.guilds = []

    def get_guild(self, gid):
        return _GuildV2(self._chan)

    async def fetch_guild(self, gid):
        return _GuildV2(self._chan)

    async def update_tournament_message(self, g, t, **kw):
        self.updates.append((t.id, kw))
        return True


@pytest.mark.asyncio
async def test_repair_recheck_skips_healthy(monkeypatch):
    """Между scan и repair карточка стала HEALTHY → правки нет."""
    from services import card_repair as cr

    t = _FT(); t.id = "t1"; t.message_id = 42
    monkeypatch.setattr(cr.tournament_store, "all",
                        lambda: iter([t]))
    monkeypatch.setattr(cr, "_scan_cashcup",
                        lambda bot: _coro([]))
    async def _audit(bot, result):
        return None
    monkeypatch.setattr(cr, "_audit_run", _audit)
    # scan() вернёт DAMAGED, но _fetch_message (recheck) — HEALTHY.
    monkeypatch.setattr(cr, "scan",
                        lambda bot: _coro([_entry()]))
    monkeypatch.setattr(cr, "_fetch_message",
                        lambda *a: _coro((cr.HEALTHY, None)))
    monkeypatch.setattr(cr.asyncio, "sleep", _nosleep)

    bot = _BotV2(_ok_msg())
    result = await cr.repair(bot)
    assert bot.updates == []
    assert result["repaired"] == []
    assert result["skipped"][0]["detail"].startswith(
        "recheck:not_damaged")


@pytest.mark.asyncio
async def test_repair_writes_backup_and_repairs(monkeypatch, tmp_path):
    """При DAMAGED пишется JSON-бэкап состояния до правок."""
    from services import card_repair as cr

    t = _FT(); t.id = "t1"; t.message_id = 42
    monkeypatch.setattr(cr.tournament_store, "all",
                        lambda: iter([t]))
    monkeypatch.setattr(cr, "_scan_cashcup",
                        lambda bot: _coro([]))
    async def _audit(bot, result):
        return None
    monkeypatch.setattr(cr, "_audit_run", _audit)
    monkeypatch.setattr(cr, "scan",
                        lambda bot: _coro([_entry()]))
    monkeypatch.setattr(cr, "_fetch_message",
                        lambda *a: _coro((cr.DAMAGED, _damaged_msg())))
    monkeypatch.setattr(cr.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(cr, "DATA_DIR", tmp_path)

    bot = _BotV2(_damaged_msg())
    result = await cr.repair(bot)
    assert result["repaired"] == ["t1"]
    backups = list(tmp_path.glob("card_repair_backup_*.json"))
    assert len(backups) == 1
    import json as _json
    data = _json.loads(backups[0].read_text(encoding="utf-8"))
    assert data["tournaments"] and data["report"]


@pytest.mark.asyncio
async def test_repair_idempotent_second_run(monkeypatch):
    """Повторный repair: scan видит HEALTHY → ноль правок."""
    from services import card_repair as cr

    monkeypatch.setattr(cr.tournament_store, "all",
                        lambda: iter([]))
    monkeypatch.setattr(cr, "_scan_cashcup",
                        lambda bot: _coro([]))
    async def _audit(bot, result):
        return None
    monkeypatch.setattr(cr, "_audit_run", _audit)
    calls = {"n": 0}

    async def _scan(bot):
        calls["n"] += 1
        # Первый запуск — DAMAGED, второй — HEALTHY.
        st = cr.DAMAGED if calls["n"] == 1 else cr.HEALTHY
        return [_entry(status=st)]
    monkeypatch.setattr(cr, "scan", _scan)
    monkeypatch.setattr(cr, "_fetch_message",
                        lambda *a: _coro((cr.HEALTHY, _ok_msg())))
    monkeypatch.setattr(cr.asyncio, "sleep", _nosleep)

    bot = _BotV2(_ok_msg())
    r1 = await cr.repair(bot)
    r2 = await cr.repair(bot)
    # В первом запуске recheck HEALTHY → правки не было;
    # второй — тоже чисто. update ни разу не вызван.
    assert bot.updates == []
    assert r1["repaired"] == [] and r2["repaired"] == []
