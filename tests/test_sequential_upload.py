"""V5 sequential upload: один скриншот за раз, комната
финализируется кнопкой «Завершить комнату».

Проверяем:
- classic → finish; classic+squad / squad+classic (mixed);
  squad+squad; classic+classic → второй отклонён;
- parse_fail второго файла — первый сохранён;
- «перезапуск» (очистка памяти) — байты из Discord shot-сообщения;
- дедуп sha, двойной finish, параллельные загрузки;
- mixed-конфликт после finalize; 2 и 3 комнаты; republish по sha.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.ocr_upload as up


def _classic_pipe(name="T"):
    return {
        "panels": {
            "left": {"name": f"{name}A", "rows": [{"name": "A1", "kills": 5}]},
            "right": {"name": f"{name}B", "rows": [{"name": "B1", "kills": 3}]},
        },
        "score": [4, 3],
        "banner": None,
        "top5": False,
        "winner_side": "left",
    }


def _squad_pipe(tag="S"):
    return {
        "squad": {
            "players": [
                {"name": f"{tag}1", "kills": 1, "deaths": 1,
                 "assists": 1, "dmg": 100},
            ],
            "banner": None,
        }
    }


class _Att:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class _Chan:
    """Fake stats_channel: send/fetch/history для seq-тестов."""

    def __init__(self):
        self.sent: list = []
        self.guild = SimpleNamespace(id=1)
        self._by_id: dict[int, SimpleNamespace] = {}

    async def send(self, content=None, view=None, files=None, **kw):
        msg = SimpleNamespace(
            id=1000 + len(self.sent), view=view, content=content,
            attachments=[],
        )
        # Сохраняем байты файлов в «attachments» сообщения.
        for f in files or []:
            data = getattr(f, "fp", None)
            raw = data.read() if data is not None else b""
            msg.attachments.append(_Att(raw))
        self.sent.append(msg)
        self._by_id[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        return self._by_id[int(mid)]

    async def history(self, limit=100):
        for m in reversed(self.sent):
            yield m


@pytest.fixture
def env(monkeypatch):
    session = {
        "mode": "rooms",
        "status": "collecting",
        "gen": 1,
        "rooms_expected": 1,
        "rooms": [],
        "rooms_received": 0,
        "progress_message_id": None,
        "progress_channel_id": 1,
    }
    tournament = SimpleNamespace(
        channel_id=1,
        guild_id=1,
        ocr_sessions={"qualifier_0": session},
        player_user_ids={},
        match_channels={},
    )
    guild = SimpleNamespace(id=1)
    stats = _Chan()

    monkeypatch.setattr(up.store, "get", lambda gid, cid: tournament)
    monkeypatch.setattr(up.store, "set", lambda t: None)
    monkeypatch.setattr(up, "_ensure_processing_card", lambda *a, **k: _noop())
    monkeypatch.setattr(up, "_rooms_file_submit_allowed", lambda *a, **k: True)
    up._SEQ_FILE_CACHE.clear()
    up._REPUBLISH_FILE_CACHE.clear()

    pipe_calls = []

    async def fake_pipeline(data, need_score=True):
        pipe_calls.append(data)
        tag = data.decode()
        if tag.startswith("FAIL"):
            return None
        if tag.startswith("squad"):
            return _squad_pipe(tag)
        return _classic_pipe(tag)

    monkeypatch.setattr(up, "_classic_pipeline", fake_pipeline)

    return SimpleNamespace(
        session=session, tournament=tournament, guild=guild,
        stats=stats, pipe_calls=pipe_calls,
    )


async def _noop():
    return None


def _item(tag: str):
    return {"filename": f"{tag}.png", "data": tag.encode()}


async def _file(e, room_index: int, tag: str):
    return await up.process_room_file(
        guild=e.guild, channel=e.stats, user=SimpleNamespace(id=42),
        tournament=e.tournament, match_type="qualifier",
        match_index=0, key="qualifier_0", session=e.session,
        stats_channel=e.stats, room_index=room_index, item=_item(tag),
    )


async def _finish(e, room_index: int = 1):
    return await up.finalize_room_collect(
        e.guild, SimpleNamespace(id=42), e.tournament,
        "qualifier", 0, room_index, stats_channel=e.stats,
    )


class TestSequentialBasics:
    @pytest.mark.asyncio
    async def test_classic_then_finish(self, env):
        assert await _file(env, 1, "classic1") == "ok"
        room = env.session["rooms"][0]
        assert room["status"] == "collecting_files"
        assert len(room["pending"]) == 1
        # rooms_received не растёт до завершения комнаты
        assert env.session["rooms_received"] == 0
        assert await _finish(env) == "ok"
        assert room["panels"]
        assert room["status"] == "awaiting_mapping"
        assert env.session["rooms_received"] == 1
        assert env.session["status"] == "awaiting_mapping"

    @pytest.mark.asyncio
    async def test_classic_plus_squad_mixed(self, env):
        await _file(env, 1, "classic1")
        assert await _file(env, 1, "squadA") == "added"
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room["kind"] == "mixed"
        assert room["panels"]["left"]["rows"]
        assert room["aux_squads"]

    @pytest.mark.asyncio
    async def test_squad_then_classic_mixed(self, env):
        assert await _file(env, 1, "squadA") == "ok"
        assert await _file(env, 1, "classic1") == "added"
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room["kind"] == "mixed"

    @pytest.mark.asyncio
    async def test_squad_plus_squad(self, env):
        await _file(env, 1, "squadA")
        assert await _file(env, 1, "squadB") == "added"
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room["kind"] == "squad"
        assert "squad" in room["panels"]
        assert "squad2" in room["panels"]

    @pytest.mark.asyncio
    async def test_classic_plus_classic_rejected(self, env):
        await _file(env, 1, "classic1")
        assert await _file(env, 1, "classic2") == "invalid_mix"
        room = env.session["rooms"][0]
        # Первый файл сохранён, второй отклонён
        assert len(room["pending"]) == 1
        assert room["pending"][0]["kind"] == "classic"

    @pytest.mark.asyncio
    async def test_third_file_rejected(self, env):
        await _file(env, 1, "squadA")
        await _file(env, 1, "squadB")
        assert await _file(env, 1, "squadC") == "room_full"
        assert len(env.session["rooms"][0]["pending"]) == 2

    @pytest.mark.asyncio
    async def test_parse_fail_keeps_first(self, env):
        assert await _file(env, 1, "classic1") == "ok"
        assert await _file(env, 1, "FAILx") == "parse_fail"
        room = env.session["rooms"][0]
        assert len(room["pending"]) == 1
        # Повторная загрузка после ошибки работает
        assert await _file(env, 1, "squadA") == "added"
        assert await _finish(env) == "ok"

    @pytest.mark.asyncio
    async def test_first_parse_fail_drops_room(self, env):
        assert await _file(env, 1, "FAILx") == "parse_fail"
        # Пустая комната убрана — следующий индекс снова 1
        assert not any(
            r.get("pending") for r in env.session["rooms"]
        )


class TestSequentialSafety:
    @pytest.mark.asyncio
    async def test_dup_sha_rejected(self, env):
        await _file(env, 1, "classic1")
        assert await _file(env, 1, "classic1") == "dup"

    @pytest.mark.asyncio
    async def test_double_finish_idempotent(self, env):
        env.session["rooms_expected"] = 2
        await _file(env, 1, "classic1")
        assert await _finish(env) == "ok"
        assert await _finish(env) == "done"
        assert env.session["rooms_received"] == 1
        # panels не дублируются
        assert list(env.session["rooms"][0]["panels"]) == ["left", "right"]

    @pytest.mark.asyncio
    async def test_parallel_second_files(self, env):
        """Два пользователя почти одновременно шлют второй файл:
        _room_lock сериализует — максимум 2 pending, без смеси."""
        await _file(env, 1, "classic1")
        r1, r2 = await asyncio.gather(
            _file(env, 1, "squadA"),
            _file(env, 1, "squadB"),
        )
        assert sorted([r1, r2]) == ["added", "room_full"]
        assert len(env.session["rooms"][0]["pending"]) == 2

    @pytest.mark.asyncio
    async def test_restart_bytes_from_discord(self, env):
        """«Перезапуск»: память пуста — байты читаются из shot-
        сообщения Discord, комната финализируется."""
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        up._SEQ_FILE_CACHE.clear()
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room["kind"] == "mixed"
        # Room message опубликовано с файлами (2 вложения)
        room_msgs = [
            m for m in env.stats.sent
            if m.view is not None and getattr(m.view, "children", None)
        ]
        assert room_msgs

    @pytest.mark.asyncio
    async def test_mixed_conflict_recorded(self, env, monkeypatch):
        """Classic DMG=100 vs Squad DMG=999 → stat_conflicts,
        блокировка готовности до выбора админом."""
        squad = _squad_pipe("X")
        squad["squad"]["players"][0]["name"] = "A1"
        squad["squad"]["players"][0]["dmg"] = 999

        async def pipe2(data, need_score=True):
            env.pipe_calls.append(data)
            tag = data.decode()
            if tag.startswith("squad"):
                return squad
            return _classic_pipe(tag)

        # Классик с A1 dmg=100 в обеих панелях? _classic_pipe даёт
        # только kills — добавим dmg к строке.
        cp = _classic_pipe("c")
        cp["panels"]["left"]["rows"][0]["dmg"] = 100
        cp["panels"]["left"]["rows"][0]["name"] = "A1"

        async def pipe3(data, need_score=True):
            env.pipe_calls.append(data)
            tag = data.decode()
            if tag.startswith("squad"):
                return squad
            return cp

        monkeypatch.setattr(up, "_classic_pipeline", pipe3)
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room.get("stat_conflicts")


class TestSequentialMultiRoom:
    @pytest.mark.asyncio
    async def test_two_rooms_full_match(self, env):
        env.session["rooms_expected"] = 2
        for ridx, tag in ((1, "r1"), (2, "r2")):
            assert await _file(env, ridx, f"classic{tag}") == "ok"
            assert await _finish(env, ridx) == "ok"
        assert env.session["status"] == "awaiting_mapping"
        assert env.session["rooms_received"] == 2

    @pytest.mark.asyncio
    async def test_three_rooms_full_match(self, env):
        env.session["rooms_expected"] = 3
        for ridx in (1, 2, 3):
            assert await _file(env, ridx, f"classic{ridx}") == "ok"
            assert await _finish(env, ridx) == "ok"
        assert env.session["status"] == "awaiting_mapping"
        assert env.session["rooms_received"] == 3

    @pytest.mark.asyncio
    async def test_unfinished_room_blocks_completion(self, env):
        env.session["rooms_expected"] = 2
        await _file(env, 1, "classic1")
        await _finish(env, 1)
        # Комната 2 не завершена → сессия не awaiting_mapping
        assert env.session["status"] == "collecting"
        assert env.session["rooms_received"] == 1


class TestSequentialRepublish:
    @pytest.mark.asyncio
    async def test_republish_same_shas_no_ocr(self, env):
        """После finalize теряем message_id → republish по тем же
        sha без повторного OCR."""
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        calls_before = len(env.pipe_calls)
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert room["message_id"]

        # Имитируем publish_fail-потерю: message_id пропал,
        # статус awaiting_mapping — republish-ветка.
        env.session["status"] = "awaiting_mapping"
        room["message_id"] = None
        room["message_ids"] = []

        # Файлы по одному — накапливаются по sha.
        r1 = await _file(env, 1, "classic1")
        assert r1 == "republish_partial"
        r2 = await _file(env, 1, "squadA")
        assert r2 == "republished"
        # OCR не запускался повторно
        assert len(env.pipe_calls) == calls_before
        assert room["message_id"]
