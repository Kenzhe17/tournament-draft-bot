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
        msg = SimpleNamespace(id=1000 + len(self.sent), view=view)
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
