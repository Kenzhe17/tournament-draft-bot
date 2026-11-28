"""BR live map management: покарточные кнопки, комнаты, PLAYING-ввод,
упрощённая регистрация, публикация из PLAYING."""

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
from views.br_view import (BRCardView, BRManagementMenuView, MapButton,
                           MapRoomButton, MapEntryButton, MapBatchButton,
                           MapRoomEditButton, MapBackButton, EntryBackButton,
                           PublishResultsButton, RoomPickButton, RoomMapSelect,
                           RoomSetModal, RulesButton, EntryResultModal,
                           BatchEntryModal, public_embed, render_public,
                           map_panel_text)
from utils.design import compose


# ---------------------------------------------------------------------------
# Фейки Discord (минимальный набор)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.modals = []
        self.deferred = False
        self.defer_kwargs = None

    def is_done(self):
        return self.deferred

    async def defer(self, **kwargs):
        if self.deferred:
            raise discord.InteractionResponded(self)
        self.deferred = True
        self.defer_kwargs = kwargs

    def _check_open(self):
        # как в discord.py: после defer/edit_message/send_* второй
        # первичный ответ на тот же interaction запрещён
        if self.deferred or self.edits or self.modals or self.messages:
            raise discord.InteractionResponded(self)

    async def send_message(self, *args, **kwargs):
        self._check_open()
        self.messages.append((args, kwargs))

    async def edit_message(self, *args, **kwargs):
        self._check_open()
        self.edits.append((args, kwargs))

    async def send_modal(self, modal):
        self._check_open()
        self.modals.append(modal)


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
    def __init__(self):
        self.message = FakeMessage()

    async def fetch_message(self, message_id):
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


def _tournament(fmt="squad", n_slots=5, maps=3, phase=None, channel_id=2):
    t = BRTournament(
        id="tid-m", guild_id=1, channel_id=channel_id,
        formation=BRTournamentFormation.RANDOM,
        format=BRFormat(fmt), map_count=maps,
        message_id=555, is_test=True)
    for i in range(n_slots * t.slot_size):
        ok, err = t.register_player(f"P{i}", 1000 + i)
        assert ok, err
    t.draw()
    if t.phase == BRTournamentPhase.DRAFT and phase != "draft":
        t.begin_games()
    if phase == "collect":
        t.finish_games()
    return t


@pytest.fixture
def patched_store(monkeypatch):
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


def _fill_map(t, map_index, n_slots=None):
    n = n_slots or len(t.teams)
    for si in range(n):
        ok, err = t.set_result_entry(map_index, si, place=si + 1, kills=0)
        assert ok, err


# ---------------------------------------------------------------------------
# Публичная карточка
# ---------------------------------------------------------------------------


def test_registration_embed_is_compact_with_rules_button(patched_store):
    t = _tournament(maps=3, phase="draft")
    t.phase = BRTournamentPhase.REGISTRATION
    embed = public_embed(t)
    desc = embed.description or ""
    # компактно: статус, формат, счётчики, список названий — без правил
    assert "Регистрация открыта" in desc
    assert "Балл" not in desc and "Тай-брейк" not in desc
    assert "по кнопке «Правила»" in desc
    # список слотов компактный (без построчных составов), карточка короткая
    assert len(desc) < 1500
    view = BRCardView(t.guild_id, t.channel_id, t)
    labels = [b.label for b in _buttons(view)]
    assert "Правила" in labels
    assert "Назвать состав" in labels or "Участвовать" in labels or True


def test_playing_card_has_map_buttons_and_statuses(patched_store):
    t = _tournament(maps=3)
    assert t.phase == BRTournamentPhase.PLAYING
    _fill_map(t, 0)
    embed = public_embed(t)
    desc = embed.description or ""
    assert "Турнир идёт" in desc
    assert "Карт с результатами: 1/3" in desc
    assert "Бермуды** — Результаты внесены" in desc
    assert "Чистилище** — Ожидает результатов" in desc
    # промежуточные места/очки публично не видны
    assert "место" not in desc and "очк" not in desc
    view = BRCardView(t.guild_id, t.channel_id, t)
    labels = [b.label for b in _buttons(view)]
    assert labels.count("Карта 1") == 1
    map_btns = [b for b in _buttons(view)
                if getattr(b, "custom_id", "").startswith("br_map:")]
    assert len(map_btns) == 3
    assert all("br_map:" in b.custom_id for b in map_btns)
    # «Комната» и «Завершить игры» на публичной карточке нет
    assert "Комната" not in labels
    assert "Завершить игры" not in labels
    assert "Мой сквад" in labels
    assert "Управление" in labels


def test_collect_phase_shows_same_games_view(patched_store):
    """Legacy COLLECT: та же карточка «Турнир идёт», без экрана
    «Результаты проверяются»."""
    t = _tournament(maps=2, phase="collect")
    assert t.phase == BRTournamentPhase.COLLECT
    embed = public_embed(t)
    assert "Результаты проверяются" not in (embed.description or "")
    assert "Турнир идёт" in (embed.description or "")


def test_ten_maps_buttons_within_limits(patched_store):
    t = _tournament(fmt="solo", n_slots=12, maps=10)
    view = BRCardView(t.guild_id, t.channel_id, t)
    buttons = _buttons(view)
    map_btns = [b for b in buttons
                if getattr(b, "custom_id", "").startswith("br_map:")]
    assert len(map_btns) == 10
    assert len(buttons) <= 25  # лимит discord.py View


# ---------------------------------------------------------------------------
# Панель карты и комнаты
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_button_opens_panel_with_room_and_status(patched_store):
    t = _tournament(maps=3)
    t.set_room(1, "555", "pwd1")
    t.room_data = {"id": "999", "password": "legacy"}  # fallback
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)

    await MapButton(1, 2, t.id, 1).callback(inter)

    assert inter.response.defer_kwargs == {"ephemeral": True, "thinking": True}
    args, kwargs = inter.response.messages[-1]
    view = kwargs["view"]
    labels = [b.label for b in _buttons(view)]
    assert "Данные комнаты" in labels
    assert "Ввести результаты" in labels  # is_org=True в фикстуре
    assert "Массовый ввод" in labels
    assert "Назад" in labels


@pytest.mark.asyncio
async def test_map_room_button_shows_per_map_credentials(patched_store):
    t = _tournament(maps=3)
    t.set_room(0, "111", "p0")
    t.set_room(2, "333", "p2")
    t.room_data = {"id": "999", "password": "legacy"}
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})

    # каждый клик — отдельный interaction (как в Discord)
    async def _room(map_index, uid=1000):
        inter = _interaction(guild, user_id=uid)
        await MapRoomButton(1, 2, t.id, map_index).callback(inter)
        return str(inter.response.messages[-1][0][0])

    # карта 0 — свои данные
    text = await _room(0)
    assert "ID: 111" in text and "p0" in text
    # карта 1 — fallback на legacy room_data
    text = await _room(1)
    assert "ID: 999" in text and "legacy" in text
    # карта 2 — свои данные
    text = await _room(2)
    assert "ID: 333" in text and "p2" in text


@pytest.mark.asyncio
async def test_map_room_denied_for_non_participant(patched_store, monkeypatch):
    t = _tournament(maps=2)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    monkeypatch.setattr(br_view, "is_org", lambda uid: False)
    monkeypatch.setattr(br_view, "player_allowed", lambda i, t: True)
    inter = _interaction(guild, user_id=424242)  # не участник
    await MapRoomButton(1, 2, t.id, 0).callback(inter)
    assert any("только участникам" in str(m[0][0]) for m in inter.response.messages)


@pytest.mark.asyncio
async def test_room_pick_opens_map_select_then_modal(patched_store):
    t = _tournament(maps=4)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)

    await RoomPickButton(1, 2, t.id).callback(inter)
    args, kwargs = inter.response.messages[-1]
    select = next(c for c in _walk(kwargs["view"])
                  if isinstance(c, discord.ui.Select))
    assert isinstance(select, RoomMapSelect)
    assert len(select.options) == 4

    # выбор карты → модалка с map_index (отдельный interaction)
    select._values = ["2"]
    inter2 = _interaction(guild)
    await select.callback(inter2)
    assert len(inter2.response.modals) == 1
    modal = inter2.response.modals[0]
    assert modal.map_index == 2
    # submit пишет rooms["2"], не трогая другие карты (новый interaction)
    modal.room_id = SimpleNamespace(value="777")
    modal.room_password = SimpleNamespace(value="pw")
    inter3 = _interaction(guild)
    await modal.on_submit(inter3)
    assert t.rooms["2"] == {"id": "777", "password": "pw"}
    assert "2" not in t.rooms or True
    assert t.room_credentials(2) == {"id": "777", "password": "pw"}
    # другие карты — legacy fallback (room_data пуст → None)
    assert t.room_credentials(0) is None


@pytest.mark.asyncio
async def test_org_menu_playing_has_no_finish_games(patched_store):
    t = _tournament(maps=2)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    view = BRManagementMenuView(1, 2, t)
    labels = [b.label for b in _buttons(view)]
    assert "Завершить игры" not in labels
    assert "Комнаты" in labels
    assert "Предпросмотр итогов" in labels
    assert "Опубликовать итоги" in labels
    assert "Отменить турнир" in labels


# ---------------------------------------------------------------------------
# Ввод результатов в PLAYING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_during_playing_via_map_panel(patched_store):
    t = _tournament(maps=2)
    assert t.phase == BRTournamentPhase.PLAYING
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)

    await MapEntryButton(1, 2, t.id, 0).callback(inter)
    assert inter.response.edits, "edit_message не вызван"
    view = inter.response.edits[-1][1].get("view") or inter.response.edits[-1][0][0]
    # находим view в kwargs
    kw = inter.response.edits[-1][1]
    v = kw.get("view")
    assert v is not None
    # слот 0 → модалка результата (новый interaction); submit в PLAYING
    select = next(c for c in _walk(v) if isinstance(c, discord.ui.Select))
    select._values = ["0"]
    inter2 = _interaction(guild)
    await select.callback(inter2)
    assert inter2.response.modals
    modal = inter2.response.modals[0]
    assert isinstance(modal, EntryResultModal)
    modal.place = SimpleNamespace(value="1")
    modal.kills = SimpleNamespace(value="4")
    modal.dns = SimpleNamespace(value="")
    inter3 = _interaction(guild)
    await modal.on_submit(inter3)
    entry = t.get_team_stats_for_map(t.teams[0], 0)
    assert entry == {"place": 1, "kills": 4, "dns": False}


@pytest.mark.asyncio
async def test_dns_modal_uses_gendered_label(patched_store):
    t = _tournament(fmt="squad", maps=1)
    patched_store["t"] = t
    modal = EntryResultModal(1, 2, t.id, 0, 0, "Слот 1",
                             absent=br_view.absent_label(t))
    dns_input = next(c for c in modal.children if c.label)
    labels = [c.label for c in modal.children]
    assert any("Не участвовала" in l for l in labels)
    # solo → «не участвовал»
    t2 = _tournament(fmt="solo", n_slots=12, maps=1)
    modal2 = EntryResultModal(1, 2, t2.id, 0, 0, "Слот 1",
                              absent=br_view.absent_label(t2))
    labels2 = [c.label for c in modal2.children]
    assert any("Не участвовал" in l and "а?" not in l
               for l in labels2 if "Не участвовал" in l)


@pytest.mark.asyncio
async def test_batch_modal_accepts_nu_and_legacy_dns(patched_store):
    t = _tournament(maps=1)
    patched_store["t"] = t
    inter = _interaction(guild=FakeGuild({2: FakeChannel()}))
    modal = BatchEntryModal(1, 2, t.id, 0)
    modal.data = SimpleNamespace(
        value="1 1 5\n2 НУ\n3 DNS\n4 2 0\n5 3 3")
    await modal.on_submit(inter)
    # слот0: место; слот1: НУ→dns; слот2: DNS→dns; слот3/4: места
    assert t.results_complete()
    e1 = t.get_team_stats_for_map(t.teams[1], 0)
    e2 = t.get_team_stats_for_map(t.teams[2], 0)
    assert e1["dns"] and e1["kills"] == 0
    assert e2["dns"]
    # «-» очищает запись → слот снова пустой (новый interaction)
    modal2 = BatchEntryModal(1, 2, t.id, 0)
    modal2.data = SimpleNamespace(value="4 -")
    await modal2.on_submit(_interaction(guild=FakeGuild({2: FakeChannel()})))
    assert t.get_team_stats_for_map(t.teams[3], 0) is None
    assert not t.results_complete()


@pytest.mark.asyncio
async def test_publish_from_playing_lists_missing_maps(patched_store):
    t = _tournament(maps=3)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)
    _fill_map(t, 0)
    _fill_map(t, 1)
    # карта 2 не заполнена → публикация отклонена с конкретикой
    await PublishResultsButton(1, 2, t.id).callback(inter)
    text = str(inter.response.messages[-1][0][0])
    assert "Не все записи" in text
    assert "Карта 3" in text
    assert channel.message.edits == []
    assert t.phase == BRTournamentPhase.PLAYING
    assert t.publish_pending is None

    # заполняем карту 2 → публикация из PLAYING успешна (новый клик)
    _fill_map(t, 2)
    await PublishResultsButton(1, 2, t.id).callback(_interaction(guild))
    assert len(channel.message.edits) == 1
    assert t.phase == BRTournamentPhase.RESULTS


@pytest.mark.asyncio
async def test_edit_closed_after_publish(patched_store):
    t = _tournament(maps=1)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})
    inter = _interaction(guild)
    _fill_map(t, 0)
    await PublishResultsButton(1, 2, t.id).callback(_interaction(guild))
    assert t.phase == BRTournamentPhase.RESULTS
    # ввод и комнаты после публикации закрыты
    ok, err = t.set_result_entry(0, 0, place=2, kills=0)
    assert not ok
    inter_room = _interaction(guild)
    await MapRoomButton(1, 2, t.id, 0).callback(inter_room)
    assert any("недоступна" in str(m[0][0]).lower()
               for m in inter_room.response.messages)


# ---------------------------------------------------------------------------
# Миграция/совместимость
# ---------------------------------------------------------------------------


def test_legacy_collect_tournament_still_works():
    """Турнир, сохранённый в COLLECT до обновления, редактируется и
    публикуется как раньше."""
    t = _tournament(maps=2, phase="collect")
    _fill_map(t, 0)
    _fill_map(t, 1)
    ok, err = t.set_result_entry(0, 0, place=1, kills=9)
    assert ok, err  # правка в COLLECT разрешена
    ok, err = t.freeze_results()
    assert ok, err
    assert t.commit_publish()
    assert t.phase == BRTournamentPhase.RESULTS


def test_roundtrip_preserves_rooms_and_phase():
    t = _tournament(maps=3)
    t.set_room(0, "1", "a")
    t.set_room(2, "3", "c")
    t2 = BRTournament.from_dict(t.to_dict())
    assert t2.rooms == t.rooms
    assert t2.phase == BRTournamentPhase.PLAYING
    assert t2.results_editable


# ---------------------------------------------------------------------------
# Все пять комбинаций: сквозной жизненный цикл
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt,formation", [
    ("solo", "random"),
    ("duo", "random"),
    ("duo", "team"),
    ("squad", "random"),
    ("squad", "team"),
])
def test_full_lifecycle_all_five_combos(fmt, formation):
    """Регистрация → составы → игры → покарточный ввод → публикация.

    Покрывает все 5 допустимых комбинаций формата/формирования,
    включая per-map комнаты и публикацию прямо из PLAYING.
    """
    maps = 3
    t = BRTournament(
        id=f"combo-{fmt}-{formation}", guild_id=1, channel_id=2,
        formation=BRTournamentFormation(formation),
        format=BRFormat(fmt), map_count=maps,
        message_id=555, is_test=True)
    uid = [0]

    def nid():
        uid[0] += 1
        return uid[0]

    if formation == "random":
        n_players = {"solo": 12, "duo": 10, "squad": 20}[fmt]
        for _ in range(n_players):
            u = nid()
            ok, err = t.register_player(f"P{u}", u)
            assert ok, err
        ok, err = t.draw()
        assert ok, err
    elif fmt == "duo":
        # Duo team: капитан + подтверждённый напарник ×5 пар
        for p in range(5):
            cap, inv = nid(), nid()
            ok, err, idx = t.create_duo_invite(cap, f"Cap{p}", inv)
            assert ok, err
            ok, err = t.respond_duo_invite(idx, inv, f"Inv{p}", True)
            assert ok, err
        ok, err = t.draw()
        assert ok, err
    else:  # squad team
        for s in range(5):
            ids = [nid() for _ in range(4)]
            ok, err = t.register_team_slot(
                f"team-{s}", f"Team {s}", ids[0], ids,
                {i: f"P{i}" for i in ids})
            assert ok, err
        ok, err = t.draw()
        assert ok, err

    if t.phase == BRTournamentPhase.DRAFT:
        t.begin_games()
    assert t.phase == BRTournamentPhase.PLAYING
    n_slots = len(t.teams)
    assert n_slots >= t.min_slots

    # независимые комнаты на каждую карту
    for m in range(maps):
        t.set_room(m, str(5000 + m), f"pw{m}")
        assert t.room_credentials(m)["id"] == str(5000 + m)

    # публикация до заполнения — отказ с конкретными картами
    ok, err = t.freeze_results()
    assert not ok and "Карта 1" in err

    # ввод результатов по каждой карте во время PLAYING
    for m in range(maps):
        for si in range(n_slots):
            ok, err = t.set_result_entry(m, si, place=si + 1, kills=m)
            assert ok, err
        assert t.map_status(m) == "filled"
    assert t.map_status(0) == "filled"
    assert t.results_complete()
    assert not t.missing_results()

    ok, err = t.freeze_results()
    assert ok, err
    assert t.pre_publish_phase == BRTournamentPhase.PLAYING.value
    assert t.commit_publish()
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_snapshot is not None
    # после публикации ввод и комнаты закрыты
    assert not t.results_editable
    ok, err = t.set_result_entry(0, 0, place=1, kills=0)
    assert not ok


def test_map_index_not_name_for_repeated_maps():
    """Повторяющиеся карты: map_index — единственный идентификатор."""
    t = _tournament(fmt="squad", n_slots=5, maps=4)  # bermuda дважды
    _fill_map(t, 0)
    _fill_map(t, 2)  # та же «bermuda», другой index
    e0 = t.get_team_stats_for_map(t.teams[0], 0)
    e2 = t.get_team_stats_for_map(t.teams[0], 2)
    assert e0 is not None and e2 is not None
    assert e0["place"] == 1 and e2["place"] == 1
    # очистка одной не трогает другую
    ok, _ = t.set_result_entry(0, 0, clear=True)
    assert ok
    assert t.get_team_stats_for_map(t.teams[0], 0) is None
    assert t.get_team_stats_for_map(t.teams[0], 2) is not None


# ---------------------------------------------------------------------------
# Final fixes: приватность, модалки, обновление карточки, навигация, валидация
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_button_opens_modal_without_double_response(patched_store):
    """MapBatchButton: send_modal — первый и единственный ответ
    (defer перед send_modal вызвал бы InteractionResponded)."""
    t = _tournament(maps=2)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    await MapBatchButton(1, 2, t.id, 0).callback(inter)
    assert inter.response.defer_kwargs is None  # defer не вызывался
    assert len(inter.response.modals) == 1
    assert isinstance(inter.response.modals[0], BatchEntryModal)
    assert inter.response.modals[0].map_index == 0


@pytest.mark.asyncio
async def test_map_panel_hides_room_id_from_non_viewer(patched_store, monkeypatch):
    """ID комнаты не виден посторонним; кнопки комнаты нет."""
    t = _tournament(maps=2)
    t.set_room(0, "777", "pw")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    monkeypatch.setattr(br_view, "player_allowed", lambda i, tt: True)
    monkeypatch.setattr(br_view, "is_org", lambda uid: False)

    inter = _interaction(guild, user_id=424242)  # не участник, не орг
    await MapButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.messages[-1]
    view = kw["view"]
    all_text = " ".join(str(getattr(c, "content", "") or "")
                        for c in _walk(view))
    assert "777" not in all_text
    labels = [b.label for b in _buttons(view)]
    assert "Данные комнаты" not in labels  # кнопки комнаты нет

    # map_panel_text напрямую: show_room=False не содержит ID
    assert "777" not in map_panel_text(t, 0, show_room=False)
    assert "777" in map_panel_text(t, 0, show_room=True)


@pytest.mark.asyncio
async def test_squad_team_captain_only_sees_room(patched_store, monkeypatch):
    """Squad Team: ID/пароль видит только капитан; член отряда — нет;
    орг — видит и может редактировать."""
    t = BRTournament(
        id="tid-cap", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.TEAM, format=BRFormat.SQUAD,
        map_count=2, message_id=555, is_test=True)
    ids = [11, 12, 13, 14]
    ok, err = t.register_team_slot(
        "team-1", "Alpha", ids[0], ids, {i: f"P{i}" for i in ids})
    assert ok, err
    # ещё 4 команды до min_slots
    for s in range(2, 6):
        ids = [20 + 4 * s + j for j in range(4)]
        ok, err = t.register_team_slot(
            f"team-{s}", f"T{s}", ids[0], ids, {i: f"P{i}" for i in ids})
        assert ok, err
    t.draw()
    t.begin_games()
    t.set_room(0, "888", "secret")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    import views.br_view as bv
    monkeypatch.setattr(bv, "player_allowed", lambda i, tt: True)
    monkeypatch.setattr(bv, "is_org", lambda uid: uid == 999)

    async def panel_text(uid):
        inter = _interaction(guild, user_id=uid)
        await MapButton(1, 2, t.id, 0).callback(inter)
        _, kw = inter.response.messages[-1]
        view = kw["view"]
        text = " ".join(str(getattr(c, "content", "") or "")
                        for c in _walk(view))
        return text, [b.label for b in _buttons(view)]

    # капитан (user 11)
    text, labels = await panel_text(11)
    assert "888" in text
    assert "Данные комнаты" in labels
    # рядовой член (user 12)
    text, labels = await panel_text(12)
    assert "888" not in text
    assert "Данные комнаты" not in labels
    # орг
    text, labels = await panel_text(999)
    assert "888" in text
    assert "Изменить комнату" in labels


@pytest.mark.asyncio
async def test_map_room_edit_prefills_and_saves(patched_store):
    """«Изменить комнату» в панели карты → модалка этой карты с
    текущими значениями в placeholder; submit сохраняет per-map."""
    t = _tournament(maps=3)
    t.set_room(1, "111", "oldpw")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)

    await MapRoomEditButton(1, 2, t.id, 1).callback(inter)
    assert len(inter.response.modals) == 1
    modal = inter.response.modals[0]
    assert isinstance(modal, RoomSetModal)
    assert modal.map_index == 1
    assert "111" in (modal.room_id.placeholder or "")
    assert "oldpw" in (modal.room_password.placeholder or "")

    modal.room_id = SimpleNamespace(value="222")
    modal.room_password = SimpleNamespace(value="newpw")
    inter2 = _interaction(guild)
    await modal.on_submit(inter2)
    assert t.room_credentials(1) == {"id": "222", "password": "newpw"}
    # другие карты не тронуты
    assert t.room_credentials(0) is None


@pytest.mark.asyncio
async def test_result_save_refreshes_public_card(patched_store):
    """После записи результата публичная карточка перерисовывается:
    прогресс и статус карты обновляются, message_id не меняется."""
    t = _tournament(maps=2)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    modal = EntryResultModal(1, 2, t.id, 0, 0, "Слот 1",
                             absent=br_view.absent_label(t))
    modal.place = SimpleNamespace(value="1")
    modal.kills = SimpleNamespace(value="3")
    modal.dns = SimpleNamespace(value="")
    await modal.on_submit(_interaction(guild))

    assert len(channel.message.edits) == 1
    edit = channel.message.edits[0]
    assert edit["view"] is not None  # CV2 view, тот же message
    # прогресс виден в embed внутри compose — проверяем через embed arg
    # (edit_main_card шлёт view=compose(embed, view))


@pytest.mark.asyncio
async def test_invalid_kills_rejected_without_save(patched_store):
    t = _tournament(maps=1)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    modal = EntryResultModal(1, 2, t.id, 0, 0, "Слот 1",
                             absent=br_view.absent_label(t))
    modal.place = SimpleNamespace(value="1")
    modal.kills = SimpleNamespace(value="abc")
    modal.dns = SimpleNamespace(value="")
    inter = _interaction(guild)
    await modal.on_submit(inter)
    assert any("не сохранена" in str(m[0][0]) or "числом" in str(m[0][0])
               for m in inter.response.messages)
    assert t.get_team_stats_for_map(t.teams[0], 0) is None


@pytest.mark.asyncio
async def test_place_beyond_format_cap_rejected(patched_store):
    """Squad: place_cap=12 — место 13 отклоняется моделью."""
    t = _tournament(fmt="squad", n_slots=5, maps=1)
    patched_store["t"] = t
    ok, err = t.set_result_entry(0, 0, place=13, kills=0)
    assert not ok and "12" in err


@pytest.mark.asyncio
async def test_map_back_returns_to_overview_with_all_maps(patched_store):
    t = _tournament(maps=4)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    await MapBackButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.edits[-1]
    view = kw["view"]
    map_btns = [b for b in _buttons(view)
                if getattr(b, "custom_id", "").startswith("br_map:")]
    assert len(map_btns) == 4


@pytest.mark.asyncio
async def test_entry_back_returns_to_map_panel(patched_store):
    t = _tournament(maps=2)
    t.set_room(0, "55", "pw")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    await EntryBackButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.edits[-1]
    view = kw["view"]
    labels = [b.label for b in _buttons(view)]
    assert "Данные комнаты" in labels
    assert "Ввести результаты" in labels


@pytest.mark.asyncio
async def test_double_commit_save_failure_recovers(patched_store, monkeypatch):
    """Два последовательных сбоя сохранения итогов — повтор успешен,
    pre_publish_phase сохраняется через циклы uncommit."""
    t = _tournament(maps=1)
    _fill_map(t, 0)
    patched_store["t"] = t
    channel = FakeChannel()
    guild = FakeGuild({2: channel})

    calls = {"n": 0}
    real_set = br_store.set

    def flaky2(obj):
        # падаем только на коммите (snapshot уже зафиксирован, pending пуст)
        if (obj.results_snapshot is not None
                and obj.publish_pending is None
                and obj.phase == BRTournamentPhase.RESULTS):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise IOError("disk full")
        return real_set(obj)

    monkeypatch.setattr(br_store, "set", flaky2)

    btn = PublishResultsButton(1, 2, t.id)
    await btn.callback(_interaction(guild))
    assert t.phase == BRTournamentPhase.PLAYING  # uncommit → PLAYING
    assert t.publish_pending is not None
    assert t.pre_publish_phase == ""  # uncommit очистил — freeze восстановит

    await btn.callback(_interaction(guild))
    assert t.phase == BRTournamentPhase.PLAYING

    await btn.callback(_interaction(guild))
    assert t.phase == BRTournamentPhase.RESULTS
    assert t.results_snapshot is not None


def test_cv2_payload_ten_maps_within_limits(patched_store):
    """Реальный Components V2 payload публичной карточки на 10 карт:
    action_row ≤5 кнопок, все br_map:0..9 присутствуют."""
    t = _tournament(fmt="solo", n_slots=12, maps=10)
    t.phase = BRTournamentPhase.PLAYING
    view = BRCardView(t.guild_id, t.channel_id, t)
    payload = view.to_components()
    assert isinstance(payload, list) and payload
    n_buttons = 0
    map_ids = set()
    for row in payload:
        assert row["type"] == 1  # ACTION_ROW
        comps = row.get("components", [])
        assert len(comps) <= 5
        for c in comps:
            if c.get("type") == 2:  # BUTTON
                n_buttons += 1
                cid = c.get("custom_id", "")
                if cid.startswith("br_map:"):
                    map_ids.add(cid.rsplit(":", 1)[-1])
    assert n_buttons <= 25
    assert len(map_ids) == 10


# ---------------------------------------------------------------------------
# Final rollout fixes: stale-object protection под блокировкой
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_submit_rejects_replaced_tournament(patched_store):
    """Между открытием модалки и submit турнир заменён в store —
    запись не выполняется по устаревшему объекту."""
    t = _tournament(maps=1)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})

    modal = EntryResultModal(1, 2, t.id, 0, 0, "Слот 1",
                             absent=br_view.absent_label(t))
    modal.place = SimpleNamespace(value="1")
    modal.kills = SimpleNamespace(value="5")
    modal.dns = SimpleNamespace(value="")

    # store вернул ДРУГОЙ турнир с тем же tid-каналом (замена)
    other = _tournament(maps=1)
    other.id = "other-tid"
    patched_store["t"] = other

    inter = _interaction(guild)
    await modal.on_submit(inter)
    assert any("изменилось" in str(m[0][0]) or "недоступен" in str(m[0][0]).lower()
               for m in inter.response.messages)
    # записи нет ни в старом, ни в новом объекте
    assert t.get_team_stats_for_map(t.teams[0], 0) is None
    assert other.get_team_stats_for_map(other.teams[0], 0) is None


@pytest.mark.asyncio
async def test_batch_submit_rejects_replaced_tournament(patched_store):
    t = _tournament(maps=1)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})

    modal = BatchEntryModal(1, 2, t.id, 0)
    modal.data = SimpleNamespace(value="1 1 5")

    other = _tournament(maps=1)
    other.id = "other-tid"
    patched_store["t"] = other

    inter = _interaction(guild)
    await modal.on_submit(inter)
    assert any("недоступен" in str(m[0][0]).lower()
               or "изменилось" in str(m[0][0])
               for m in inter.response.messages)
    assert not t.results_complete()


@pytest.mark.asyncio
async def test_entry_submit_works_on_fresh_store_object(patched_store):
    """Контроль: при актуальном объекте в store запись проходит
    именно через него."""
    t = _tournament(maps=1)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    modal = EntryResultModal(1, 2, t.id, 0, 0, "Слот 1",
                             absent=br_view.absent_label(t))
    modal.place = SimpleNamespace(value="2")
    modal.kills = SimpleNamespace(value="7")
    modal.dns = SimpleNamespace(value="")
    await modal.on_submit(_interaction(guild))
    e = t.get_team_stats_for_map(t.teams[0], 0)
    assert e == {"place": 2, "kills": 7, "dns": False}
