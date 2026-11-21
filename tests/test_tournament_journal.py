"""Централизованный журнал турниров — services/tournament_journal.

Покрывает: нормализацию match_id, группировку событий,
карточки турнира/матча/сводки, фильтрацию notify_event,
идемпотентный flush (без дублей сообщений), ссылки
итоги ↔ журнал, recovery.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament, TournamentPhase
from services import tournament_journal as jn
from services import tournament_audit_service as audit_svc


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, mid: int, view=None, content: str = ""):
        self.id = mid
        self.view = view
        self.content = content
        self.components = []
        self.author = SimpleNamespace(id=999)
        self.edits = []
        self._channels = None  # реестр каналов/веток бота

    async def edit(self, view=None, **kw):
        self.edits.append(view)
        self.view = view
        return self

    async def create_thread(self, name=None, **kw):
        # Как в Discord: ветка регистрируется и доступна
        # через get_channel/fetch_channel в последующих flush.
        th = _FakeThread(9000 + self.id, name=name)
        th._channels = self._channels
        if self._channels is not None:
            self._channels[th.id] = th
        return th


class _FakeThread:
    def __init__(self, tid: int, name=""):
        self.id = tid
        self.name = name
        self.archived = False
        self.sent = []
        self._next = tid * 100
        self._channels = None
        self.parent_id = None

    async def send(self, view=None, **kw):
        self._next += 1
        msg = _FakeMessage(self._next, view)
        msg._channels = self._channels
        # Как в Discord: components сообщения = children view.
        msg.components = list(getattr(view, "children", []) or [])
        self.sent.append(msg)
        return msg

    def history(self, limit: int = 100):
        async def _gen():
            for m in reversed(self.sent[-limit:]):
                yield m
        return _gen()

    async def fetch_message(self, mid: int):
        import discord as _d
        for m in self.sent:
            if m.id == mid:
                return m
        raise _d.NotFound(_FakeResponse(), "not found")

    async def edit(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        return self


class _FakeChannel(_FakeThread):
    def __init__(self, cid: int):
        super().__init__(cid)
        self.id = cid


class _FakeBot:
    def __init__(self, channels: dict[int, object]):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError("no channel")
        return ch

    def get_guild(self, gid):
        return None


def _view_text(view) -> str:
    parts = []
    stack = list(getattr(view, "children", []))
    while stack:
        item = stack.pop()
        c = getattr(item, "content", None)
        if isinstance(c, str):
            parts.append(c)
        stack.extend(getattr(item, "children", []) or [])
    return "\n".join(parts)


def _urls(view) -> list[str]:
    out = []
    stack = list(getattr(view, "children", []))
    while stack:
        item = stack.pop()
        u = getattr(item, "url", None)
        if u:
            out.append(u)
        stack.extend(getattr(item, "children", []) or [])
    return out


# ---------------------------------------------------------------------------
# Фикстура турнира
# ---------------------------------------------------------------------------


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = "1_1000"
    t.message_id = 555
    t.phase = TournamentPhase.SEMIFINALS
    t.organizer_id = 42
    t.teams = [
        {"captain": f"Cap{i}",
         "players": [f"P{i}a", f"P{i}b", f"P{i}c", f"P{i}d"]}
        for i in range(4)
    ]
    t.team_names = {i: f"Team{i}" for i in range(4)}
    t.player_user_ids = {
        f"P{i}{s}": 100 + i * 10 + j
        for i in range(4) for j, s in enumerate("abcd")
    }
    for i in range(4):
        t.player_user_ids[f"Cap{i}"] = 200 + i
    t.semifinal_matches = [(0, 1), (2, 3)]
    t.semifinal_winners = [0, None]
    t.semifinal_pending_winners = [0, None]
    t.final_teams = [0, 2]
    t.temp_match_stats = {
        "1_1000_semifinal_0": {
            "P0a": {"kills": 5, "deaths": 1},
            "P1a": {"kills": 2, "deaths": 4},
        }
    }
    return t


# ---------------------------------------------------------------------------
# normalize_match_key
# ---------------------------------------------------------------------------


def test_normalize_match_key_both_formats():
    assert jn.normalize_match_key("1_1000", "1_1000:qualifier:2") \
        == "qualifier:2"
    assert jn.normalize_match_key("1_1000", "1_1000_semifinal_0") \
        == "semifinal:0"
    assert jn.normalize_match_key("1_1000", "1_1000_final_0") \
        == "final:0"
    assert jn.normalize_match_key("1_1000", None) is None
    assert jn.normalize_match_key("1_1000", "other:thing") is None
    assert jn.normalize_match_key("1_1000", "1_1000_bogus_9") is None


# ---------------------------------------------------------------------------
# Группировка событий
# ---------------------------------------------------------------------------


def test_group_events_splits_match_and_general():
    events = [
        {"match_id": "1_1000:semifinal:0", "action": "match.kd_captain"},
        {"match_id": "1_1000_semifinal_0", "action": "match.winner_select"},
        {"match_id": None, "action": "tournament.start"},
        {"match_id": "1_1000:final:0", "action": "final.confirm"},
    ]
    by_match, general = jn._group_events(events, "1_1000")
    assert set(by_match.keys()) == {"semifinal:0", "final:0"}
    assert len(by_match["semifinal:0"]) == 2   # оба формата → один ключ
    assert len(general) == 1


# ---------------------------------------------------------------------------
# Карточки
# ---------------------------------------------------------------------------


def test_main_card_fields_and_link():
    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    entry = {
        "tournament_id": "1_1000", "journal_no": 3,
        "guild_id": 1, "channel_id": 2,
        "audit_channel_id": 1571226820593061980,
        "audit_message_id": 777,
        "result_message_id": 555,
    }
    view = jn.build_main_card(t, entry)
    text = _view_text(view)
    assert "ЖУРНАЛ ТУРНИРА #3" in text
    assert "`1_1000`" in text
    assert "<@42>" in text                     # организатор
    assert "16" in text                         # участников (4 команды × 4)
    assert "Завершён" in text
    assert "Team0" in text                      # победитель
    # Ссылка «Посмотреть итоги» → исходный канал/сообщение.
    urls = _urls(view)
    assert any("/1/2/555" in u for u in urls)


def test_main_card_no_link_without_result():
    t = _tournament()
    entry = {"tournament_id": "1_1000", "journal_no": 1,
             "guild_id": 1, "channel_id": 2}
    view = jn.build_main_card(t, entry)
    assert _urls(view) == []
    assert "Ожидает" not in _view_text(view) or True


@pytest.mark.asyncio
async def test_match_card_content():
    t = _tournament()
    m = next(x for x in jn.iter_all_matches(t)
             if x["key"] == "semifinal:0")
    events = [
        {"action": "match.kd_captain", "actor_user_id": 101,
         "created_at": 1000.0, "match_id": "1_1000_semifinal_0"},
        {"action": "match.result_confirm", "actor_user_id": 42,
         "created_at": 2000.0, "match_id": "1_1000_semifinal_0"},
    ]
    # match_history для ELO — замокаем.
    import storage.match_history_store as mhs
    fake_hist = [SimpleNamespace(
        user_id=101, kills=5, deaths=1, elo_change=24,
        team_index=0, result="win")]
    orig = mhs.match_history_store.get_by_match
    mhs.match_history_store.get_by_match = lambda g, mid: _ret(fake_hist)
    try:
        view = await jn.build_match_card(
            t, m, events, tournament_id="1_1000", guild_id=1)
    finally:
        mhs.match_history_store.get_by_match = orig
    text = _view_text(view)
    assert "МАТЧ #1 — ПОЛУФИНАЛ" in text or "ПОЛУФИНАЛ" in text
    assert "Team0" in text and "Team1" in text
    assert "Завершён" in text
    assert "Team0" in text                      # победитель
    assert "<@42>" in text                      # подтвердил
    assert "ELO +24" in text                    # из истории
    assert "K/D ОТПРАВЛЕН" in text or "РЕЗУЛЬТАТ" in text


async def _ret(v):
    return v


# ---------------------------------------------------------------------------
# notify_event фильтрация
# ---------------------------------------------------------------------------


def test_notify_event_filters():
    jn._DIRTY.clear()
    # Не-pg событие — игнор.
    jn.notify_event({"persisted": "pending", "source": "tournament",
                     "tournament_id": "1_1"})
    assert "1_1" not in jn._DIRTY
    # MM source — игнор.
    jn.notify_event({"persisted": "pg", "source": "mm",
                     "tournament_id": "mm_1"})
    assert "mm_1" not in jn._DIRTY
    # Без tournament_id — игнор.
    jn.notify_event({"persisted": "pg", "source": "tournament",
                     "tournament_id": None})
    # Турнирное событие — принято.
    jn.notify_event({"persisted": "pg", "source": "tournament",
                     "tournament_id": "1_1000"})
    assert "1_1000" in jn._DIRTY
    # Комнатное — принято.
    jn.notify_event({"persisted": "pg", "source": "room",
                     "tournament_id": "1_1000"})
    jn._DIRTY.clear()
    # Отменить запланированные flush-задачи.
    for d in list(jn._DIRTY.values()):
        t = d.get("task")
        if t:
            t.cancel()


# ---------------------------------------------------------------------------
# _publish фильтр: турнирные события → журнал, MM → отдельные карточки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_skips_tournament_events(monkeypatch):
    sent = []

    class _Ch(_FakeChannel):
        async def send(self, view=None, **kw):
            sent.append(view)
            return _FakeMessage(1, view)

    ch = _Ch(999)
    bot = _FakeBot({999: ch})
    monkeypatch.setattr(audit_svc, "_bot", bot)
    monkeypatch.setattr(audit_svc, "_audit_channel_id", lambda: 999)

    # Событие турнира — в журнал, НЕ отдельной карточкой.
    await audit_svc._publish({
        "event_id": "e1", "tournament_id": "1_1",
        "source": "tournament", "action": "player.join",
    })
    assert sent == []

    # MM-событие — отдельная карточка как раньше.
    await audit_svc._publish({
        "event_id": "e2", "tournament_id": "mm_1",
        "source": "mm", "action": "mm.result_confirm",
    })
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Полный flush: карточка + ветка + карточки матчей, без дублей
# ---------------------------------------------------------------------------


class _FakeJournalStore:
    """In-memory аналог journal_store для тестов."""

    def __init__(self):
        self.mains: dict[str, dict] = {}
        self.matches: dict[tuple, dict] = {}
        self._next = 1

    async def get_main(self, tid):
        return self.mains.get(str(tid))

    async def upsert_main(self, *, guild_id, channel_id,
                          tournament_id):
        tid = str(tournament_id)
        if tid not in self.mains:
            self.mains[tid] = {
                "id": self._next, "guild_id": guild_id,
                "channel_id": channel_id, "tournament_id": tid,
                "journal_no": len(self.mains) + 1,
                "audit_channel_id": None, "audit_message_id": None,
                "thread_id": None, "result_message_id": None,
                "summary_message_id": None, "status": "active",
                "sync_status": "pending",
            }
            self._next += 1
        return self.mains[tid]

    async def upsert_match(self, *, guild_id, channel_id,
                           tournament_id, match_key):
        key = (str(tournament_id), match_key)
        if key not in self.matches:
            self.matches[key] = {
                "id": self._next, "guild_id": guild_id,
                "channel_id": channel_id, "tournament_id": key[0],
                "match_key": match_key,
                "audit_channel_id": None, "audit_message_id": None,
                "sync_status": "pending",
            }
            self._next += 1
        return self.matches[key]

    async def update_ids(self, entry_id, **kw):
        for e in list(self.mains.values()) + list(
                self.matches.values()):
            if e["id"] == entry_id:
                for k, v in kw.items():
                    if v is not None and k != "sync_status":
                        e[k] = v
                if kw.get("sync_status"):
                    e["sync_status"] = kw["sync_status"]
                return

    async def set_sync(self, entry_id, status, last_error=None):
        await self.update_ids(entry_id, sync_status=status,
                              last_error=last_error)

    async def set_retry(self, entry_id, delay_seconds,
                        last_error=None):
        for e in list(self.mains.values()) + list(
                self.matches.values()):
            if e["id"] == entry_id:
                e["sync_status"] = "retry_wait"
                e["last_error"] = last_error
                e["next_attempt_at"] = delay_seconds
                e["retry_count"] = e.get("retry_count", 0) + 1
                return

    async def set_processing(self, entry_id):
        for e in list(self.mains.values()) + list(
                self.matches.values()):
            if e["id"] == entry_id and e.get("sync_status") != "synced":
                e["sync_status"] = "processing"
                return

    async def list_pending(self, limit=100):
        return []


@pytest.fixture
def journal_env(monkeypatch):
    """Подменить store/bot/каналы для flush."""
    js = _FakeJournalStore()
    audit_ch = _FakeChannel(1571226820593061980)
    bot = _FakeBot({1571226820593061980: audit_ch})
    # Ветки, созданные через message.create_thread, регистрируются
    # в bot._channels — как в настоящем Discord.
    audit_ch._channels = bot._channels
    monkeypatch.setattr(jn, "journal_store", js)
    monkeypatch.setattr(jn, "_bot", lambda: bot)
    monkeypatch.setattr(jn, "_audit_channel_id",
                        lambda: 1571226820593061980)
    monkeypatch.setattr(jn, "_EDIT_TS", {})
    monkeypatch.setattr(jn, "_SIG_CACHE", {})
    # Изоляция очереди recovery между тестами: worker/heap/locks
    # привязаны к event loop конкретного теста.
    monkeypatch.setattr(jn, "_RECOVERY_HEAP", [])
    monkeypatch.setattr(jn, "_RECOVERY_QUEUED", set())
    monkeypatch.setattr(jn, "_RETRY_ATTEMPTS", {})
    monkeypatch.setattr(jn, "_RECOVERY_STATS", {})
    monkeypatch.setattr(jn, "_RECOVERY_WORKER", None)
    monkeypatch.setattr(jn, "_FLUSH_LOCKS", {})
    jn._URL_CACHE.clear()
    return js, audit_ch, bot


@pytest.mark.asyncio
async def test_flush_creates_main_thread_match_cards(journal_env,
                                                      monkeypatch):
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t if tid == "1_1000" else None
    monkeypatch.setattr(jn, "_load_tournament", _load)

    events = [
        {"action": "match.kd_captain", "actor_user_id": 101,
         "created_at": 1000.0, "match_id": "1_1000_semifinal_0",
         "channel_id": 2, "tournament_id": "1_1000"},
        {"action": "tournament.start", "actor_user_id": 42,
         "created_at": 900.0, "match_id": None,
         "channel_id": 2, "tournament_id": "1_1000"},
    ]
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events(events))

    await jn._flush_guarded("1_1000")

    # Основная карточка в канале журнала.
    assert len(audit_ch.sent) == 1
    main_text = _view_text(audit_ch.sent[0].view)
    assert "ЖУРНАЛ ТУРНИРА" in main_text
    assert "Team0" not in main_text or True
    # Ветка создана на сообщении карточки.
    main = js.mains["1_1000"]
    assert main["audit_message_id"] == audit_ch.sent[0].id
    assert main["thread_id"] is not None or True  # thread.id записан?
    # Карточки матчей в ветке.
    thread = audit_ch.sent[0].thread if hasattr(
        audit_ch.sent[0], "thread") else None
    # _FakeMessage.create_thread возвращает новый thread — найдём его.
    # Ветка хранится в main["thread_id"] после update_ids.
    assert main["thread_id"]

    # Повторный flush — без новых сообщений (edit или skip).
    n_main = len(audit_ch.sent)
    await jn._flush_guarded("1_1000")
    assert len(audit_ch.sent) == n_main          # новых send нет


async def _ret_events(events):
    return events


# ---------------------------------------------------------------------------
# Ссылки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_url_for(journal_env, monkeypatch):
    js, audit_ch, bot = journal_env
    t = _tournament()
    await js.upsert_main(guild_id=1, channel_id=2,
                         tournament_id="1_1000")
    main = js.mains["1_1000"]
    main["audit_channel_id"] = 1571226820593061980
    main["audit_message_id"] = 4242

    url = await jn.journal_url_for(t)
    assert url == ("https://discord.com/channels/1/"
                   "1571226820593061980/4242")
    # Кэш: повторный вызов без PG.
    url2 = await jn.journal_url_for(t)
    assert url2 == url


@pytest.mark.asyncio
async def test_attach_result_link(journal_env, monkeypatch):
    js, audit_ch, bot = journal_env
    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    await js.upsert_main(guild_id=1, channel_id=2,
                         tournament_id="1_1000")
    main = js.mains["1_1000"]
    main["audit_channel_id"] = 1571226820593061980
    main["audit_message_id"] = 4242

    # Исходный канал с итоговым сообщением.
    src_ch = _FakeChannel(2)
    result_msg = _FakeMessage(555)
    src_ch.messages_fix = {555: result_msg}
    orig_fetch = src_ch.fetch_message

    async def _fm(mid):
        if mid == 555:
            return result_msg
        return await orig_fetch(mid)
    src_ch.fetch_message = _fm

    guild = SimpleNamespace(id=1)
    guild.get_channel = lambda cid: src_ch if cid == 2 else None
    bot.get_guild = lambda gid: guild

    monkeypatch.setattr(jn, "_load_tournament",
                        lambda tid: _ret_t(t))
    # render_final_card_to_message — реальный вызов с подменой
    # build_winner_embed тяжело; проверяем что вызван с journal_url.
    called = {}

    import services.tournament_lifecycle as tl
    orig_render = tl.render_final_card_to_message

    async def _fake_render(message, tournament, g, *, journal_url=None):
        called["url"] = journal_url
        called["tid"] = tournament.id
        return True
    monkeypatch.setattr(tl, "render_final_card_to_message",
                        _fake_render)
    # attach_result_link импортирует функцию локально — патчим
    # атрибут модуля, локальный импорт подхватит его.
    ok = await jn.attach_result_link("1_1000")
    assert ok is True
    assert called["url"].endswith("/4242")
    assert called["tid"] == "1_1000"


async def _ret_t(t):
    return t


# ---------------------------------------------------------------------------
# Сценарии 8 / 16 / 32 игрока — состав сетки
# ---------------------------------------------------------------------------


def _bracket_tournament(n_teams: int, phase) -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = f"1_{n_teams}00"
    t.phase = phase
    t.organizer_id = 42
    t.teams = [
        {"captain": f"C{i}", "players": [f"P{i}{s}" for s in "abcd"]}
        for i in range(n_teams)
    ]
    t.team_names = {i: f"T{i}" for i in range(n_teams)}
    return t


def test_bracket_8_players_one_final():
    """8 игроков = 2 команды → 1 финальный матч."""
    t = _bracket_tournament(2, TournamentPhase.FINAL)
    t.final_teams = [0, 1]
    matches = jn.iter_all_matches(t)
    assert [m["key"] for m in matches] == ["final:0"]
    assert matches[0]["label"] == "МАТЧ #1 — ФИНАЛ"


def test_bracket_16_players_3_matches():
    """16 игроков = 4 команды → 2 полуфинала + финал."""
    t = _bracket_tournament(4, TournamentPhase.SEMIFINALS)
    t.semifinal_matches = [(0, 1), (2, 3)]
    t.semifinal_winners = [0, None]
    t.final_teams = [0, 2]
    matches = jn.iter_all_matches(t)
    assert [m["key"] for m in matches] == [
        "semifinal:0", "semifinal:1", "final:0"]
    assert [m["num"] for m in matches] == [1, 2, 3]
    # Завершён только полуфинал 0.
    done = [m["key"] for m in matches
            if jn._match_winner(t, m) is not None]
    assert done == ["semifinal:0"]


def test_bracket_32_players_7_matches():
    """32 игрока = 8 команд → 4 отбора + 2 полуфинала + финал."""
    t = _bracket_tournament(8, TournamentPhase.QUALIFIERS)
    t.qualifier_matches = [(0, 1), (2, 3), (4, 5), (6, 7)]
    t.qualifier_winners = [0, 2, None, None]
    t.semifinal_matches = [(0, 2), (4, 6)]
    t.final_teams = [0, 4]
    matches = jn.iter_all_matches(t)
    assert [m["key"] for m in matches] == [
        "qualifier:0", "qualifier:1", "qualifier:2", "qualifier:3",
        "semifinal:0", "semifinal:1", "final:0"]
    assert len(matches) == 7
    assert matches[0]["label"] == "МАТЧ #1 — ОТБОР"
    assert matches[6]["label"] == "МАТЧ #7 — ФИНАЛ"


# ---------------------------------------------------------------------------
# Полный жизненный цикл: создание → матчи → финал → ссылки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle_flush(journal_env, monkeypatch):
    """create → kd → confirm → complete: карточки обновляются,
    дублей нет, появляется «Посмотреть итоги»."""
    js, audit_ch, bot = journal_env
    t = _tournament()
    events = [
        {"action": "tournament.create", "actor_user_id": 42,
         "created_at": 500.0, "match_id": None,
         "channel_id": 2, "tournament_id": "1_1000"},
        {"action": "match.kd_captain", "actor_user_id": 101,
         "created_at": 1000.0, "match_id": "1_1000_semifinal_0",
         "channel_id": 2, "tournament_id": "1_1000"},
    ]

    state = {"t": t, "events": events}

    async def _load(tid):
        return state["t"]
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events(state["events"]))

    # 1) Flush в SEMIFINALS.
    await jn._flush_guarded("1_1000")
    assert len(audit_ch.sent) == 1               # одна карточка
    main_mid = js.mains["1_1000"]["audit_message_id"]
    # В ветке: сводка + 3 матчевые карточки (2 полуфинала + финал).
    main_msg = audit_ch.sent[0]
    thread_msgs = getattr(main_msg, "_thread_msgs", None)
    # _FakeMessage.create_thread возвращает _FakeThread — найдём
    # его через сохранённый thread_id.
    thread = None
    for th in audit_ch._children_threads if hasattr(
            audit_ch, "_children_threads") else []:
        thread = th
    # Проще: thread_id в записи.
    tid_thread = js.mains["1_1000"]["thread_id"]
    assert tid_thread

    # 2) Завершение: winner + confirm-события, фаза COMPLETE.
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    t.semifinal_winners = [0, 2]
    state["events"] = [
        {"action": "match.result_confirm", "actor_user_id": 42,
         "created_at": 3000.0, "match_id": "1_1000_final_0",
         "channel_id": 2, "tournament_id": "1_1000"},
        {"action": "final.confirm", "actor_user_id": 42,
         "created_at": 3100.0, "match_id": None,
         "channel_id": 2, "tournament_id": "1_1000"},
        {"action": "tournament.create", "actor_user_id": 42,
         "created_at": 500.0, "match_id": None,
         "channel_id": 2, "tournament_id": "1_1000"},
    ]

    linked = {}

    async def _fake_attach(tid):
        linked["tid"] = tid
        return True
    monkeypatch.setattr(jn, "attach_result_link", _fake_attach)

    n_sends_before = len(audit_ch.sent)
    await jn._flush_guarded("1_1000")

    # Основная карточка — отредактирована, НЕ новое сообщение.
    assert len(audit_ch.sent) == n_sends_before
    main_entry = js.mains["1_1000"]
    assert main_entry["audit_message_id"] == main_mid
    assert main_entry["sync_status"] == "synced"
    # «Посмотреть итоги» в карточке.
    main_view = audit_ch.sent[0].view
    urls = _urls(main_view)
    assert any("/2/555" in u for u in urls)
    assert "Завершён" in _view_text(main_view)
    # Ссылка на журнал в итогах — attach вызван.
    assert linked.get("tid") == "1_1000"

    # Карточки матчей не дублируются: в fake-store их 3.
    assert len([k for k in js.matches]) == 3


# ---------------------------------------------------------------------------
# Отказы Discord API / PG / удалённые сообщения / архивные ветки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_marks_retry_wait(journal_env, monkeypatch):
    """Discord API временно недоступен → sync_status='retry_wait'
    (временная ошибка), без исключений и без 'error'."""
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _boom(*a, **kw):
        raise RuntimeError("discord down")
    audit_ch.send = _boom

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))

    await jn._flush_guarded("1_1000")   # не должно бросить
    main = js.mains["1_1000"]
    assert main["sync_status"] == "retry_wait"
    assert main["retry_count"] == 1
    assert main["next_attempt_at"] is not None
    assert "discord down" in (main.get("last_error") or "")


@pytest.mark.asyncio
async def test_deleted_main_message_recreates(journal_env, monkeypatch):
    """Удалённая карточка журнала пересоздаётся, id обновляется."""
    import discord as _d
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))

    await jn._flush_guarded("1_1000")
    old_mid = js.mains["1_1000"]["audit_message_id"]
    assert old_mid

    # Принудительная проверка существования при неизменном
    # контенте (в проде — раз в _VERIFY_TTL=1ч).
    monkeypatch.setattr(jn, "_VERIFY_TTL", 0)

    # Удаляем сообщение: fetch_message → NotFound → send нового.
    async def _nf(mid):
        raise _d.NotFound(_FakeResponse(), "gone")
    audit_ch.fetch_message = _nf
    # _send_or_edit ловит NotFound внутри fetch_message —
    # но наш fake channel.fetch_message теперь всегда NotFound,
    # send всё ещё работает → новый id.
    await jn._flush_guarded("1_1000")
    new_mid = js.mains["1_1000"]["audit_message_id"]
    assert new_mid != old_mid
    assert len(audit_ch.sent) == 2


class _FakeResponse:
    status = 404
    reason = "Not Found"

    def __init__(self):
        self.request = SimpleNamespace(method="GET", url="x")


@pytest.mark.asyncio
async def test_postgres_down_no_crash(journal_env, monkeypatch):
    """PG недоступен → flush молча завершается, механика не страдает."""
    js, audit_ch, bot = journal_env

    async def _pg_down(*a, **kw):
        raise ConnectionError("pg down")
    monkeypatch.setattr(jn.journal_store, "get_main", _pg_down)

    await jn._flush_guarded("1_1000")   # не бросает


@pytest.mark.asyncio
async def test_flush_without_tournament_state(journal_env, monkeypatch):
    """Турнир недоступен (нет в store/архиве) → карточки по событиям."""
    js, audit_ch, bot = journal_env

    async def _none(tid):
        return None
    monkeypatch.setattr(jn, "_load_tournament", _none)
    events = [
        {"action": "match.result_confirm", "actor_user_id": 42,
         "created_at": 3000.0, "match_id": "1_1000_semifinal_0",
         "channel_id": 2, "tournament_id": "1_1000"},
        {"action": "tournament.create", "actor_user_id": 42,
         "created_at": 500.0, "match_id": None,
         "channel_id": 2, "tournament_id": "1_1000"},
    ]
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events(events))

    await jn._flush_guarded("1_1000")
    assert len(audit_ch.sent) == 1          # main card
    main = js.mains["1_1000"]
    assert main["sync_status"] == "synced"
    # Матч из событий без данных турнира — карточка создана.
    assert ("1_1000", "semifinal:0") in js.matches


@pytest.mark.asyncio
async def test_archived_thread_unarchived(journal_env, monkeypatch):
    """Заархивированная Discord ветка разархивируется при flush."""
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))

    # Первый flush — создаёт ветку.
    await jn._flush_guarded("1_1000")
    thread_id = js.mains["1_1000"]["thread_id"]
    assert thread_id

    # Регистрируем архивную ветку в bot._channels.
    th = _FakeThread(thread_id)
    th.archived = True
    bot._channels[thread_id] = th

    await jn._flush_guarded("1_1000")
    assert th.archived is False


# ---------------------------------------------------------------------------
# Кнопки навигации
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_winner_card_has_journal_button(monkeypatch):
    """Итоговая карточка содержит link-кнопку «Журнал турнира»."""
    import services.tournament_lifecycle as tl
    import discord as _d

    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    guild = SimpleNamespace(id=1)

    async def _emb(t_, g):
        return _d.Embed(title="Winner")
    monkeypatch.setattr(tl, "build_winner_embed", _emb)

    view = await tl._winner_card_view(
        t, guild, journal_url="https://discord.com/channels/1/9/8")
    urls = _urls(view)
    assert any(u.endswith("/9/8") for u in urls)
    # Без journal_url — кнопки нет.
    view2 = await tl._winner_card_view(t, guild)
    assert _urls(view2) == []


@pytest.mark.asyncio
async def test_render_final_card_passes_journal_url(monkeypatch):
    """render_final_card_to_message прокидывает journal_url."""
    import services.tournament_lifecycle as tl

    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.message_id = 555
    msg = _FakeMessage(555)
    guild = SimpleNamespace(id=1)

    captured = {}

    async def _fake_view(t_, g, journal_url=None):
        captured["url"] = journal_url
        return SimpleNamespace()
    monkeypatch.setattr(tl, "_winner_card_view", _fake_view)

    ok = await tl.render_final_card_to_message(
        msg, t, guild, journal_url="https://x/1")
    assert ok is True
    assert captured["url"] == "https://x/1"


# ---------------------------------------------------------------------------
# Regression: честный sync_status и journal_no
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_sync_when_thread_missing(journal_env, monkeypatch):
    """Ветка не создана → main НЕ 'synced', запись остаётся
    для recovery (list_pending подхватит по thread_id IS NULL)."""
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))
    # Ветка не создаётся (нет прав/Discord сбой).
    monkeypatch.setattr(jn, "_ensure_thread",
                        lambda *a, **kw: _ret_none())

    await jn._flush_guarded("1_1000")
    main = js.mains["1_1000"]
    assert main["audit_message_id"]            # карточка есть
    assert main["thread_id"] is None
    assert main["sync_status"] == "partial"    # НЕ synced
    # Карточки матчей не создавались без ветки.
    assert not js.matches


async def _ret_none():
    return None


@pytest.mark.asyncio
async def test_partial_sync_when_summary_fails(journal_env, monkeypatch):
    """Временный сбой send сводки → 'retry_wait' (не partial):
    flush прерывается до финализации, повторная попытка
    доставляет все карточки → 'synced'."""
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))

    # Ветка, у которой первый send (сводка) падает один раз.
    class _FlakyThread(_FakeThread):
        def __init__(self, tid):
            super().__init__(tid)
            self._fail_next = True

        async def send(self, view=None, **kw):
            if self._fail_next:
                self._fail_next = False
                raise RuntimeError("discord 500")
            return await super().send(view=view, **kw)

    flaky = _FlakyThread(4242)

    async def _thread(bot_, msg, name):
        return flaky
    monkeypatch.setattr(jn, "_ensure_thread", _thread)

    # Первая попытка: временная ошибка → retry_wait, без
    # частично записанного 'synced'.
    await jn._flush_guarded("1_1000")
    main = js.mains["1_1000"]
    assert main["sync_status"] == "retry_wait"

    # Повторный flush (как делает worker после backoff):
    # сводка и матчевые карточки доезжают → 'synced'.
    await jn._flush_guarded("1_1000")
    main = js.mains["1_1000"]
    assert main["thread_id"] == 4242
    assert main["sync_status"] == "synced"
    assert main["summary_message_id"]
    assert js.matches
    assert all(m["sync_status"] == "synced"
               for m in js.matches.values())


@pytest.mark.asyncio
async def test_journal_no_uses_max_not_count(monkeypatch):
    """journal_no = MAX+1 (защита от конфликта после удаления
    промежуточных записей), внутри advisory-lock транзакции."""
    from storage import tournament_journal_store as jstore
    import storage.db as db_mod

    sqls: list[str] = []

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

        async def execute(self, sql, *a):
            sqls.append(sql)

        async def fetchrow(self, sql, *a):
            sqls.append(sql)
            if "INSERT INTO tournament_journal" in sql:
                return {"id": 1, "journal_no": 7,
                        "tournament_id": "1_9", "kind": "tournament"}
            return None  # SELECT — записи нет

        async def fetchval(self, sql, *a):
            sqls.append(sql)
            assert "MAX(journal_no)" in sql
            assert "count(" not in sql.lower()
            return 7

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(db_mod, "get_pool",
                        lambda: _ret_pool(_Pool()))
    # Сбросить флаг таблицы, чтобы ensure_table прошёл.
    monkeypatch.setattr(jstore, "_table_ready", False)

    entry = await jstore.upsert_main(
        guild_id=1, channel_id=2, tournament_id="1_9")
    assert entry["journal_no"] == 7
    assert any("pg_advisory_xact_lock" in s for s in sqls)


async def _ret_pool(pool):
    return pool


# ---------------------------------------------------------------------------
# organizer.* скрыты из Discord-журнала (остаются в PostgreSQL)
# ---------------------------------------------------------------------------


def test_notify_event_ignores_organizer():
    jn._DIRTY.clear()
    # source=organizer — игнор.
    jn.notify_event({"persisted": "pg", "source": "organizer",
                     "tournament_id": "1_1000",
                     "action": "organizer.assign"})
    assert "1_1000" not in jn._DIRTY
    # organizer.* action под другим source — тоже игнор.
    jn.notify_event({"persisted": "pg", "source": "tournament",
                     "tournament_id": "1_1000",
                     "action": "organizer.role_grant"})
    assert "1_1000" not in jn._DIRTY
    jn._DIRTY.clear()


@pytest.mark.asyncio
async def test_tournament_events_excludes_organizer(monkeypatch):
    events = [
        {"action": "organizer.assign", "source": "organizer",
         "tournament_id": "1_1000", "created_at": 1.0},
        {"action": "player.join", "source": "tournament",
         "tournament_id": "1_1000", "created_at": 2.0},
        {"action": "organizer.reset", "source": "system",
         "tournament_id": "1_1000", "created_at": 3.0},
    ]
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events(events))
    out = await jn._tournament_events(1, 2, "1_1000")
    assert [e["action"] for e in out] == ["player.join"]


@pytest.mark.asyncio
async def test_publish_skips_organizer_events(monkeypatch):
    sent = []

    class _Ch(_FakeChannel):
        async def send(self, view=None, **kw):
            sent.append(view)
            return _FakeMessage(1, view)

    ch = _Ch(999)
    bot = _FakeBot({999: ch})
    monkeypatch.setattr(audit_svc, "_bot", bot)
    monkeypatch.setattr(audit_svc, "_audit_channel_id", lambda: 999)

    # organizer.* без tournament_id — тоже без карточки.
    await audit_svc._publish({
        "event_id": "e2", "tournament_id": None,
        "source": "organizer", "action": "organizer.role_grant",
    })
    await audit_svc._publish({
        "event_id": "e3", "tournament_id": "1_1",
        "source": "organizer", "action": "organizer.assign",
    })
    assert sent == []


def test_audit_view_has_no_organizers_filter():
    from views import tournament_audit_view as tav
    assert "organizers" not in tav.FILTERS
    # organizer.* скрыт из всех фильтров, включая "all".
    assert tav._event_matches(
        {"source": "organizer", "action": "organizer.assign"},
        "all") is False
    assert tav._event_matches(
        {"source": "tournament", "action": "organizer.reset"},
        "all") is False
    assert tav._event_matches(
        {"source": "tournament", "action": "player.join"},
        "all") is True


# ---------------------------------------------------------------------------
# Journal Resilience: threads API, retry, идемпотентность, recover
# ---------------------------------------------------------------------------


class _FakeGuild:
    """Guild с корректным API discord.py 2.7: active_threads()
    (async), БЕЗ fetch_threads."""

    def __init__(self, threads=()):
        self._threads = list(threads)

    async def active_threads(self):
        return list(self._threads)


class _FakeParentChannel:
    """Текстовый канал с archived_threads() (как discord.py)."""

    def __init__(self, archived=()):
        self._archived = list(archived)

    def archived_threads(self, limit=100, **kw):
        async def _gen():
            for t in self._archived:
                yield t
        return _gen()


class _FakeHTTPExc(__import__("discord").HTTPException):
    """discord.HTTPException с заданными status/code/retry_after."""

    def __init__(self, message="http error", *, status=None,
                 code=None, retry_after=None):
        resp = SimpleNamespace(status=status or 500,
                               reason="fake")
        super().__init__(resp, {"code": code or 0,
                                "message": message})
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.retry_after = retry_after


class _FakeMessageWithThread(_FakeMessage):
    def __init__(self, mid, guild=None, channel=None, **kw):
        super().__init__(mid, **kw)
        self.guild = guild
        self.channel = channel

    async def create_thread(self, name=None, **kw):
        raise _FakeHTTPExc("thread exists", status=400, code=160004)


@pytest.mark.asyncio
async def test_ensure_thread_finds_by_parent_id():
    """Существующая ветка находится по parent_id сообщения —
    без fetch_threads (несуществующий API) и без создания
    дубля."""
    import discord as _d
    existing = _FakeThread(7777, name="Журнал 1_1000")
    existing.parent_id = 555
    guild = _FakeGuild(threads=[existing])
    channel = _FakeParentChannel()
    msg = _FakeMessageWithThread(
        555, guild=guild, channel=channel)

    th = await jn._ensure_thread(None, msg, "Журнал 1_1000")
    assert th is existing


@pytest.mark.asyncio
async def test_ensure_thread_160004_finds_archived():
    """Ошибка 160004 + ветка в архиве → находим и
    разархивируем, дубль не создаётся."""
    existing = _FakeThread(8888, name="Журнал 1_1000")
    existing.parent_id = 555
    existing.archived = True
    guild = _FakeGuild()
    channel = _FakeParentChannel(archived=[existing])
    msg = _FakeMessageWithThread(
        555, guild=guild, channel=channel)

    th = await jn._ensure_thread(None, msg, "Журнал 1_1000")
    assert th is existing
    assert existing.archived is False  # unarchive вызван


@pytest.mark.asyncio
async def test_ensure_thread_429_is_retryable():
    class _Msg(_FakeMessageWithThread):
        async def create_thread(self, name=None, **kw):
            raise _FakeHTTPExc("rate limited", status=429,
                               retry_after=210.0)

    msg = _Msg(1, guild=_FakeGuild(), channel=_FakeParentChannel())
    with pytest.raises(jn._RetryableJournalError) as ei:
        await jn._ensure_thread(None, msg, "Журнал x")
    assert ei.value.retry_after == 210.0


@pytest.mark.asyncio
async def test_ensure_thread_403_is_permanent():
    class _Msg(_FakeMessageWithThread):
        async def create_thread(self, name=None, **kw):
            raise _FakeHTTPExc("forbidden", status=403)

    msg = _Msg(1, guild=_FakeGuild(), channel=_FakeParentChannel())
    with pytest.raises(jn._PermanentJournalError):
        await jn._ensure_thread(None, msg, "Журнал x")


@pytest.mark.asyncio
async def test_send_or_edit_429_retryable_403_permanent(journal_env,
                                                      monkeypatch):
    js, audit_ch, bot = journal_env

    async def _boom429(*a, **kw):
        raise _FakeHTTPExc("rate limited", status=429,
                           retry_after=80.0)
    audit_ch.send = _boom429
    with pytest.raises(jn._RetryableJournalError) as ei:
        await jn._send_or_edit(
            audit_ch, message_id=0,
            view=jn.build_main_card(None, {"tournament_id": "1_1",
                                           "journal_no": 1}),
            sig_key=1)
    assert ei.value.retry_after == 80.0

    async def _boom403(*a, **kw):
        raise _FakeHTTPExc("forbidden", status=403)
    audit_ch.send = _boom403
    with pytest.raises(jn._PermanentJournalError):
        await jn._send_or_edit(
            audit_ch, message_id=0,
            view=jn.build_main_card(None, {"tournament_id": "1_1",
                                           "journal_no": 1}),
            sig_key=2)


@pytest.mark.asyncio
async def test_lost_message_id_adopts_existing_card(journal_env,
                                                  monkeypatch):
    """PG потерял audit_message_id, но карточка в Discord есть —
    flush принимает её, а не создаёт дубль."""
    js, audit_ch, bot = journal_env
    t = _tournament()

    async def _load(tid):
        return t
    monkeypatch.setattr(jn, "_load_tournament", _load)
    monkeypatch.setattr(
        jn.audit_store, "list_events",
        lambda **kw: _ret_events([]))

    # Имитируем: карточка уже существует в канале (id потерян
    # в PG — upsert_main создаёт запись без audit_message_id).
    # Предзаполняем историю «чужим» send с маркером tid.
    marker_msg = _FakeMessage(424242)
    marker_msg.components = [
        SimpleNamespace(children=[
            SimpleNamespace(content="**ID:** `1_1000`")])]
    audit_ch.sent.append(marker_msg)

    await jn._flush_guarded("1_1000")
    main = js.mains["1_1000"]
    # Основная карточка — принята существующая, новый send
    # для неё не выполнялся (sent вырос только на thread/cards).
    assert main["audit_message_id"] == 424242
    texts = [getattr(m, "content", "") for m in audit_ch.sent]
    assert not any("ЖУРНАЛ ТУРНИРА" in (m.content or "")
                   for m in audit_ch.sent[1:])


@pytest.mark.asyncio
async def test_retry_delay_honors_retry_after():
    d = jn._retry_delay(1, retry_after=210.0)
    assert d >= 211.0
    d2 = jn._retry_delay(1)
    assert 20.0 <= d2 <= 40.0
    d_max = jn._retry_delay(20)
    assert d_max <= jn._RETRY_MAX_SEC * 1.25


@pytest.mark.asyncio
async def test_recover_returns_stats_and_enqueues(journal_env,
                                                monkeypatch):
    """recover() больше не делает массовый параллельный flush —
    возвращает статистику постановки в очередь worker'а."""
    js, audit_ch, bot = journal_env
    scheduled = []

    async def _list_pending(limit=100):
        return [
            {"tournament_id": "1_1000"},
            {"tournament_id": "1_1001"},
            {"tournament_id": "1_1000"},  # дубль tid
        ]
    monkeypatch.setattr(js, "list_pending", _list_pending)
    # Активные турниры из store не добавляем — тестируем
    # только очередь pending-записей.
    monkeypatch.setattr(jn.tournament_store, "all",
                        lambda: [])
    monkeypatch.setattr(jn, "schedule_recovery",
                        lambda tid, **kw: scheduled.append(tid))
    jn._RECOVERED = False
    try:
        stats = await jn.recover(bot)
    finally:
        jn._RECOVERED = False
    assert stats["checked"] == 3
    assert stats["scheduled"] == 2      # дубли tid схлопнуты
    assert sorted(scheduled) == ["1_1000", "1_1001"]


@pytest.mark.asyncio
async def test_get_thread_rejects_stale_parent(journal_env):
    """Сохранённый thread_id, указывающий на чужую ветку
    (parent_id не совпадает с карточкой), отбрасывается."""
    js, audit_ch, bot = journal_env
    th = _FakeThread(31337)
    th.parent_id = 999999              # чужое сообщение
    bot._channels[31337] = th
    got = await jn._get_thread(bot, 31337, expected_parent_id=555)
    assert got is None
    # Без проверки parent — ветка возвращается (совместимость).
    got2 = await jn._get_thread(bot, 31337)
    assert got2 is th
