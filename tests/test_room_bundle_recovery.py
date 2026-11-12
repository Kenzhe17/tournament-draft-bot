"""Тесты rooms-bundle: полный сценарий и восстановление после
ошибки публикации Discord-сообщения (P0 hotfix).

Проверяем:
- комната → OCR → публикация сообщения с кнопками → дозагрузка →
  завершение комплекта (awaiting_mapping);
- ошибка Discord send → publish_fail без потери статистики;
- повторная загрузка тех же файлов → republish только интерфейса
  (без повторного OCR/статистики, без дублей сообщений);
- отменённая/устаревшая сессия не восстанавливается.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.ocr_upload as up


def _pipe():
    return {
        "panels": {
            "left": {"name": "TeamA", "rows": [{"name": "A1"}]},
            "right": {"name": "TeamB", "rows": [{"name": "B1"}]},
        },
        "score": [4, 3],
        "banner": None,
        "top5": False,
        "winner_side": "left",
    }


class _Chan:
    """Fake stats_channel: send() пишет в sent, может падать."""

    def __init__(self, fail_times: int = 0):
        self.sent: list = []
        self.fail_times = fail_times
        self.guild = SimpleNamespace(id=1)

    async def send(self, content=None, view=None, files=None, **kw):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("discord send failed")
        msg = SimpleNamespace(
            id=1000 + len(self.sent), view=view, content=content
        )
        self.sent.append(msg)
        return msg


@pytest.fixture
def env(monkeypatch):
    """Изолированный tournament+session+store для process_room_bundle."""
    session = {
        "mode": "rooms",
        "status": "collecting",
        "gen": 1,
        "rooms_expected": 2,
        "rooms": [],
        "rooms_received": 0,
    }
    tournament = SimpleNamespace(
        channel_id=1,
        ocr_sessions={"qualifier_0": session},
    )
    guild = SimpleNamespace(id=1)
    channel = SimpleNamespace(guild=guild)

    monkeypatch.setattr(up.store, "get", lambda gid, cid: tournament)
    monkeypatch.setattr(up.store, "set", lambda t: None)

    async def fake_card(*a, **kw):
        return None

    monkeypatch.setattr(up, "_ensure_processing_card", fake_card)

    pipe_calls = []

    async def fake_pipeline(data, need_score=True):
        pipe_calls.append(data)
        return _pipe()

    monkeypatch.setattr(up, "_classic_pipeline", fake_pipeline)

    return SimpleNamespace(
        session=session, tournament=tournament, guild=guild,
        channel=channel, pipe_calls=pipe_calls,
    )


def _items(tag: str):
    return [{"filename": f"{tag}.png", "data": f"img-{tag}".encode()}]


async def _bundle(e, room_index: int, tag: str, stats_channel):
    return await up.process_room_bundle(
        guild=e.guild,
        channel=stats_channel,
        user=SimpleNamespace(id=42),
        tournament=e.tournament,
        match_type="qualifier",
        match_index=0,
        key="qualifier_0",
        session=e.session,
        stats_channel=stats_channel,
        room_index=room_index,
        items=_items(tag),
    )


class TestRoomsFullFlow:
    @pytest.mark.asyncio
    async def test_two_rooms_then_awaiting_mapping(self, env):
        stats = _Chan()
        r1 = await _bundle(env, 1, "r1", stats)
        assert r1 == "ok"
        room1 = env.session["rooms"][0]
        assert room1["panels"]
        assert room1["message_id"] == 1000
        assert env.session["status"] == "collecting"
        assert len(stats.sent) == 1
        # Кнопки сопоставления под сообщением комнаты
        assert stats.sent[0].view is not None
        assert len(stats.sent[0].view.children) >= 2

        r2 = await _bundle(env, 2, "r2", stats)
        assert r2 == "ok"
        # Комплект полный → awaiting_mapping
        assert env.session["status"] == "awaiting_mapping"
        assert env.session["rooms_received"] == 2
        assert len(stats.sent) == 2
        assert len(env.pipe_calls) == 2

    @pytest.mark.asyncio
    async def test_room_done_no_republish(self, env):
        stats = _Chan()
        await _bundle(env, 1, "r1", stats)
        n_sent = len(stats.sent)
        n_pipes = len(env.pipe_calls)
        r = await _bundle(env, 1, "r1b", stats)
        assert r == "room_done"
        assert len(stats.sent) == n_sent
        assert len(env.pipe_calls) == n_pipes


class TestPublishFailureRecovery:
    @pytest.mark.asyncio
    async def test_send_error_publish_fail_keeps_stats(self, env):
        stats = _Chan(fail_times=10)
        r = await _bundle(env, 1, "r1", stats)
        assert r == "publish_fail"
        room = env.session["rooms"][0]
        # Статистика сохранена, сообщение не опубликовано
        assert room["panels"]
        assert room["message_id"] is None
        assert env.session["rooms_received"] == 1

    @pytest.mark.asyncio
    async def test_reupload_recovers_interface_only(self, env):
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        n_pipes = len(env.pipe_calls)

        # Discord «починился» — те же файлы → republish без OCR
        stats.fail_times = 0
        r = await _bundle(env, 1, "r1", stats)
        assert r == "ok"
        room = env.session["rooms"][0]
        assert room["message_id"] == 1000
        assert len(stats.sent) == 1
        assert stats.sent[0].view is not None
        # OCR НЕ запускался повторно, статистика не дублировалась
        assert len(env.pipe_calls) == n_pipes
        assert env.session["rooms_received"] == 1

    @pytest.mark.asyncio
    async def test_republish_skips_cancelled_session(self, env):
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        env.session["status"] = "cancelled"
        stats.fail_times = 0
        r = await _bundle(env, 1, "r1", stats)
        assert r == "cancelled"
        assert len(stats.sent) == 0

    @pytest.mark.asyncio
    async def test_republish_skips_stale_gen(self, env):
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        # Сессия пересоздана: в store — новая gen, переданная — старая
        env.tournament.ocr_sessions["qualifier_0"] = {
            "mode": "rooms", "status": "collecting", "gen": 2,
            "rooms_expected": 2, "rooms": [], "rooms_received": 0,
        }
        stats.fail_times = 0
        r = await _bundle(env, 1, "r1", stats)
        assert r == "cancelled"
        assert len(stats.sent) == 0

    @pytest.mark.asyncio
    async def test_second_room_completes_after_recovery(self, env):
        """Полный сценарий: комната 1 publish_fail → recover →
        комната 2 → комплект завершён."""
        stats = _Chan(fail_times=2)  # обе попытки publish падают
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"

        stats.fail_times = 0
        assert await _bundle(env, 1, "r1", stats) == "ok"
        assert await _bundle(env, 2, "r2", stats) == "ok"
        assert env.session["status"] == "awaiting_mapping"
        assert env.session["rooms_received"] == 2
        assert len(stats.sent) == 2  # ровно по сообщению на комнату


# ---------------------------------------------------------------------------
# UI e2e: RoomCollectView._on_add → RoomUploadModal.on_submit
# ---------------------------------------------------------------------------


class _RespUI:
    def __init__(self) -> None:
        self.sent: list = []
        self.modal = None
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kwargs):
        self.deferred = True


class _FollowupUI:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class _IXUI:
    def __init__(self, user_id: int = 42):
        self.guild = SimpleNamespace(id=1)
        self.guild_id = 1
        fake_guild = SimpleNamespace(roles=[], get_role=lambda i: None)
        self.user = SimpleNamespace(
            id=user_id, display_name="u", name="u",
            guild=fake_guild,
            roles=[],
            guild_permissions=SimpleNamespace(administrator=True),
        )
        self.channel = SimpleNamespace(id=9)
        self.response = _RespUI()
        self.followup = _FollowupUI()

    async def edit_original_response(self, **kwargs):
        pass


def _fake_file(name: str, data: bytes):
    async def _read():
        return data
    return SimpleNamespace(filename=name, read=_read)


def _modal_for(env, room_index: int, files: list, gen: int = 1):
    """RoomUploadModal с подменёнными file values (без Discord)."""
    t = env.tournament
    modal = up.RoomUploadModal(1, 1, t, "qualifier", 0, room_index, gen)
    modal.upload = SimpleNamespace(
        component=SimpleNamespace(values=files)
    )
    return modal


class TestRoomRecoveryUI:
    @pytest.mark.asyncio
    async def test_button_prioritizes_republish_room(self, env, monkeypatch):
        """publish_fail комнаты 1 → «Добавить скриншоты» открывает
        модалку именно для неё, а не для следующей пустой."""
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"

        view = up.RoomCollectView(1, 1, env.tournament,
                                  "qualifier", 0, 1)
        ix = _IXUI(user_id=42)
        await view._on_add(ix)
        assert ix.response.modal is not None
        assert ix.response.modal.room_index == 1

    @pytest.mark.asyncio
    async def test_button_republish_in_awaiting_mapping(self, env):
        """Последняя комната publish_fail → сессия уже
        awaiting_mapping — кнопка всё равно предлагает восстановление."""
        env.session["rooms_expected"] = 1
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        assert env.session["status"] == "awaiting_mapping"

        view = up.RoomCollectView(1, 1, env.tournament,
                                  "qualifier", 0, 1)
        ix = _IXUI(user_id=42)
        await view._on_add(ix)
        assert ix.response.modal is not None
        assert ix.response.modal.room_index == 1

    @pytest.mark.asyncio
    async def test_button_full_awaiting_mapping_no_modal(self, env):
        """awaiting_mapping без проблемных комнат → подсказка,
        модалка не открывается."""
        env.session["rooms_expected"] = 1
        stats = _Chan()
        assert await _bundle(env, 1, "r1", stats) == "ok"
        assert env.session["status"] == "awaiting_mapping"

        view = up.RoomCollectView(1, 1, env.tournament,
                                  "qualifier", 0, 1)
        ix = _IXUI(user_id=42)
        await view._on_add(ix)
        assert ix.response.modal is None
        assert ix.response.sent

    @pytest.mark.asyncio
    async def test_modal_republish_same_file_no_ocr(self, env, monkeypatch):
        """Модалка с теми же байтами → republish, pipeline не зовётся."""
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        n_pipes = len(env.pipe_calls)
        stats.fail_times = 0

        async def fake_stats_channel(interaction, tournament):
            return stats
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        modal = _modal_for(env, 1, [_fake_file("r1.png", b"img-r1")])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)

        room = env.session["rooms"][0]
        assert room["message_id"] == 1000
        assert len(stats.sent) == 1
        assert len(env.pipe_calls) == n_pipes  # повторного OCR нет
        assert any("обработана" in (m or "") for m in ix.followup.sent)

    @pytest.mark.asyncio
    async def test_modal_republish_wrong_file_rejected(self, env, monkeypatch):
        """Другой файл → sha_mismatch, статистика и сообщения не тронуты."""
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"

        async def fake_stats_channel(interaction, tournament):
            return stats
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        modal = _modal_for(env, 1, [_fake_file("other.png", b"DIFFERENT")])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)

        room = env.session["rooms"][0]
        assert room["message_id"] is None
        assert len(stats.sent) == 0
        assert any("не совпадают" in (m or "") for m in ix.followup.sent)

    @pytest.mark.asyncio
    async def test_modal_republish_two_squad_files(self, env, monkeypatch):
        """Комната из двух squad-скринов: republish принимает оба
        файла в любом порядке, чужой — отклоняет."""
        sa = up._sha256_bytes(b"squad-a")
        sb = up._sha256_bytes(b"squad-b")
        env.session["rooms"] = [{
            "room_index": 1, "sha": sa, "shas": [sa, sb],
            "kind": "squad", "status": "awaiting_mapping",
            "panels": {"squad": {"name": "Отряд", "rows": []}},
            "banners": {}, "maps": {}, "panel_picks": {},
            "winner_side": None, "banner": None, "score": None,
            "top5": False, "message_id": None, "message_ids": [],
            "uploaded_at": "2026-01-01T00:00:00",
        }]
        stats = _Chan()

        async def fake_stats_channel(interaction, tournament):
            return stats
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        # Файлы в обратном порядке — sha-мультимножество совпадает
        modal = _modal_for(env, 1, [
            _fake_file("b.png", b"squad-b"),
            _fake_file("a.png", b"squad-a"),
        ])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)
        room = env.session["rooms"][0]
        assert room["message_id"] is not None
        assert len(stats.sent) == 1
        assert len(env.pipe_calls) == 0  # OCR не запускался

    @pytest.mark.asyncio
    async def test_modal_cancelled_session_rejected(self, env, monkeypatch):
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        env.session["status"] = "cancelled"

        async def fake_stats_channel(interaction, tournament):
            return stats
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        modal = _modal_for(env, 1, [_fake_file("r1.png", b"img-r1")])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)
        room = env.session["rooms"][0]
        assert room["message_id"] is None
        assert any("отменена" in (m or "") or "устарела" in (m or "")
                   for m in ix.followup.sent)

    @pytest.mark.asyncio
    async def test_button_cancelled_session_no_modal(self, env):
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        env.session["status"] = "cancelled"

        view = up.RoomCollectView(1, 1, env.tournament,
                                  "qualifier", 0, 1)
        ix = _IXUI(user_id=42)
        await view._on_add(ix)
        assert ix.response.modal is None
        assert ix.response.sent

    @pytest.mark.asyncio
    async def test_republish_adopts_existing_message(self, env):
        """Идемпотентность: send прошёл, но message_id не сохранился —
        republish находит существующее сообщение по custom_id кнопок
        и НЕ создаёт дубль."""
        stats = _Chan(fail_times=10)
        assert await _bundle(env, 1, "r1", stats) == "publish_fail"
        room = env.session["rooms"][0]
        assert room["message_id"] is None

        # В канале уже есть сообщение этой комнаты (имитация:
        # Discord send успешен, локальный store.set упал).
        msg = SimpleNamespace(
            id=555,
            components=[SimpleNamespace(children=[SimpleNamespace(
                custom_id="room_map:1:1:qualifier:0:1:0",
            )])],
        )

        async def hist(limit=100):
            yield msg
        stats.history = hist
        stats.fail_times = 0

        r = await _bundle(env, 1, "r1", stats)
        assert r == "ok"
        assert room["message_id"] == 555
        assert room["message_ids"] == [555]
        assert len(stats.sent) == 0  # дубль не отправлен
        assert len(env.pipe_calls) == 1  # OCR не повторялся


# ---------------------------------------------------------------------------
# Смешанная загрузка комнаты: classic + squad
# ---------------------------------------------------------------------------


def _classic_pipe(rows_a, rows_b, team_a="TA", team_b="TB"):
    return {
        "panels": {
            "left": {"name": team_a, "rows": rows_a},
            "right": {"name": team_b, "rows": rows_b},
        },
        "score": None,
        "banner": None,
        "top5": False,
        "winner_side": "left",
    }


def _squad_pipe(players, banner="win"):
    return {"squad": {"players": players, "banner": banner}}


def _prow(name, k=1, d=1, a=1, dmg=100):
    return {"name": name, "kills": k, "deaths": d, "assists": a,
            "dmg": dmg}


@pytest.fixture
def mixed_env(monkeypatch):
    """env + pipeline, диспетчеризующий classic/squad по содержимому."""
    session = {
        "mode": "rooms",
        "status": "collecting",
        "gen": 1,
        "rooms_expected": 2,
        "rooms": [],
        "rooms_received": 0,
        "winner_pending": True,
        "winning_team_index": None,
        "uploaded_by": 7,
        "uploaded_by_name": "creator",
        "teams": [0, 1],
        "maps": {},
        "panel_picks": {},
    }
    tournament = SimpleNamespace(
        channel_id=1,
        ocr_sessions={"qualifier_0": session},
        teams=[{"players": []}, {"players": []}],
        player_user_ids={},
        mm_search_channel_id=None,
    )
    guild = SimpleNamespace(id=1)
    channel = SimpleNamespace(guild=guild)

    monkeypatch.setattr(up.store, "get", lambda gid, cid: tournament)
    monkeypatch.setattr(up.store, "set", lambda t: None)

    card_calls = []

    async def fake_card(*a, **kw):
        card_calls.append(a)

    monkeypatch.setattr(up, "_ensure_processing_card", fake_card)

    pipes = []
    pipe_calls = []

    async def fake_pipeline(data, need_score=True):
        pipe_calls.append(data)
        pipe = pipes.pop(0) if pipes else _pipe()
        return pipe

    monkeypatch.setattr(up, "_classic_pipeline", fake_pipeline)

    return SimpleNamespace(
        session=session, tournament=tournament, guild=guild,
        channel=channel, pipe_calls=pipe_calls, pipes=pipes,
        card_calls=card_calls,
    )


def _mixed_items():
    return [
        {"filename": "full.png", "data": b"FULL-SCREEN"},
        {"filename": "squad.png", "data": b"SQUAD-SHOT"},
    ]


async def _bundle_items(e, room_index: int, items, stats_channel):
    return await up.process_room_bundle(
        guild=e.guild,
        channel=stats_channel,
        user=SimpleNamespace(id=42),
        tournament=e.tournament,
        match_type="qualifier",
        match_index=0,
        key="qualifier_0",
        session=e.session,
        stats_channel=stats_channel,
        room_index=room_index,
        items=items,
    )


class TestMixedRoomBundle:
    @pytest.mark.asyncio
    async def test_classic_plus_squad_accepted(self, mixed_env):
        """classic + squad: panels — только от classic, squad уходит
        в aux_squads, оба sha сохранены, статистика не дублируется."""
        e = mixed_env
        rows_a = [_prow("A1", dmg=500), _prow("A2")]
        rows_b = [_prow("B1"), _prow("B2")]
        e.pipes.extend([
            _classic_pipe(rows_a, rows_b),
            _squad_pipe([_prow("A1", dmg=500)]),
        ])
        stats = _Chan()
        r = await _bundle_items(e, 1, _mixed_items(), stats)
        assert r == "ok"

        room = e.session["rooms"][0]
        assert room["kind"] == "mixed"
        # Только classic-панели: squad НЕ добавляет вторую команду
        assert set(room["panels"]) == {"left", "right"}
        assert len(room["panels"]["left"]["rows"]) == 2
        # SHA обоих файлов сохранены
        assert room["sha"] == up._sha256_bytes(b"FULL-SCREEN")
        assert up._sha256_bytes(b"SQUAD-SHOT") in room["shas"]
        assert len(room["aux_squads"]) == 1
        assert room["aux_squads"][0]["players"][0]["name"] == "A1"
        assert e.session["rooms_received"] == 1
        assert len(stats.sent) == 1
        # Карточка обработки — только на успешном пути
        assert len(e.card_calls) == 1

    @pytest.mark.asyncio
    async def test_squad_plus_classic_order(self, mixed_env):
        """squad + classic (обратный порядок) — тот же результат."""
        e = mixed_env
        e.pipes.extend([
            _squad_pipe([_prow("B1", dmg=50)]),
            _classic_pipe([_prow("A1")], [_prow("B1", dmg=50)]),
        ])
        items = list(reversed(_mixed_items()))
        stats = _Chan()
        r = await _bundle_items(e, 1, items, stats)
        assert r == "ok"
        room = e.session["rooms"][0]
        assert room["kind"] == "mixed"
        assert room["sha"] == up._sha256_bytes(b"FULL-SCREEN")
        assert up._sha256_bytes(b"SQUAD-SHOT") in room["shas"]

    @pytest.mark.asyncio
    async def test_squad_fills_missing_classic_stats(self, mixed_env):
        """None-поле classic-строки заполняется из squad при
        уникальном совпадении имени — без суммирования."""
        e = mixed_env
        e.pipes.extend([
            _classic_pipe(
                [_prow("UniqName", dmg=None), _prow("A2")],
                [_prow("B1")],
            ),
            _squad_pipe([_prow("Uniq Name!", dmg=777)]),
        ])
        stats = _Chan()
        assert await _bundle_items(e, 1, _mixed_items(), stats) == "ok"
        row = e.session["rooms"][0]["panels"]["left"]["rows"][0]
        assert row["dmg"] == 777  # пробел/пунктуация не мешают match
        room = e.session["rooms"][0]
        assert room.get("aux_filled")
        assert not room.get("stat_conflicts")

    @pytest.mark.asyncio
    async def test_squad_conflict_no_autosum(self, mixed_env):
        """Расхождение значений: classic сохраняется, конфликт
        фиксируется для этапа проверки итогов — без автосуммы."""
        e = mixed_env
        e.pipes.extend([
            _classic_pipe([_prow("A1", dmg=100)], [_prow("B1")]),
            _squad_pipe([_prow("a1", dmg=200)]),
        ])
        stats = _Chan()
        assert await _bundle_items(e, 1, _mixed_items(), stats) == "ok"
        room = e.session["rooms"][0]
        row = room["panels"]["left"]["rows"][0]
        assert row["dmg"] == 100  # НЕ 300 и НЕ 200
        assert row["stat_conflict"] is True
        assert room["stat_conflicts"] == [
            {"side": "left", "row": 0, "name": "A1", "field": "dmg",
             "classic": 100, "squad": 200, "resolved": False}
        ]
        # Предупреждение в сообщении комнаты
        assert "расходится" in stats.sent[0].content

    @pytest.mark.asyncio
    async def test_two_classics_still_invalid_mix(self, mixed_env):
        """classic + classic — по-прежнему invalid_mix; пустая
        комната удаляется, карточка обработки НЕ трогается."""
        e = mixed_env
        e.pipes.extend([_pipe(), _pipe()])
        items = [
            {"filename": "a.png", "data": b"FULL-A"},
            {"filename": "b.png", "data": b"FULL-B"},
        ]
        stats = _Chan()
        r = await _bundle_items(e, 1, items, stats)
        assert r == "invalid_mix"
        assert e.session["rooms"] == []
        assert e.card_calls == []  # processing card не включалась
        assert len(stats.sent) == 0

    @pytest.mark.asyncio
    async def test_mixed_republish_sha_any_order(self, mixed_env):
        """publish_fail смешанной комнаты → republish тех же двух
        файлов в любом порядке без повторного OCR."""
        e = mixed_env
        e.pipes.extend([
            _classic_pipe([_prow("A1")], [_prow("B1")]),
            _squad_pipe([_prow("A1")]),
        ])
        stats = _Chan(fail_times=10)
        assert await _bundle_items(e, 1, _mixed_items(), stats) == "publish_fail"
        n_pipes = len(e.pipe_calls)
        room = e.session["rooms"][0]
        assert room["message_id"] is None

        stats.fail_times = 0
        items = list(reversed(_mixed_items()))
        r = await _bundle_items(e, 1, items, stats)
        assert r == "ok"
        assert room["message_id"] is not None
        assert len(e.pipe_calls) == n_pipes  # OCR не повторялся
        assert len(stats.sent) == 1

    @pytest.mark.asyncio
    async def test_mixed_reupload_room_done(self, mixed_env):
        """Повторная загрузка заполненной смешанной комнаты —
        room_done, без OCR и без дублей."""
        e = mixed_env
        e.pipes.extend([
            _classic_pipe([_prow("A1")], [_prow("B1")]),
            _squad_pipe([_prow("A1")]),
        ])
        stats = _Chan()
        assert await _bundle_items(e, 1, _mixed_items(), stats) == "ok"
        n_pipes = len(e.pipe_calls)
        r = await _bundle_items(e, 1, _mixed_items(), stats)
        assert r in ("room_done", "dup")
        assert len(e.pipe_calls) == n_pipes
        assert e.session["rooms_received"] == 1


class TestBundleClaimRelease:
    @pytest.mark.asyncio
    async def test_rejected_first_file_releases_claim(
        self, mixed_env, monkeypatch
    ):
        """Первый файл отклонён (parse_fail) → claim откатывается:
        winner_pending/команда-победитель/uploaded_by восстанавливаются,
        пустая комната удаляется, карточка не блокируется."""
        e = mixed_env
        e.pipes.append(None)  # pipeline → parse_fail

        async def fake_stats_channel(interaction, tournament):
            return _Chan()
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        modal = _modal_for(e, 1, [_fake_file("bad.png", b"NOT-A-RESULT")])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)

        s = e.session
        assert s["winner_pending"] is True
        assert s["winning_team_index"] is None
        assert s["uploaded_by"] == 7  # creator, не отправитель файла
        assert s["rooms"] == []
        assert s["rooms_received"] == 0
        assert any("Не удалось распознать" in (m or "")
                   for m in ix.followup.sent)

    @pytest.mark.asyncio
    async def test_accepted_first_file_keeps_claim(self, mixed_env, monkeypatch):
        """Принятый первый файл — claim сохраняется."""
        e = mixed_env
        e.pipes.append(_pipe())

        async def fake_stats_channel(interaction, tournament):
            return _Chan()
        monkeypatch.setattr(up, "_stats_channel", fake_stats_channel)

        modal = _modal_for(e, 1, [_fake_file("ok.png", b"img-ok")])
        ix = _IXUI(user_id=42)
        await modal.on_submit(ix)

        s = e.session
        assert s["winner_pending"] is False
        assert s["uploaded_by"] == 42
        assert len(s["rooms"]) == 1
        assert s["rooms"][0]["panels"]
