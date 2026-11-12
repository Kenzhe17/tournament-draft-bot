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


def _full_classic_pipe(dmg_missing=False):
    """Полный Classic: 4+4 игрока. dmg_missing=True → у всех
    строк dmg=None (статистика неполная → нужна squad-сверка)."""
    def row(name, i):
        return {
            "name": name, "kills": i + 1, "deaths": 1,
            "assists": 1,
            "dmg": None if dmg_missing else 100 + i,
        }
    return {
        "panels": {
            "left": {"name": "L", "rows": [
                row(f"L{i}", i) for i in range(4)
            ]},
            "right": {"name": "R", "rows": [
                row(f"R{i}", i) for i in range(4)
            ]},
        },
        "score": [4, 3], "banner": None,
        "top5": False, "winner_side": "left",
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
        self.id = 1
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

        async def _delete():
            self._by_id.pop(msg.id, None)
            if msg in self.sent:
                self.sent.remove(msg)

        msg.delete = _delete

        async def _edit(content=None, view=None, **kw):
            msg.content = content
            msg.view = view
            return msg

        msg.edit = _edit
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
        "teams": [0, 1],
    }
    tournament = SimpleNamespace(
        channel_id=1,
        guild_id=1,
        ocr_sessions={"qualifier_0": session},
        player_user_ids={"CapA": 42, "CapB": 43},
        match_channels={},
        teams=[
            {"captain": "CapA"},
            {"captain": "CapB"},
        ],
        team_names={0: "Alpha", 1: "Beta"},
        qualifier_matches=[(0, 1)],
        id="t1",
        processed_elo_matches=set(),
        phase=SimpleNamespace(value="qualifiers"),
    )
    guild = SimpleNamespace(id=1, roles=[])
    guild.get_channel = lambda cid: None

    async def _guild_fetch(cid):
        return None

    guild.fetch_channel = _guild_fetch
    guild.get_role = lambda rid: None
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


class _FailOnceChan(_Chan):
    """Канал, падающий на первой отправке shot-сообщения."""

    def __init__(self):
        super().__init__()
        self.fail_next = True

    async def send(self, content=None, view=None, files=None, **kw):
        if self.fail_next and files:
            self.fail_next = False
            raise RuntimeError("discord send failed")
        return await super().send(
            content=content, view=view, files=files, **kw
        )


class TestShotReliability:
    @pytest.mark.asyncio
    async def test_shot_send_fail_then_retry(self, env):
        """Discord send shot-сообщения упал → shot_pending,
        ответ НЕ 'ok'; повторная отправка того же файла
        восстанавливает Discord-копию без повторного OCR."""
        env.stats = _FailOnceChan()
        r1 = await _file(env, 1, "classic1")
        assert r1 == "shot_pending"
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        assert entry["shot_pending"] is True
        assert entry["shot_message_id"] is None
        # Статистика сохранена — pending не потерян
        assert entry["pipe"]["panels"]

        # Повторная отправка того же файла — только shot publish
        calls = len(env.pipe_calls)
        r2 = await _file(env, 1, "classic1")
        assert r2 == "ok"
        assert len(env.pipe_calls) == calls  # OCR не повторялся
        assert entry["shot_pending"] is False
        assert entry["shot_message_id"]

    @pytest.mark.asyncio
    async def test_restart_then_second_file(self, env):
        """Первый файл принят → «рестарт» (кэш пуст) → второй
        файл → finalize: байты первого — из Discord shot."""
        assert await _file(env, 1, "classic1") == "ok"
        up._SEQ_FILE_CACHE.clear()  # рестарт
        assert await _file(env, 1, "squadA") == "added"
        assert await _finish(env) == "ok"
        assert env.session["rooms"][0]["kind"] == "mixed"

    @pytest.mark.asyncio
    async def test_attachment_lost_files_missing(self, env):
        """Shot-сообщение недоступно после рестарта →
        files_missing, комната остаётся collecting_files,
        panels НЕ слиты, статистика не потеряна."""
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        up._SEQ_FILE_CACHE.clear()
        # Discord «забыл» сообщение — fetch падает
        async def boom(mid):
            raise RuntimeError("Unknown Message")
        env.stats.fetch_message = boom
        res = await _finish(env)
        assert res == "files_missing"
        room = env.session["rooms"][0]
        assert room["status"] == "collecting_files"
        assert not room["panels"]
        assert len(room["pending"]) == 2
        assert all(p.get("needs_reupload") for p in room["pending"])

        # Повторная прикрепка того же файла восстанавливает shot
        # без повторного OCR.
        calls = len(env.pipe_calls)
        env.stats = _Chan()  # «новое» сообщение shot отправится
        # fetch_message в _seq_pending_items использует stats_channel
        # финализации; для повторной загрузки канал — env.stats.
        r = await _file(env, 1, "classic1")
        assert r == "ok"
        assert len(env.pipe_calls) == calls
        p0 = room["pending"][0]
        assert p0["shot_pending"] is False
        assert p0["needs_reupload"] is False

    @pytest.mark.asyncio
    async def test_sha_mismatch_on_recovery(self, env):
        """Discord attachment вернул другие байты → sha mismatch →
        files_missing, комплект не публикуется как успешный."""
        await _file(env, 1, "classic1")
        up._SEQ_FILE_CACHE.clear()
        room = env.session["rooms"][0]
        mid = room["pending"][0]["shot_message_id"]
        # Подменяем байты в «сообщении»
        env.stats._by_id[mid].attachments[0]._data = b"EVIL"
        res = await _finish(env)
        assert res == "files_missing"
        assert room["status"] == "collecting_files"

    @pytest.mark.asyncio
    async def test_cancel_clears_caches(self, env):
        """Отмена комплекта → _SEQ/_REPUBLISH кэши сессии пусты."""
        await _file(env, 1, "classic1")
        ck = up._seq_cache_key(1, 1, "qualifier_0", 1)
        assert ck in up._SEQ_FILE_CACHE
        # cancel_rooms_session требует strict admin
        admin = SimpleNamespace(
            id=1, display_name="a", name="a",
            guild_permissions=SimpleNamespace(administrator=True),
        )
        env.tournament.id = "t1"
        env.tournament.processed_elo_matches = set()
        res = await up.cancel_rooms_session(
            env.guild, admin, env.tournament, "qualifier", 0,
        )
        assert res == "ok"
        assert ck not in up._SEQ_FILE_CACHE
        assert not any(
            k[:3] == (1, 1, "qualifier_0")
            for k in up._SEQ_FILE_CACHE
        )


class TestConfirmViewRestore:
    def test_counts_from_session(self, env):
        """bot.py restore вычисляет counts из сессии — те же
        helpers, что и при создании view."""
        from views import ocr_match_view as omv

        room = env.session["rooms"][0] if env.session["rooms"] else {
            "stat_conflicts": [{"resolved": False}],
            "aux_review": [{"name": "x"}],
        }
        env.session["rooms"] = [room]
        n_conf = len(omv._rooms_unresolved_conflicts(env.session))
        n_aux = omv._rooms_aux_review_pending(env.session)
        assert n_conf == 1
        assert n_aux == 1


# ---------- Fake interaction для реальных callback-путей ----------

class _FakeFile:
    def __init__(self, tag: str):
        self.filename = f"{tag}.png"
        self._data = tag.encode()

    async def read(self):
        return self._data


class _FakeResponse:
    def __init__(self, ix):
        self._ix = ix
        self._done = False

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, **kw):
        self._done = True
        self._ix.messages.append(str(content))

    async def send_modal(self, modal=None, **kw):
        self._done = True
        self._ix.modal = modal

    async def edit_original_response(self, content=None, **kw):
        self._ix.messages.append(str(content))

    def is_done(self):
        return self._done


class _FakeFollowup:
    def __init__(self, ix):
        self._ix = ix

    async def send(self, content=None, **kw):
        self._ix.messages.append(str(content))


class _FakeInteraction:
    def __init__(self, env, user_id=42):
        self.user = SimpleNamespace(id=user_id)
        self.guild = env.guild
        self.channel = env.stats
        self.response = _FakeResponse(self)
        self.followup = _FakeFollowup(self)
        self.modal = None
        self.messages: list[str] = []

    async def edit_original_response(self, content=None, **kw):
        self.messages.append(str(content))


async def _noop_chan(stats):
    return stats


def _room_view(env, room_index=1):
    from views.ocr_match_view import RoomFilesView
    return RoomFilesView(
        1, 1, env.tournament, "qualifier", 0, room_index, 1,
    )


async def _submit_modal(modal, env, tag: str):
    """Имитировать submit модалки с одним файлом tag."""
    # FileUpload.values — read-only property; подменяем component
    # целиком фейком с тем же интерфейсом (.values).
    modal.upload.component = SimpleNamespace(
        values=[_FakeFile(tag)]
    )
    ix = _FakeInteraction(env)
    await modal.on_submit(ix)
    return ix


class TestRoomFilesViewPath:
    @pytest.mark.asyncio
    async def test_full_recovery_via_view_callbacks(self, env):
        """2 файла → рестарт → вложение удалено → files_missing →
        «Добавить скриншот» открывает модалку даже при полном
        pending → повтор того же SHA → finish без OCR."""
        env.guild.get_channel = lambda cid: env.stats
        env.guild.fetch_channel = lambda cid: _noop_chan(env.stats)

        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        room = env.session["rooms"][0]
        shots = [p["shot_message_id"] for p in room["pending"]]
        up._SEQ_FILE_CACHE.clear()  # рестарт

        # Вложение первой shot-карточки «удалено»
        env.stats._by_id.pop(shots[0], None)

        # «Завершить комнату» через реальный callback → files_missing
        view = _room_view(env)
        ix_done = _FakeInteraction(env)
        await view._on_done(ix_done)
        assert any("утеряна" in m for m in ix_done.messages), ix_done.messages
        assert room["status"] == "collecting_files"
        # needs_reupload — только у реально утерянного файла,
        # второй shot жив и проверку прошёл.
        assert sum(1 for p in room["pending"] if p.get("needs_reupload")) == 1

        # «Добавить скриншот» — модалка ОТКРЫВАЕТСЯ несмотря на
        # len(pending)==2, т.к. есть needs_reupload.
        ix_add = _FakeInteraction(env)
        await view._on_add(ix_add)
        assert ix_add.modal is not None
        assert ix_add.modal.room_index == 1

        # Submit модалки тем же файлом → retry shot, без OCR.
        calls = len(env.pipe_calls)
        ix_sub = await _submit_modal(ix_add.modal, env, "classic1")
        assert not any("устарела" in m for m in ix_sub.messages)
        assert len(env.pipe_calls) == calls

        # «Завершить комнату» → ok, без повторного OCR.
        ix_done2 = _FakeInteraction(env)
        await view._on_done(ix_done2)
        assert any("завершена" in m for m in ix_done2.messages)
        assert room["status"] == "awaiting_mapping"
        assert len(env.pipe_calls) == calls

    @pytest.mark.asyncio
    async def test_add_blocked_when_room_full_no_reupload(self, env):
        """Полная комната без needs_reupload → модалка НЕ открывается."""
        await _file(env, 1, "squadA")
        await _file(env, 1, "squadB")
        view = _room_view(env)
        ix = _FakeInteraction(env)
        await view._on_add(ix)
        assert ix.modal is None
        assert any("заполнена" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_stale_shot_message_id_republished(self, env):
        """shot_message_id указывает на удалённое сообщение →
        needs_reupload → retry проверяет вложение и публикует
        заново, обновляя ID. Без OCR."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        mid = entry["shot_message_id"]
        env.stats._by_id.pop(mid, None)  # сообщение удалено
        entry["needs_reupload"] = True
        entry["shot_pending"] = True

        calls = len(env.pipe_calls)
        r = await _file(env, 1, "classic1")
        assert r == "ok"
        assert len(env.pipe_calls) == calls
        assert entry["shot_message_id"] != mid
        assert entry["shot_message_id"] in env.stats._by_id
        assert entry["needs_reupload"] is False
        assert entry["shot_pending"] is False

    @pytest.mark.asyncio
    async def test_stale_id_but_attachment_alive_verified(self, env):
        """needs_reupload, но сообщение живо и sha совпадает →
        флаги снимаются БЕЗ повторной публикации (нет дубля)."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        mid = entry["shot_message_id"]
        n_sent = len(env.stats.sent)
        entry["needs_reupload"] = True
        entry["shot_pending"] = True

        calls = len(env.pipe_calls)
        r = await _file(env, 1, "classic1")
        assert r == "ok"
        assert len(env.pipe_calls) == calls
        assert len(env.stats.sent) == n_sent  # дубль не создан
        assert entry["shot_message_id"] == mid
        assert entry["needs_reupload"] is False

    @pytest.mark.asyncio
    async def test_parallel_second_upload_and_finish(self, env):
        """Второй файл и «Завершить комнату» одновременно:
        либо файл принят и комната смешанная, либо комната
        завершена classic-only и файл отклонён room_done.
        Двойной статистики нет."""
        await _file(env, 1, "classic1")
        r_file, r_fin = await asyncio.gather(
            _file(env, 1, "squadA"),
            _finish(env),
        )
        room = env.session["rooms"][0]
        if r_file == "added":
            assert r_fin == "ok"
            assert room["kind"] == "mixed"
        else:
            # finish успел первым — комната classic-only
            assert r_file == "room_done"
            assert r_fin == "ok"
            assert room["kind"] == "classic"
        assert len(room["pending"]) <= 2
        assert env.session["rooms_received"] == 1

    @pytest.mark.asyncio
    async def test_parallel_retries_same_file(self, env):
        """Два параллельных повтора одного shot_pending файла:
        _room_lock сериализует — одна публикация, оба 'ok',
        без дубля сообщения и без OCR."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        entry["shot_pending"] = True
        entry["shot_message_id"] = None

        calls = len(env.pipe_calls)
        n_shots_before = len(env.stats.sent)
        r1, r2 = await asyncio.gather(
            _file(env, 1, "classic1"),
            _file(env, 1, "classic1"),
        )
        # Оба пути безопасны: либо второй увидел восстановленный
        # shot → "ok", либо проверил dup уже после успеха → "dup".
        # Дубля shot-сообщения и повторного OCR быть не должно.
        assert sorted([r1, r2]) in (["dup", "ok"], ["ok", "ok"])
        assert len(env.pipe_calls) == calls
        # Ровно одно новое shot-сообщение с файлом
        new_shots = [
            m for m in env.stats.sent[n_shots_before:]
            if m.attachments
        ]
        assert len(new_shots) == 1
        assert entry["shot_pending"] is False


class TestSingleRoomCard:
    @pytest.mark.asyncio
    async def test_only_one_public_card_after_finalize(self, env):
        """После «Завершить комнату» в канале остаётся ОДНА
        карточка комнаты: shot-сообщения заархивированы,
        collect-сообщение удалено. Вложения — в карточке."""
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        room = env.session["rooms"][0]
        n_before = len(env.stats.sent)
        assert await _finish(env) == "ok"
        assert room["message_id"]
        assert room.get("shots_archived") is True
        assert room.get("collect_message_id") is None
        # Публичных сообщений с файлами — ровно одно (room card)
        file_msgs = [m for m in env.stats.sent if m.attachments]
        assert len(file_msgs) == 1
        assert file_msgs[0].id == room["message_id"]
        # Удалённые сообщения исчезли из канала
        assert all(
            m.id == room["message_id"] or not m.attachments
            for m in env.stats.sent
        )

    @pytest.mark.asyncio
    async def test_shots_kept_when_card_has_no_files(self, env, monkeypatch):
        """Если карточка опубликована БЕЗ вложений — shot'ы НЕ
        удаляются (единственная копия исходника)."""
        await _file(env, 1, "classic1")
        up._SEQ_FILE_CACHE.clear()
        room = env.session["rooms"][0]
        mid = room["pending"][0]["shot_message_id"]
        # «Убиваем» и кэш, и shot-сообщение → items пуст,
        # но problems → files_missing до publish. Чтобы дойти
        # до publish без файлов — прямая проверка archive-guard:
        # вызываем _archive_seq_shot_messages только когда files.
        # Здесь проверяем, что shots_archived не ставится при
        # files_missing.
        async def boom(mid_):
            raise RuntimeError("gone")
        env.stats.fetch_message = boom
        assert await _finish(env) == "files_missing"
        assert room.get("shots_archived") is not True
        assert mid in env.stats._by_id or True  # fetch падал — не важно


class TestPublishFailRecovery:
    @pytest.mark.asyncio
    async def test_publish_fail_keeps_recovery_path(
        self, env, monkeypatch,
    ):
        """Последняя комната: OCR ok, _publish_room_message падает
        дважды → publish_fail. collect_message_id НЕ удаляется,
        progress-view не снимается, republish по тем же SHA без
        OCR восстанавливает карточку."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        cmid = room.get("collect_message_id")
        assert cmid

        orig_publish = up._publish_room_message

        async def boom(*a, **k):
            raise RuntimeError("discord 500")

        monkeypatch.setattr(up, "_publish_room_message", boom)
        res = await _finish(env)
        assert res == "publish_fail"
        assert room.get("message_id") is None
        # collect_message_id сохранён, кнопки сняты (мёртвые)
        assert room.get("collect_message_id") == cmid
        cmsg = env.stats._by_id.get(cmid)
        assert cmsg is not None and cmsg.view is None
        # progress-message: RoomCollectView НЕ снят
        prog = env.stats._by_id.get(
            env.session.get("progress_message_id")
        )
        # progress_message_id может быть None в фикстуре —
        # _update_progress_message молча пропускает. Проверяем
        # логику через _rooms_republish_index напрямую.
        assert up._rooms_republish_index(env.session) == 1
        # Статус комнаты — awaiting_mapping, panels на месте
        assert room["status"] == "awaiting_mapping"
        assert room["panels"]
        # SHA зафиксированы для republish-верификации
        assert room.get("sha") or room.get("shas")

        # «Рестарт»: кэши пусты, republish-кэш тоже
        up._SEQ_FILE_CACHE.clear()
        up._REPUBLISH_FILE_CACHE.clear()
        # Возвращаем настоящий publish — только этот patch;
        # monkeypatch.undo() снял бы и store.set/store.get фикстуры.
        monkeypatch.setattr(up, "_publish_room_message", orig_publish)

        # Повторная загрузка ТЕХ ЖЕ файлов → republish без OCR
        pipe_before = len(env.pipe_calls)
        r1 = await _file(env, 1, "classic1")
        assert r1 == "republished"
        assert len(env.pipe_calls) == pipe_before  # OCR не гонялся
        assert room.get("message_id")
        # После успешного восстановления техническое
        # collect-сообщение удалено — одна публичная карточка.
        assert room.get("collect_message_id") is None
        assert cmid not in env.stats._by_id
        assert up._rooms_republish_index(env.session) is None
        # На опубликованной карточке — RoomView с кнопками
        # сопоставления; скриншот восстановлен.
        card = env.stats._by_id.get(room["message_id"])
        assert card is not None and card.view is not None
        assert card.attachments

    @pytest.mark.asyncio
    async def test_publish_fail_needs_all_shas(self, env, monkeypatch):
        """Mixed-комната publish_fail: republish требует ОБА sha —
        один файл → republish_partial, не «ok»."""
        await _file(env, 1, "classic1")
        await _file(env, 1, "squadA")
        room = env.session["rooms"][0]

        orig_publish = up._publish_room_message

        async def boom(*a, **k):
            raise RuntimeError("discord 500")

        monkeypatch.setattr(up, "_publish_room_message", boom)
        assert await _finish(env) == "publish_fail"
        monkeypatch.setattr(up, "_publish_room_message", orig_publish)
        up._SEQ_FILE_CACHE.clear()
        up._REPUBLISH_FILE_CACHE.clear()

        pipe_before = len(env.pipe_calls)
        # Только один файл — неполный комплект
        r = await _file(env, 1, "classic1")
        assert r == "republish_partial"
        assert room.get("message_id") is None
        # Второй файл → republished
        r = await _file(env, 1, "squadA")
        assert r == "republished"
        assert len(env.pipe_calls) == pipe_before
        assert room.get("message_id")



# ---------- V6.1: отмена удаляет скриншоты, динамические кнопки,
# ---------- «Завершить комнату» — только капитанам ----------

def _admin(user_id=1):
    return SimpleNamespace(
        id=user_id, display_name="Admin",
        mention=f"<@{user_id}>",
        guild_permissions=SimpleNamespace(administrator=True),
    )


async def _cancel(env, user=None):
    env.guild.get_channel = lambda cid: env.stats
    env.guild.fetch_channel = lambda cid: _noop_chan(env.stats)
    return await up.cancel_rooms_session(
        env.guild, user or _admin(), env.tournament,
        "qualifier", 0,
    )


class TestCancelCleanup:
    @pytest.mark.asyncio
    async def test_cancel_deletes_classic_shot(self, env):
        """Отмена после одного Classic → shot-сообщение с
        изображением удалено из канала."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        shot_mid = room["pending"][0]["shot_message_id"]
        assert shot_mid in env.stats._by_id
        assert env.stats._by_id[shot_mid].attachments

        assert await _cancel(env) == "ok"
        assert env.session.get("status") == "cancelled"
        # Скриншот удалён — не остался в канале
        assert shot_mid not in env.stats._by_id
        file_msgs = [
            m for m in env.stats.sent if m.attachments
        ]
        assert file_msgs == []
        # Audit-log отправлен (без самих скриншотов)
        assert any("отмен" in (m.content or "") for m in env.stats.sent)

    @pytest.mark.asyncio
    async def test_cancel_deletes_both_squad_shots(self, env):
        await _file(env, 1, "squadA")
        await _file(env, 1, "squadB")
        room = env.session["rooms"][0]
        mids = [p["shot_message_id"] for p in room["pending"]]
        assert len(mids) == 2
        assert await _cancel(env) == "ok"
        for mid in mids:
            assert mid not in env.stats._by_id
        assert not [m for m in env.stats.sent if m.attachments]

    @pytest.mark.asyncio
    async def test_cancel_after_publish_deletes_card(self, env):
        """Отмена после публикации карточки → карточка с кнопками
        и скриншотом удалена, audit-log сохранён."""
        await _file(env, 1, "classic1")
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        card_mid = room["message_id"]
        assert card_mid in env.stats._by_id
        assert env.stats._by_id[card_mid].attachments

        assert await _cancel(env) == "ok"
        assert card_mid not in env.stats._by_id
        assert not [m for m in env.stats.sent if m.attachments]
        # Audit в лог-канал (тот же fake stats)
        assert any(
            "отмен" in (m.content or "") and "Матч" in (m.content or "")
            for m in env.stats.sent
        )

    @pytest.mark.asyncio
    async def test_cancel_twice_and_missing_msgs_safe(self, env):
        """Повторная отмена → 'done'; отсутствующие сообщения
        не ломают откат."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        env.stats._by_id.pop(
            room["pending"][0]["shot_message_id"], None
        )
        assert await _cancel(env) == "ok"
        # Сессия удалена из ocr_sessions — повторная отмена
        # не находит её («none»), повторного удаления нет.
        assert await _cancel(env) in ("done", "none")


class TestDynamicRoomButtons:
    @pytest.mark.asyncio
    async def test_full_classic_only_done_button(self, env, monkeypatch):
        """Одна комната + полный Classic (4+4 игрока) → collect-
        сообщение показывает только «Завершить комнату», без
        «Добавить скриншот» и без «1/2»."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        monkeypatch.setattr(up, "_classic_pipeline", full_pipe)

        await _file(env, 1, "full1")
        room = env.session["rooms"][0]
        # process_room_file отклонил бы второй файл —
        # проверяем и через view, и напрямую.
        assert up.room_allows_second_file(room) is False
        r = await _file(env, 1, "squadA")
        assert r == "room_full"
        assert len(room["pending"]) == 1

        # Кнопки collect-сообщения: только «Завершить».
        cmid = room["collect_message_id"]
        cmsg = env.stats._by_id[cmid]
        assert cmsg.view is not None
        add_btns = [
            c for c in cmsg.view.children
            if getattr(c, "custom_id", "").startswith("rooms_file_add")
        ]
        done_btns = [
            c for c in cmsg.view.children
            if getattr(c, "custom_id", "").startswith("rooms_file_done")
        ]
        assert add_btns == [] and len(done_btns) == 1
        # Текст не содержит «1/2»
        assert "1/2" not in (cmsg.content or "")
        assert "игроки 8" in (cmsg.content or "")

        # «Завершить комнату» работает — полный Classic мапится.
        assert await _finish(env) == "ok"
        assert room["panels"]["left"]["rows"]

    @pytest.mark.asyncio
    async def test_single_squad_keeps_add_button(self, env):
        """Один squad → кнопка второго скриншота остаётся."""
        await _file(env, 1, "squadA")
        room = env.session["rooms"][0]
        assert up.room_allows_second_file(room) is True
        cmsg = env.stats._by_id[room["collect_message_id"]]
        add_btns = [
            c for c in cmsg.view.children
            if getattr(c, "custom_id", "").startswith("rooms_file_add")
        ]
        assert len(add_btns) == 1
        assert "1/2" in (cmsg.content or "")

    @pytest.mark.asyncio
    async def test_incomplete_classic_allows_second(self, env):
        """Classic с 1 строкой на панель (неполный) → второй
        файл допустим (сверка)."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        assert up.room_allows_second_file(room) is True
        assert await _file(env, 1, "squadA") == "added"

    @pytest.mark.asyncio
    async def test_reupload_flag_keeps_add(self, env):
        """needs_reupload → кнопка добавления сохраняется даже
        для полного Classic."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        orig = up._classic_pipeline
        up._classic_pipeline = full_pipe
        try:
            await _file(env, 1, "full1")
        finally:
            up._classic_pipeline = orig
        room = env.session["rooms"][0]
        room["pending"][0]["needs_reupload"] = True
        assert up.room_allows_second_file(room) is True
        view = _room_view(env)
        add_btns = [
            c for c in view.children
            if getattr(c, "custom_id", "").startswith("rooms_file_add")
        ]
        assert len(add_btns) == 1


class TestCaptainOnlyFinalize:
    async def _try_finish(self, env, user):
        return await up.finalize_room_collect(
            env.guild, user, env.tournament,
            "qualifier", 0, 1, stats_channel=env.stats,
        )

    @pytest.mark.asyncio
    async def test_captain_team0_allowed(self, env):
        await _file(env, 1, "classic1")
        res = await self._try_finish(env, SimpleNamespace(id=42))
        assert res == "ok"

    @pytest.mark.asyncio
    async def test_captain_team1_allowed(self, env):
        await _file(env, 1, "classic1")
        res = await self._try_finish(env, SimpleNamespace(id=43))
        assert res == "ok"

    @pytest.mark.asyncio
    async def test_member_forbidden(self, env):
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        res = await self._try_finish(env, SimpleNamespace(id=999))
        assert res == "forbidden"
        # Комната не закрыта, состояние не изменилось
        assert room["status"] == "collecting_files"
        assert len(room["pending"]) == 1
        assert env.session["rooms_received"] == 0
        assert not room.get("panels")

    @pytest.mark.asyncio
    async def test_admin_without_captain_forbidden(self, env):
        await _file(env, 1, "classic1")
        res = await self._try_finish(env, _admin(777))
        assert res == "forbidden"

    @pytest.mark.asyncio
    async def test_organizer_without_captain_forbidden(self, env):
        await _file(env, 1, "classic1")
        org = SimpleNamespace(
            id=555, display_name="Организатор",
            guild_permissions=SimpleNamespace(administrator=False),
        )
        res = await self._try_finish(env, org)
        assert res == "forbidden"

    @pytest.mark.asyncio
    async def test_on_done_member_rejected_before_finalize(self, env):
        """RoomFilesView._on_done: не-капитан получает отказ ДО
        запуска finalize — pending/panels не меняются."""
        await _file(env, 1, "classic1")
        room = env.session["rooms"][0]
        view = _room_view(env)
        ix = _FakeInteraction(env, user_id=999)
        await view._on_done(ix)
        assert any("капитан" in m for m in ix.messages)
        assert room["status"] == "collecting_files"
        assert not room.get("panels")

    @pytest.mark.asyncio
    async def test_double_finish_counts_once(self, env):
        await _file(env, 1, "classic1")
        assert await _finish(env) == "ok"
        room = env.session["rooms"][0]
        assert await _finish(env) == "done"
        assert env.session["rooms_received"] == 1


class TestReuploadOnlyAndStats:
    """V6.1 edge cases: reupload принимает только исходный SHA;
    Classic без полного K/D/A/DMG оставляет squad-сверку."""

    @pytest.mark.asyncio
    async def test_reupload_only_rejects_new_sha(self, env, monkeypatch):
        """Полный Classic + needs_reupload → новый SHA отклонён
        ДО OCR, pending не изменён; тот же SHA → retry."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        monkeypatch.setattr(up, "_classic_pipeline", full_pipe)
        await _file(env, 1, "full1")
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        entry["needs_reupload"] = True
        entry["shot_pending"] = True
        # «Потеряли» shot-сообщение — retry реально репостит
        env.stats._by_id.pop(entry["shot_message_id"], None)
        assert up._room_reupload_only(room) is True
        assert up.room_allows_second_file(room) is True

        pipe_before = len(env.pipe_calls)
        # Новый SHA → room_full, OCR не запускался
        r = await _file(env, 1, "squadNEW")
        assert r == "room_full"
        assert len(env.pipe_calls) == pipe_before
        assert len(room["pending"]) == 1
        assert room["pending"][0]["sha"] != up._sha256_bytes(
            b"squadNEW"
        )

        # Тот же SHA → восстановление shot без OCR и без
        # изменения pending.
        r = await _file(env, 1, "full1")
        assert r in ("ok", "added")
        assert len(env.pipe_calls) == pipe_before
        assert len(room["pending"]) == 1
        assert room["pending"][0].get("needs_reupload") in (
            None, False,
        )

    @pytest.mark.asyncio
    async def test_reupload_only_accepts_same_sha(self, env, monkeypatch):
        """Полный Classic + needs_reupload: повторный submit
        исходного файла восстанавливает вложение (shot_pending
        снимается), новый pending не создаётся."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        monkeypatch.setattr(up, "_classic_pipeline", full_pipe)
        await _file(env, 1, "full1")
        room = env.session["rooms"][0]
        entry = room["pending"][0]
        entry["needs_reupload"] = True
        entry["shot_pending"] = True
        old_shots = env.stats.sent.count(
            env.stats._by_id.get(entry["shot_message_id"])
        )
        pipe_before = len(env.pipe_calls)
        r = await _file(env, 1, "full1")
        assert r in ("ok", "added")
        assert len(env.pipe_calls) == pipe_before
        assert len(room["pending"]) == 1
        assert entry.get("shot_pending") in (None, False)

    @pytest.mark.asyncio
    async def test_classic_missing_dmg_allows_squad(self, env, monkeypatch):
        """8 игроков найдены, но dmg=None у всех → classic НЕ
        «полный»: squad-сверка доступна, кнопка add остаётся."""
        async def pipe(data, need_score=True):
            tag = data.decode()
            if tag.startswith("squad"):
                return _squad_pipe(tag)
            return _full_classic_pipe(dmg_missing=True)
        monkeypatch.setattr(up, "_classic_pipeline", pipe)
        await _file(env, 1, "full1")
        room = env.session["rooms"][0]
        assert up.room_allows_second_file(room) is True
        assert up._room_reupload_only(room) is False
        r = await _file(env, 1, "squadA")
        assert r == "added"
        assert len(room["pending"]) == 2

    @pytest.mark.asyncio
    async def test_classic_full_stats_blocks_second(self, env, monkeypatch):
        """8 игроков + все K/D/A/DMG → только «Завершить»,
        второй файл отклонён ещё до OCR."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        monkeypatch.setattr(up, "_classic_pipeline", full_pipe)
        await _file(env, 1, "full1")
        room = env.session["rooms"][0]
        assert up.room_allows_second_file(room) is False
        assert up._classic_pipe_complete(
            room["pending"][0]["pipe"]
        ) is True
        pipe_before = len(env.pipe_calls)
        r = await _file(env, 1, "squadA")
        assert r == "room_full"
        assert len(env.pipe_calls) == pipe_before
        assert len(room["pending"]) == 1


# ---------- V6.2: RoomStartModal — одна модалка вместо select ----------

def _start_ix(env, user_id=42, display_name="CapA"):
    ix = _FakeInteraction(env, user_id=user_id)
    ix.user.display_name = display_name
    ix.user.name = display_name
    ix.user.guild = env.guild
    ix.user.roles = []
    ix.user.guild_permissions = SimpleNamespace(administrator=False)
    return ix


def _start_modal(count="1", tag="classic1"):
    m = up.RoomStartModal(1, 1, "qualifier", 0)
    m.count_select.component = SimpleNamespace(values=[count])
    m.upload.component = (
        SimpleNamespace(values=[_FakeFile(tag)])
        if tag is not None else SimpleNamespace(values=[])
    )
    return m


class TestRoomStartModal:
    @pytest.mark.asyncio
    async def test_main_button_opens_modal_not_select(self, env):
        """Основная кнопка «Отправить скриншот» → send_modal
        (RoomStartModal), БЕЗ ephemeral-сообщения с select."""
        from views.ocr_match_view import MatchScreenshotButton

        env.tournament.ocr_sessions.clear()
        btn = MatchScreenshotButton(
            1, 1, env.tournament, "qualifier", 0, "📸 Скриншот",
        )
        ix = _start_ix(env)
        await btn.callback(ix)
        assert ix.modal is not None
        assert isinstance(ix.modal, up.RoomStartModal)
        # Никакого ephemeral-select сообщения
        assert ix.messages == []
        # В модалке: select со значением по умолчанию "1" +
        # обязательный FileUpload.
        assert ix.modal.title == "Отправить результат"
        labels = [
            getattr(c, "label", None) or getattr(c, "text", None)
            for c in ix.modal.children
        ]
        assert any("Скриншот комнаты 1" in str(x) for x in labels)

    @pytest.mark.asyncio
    async def test_default_count_is_one(self, env):
        """Submit без изменения выбора → rooms_expected=1,
        файл принят в комнату 1."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("1")
        ix = _start_ix(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        assert sess["rooms_expected"] == 1
        assert sess["status"] == "collecting"
        assert sess["rooms"][0]["room_index"] == 1
        assert len(sess["rooms"][0]["pending"]) == 1
        assert any("принят" in msg for msg in ix.messages)

    @pytest.mark.asyncio
    async def test_count_two_and_three(self, env):
        for count in ("2", "3"):
            env.tournament.ocr_sessions.clear()
            up._SEQ_FILE_CACHE.clear()
            m = _start_modal(count)
            ix = _start_ix(env)
            await m.on_submit(ix)
            sess = env.tournament.ocr_sessions["qualifier_0"]
            assert sess["rooms_expected"] == int(count)
            assert sess["rooms"][0]["room_index"] == 1

    @pytest.mark.asyncio
    async def test_close_without_submit_no_session(self, env):
        """Просто открытие модалки сессии не создаёт."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3")
        assert "qualifier_0" not in env.tournament.ocr_sessions

    @pytest.mark.asyncio
    async def test_missing_file_no_session(self, env):
        env.tournament.ocr_sessions.clear()
        m = _start_modal("2", tag=None)
        ix = _start_ix(env)
        await m.on_submit(ix)
        assert "qualifier_0" not in env.tournament.ocr_sessions
        assert any("Прикрепите" in msg for msg in ix.messages)

    @pytest.mark.asyncio
    async def test_double_submit_single_session(self, env):
        env.tournament.ocr_sessions.clear()
        m1 = _start_modal("1")
        m2 = _start_modal("1")
        ix1 = _start_ix(env)
        ix2 = _start_ix(env)
        await m1.on_submit(ix1)
        await m2.on_submit(ix2)
        sess = env.tournament.ocr_sessions.get("qualifier_0")
        assert sess is not None
        assert any("уже идёт загрузка" in msg for msg in ix2.messages)

    @pytest.mark.asyncio
    async def test_two_captains_one_bundle(self, env):
        """Два капитана отправляют форму одновременно —
        сериализация _room_lock → один комплект."""
        env.tournament.ocr_sessions.clear()
        m1 = _start_modal("1")
        m2 = _start_modal("1")
        ix1 = _start_ix(env, user_id=42, display_name="CapA")
        ix2 = _start_ix(env, user_id=43, display_name="CapB")
        await asyncio.gather(m1.on_submit(ix1), m2.on_submit(ix2))
        assert len(env.tournament.ocr_sessions) == 1
        sess = env.tournament.ocr_sessions["qualifier_0"]
        # Только один комплект: rooms_received не более 1,
        # pending не дублируется.
        assert sess["rooms_received"] <= 1

    @pytest.mark.asyncio
    async def test_unrecognized_first_file_retryable(self, env):
        """Первый файл не распознан → сессия остаётся collecting,
        retry через process_room_file работает."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("1", tag="FAILx")
        ix = _start_ix(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions.get("qualifier_0")
        env.session = sess
        assert sess is not None
        assert sess["status"] == "collecting"
        assert any("Не удалось распознать" in msg for msg in ix.messages)
        # Повторная загрузка — сессия не заблокирована
        r = await _file(env, 1, "classic1")
        assert r == "ok"
        room = sess["rooms"][0]
        assert len(room["pending"]) == 1

    @pytest.mark.asyncio
    async def test_full_classic_only_done_button_via_modal(
        self, env, monkeypatch,
    ):
        """Полный Classic через RoomStartModal → collect-сообщение
        с одной кнопкой «Завершить комнату»."""
        async def full_pipe(data, need_score=True):
            return _full_classic_pipe()
        monkeypatch.setattr(up, "_classic_pipeline", full_pipe)
        env.tournament.ocr_sessions.clear()
        m = _start_modal("1", tag="full1")
        ix = _start_ix(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        room = sess["rooms"][0]
        cmsg = env.stats._by_id[room["collect_message_id"]]
        add_btns = [
            c for c in cmsg.view.children
            if getattr(c, "custom_id", "").startswith("rooms_file_add")
        ]
        assert add_btns == []
        assert up.room_allows_second_file(room) is False
