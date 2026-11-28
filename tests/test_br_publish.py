"""Публикация итогов BR: снимок, финальные кнопки, сбой Discord, рестарт.

Проверяет фактический payload message.edit (финальный embed + кнопки
«По картам»/«Мой сквад» без «Комната»/«Управление»), идемпотентность
и восстановление отложенной публикации после «рестарта».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import discord

from models.br_tournament import (BRFormat, BRTournament, BRTournamentFormation,
                                  BRTournamentPhase)
from storage.br_store import br_store
from views import br_view
from views.br_view import (BRCardView, PublishResultsButton,
                           resume_pending_publish)
from utils.design import compose


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False
        self.defer_kwargs = None

    def is_done(self):
        return self.deferred

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeFollowup:
    def __init__(self, response):
        self._response = response

    async def send(self, *args, **kwargs):
        self._response.messages.append((args, kwargs))


class FakeMessage:
    def __init__(self, message_id=555):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, fail=False):
        self.message = FakeMessage()
        self.fail = fail

    async def fetch_message(self, message_id):
        if self.fail:
            raise discord.HTTPException(None, "boom")
        return self.message


class FakeGuild:
    def __init__(self, channels):
        self.id = 1
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_role(self, role_id):
        return None

    def get_member(self, user_id):
        return None


class FakeClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild


def _interaction(guild, user_id=999):
    resp = FakeResponse()
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=f"User{user_id}",
                             name=f"user{user_id}"),
        guild=guild,
        channel=guild.get_channel(2),
        response=resp,
        followup=FakeFollowup(resp),
    )


def _walk(view):
    for child in getattr(view, "children", []):
        yield child
        yield from _walk(child)


def _buttons(view):
    return [c for c in _walk(view) if isinstance(c, discord.ui.Button)]


def _collect_tournament(fmt="squad", n_slots=5, maps=2, channel_id=2):
    t = BRTournament(
        id="tid-1", guild_id=1, channel_id=channel_id,
        formation=BRTournamentFormation.RANDOM,
        format=BRFormat(fmt), map_count=maps,
        message_id=555, is_test=True)
    for i in range(n_slots * t.slot_size):
        ok, err = t.register_player(f"P{i}", 1000 + i)
        assert ok, err
    t.draw()
    if t.phase == BRTournamentPhase.DRAFT:
        t.begin_games()
    t.finish_games()
    for si in range(n_slots):
        for m in range(maps):
            ok, err = t.set_result_entry(m, si, place=si + 1, kills=m)
            assert ok, err
    assert t.results_complete()
    return t


@pytest.fixture
def patched_store(monkeypatch):
    """br_store → один турнир в памяти, set() только записывает."""
    state = {"t": None, "sets": []}

    def _get(gid, cid):
        t = state["t"]
        return t if t and t.guild_id == gid and t.channel_id == cid else None

    monkeypatch.setattr(br_store, "get", _get)
    monkeypatch.setattr(br_store, "get_any", _get)
    monkeypatch.setattr(br_store, "history", lambda: [])
    monkeypatch.setattr(br_store, "set", lambda t: state["sets"].append(t))
    monkeypatch.setattr(br_view, "is_org", lambda uid: True)
    return state


# ---------------------------------------------------------------------------
# Финальные кнопки в payload message.edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_edit_payload_has_final_buttons(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    assert len(channel.message.edits) == 1
    view = channel.message.edits[0]["view"]
    buttons = _buttons(view)
    labels = [b.label for b in buttons]
    cids = [b.custom_id for b in buttons]
    # финальные кнопки: пагинация по картам + «Мой сквад»
    assert "Мой сквад" in labels
    assert "◀" in labels and "▶" in labels
    assert any("br_rpage" in c for c in cids)
    # «Комната» и «Управление» в публичном финале отсутствуют
    assert "Комната" not in labels
    assert "Управление" not in labels
    assert not any("br_room" in c or "br_org_menu" in c for c in cids)
    # состояние закоммичено: RESULTS + снимок в архивном объекте
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_published and t.results_snapshot is not None
    assert patched_store["sets"]


@pytest.mark.asyncio
async def test_publish_rejects_stale_tournament_id(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, "other-tid")
    await btn.callback(inter)
    assert channel.message.edits == []
    assert t.phase == BRTournamentPhase.COLLECT
    assert any("устарела" in str(m[0][0]) for m in inter.response.messages)


# ---------------------------------------------------------------------------
# Сбой Discord → повтор с тем же снимком
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_discord_failure_keeps_pending_and_retry_same_snapshot(
        patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel(fail=True)
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    # Discord упал: карточка не тронута, состояние COLLECT+pending
    assert channel.message.edits == []
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is not None
    assert any("НЕ опубликованы" in str(m[0][0]) for m in inter.response.messages)
    snap1 = [dict(r) for r in t.publish_pending]

    # повтор: Discord починился — тот же снимок, без второго сообщения
    channel.fail = False
    await btn.callback(inter)
    assert len(channel.message.edits) == 1  # edit того же message_id
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_snapshot == snap1


@pytest.mark.asyncio
async def test_double_publish_is_idempotent(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)
    btn = PublishResultsButton(1, 2, t.id)

    await btn.callback(inter)
    await btn.callback(inter)  # повторный клик
    # вторая публикация не делает второй edit и не падает
    assert len(channel.message.edits) == 1
    assert t.phase == BRTournamentPhase.RESULTS


# ---------------------------------------------------------------------------
# Рестарт посередине публикации
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_pending_publish_after_restart(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    assert t.freeze_results()[0]
    br_store.set(t)  # «сбой» после заморозки, до commit
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is not None

    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    client = FakeClient(guild)

    ok = await resume_pending_publish(t, client)
    assert ok
    assert t.phase == BRTournamentPhase.RESULTS
    assert len(channel.message.edits) == 1
    # payload — финальные кнопки
    buttons = _buttons(channel.message.edits[0]["view"])
    labels = [b.label for b in buttons]
    assert "Мой сквад" in labels and "Комната" not in labels
    assert "Управление" not in labels


@pytest.mark.asyncio
async def test_resume_without_pending_is_noop(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    ok = await resume_pending_publish(t, FakeClient(FakeGuild({})))
    assert not ok
    assert t.phase == BRTournamentPhase.COLLECT


# ---------------------------------------------------------------------------
# Сбой сохранения после успешного edit → повтор работает
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_save_failure_allows_retry(monkeypatch, patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    results_sets = {"n": 0}
    real_set = br_store.set  # уже замоканный fixture'ом — только запись

    def flaky_set(obj):
        if obj.phase == BRTournamentPhase.RESULTS:
            results_sets["n"] += 1
            if results_sets["n"] == 1:
                raise IOError("disk full")
        return real_set(obj)

    monkeypatch.setattr(br_store, "set", flaky_set)
    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    # commit упал: объект откачен в COLLECT+pending, карточка уже финал
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is not None
    assert t.results_snapshot is None
    assert any("сохранение не удалось" in str(m[0][0])
               for m in inter.response.messages)

    # повторный клик: тот же снимок, коммит успешен
    await btn.callback(inter)
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_snapshot is not None
    assert len(channel.message.edits) == 2  # edit идемпотентен по message_id


# ---------------------------------------------------------------------------
# Отмена vs публикация: карточка и архив всегда совпадают
# ---------------------------------------------------------------------------


def _card_texts(view):
    texts = []
    for child in _walk(view):
        content = getattr(child, "content", None)
        if isinstance(content, str):
            texts.append(content)
    return texts


@pytest.mark.asyncio
async def test_cancel_during_publish_restores_card(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    # имитация: Discord edit финала прошёл, но до commit фазу сменили
    real_edit = channel.message.edit

    async def edit_then_cancel(**kwargs):
        await real_edit(**kwargs)
        t.phase = BRTournamentPhase.CANCELLED
        t.cancel_reason = "отменено параллельно"

    channel.message.edit = edit_then_cancel

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(inter)

    # турнир остался CANCELLED, карточка перерисована в отменённый вид
    assert t.phase == BRTournamentPhase.CANCELLED
    assert len(channel.message.edits) == 2
    last_texts = _card_texts(channel.message.edits[-1]["view"])
    assert any("Турнир отменён" in s for s in last_texts)
    assert any("прервана" in str(m[0][0]) for m in inter.response.messages)


# ---------------------------------------------------------------------------
# Порядок запуска: resume после READY
# ---------------------------------------------------------------------------


def test_br_resume_runs_in_on_ready_not_setup_hook():
    src = (Path(__file__).resolve().parents[1] / "bot.py").read_text(
        encoding="utf-8")
    setup_idx = src.index("async def setup_hook")
    ready_idx = src.index("async def on_ready")
    setup_body = src[setup_idx:ready_idx]
    ready_body = src[ready_idx:]
    # регистрация persistent views — в setup_hook (рано, безопасно)
    assert "BRCardView" in setup_body
    # resume/sync карточек — только после READY (кэш гильдий готов)
    assert "resume_pending_publish" not in setup_body
    assert "resume_pending_publish" in ready_body
    assert "sync_public_card" in ready_body


# ---------------------------------------------------------------------------
# Откат archive при сбое записи (store)
# ---------------------------------------------------------------------------


def test_archive_save_failure_keeps_active_in_memory():
    from storage.br_store import BRStore

    t = _collect_tournament()
    t.phase = BRTournamentPhase.RESULTS
    t.results_published = True
    store = BRStore.__new__(BRStore)
    store._tournaments = {(1, 2): t}
    store._history = []

    def boom():
        raise IOError("disk full")

    store.save = boom
    with pytest.raises(IOError):
        store.set(t)
    # память откачена: турнир всё ещё активный, история пуста
    assert store.get(1, 2) is t
    assert store.history() == []


# ---------------------------------------------------------------------------
# Этап 2: is_test — только орги, без влияния на реальные данные
# ---------------------------------------------------------------------------


def _registration_tournament(is_test=True):
    t = BRTournament(
        id="tid-test", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.RANDOM,
        format=BRFormat.SOLO, map_count=1,
        message_id=555, is_test=is_test)
    return t


@pytest.mark.asyncio
async def test_test_tournament_join_blocked_for_non_org(patched_store,
                                                        monkeypatch):
    import services.vac_service as vac_service
    from views.br_view import JoinLeaveButton

    monkeypatch.setattr(vac_service, "check_vac",
                        lambda *a, **k: _noop())
    # is_org: только uid=999 — «владелец»
    monkeypatch.setattr(br_view, "is_org", lambda uid: uid == 999)

    t = _registration_tournament(is_test=True)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    # обычный игрок не может зарегистрироваться в тестовом BR
    inter = _interaction(guild, user_id=555)
    await JoinLeaveButton(1, 2, t.id, "join").callback(inter)
    assert not t.players
    assert any("тестовый турнир" in str(m[0][0]).lower()
               for m in inter.response.messages)

    # орг может
    inter2 = _interaction(guild, user_id=999)
    await JoinLeaveButton(1, 2, t.id, "join").callback(inter2)
    assert len(t.players) == 1


async def _noop():
    return None


@pytest.mark.asyncio
async def test_regular_tournament_join_allowed_for_players(patched_store,
                                                           monkeypatch):
    import services.vac_service as vac_service
    from views.br_view import JoinLeaveButton

    monkeypatch.setattr(vac_service, "check_vac", lambda *a, **k: _noop())
    monkeypatch.setattr(br_view, "is_org", lambda uid: False)

    t = _registration_tournament(is_test=False)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    inter = _interaction(guild, user_id=555)
    await JoinLeaveButton(1, 2, t.id, "join").callback(inter)
    assert len(t.players) == 1


@pytest.mark.asyncio
async def test_purge_removes_test_tournament_completely(patched_store,
                                                        monkeypatch):
    from views.br_view import CancelConfirmButton

    removed = []
    monkeypatch.setattr(
        br_store, "remove",
        lambda gid, cid, tid=None: removed.append((gid, cid, tid)) or True)

    t = _collect_tournament()
    t.is_test = True
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = CancelConfirmButton(1, 2, t.id, "cleanup", purge=True)
    await btn.callback(inter)

    assert removed == [(1, 2, t.id)]
    assert any("полностью удалён" in str(m[0][0])
               for m in inter.response.messages)
    # карточка заменена заглушкой
    assert channel.message.edits


@pytest.mark.asyncio
async def test_purge_refuses_non_test_tournament(patched_store, monkeypatch):
    from views.br_view import CancelConfirmButton

    removed = []
    monkeypatch.setattr(
        br_store, "remove",
        lambda *a, **k: removed.append(a) or True)

    t = _collect_tournament()
    t.is_test = False
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    btn = CancelConfirmButton(1, 2, t.id, "x", purge=True)
    await btn.callback(inter)

    assert removed == []
    assert any("только тестовый" in str(m[0][0])
               for m in inter.response.messages)


def test_store_remove_and_load_resilience(tmp_path, monkeypatch):
    import json
    import storage.br_store as br_store_mod
    from storage.br_store import BRStore

    store = BRStore.__new__(BRStore)
    t = _collect_tournament()
    store._tournaments = {(1, 2): t}
    store._history = [t]
    monkeypatch.setattr(store, "save", lambda: None)

    # удаляем только активный (история по id не трогается? — remove по id
    # удаляет и активный, и архивную запись с тем же tid)
    assert store.remove(1, 2, t.id) is True
    assert store.get(1, 2) is None
    assert store.history() == []
    # повтор — False
    assert store.remove(1, 2, t.id) is False

    # load(): битые записи пропускаются, валидные грузятся
    data = {
        "active": {
            "1:2": "not-a-dict",          # упадёт в from_dict → пропуск
            "bad-key": {"id": "x"},       # нет ':' → пропуск
            "9:9": _collect_tournament().to_dict(),
        },
        "history": ["garbage", {"id": "h1"}],
    }
    f = tmp_path / "br.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(br_store_mod, "BR_DATA_FILE", f)
    s2 = BRStore.__new__(BRStore)
    s2.load()
    assert (9, 9) in s2._tournaments
    assert len(s2._tournaments) == 1
    # {"id": "h1"} — минимальный dict, from_dict его проглотит или пропустит
    assert all(isinstance(h, BRTournament) for h in s2._history)


# ---------------------------------------------------------------------------
# Safety gate: роли, карантин повреждённых данных, откаты publish
# ---------------------------------------------------------------------------


class _FakeRole:
    pass


class _FakeMember:
    def __init__(self, role):
        self.roles = [role]
        self.removed = []

    async def remove_roles(self, role):
        self.removed.append(role)
        self.roles.remove(role)


class _RoleGuild(FakeGuild):
    def __init__(self, channels, role=None, members=None):
        super().__init__(channels)
        self._role = role
        self._members = members or {}

    def get_role(self, role_id):
        return self._role

    def get_member(self, user_id):
        return self._members.get(user_id)


def _role_tournament(is_test, member_ids):
    t = _collect_tournament()
    t.is_test = is_test
    # участники слота 0 — проверяемые ID
    t.teams[0]["member_ids"] = list(member_ids)
    t.players = [{"id": uid, "name": f"P{uid}"} for uid in member_ids]
    return t


@pytest.mark.asyncio
async def test_strip_roles_noop_for_test_tournament(monkeypatch):
    from views.br_view import strip_tournament_roles

    role = _FakeRole()
    member = _FakeMember(role)
    guild = _RoleGuild({2: FakeChannel()}, role=role, members={1001: member})
    t = _role_tournament(is_test=True, member_ids=[1001])

    monkeypatch.setattr(br_store, "get_by_guild", lambda gid: [t])
    await strip_tournament_roles(guild, t)
    assert member.removed == []
    assert role in member.roles


@pytest.mark.asyncio
async def test_strip_roles_ignores_test_tournaments_in_busy(monkeypatch):
    """Участник тестового BR не «удерживает» реальную роль: при снятии
    роли у реального турнира test-записи не учитываются как busy."""
    from views.br_view import strip_tournament_roles

    role = _FakeRole()
    member = _FakeMember(role)
    guild = _RoleGuild({2: FakeChannel()}, role=role, members={1001: member})
    real_t = _role_tournament(is_test=False, member_ids=[1001])
    test_t = _role_tournament(is_test=True, member_ids=[1001])
    test_t.id = "tid-test-other"

    # оба «активны» в одном guild: реальный + тестовый с тем же uid
    monkeypatch.setattr(br_store, "get_by_guild",
                        lambda gid: [real_t, test_t])
    await strip_tournament_roles(guild, real_t)
    # роль снята: test-турнир не считается занятостью
    assert member.removed == [role]


def test_corrupt_entries_survive_save_reload(tmp_path, monkeypatch):
    import json
    import storage.br_store as br_store_mod
    from storage.br_store import BRStore

    valid = _collect_tournament()
    src = tmp_path / "br.json"
    src.write_text(json.dumps({
        "active": {"1:2": valid.to_dict(), "3:4": "broken-entry"},
        "history": [{"id": "h-ok"}, "junk-item"],
    }), encoding="utf-8")
    monkeypatch.setattr(br_store_mod, "BR_DATA_FILE", src)

    s1 = BRStore.__new__(BRStore)
    s1.load()
    assert s1.corrupt_count == 2
    assert (1, 2) in s1._tournaments

    # save → reload: повреждённые записи не теряются
    s1.save()
    raw = json.loads(src.read_text(encoding="utf-8"))
    assert raw["active"]["3:4"] == "broken-entry"
    assert "junk-item" in raw["history"]

    s2 = BRStore.__new__(BRStore)
    s2.load()
    assert s2.corrupt_count == 2
    assert (1, 2) in s2._tournaments


def test_whole_file_corrupt_preserved_before_overwrite(tmp_path, monkeypatch):
    import storage.br_store as br_store_mod
    from storage.br_store import BRStore

    src = tmp_path / "br.json"
    original = b'{"active": {broken json!!!'
    src.write_bytes(original)
    monkeypatch.setattr(br_store_mod, "BR_DATA_FILE", src)

    s = BRStore.__new__(BRStore)
    s.load()
    assert s._tournaments == {} and s._history == []
    assert s._preserve_corrupt_file is True

    s.save()  # перезапись — но исходник сохранён отдельным файлом
    backups = list(tmp_path.glob("br.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    # основной файл — валидный JSON новой схемы
    import json
    assert json.loads(src.read_text(encoding="utf-8"))["active"] == {}


@pytest.mark.asyncio
async def test_freeze_rollback_when_pending_save_fails(patched_store,
                                                        monkeypatch):
    """Сбой br_store.set после freeze_results: publish_pending откачен."""
    from views.br_view import PublishResultsButton

    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    calls = {"n": 0}

    def flaky_set(obj):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IOError("disk full")
        patched_store["sets"].append(obj)

    monkeypatch.setattr(br_store, "set", flaky_set)
    await PublishResultsButton(1, 2, t.id).callback(inter)

    # freeze откачен: pending не завис в памяти, фаза COLLECT
    assert t.phase == BRTournamentPhase.COLLECT
    assert t.publish_pending is None
    assert t.results_snapshot is None
    assert any("не удалось сохранить снимок" in str(m[0][0]).lower()
               for m in inter.response.messages)


@pytest.mark.asyncio
async def test_resume_restores_card_when_phase_changed(patched_store):
    """resume: cancel/purge между edit и commit → карточка возвращается."""
    from views.br_view import resume_pending_publish

    t = _collect_tournament()
    t.phase = BRTournamentPhase.COLLECT
    snap = t.calculate_final_results()
    t.publish_pending = snap
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    # к моменту commit турнир уже отменён (другой объект в store)
    cancelled = _collect_tournament()
    cancelled.id = t.id
    cancelled.phase = BRTournamentPhase.CANCELLED
    cancelled.cancel_reason = "отменён параллельно"
    patched_store["t"] = cancelled

    ok = await resume_pending_publish(t, FakeClient(guild))
    assert ok is False
    # edit финала + edit восстановления актуального вида
    assert len(channel.message.edits) == 2


@pytest.mark.asyncio
async def test_resume_restores_card_when_purged(patched_store):
    from views.br_view import resume_pending_publish

    t = _collect_tournament()
    t.phase = BRTournamentPhase.COLLECT
    t.publish_pending = t.calculate_final_results()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    # purge: store.get → None (турнира больше нет)
    patched_store["t"] = None

    ok = await resume_pending_publish(t, FakeClient(guild))
    assert ok is False
    assert len(channel.message.edits) == 2


@pytest.mark.asyncio
async def test_defer_kwargs_include_thinking(patched_store):
    t = _collect_tournament()
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)
    await PublishResultsButton(1, 2, t.id).callback(inter)
    assert inter.response.defer_kwargs == {"ephemeral": True, "thinking": True}


def test_discord_interaction_types_support_defer_and_followup():
    """Проверка реальных типов discord.py, а не только mock."""
    import inspect
    from discord import Interaction, InteractionResponse, Webhook

    sig = inspect.signature(InteractionResponse.defer)
    assert {"ephemeral", "thinking"} <= set(sig.parameters)
    assert hasattr(InteractionResponse, "is_done")
    # interaction.followup — Webhook, send принимает ephemeral и view
    assert "followup" in dir(Interaction)
    send_sig = inspect.signature(Webhook.send)
    assert "ephemeral" in send_sig.parameters
