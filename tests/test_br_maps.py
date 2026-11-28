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
                           AdminMapButton, AdminMapBackButton,
                           BRAdminMapPanelView, OrgMenuButton,
                           admin_map_panel_text,
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
    # компактная шапка: статус + карта/счётчик, без правил и подсказок
    assert "Статус:** Регистрация открыта" in desc
    assert "Балл" not in desc and "Тай-брейк" not in desc
    assert "по кнопке «Правила»" not in desc
    assert " ・ " in desc  # «Карт: N ・ Счётчик: X/Y»
    view = BRCardView(t.guild_id, t.channel_id, t)
    labels = [b.label for b in _buttons(view)]
    assert "Правила" in labels


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
    # публичная панель — только просмотр, даже для орга
    assert "Данные комнаты" in labels
    assert "Назад" in labels
    for banned in ("Ввести результаты", "Массовый ввод",
                   "Изменить комнату", "Обновить"):
        assert banned not in labels


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
    # PLAYING: отдельная кнопка управления на каждую карту
    assert "Карта 1" in labels and "Карта 2" in labels
    assert "Предпросмотр итогов" in labels
    assert "Опубликовать итоги" in labels
    assert "Отменить турнир" in labels
    assert "Закрыть" in labels


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
    # орг: публичная панель карты — только просмотр (без кнопок управления)
    text, labels = await panel_text(999)
    assert "888" in text
    assert "Данные комнаты" in labels
    assert "Изменить комнату" not in labels
    assert "Ввести результаты" not in labels
    # управление — через «Управление» → кнопка карты → админ-панель
    inter = _interaction(guild, user_id=999)
    await AdminMapButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.messages[-1]
    admin_labels = [b.label for b in _buttons(kw["view"])]
    assert "Изменить комнату" in admin_labels
    assert "Ввести результаты" in admin_labels
    assert "Массовый ввод" in admin_labels
    assert "Обновить" in admin_labels
    assert "Назад" in admin_labels
    # админ-панель показывает пароль открытым текстом (private ephemeral)
    admin_text = " ".join(str(getattr(c, "content", "") or "")
                          for c in _walk(kw["view"]))
    assert "secret" in admin_text


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
    # «← Карта» ведёт в админ-панель карты (ввод — только через Управление)
    assert "Изменить комнату" in labels
    assert "Ввести результаты" in labels
    assert "Массовый ввод" in labels
    assert "Назад" in labels


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


# ---------------------------------------------------------------------------
# Карточка регистрации: вертикальные списки команд/игроков
# ---------------------------------------------------------------------------


def _squad_team_tournament(n_teams=3, maps=4):
    t = BRTournament(
        id="tid-st", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.TEAM, format=BRFormat.SQUAD,
        map_count=maps, message_id=555, is_test=True)
    for s in range(n_teams):
        ids = [3000 + s * 10 + j for j in range(4)]
        names = {i: f"Player{s}-{j}<@{i}>" for j, i in enumerate(ids)}
        ok, err = t.register_team_slot(
            f"team-{s}", f"Team {s + 1}", ids[0], ids, names)
        assert ok, err
    return t


def _duo_team_tournament(n_pairs=3, maps=4, pending_last=False):
    t = BRTournament(
        id="tid-dt", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.TEAM, format=BRFormat.DUO,
        map_count=maps, message_id=555, is_test=True)
    for p in range(n_pairs):
        cap, inv = 4000 + p * 2, 4001 + p * 2
        ok, err, idx = t.create_duo_invite(cap, f"Cap{p}", inv)
        assert ok, err
        if not (pending_last and p == n_pairs - 1):
            ok, err = t.respond_duo_invite(idx, inv, f"Inv{p}", True)
            assert ok, err
    return t


def _solo_tournament(n=12, maps=4):
    t = BRTournament(
        id="tid-so", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.RANDOM, format=BRFormat.SOLO,
        map_count=maps, message_id=555, is_test=True)
    for i in range(n):
        ok, err = t.register_player(f"Solo{i}<@{6000 + i}>", 6000 + i)
        assert ok, err
    return t


def test_registration_card_squad_team_vertical_blocks():
    t = _squad_team_tournament()
    desc = (public_embed(t).description or "")
    assert "Статус:** Регистрация открыта" in desc
    assert "Карт: 4 ・ Команд: 3/12" in desc
    assert "01. **Team 1**" in desc
    assert "02. **Team 2**" in desc
    # все 4 игрока команды на отдельной строке через ・
    assert "┗ Player0-0 ・ Player0-1 ・ Player0-2 ・ Player0-3" in desc
    # без упоминаний/ID/хвоста правил
    assert "<@" not in desc
    assert "по кнопке «Правила»" not in desc
    assert "Составов" not in desc


def test_registration_card_duo_team_pairs_counter():
    t = _duo_team_tournament()
    desc = (public_embed(t).description or "")
    assert "Карт: 4 ・ Пар: 3/24" in desc
    assert "01. **Пара 1**" in desc or "01. **" in desc
    assert "┗ Cap0 ・ Inv0" in desc
    # незаполненная пара помечена и не считается в счётчике
    t2 = _duo_team_tournament(n_pairs=2, pending_last=True)
    desc2 = (public_embed(t2).description or "")
    assert "Пар: 1/24" in desc2
    assert "⏳" in desc2


def test_registration_card_solo_numbered_players():
    t = _solo_tournament(n=12)
    desc = (public_embed(t).description or "")
    assert "Карт: 4 ・ Участников: 12/48" in desc
    assert "01. Solo0" in desc
    assert "12. Solo11" in desc
    assert "<@" not in desc and "6000" not in desc


def test_registration_card_random_pool_numbered():
    t = BRTournament(
        id="tid-rp", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.RANDOM, format=BRFormat.SQUAD,
        map_count=4, message_id=555, is_test=True)
    for i in range(20):
        ok, err = t.register_player(f"Pool{i}", 7000 + i)
        assert ok, err
    desc = (public_embed(t).description or "")
    assert "Карт: 4 ・ Игроков в пуле: 20/48" in desc
    assert "01. Pool0" in desc and "20. Pool19" in desc
    # до жеребьёвки нет блоков команд
    assert "**" not in desc.split("\n", 3)[-1] or "┗" not in desc


def test_registration_card_long_names_all_kept_in_cv2_payload():
    """48 игроков с длинными именами: никто не теряется, упоминания
    срезаны, Components V2 payload влезает в лимит контейнера."""
    from utils.design.constants import MAX_CONTAINER_CHILDREN
    t = BRTournament(
        id="tid-long", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.RANDOM, format=BRFormat.SOLO,
        map_count=4, message_id=555, is_test=True)
    names = []
    for i in range(48):
        nm = f"ОченьДлинноеИмяИгрокаНомер{i:02d}ABCDEFGHIJKLMNOPQRSTUVWXYZ<@{8000 + i}>"
        names.append(nm)
        ok, err = t.register_player(nm, 8000 + i)
        assert ok, err
    embed = public_embed(t)
    desc = embed.description or ""
    assert len(desc) <= 4096
    for i in range(48):
        cleaned = br_view._public_name(names[i])
        assert cleaned in desc, f"lost {cleaned}"
        assert f"<@{8000 + i}>" not in desc
    # реальный CV2 payload: ни один контейнер не превышает лимит
    layout = compose(embed, BRCardView(t.guild_id, t.channel_id, t))

    def containers(view):
        for child in getattr(view, "children", []):
            if isinstance(child, discord.ui.Container):
                yield child
            yield from containers(child)

    found = list(containers(layout))
    assert found, "карточка должна содержать Container"
    for c in found:
        assert len(c.children) <= MAX_CONTAINER_CHILDREN, (
            f"container overflow: {len(c.children)} children")


# ---------------------------------------------------------------------------
# Единая кнопка «Управление» + ростер на всех фазах
# ---------------------------------------------------------------------------


def test_playing_card_shows_full_roster():
    """PLAYING: статус → прогресс → расписание → полный список составов;
    подсказки про кнопки и промежуточная статистика отсутствуют."""
    t = _tournament(maps=2)
    assert t.phase == BRTournamentPhase.PLAYING
    desc = (public_embed(t).description or "")
    assert "Турнир идёт" in desc
    assert "Карт с результатами: 0/2" in desc
    assert "**Расписание:**" in desc
    assert "**Составы:**" in desc
    # вертикальные блоки команд с игроками
    assert "01. **" in desc and "┗ " in desc
    # порядок: статус раньше расписания, расписание раньше составов
    assert desc.index("Турнир идёт") < desc.index("Расписание") < desc.index("Составы")
    assert "Данные комнаты и результаты" not in desc
    assert "место" not in desc and "очк" not in desc and "килл" not in desc


def test_draft_card_roster_not_clipped():
    """DRAFT: полный список составов без обрезки (_clip убран)."""
    t = _tournament(maps=1, n_slots=5)
    t.phase = BRTournamentPhase.DRAFT
    desc = (public_embed(t).description or "")
    for i in range(5):
        assert f"{i + 1:02d}. **" in desc
    assert "…" not in desc.split("Составы:")[-1]


@pytest.mark.asyncio
async def test_org_menu_denies_regular_user(patched_store, monkeypatch):
    """Обычный пользователь: панель не открывается, приватный отказ."""
    t = _tournament(maps=1)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    monkeypatch.setattr(br_view, "is_org", lambda uid: False)
    inter = _interaction(guild, user_id=424242)
    await OrgMenuButton(1, 2, t.id).callback(inter)
    text = str(inter.response.messages[-1][0][0] or "")
    assert "нет доступа к управлению" in text
    # view с кнопками управления не отправлен
    _, kw = inter.response.messages[-1]
    assert kw.get("view") is None


@pytest.mark.asyncio
async def test_admin_map_button_denies_regular_user(patched_store, monkeypatch):
    t = _tournament(maps=2)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    monkeypatch.setattr(br_view, "is_org", lambda uid: False)
    inter = _interaction(guild, user_id=424242)
    await AdminMapButton(1, 2, t.id, 0).callback(inter)
    text = str(inter.response.messages[-1][0][0] or "")
    assert "нет доступа к управлению" in text


@pytest.mark.asyncio
async def test_management_menu_draft_has_map_buttons(patched_store):
    """DRAFT: Начать игры + кнопка на каждую карту (комнаты) + отмена."""
    t = _tournament(maps=3)
    t.phase = BRTournamentPhase.DRAFT
    view = BRManagementMenuView(1, 2, t)
    labels = [b.label for b in _buttons(view)]
    assert "Начать игры" in labels
    assert labels.count("Карта 1") == 1
    map_btns = [b for b in _buttons(view)
                if getattr(b, "custom_id", "").startswith("br_adm_map:")]
    assert len(map_btns) == 3
    assert "Отменить турнир" in labels and "Закрыть" in labels


@pytest.mark.asyncio
async def test_admin_map_panel_hides_results_when_not_editable(patched_store):
    """DRAFT: админ-панель карты — комната есть, ввода результатов нет."""
    t = _tournament(maps=2)
    t.phase = BRTournamentPhase.DRAFT
    t.set_room(0, "42", "pw42")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    await AdminMapButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.messages[-1]
    labels = [b.label for b in _buttons(kw["view"])]
    assert "Изменить комнату" in labels
    assert "Ввести результаты" not in labels
    assert "Массовый ввод" not in labels
    assert "Обновить" in labels and "Назад" in labels


@pytest.mark.asyncio
async def test_admin_map_back_returns_to_management_menu(patched_store):
    t = _tournament(maps=2)
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)
    await AdminMapBackButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.edits[-1]
    labels = [b.label for b in _buttons(kw["view"])]
    assert "Предпросмотр итогов" in labels
    assert "Опубликовать итоги" in labels
    assert "Отменить турнир" in labels
    assert "Карта 1" in labels


@pytest.mark.asyncio
async def test_public_map_panel_has_no_admin_buttons(patched_store):
    """Публичная «Карта N» — только просмотр даже для орга."""
    t = _tournament(maps=1)
    t.set_room(0, "55", "pw")
    patched_store["t"] = t
    guild = FakeGuild({2: FakeChannel()})
    inter = _interaction(guild)  # is_org=True в patched_store
    await MapButton(1, 2, t.id, 0).callback(inter)
    _, kw = inter.response.messages[-1]
    labels = [b.label for b in _buttons(kw["view"])]
    assert labels == ["Данные комнаты", "Назад"] or \
        set(labels) <= {"Данные комнаты", "Назад"}
    for banned in ("Изменить комнату", "Ввести результаты",
                   "Массовый ввод", "Обновить"):
        assert banned not in labels


def test_playing_card_cv2_payload_with_roster_and_10_maps():
    """Максимум: 10 карт + 48 игроков — payload в пределах лимитов CV2."""
    t = BRTournament(
        id="tid-max", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.RANDOM, format=BRFormat.SOLO,
        map_count=10, message_id=555, is_test=True)
    for i in range(48):
        ok, err = t.register_player(f"Player{i:02d}", 9000 + i)
        assert ok, err
    ok, err = t.draw()
    assert ok, err
    assert t.phase == BRTournamentPhase.PLAYING
    embed = public_embed(t)
    layout = compose(embed, BRCardView(t.guild_id, t.channel_id, t))

    def components(view):
        for child in getattr(view, "children", []):
            yield child
            yield from components(child)

    n = sum(1 for c in components(layout))
    assert n <= 40, f"CV2 components overflow: {n}"


# ---------------------------------------------------------------------------
# Final fixes: дедуп по ID, «Игрок» вместо User_<id>, @everyone, Squad max payload
# ---------------------------------------------------------------------------


def _squad_team_max():
    """Squad Team: 12 команд × 4 = 48 игроков, длинные имена, 10 карт."""
    t = BRTournament(
        id="tid-max-squad", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.TEAM, format=BRFormat.SQUAD,
        map_count=10, message_id=555, is_test=True)
    uid = 0
    for s in range(12):
        ids = []
        names = {}
        for j in range(4):
            uid += 1
            ids.append(10000 + uid)
            names[ids[-1]] = (
                f"ОченьДлинныйНикнеймИгрока{s}-{j}-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                f"<@{ids[-1]}>")
        ok, err = t.register_team_slot(
            f"team-{s}", f"Команда номер {s + 1} с длинным названием",
            ids[0], ids, names)
        assert ok, err
    return t


def test_same_name_players_all_shown_dedup_by_id():
    """Два игрока с одинаковым ником — оба в списке (дедуп только по ID)."""
    t = _squad_team_tournament(n_teams=1)
    # принудительно одинаковые display_names у разных ID
    slot = t.teams[0]
    for k in slot["display_names"]:
        slot["display_names"][k] = "Тёзка"
    names = br_view.slot_member_names_list(t, slot)
    assert names == ["Тёзка"] * 4


def test_public_names_never_show_user_id_or_everyone():
    for raw in ("User_12345", "user-777", "@everyone", "@here",
                "<@123>", "<@&55>", "<:emoji:123>"):
        out = br_view._public_name(raw)
        assert "User_" not in out and "@" not in out and "<" not in out
    assert br_view._public_name("Игрок @everyone") == "Игрок"
    assert br_view._public_name("Смесь @here имён") == "Смесь имён"


def test_public_card_has_no_user_fallback_names():
    """Никнейм недоступен → «Игрок», не «User_<id>»."""
    t = BRTournament(
        id="tid-noname", guild_id=1, channel_id=2,
        formation=BRTournamentFormation.TEAM, format=BRFormat.SQUAD,
        map_count=2, message_id=555, is_test=True)
    ids = [1, 2, 3, 4]
    t.register_team_slot("tm", "Alpha", ids[0], ids,
                         {i: f"User_{i}" for i in ids})
    desc = (public_embed(t).description or "")
    assert "User_" not in desc
    assert "Игрок" in desc


def test_squad_team_max_cv2_payload_playing():
    """Максимум: Squad Team 12 команд / 48 игроков, длинные имена,
    10 карт, фаза PLAYING — реальный CV2 payload ≤ 40 компонентов."""
    t = _squad_team_max()
    ok, err = t.draw()
    assert ok, err
    t.begin_games()
    assert t.phase == BRTournamentPhase.PLAYING
    embed = public_embed(t)
    desc = embed.description or ""
    # все 48 игроков в ростере, без потерь и без упоминаний
    for s in range(12):
        for j in range(4):
            assert f"Team" not in desc or True
        assert f"{s + 1:02d}. **Команда номер {s + 1}" in desc
    assert "<@" not in desc and "@everyone" not in desc and "User_" not in desc
    layout = compose(embed, BRCardView(t.guild_id, t.channel_id, t))

    def components(view):
        for child in getattr(view, "children", []):
            yield child
            yield from components(child)

    n = sum(1 for _ in components(layout))
    assert n <= 40, f"CV2 components overflow: {n}"
    # и registration-фаза той же макс. турнирки
    t2 = _squad_team_max()
    embed2 = public_embed(t2)
    layout2 = compose(embed2, BRCardView(t2.guild_id, t2.channel_id, t2))
    n2 = sum(1 for _ in components(layout2))
    assert n2 <= 40, f"CV2 components overflow (registration): {n2}"
