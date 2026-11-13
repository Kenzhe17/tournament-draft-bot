"""Bundle-загрузка OCR (/tournament create): 1 скриншот на комнату,
файлы привязываются по порядку, «Завершить загрузку» только при
полном комплекте. MM/legacy-сессии без bundle_v2 — старый флоу.
"""

from __future__ import annotations

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
    def __init__(self, cid=1):
        self.sent: list = []
        self.id = cid
        self.guild = SimpleNamespace(id=1)
        self._by_id: dict[int, SimpleNamespace] = {}

    async def send(self, content=None, view=None, files=None, **kw):
        msg = SimpleNamespace(
            id=1000 + len(self.sent), view=view, content=content,
            attachments=[],
        )
        for f in files or []:
            fp = getattr(f, "fp", None)
            raw = fp.read() if fp is not None else b""
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


class _FakeFile:
    def __init__(self, tag):
        self.filename = f"{tag}.png"
        self._data = tag.encode()

    async def read(self):
        return self._data


class _FakeInteraction:
    def __init__(self, env, user_id=42):
        self.guild = env.guild
        self.channel = env.stats
        self.user = SimpleNamespace(
            id=user_id, display_name="CapA", name="CapA",
            roles=[], guild=env.guild,
            guild_permissions=SimpleNamespace(administrator=False),
        )
        self.messages: list[str] = []
        self.followups: list[str] = []
        self.modal = None
        self.response = SimpleNamespace(
            defer=self._defer,
            send_message=self._send_message,
            send_modal=self._send_modal,
            is_done=lambda: True,
        )
        self.followup = SimpleNamespace(send=self._followup_send)
        self.client = SimpleNamespace()

    async def _defer(self, **kw):
        return None

    async def _send_message(self, content=None, **kw):
        self.messages.append(str(content))

    async def _send_modal(self, modal):
        self.modal = modal

    async def _followup_send(self, content=None, **kw):
        self.followups.append(str(content))

    async def edit_original_response(self, content=None, **kw):
        self.followups.append(str(content))


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
        "bundle_v2": True,
    }
    tournament = SimpleNamespace(
        channel_id=1,
        guild_id=1,
        ocr_sessions={"qualifier_0": session},
        player_user_ids={"CapA": 42, "CapB": 43},
        match_channels={},
        teams=[
            {"captain": "CapA", "players": ["CapA", "P1"]},
            {"captain": "CapB", "players": ["CapB", "P2"]},
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

    async def fake_pipeline(data, need_score=True):
        tag = data.decode()
        if tag.startswith("FAIL"):
            return None
        if tag.startswith("squad"):
            return _squad_pipe(tag)
        return _classic_pipe(tag)

    monkeypatch.setattr(up, "_classic_pipeline", fake_pipeline)

    return SimpleNamespace(
        session=session, tournament=tournament, guild=guild,
        stats=stats,
    )


async def _noop():
    return None


def _item(tag: str):
    return {"filename": f"{tag}.png", "data": tag.encode()}


def _start_modal(count="1", tags=("classic1",)):
    m = up.RoomStartModal(1, 1, "qualifier", 0, bundle=True)
    m.count_select.component = SimpleNamespace(values=[count])
    m.upload.component = SimpleNamespace(
        values=[_FakeFile(t) for t in tags]
    )
    return m


def _bundle_modal(env, max_files=3, tags=("c1", "c2", "c3")):
    m = up.BundleUploadModal(
        1, 1, env.tournament, "qualifier", 0,
        int(env.session["gen"]), max_files,
    )
    m.upload.component = SimpleNamespace(
        values=[_FakeFile(t) for t in tags[:max_files]]
    )
    return m


def _collect_view(env):
    return up.RoomCollectView(
        1, 1, env.tournament, "qualifier", 0,
        int(env.session.get("gen", 1)),
    )


def _btns(view, prefix):
    return [
        c for c in view.children
        if getattr(c, "custom_id", "").startswith(prefix)
    ]


class TestBundleSessionFlag:
    @pytest.mark.asyncio
    async def test_plain_tournament_gets_bundle_flag(self, env, monkeypatch):
        env.tournament.ocr_sessions.clear()
        env.tournament.mm_search_channel_id = None
        saved = []
        monkeypatch.setattr(up.store, "set", lambda t: saved.append(t))

        async def _send(*a, **k):
            return SimpleNamespace(id=777)

        stats = _Chan()
        stats.send = _send
        session = await up.create_rooms_session(
            env.guild, stats,
            SimpleNamespace(id=9, display_name="u", name="u"),
            env.tournament, "qualifier", 0, 3,
        )
        assert session["bundle_v2"] is True

    @pytest.mark.asyncio
    async def test_mm_tournament_keeps_legacy_flow(self, env, monkeypatch):
        env.tournament.ocr_sessions.clear()
        env.tournament.mm_search_channel_id = 999
        stats = _Chan()

        async def _send(*a, **k):
            return SimpleNamespace(id=778)

        stats.send = _send
        session = await up.create_rooms_session(
            env.guild, stats,
            SimpleNamespace(id=9, display_name="u", name="u"),
            env.tournament, "qualifier", 0, 2,
        )
        assert session["bundle_v2"] is False
        # MM: старый флоу — кнопки «Добавить»+«Отменить», без finish.
        view = _collect_view(env)
        env.session = session
        view2 = up.RoomCollectView(
            1, 1, env.tournament, "qualifier", 0, session["gen"],
        )
        assert _btns(view2, "rooms_add")
        assert not _btns(view2, "rooms_finish")


class TestBundleStartModal:
    @pytest.mark.asyncio
    async def test_three_files_three_rooms(self, env):
        """count=3 + 3 файла → комнаты 1,2,3 по одному файлу."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3", tags=("r1", "r2", "r3"))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        assert sess["bundle_v2"] is True
        assert sess["rooms_expected"] == 3
        by_idx = {r["room_index"]: r for r in sess["rooms"]}
        assert set(by_idx) == {1, 2, 3}
        for i in (1, 2, 3):
            assert len(by_idx[i]["pending"]) == 1
        # Per-room collect-сообщений нет.
        assert all(
            r.get("collect_message_id") is None for r in sess["rooms"]
        )

    @pytest.mark.asyncio
    async def test_extra_files_rejected(self, env):
        """Файлов больше, чем комнат → отказ до создания сессии."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("1", tags=("a", "b"))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        assert "qualifier_0" not in env.tournament.ocr_sessions
        assert any("больше" in msg for msg in ix.followups)

    @pytest.mark.asyncio
    async def test_partial_bundle_progress(self, env):
        """count=3, один файл → Скриншоты 1/3, только «Добавить»."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3", tags=("only1",))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        text = up._rooms_progress_text(sess)
        assert "Скриншоты: 1/3" in text
        assert "Комнаты: 1/3" in text
        view = _collect_view(env)
        assert _btns(view, "rooms_add")
        assert not _btns(view, "rooms_finish")

    @pytest.mark.asyncio
    async def test_full_bundle_shows_finish(self, env):
        """3/3 → «Завершить загрузку», «Добавить» скрыта."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3", tags=("r1", "r2", "r3"))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        text = up._rooms_progress_text(sess)
        assert "Скриншоты: 3/3" in text
        assert "Завершить загрузку" in text
        view = _collect_view(env)
        assert _btns(view, "rooms_finish")
        assert not _btns(view, "rooms_add")

    @pytest.mark.asyncio
    async def test_squad_file_rejected(self, env):
        """Скриншот отряда в bundle-режиме → invalid_classic,
        комната остаётся пустой."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("1", tags=("squadX",))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        assert not sess["rooms"] or not sess["rooms"][0].get("pending")
        assert any("полный экран" in msg for msg in ix.followups)


class TestBundleDoseUpload:
    @pytest.mark.asyncio
    async def test_dose_upload_fills_remaining_rooms(self, env):
        """После 1/3 «Добавить скриншоты» → модалка принимает
        до 2 файлов и заполняет комнаты 2 и 3."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3", tags=("r1",))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess

        bm = _bundle_modal(env, max_files=2, tags=("r2", "r3"))
        ix2 = _FakeInteraction(env)
        await bm.on_submit(ix2)
        by_idx = {r["room_index"]: r for r in sess["rooms"]}
        assert len(by_idx[2]["pending"]) == 1
        assert len(by_idx[3]["pending"]) == 1
        assert up._bundle_upload_complete(sess) is True

    @pytest.mark.asyncio
    async def test_dose_upload_never_exceeds_rooms(self, env):
        """3 файла при 1 свободной комнате → принят 1, лишние
        отброшены с предупреждением."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("2", tags=("r1", "r2"))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        # Сессия полна — модалка не должна открываться/принимать.
        bm = _bundle_modal(env, max_files=3, tags=("x", "y", "z"))
        ix2 = _FakeInteraction(env)
        await bm.on_submit(ix2)
        # Все комнаты по-прежнему по 1 файлу, лишние sha — дубли.
        for r in sess["rooms"]:
            assert len(r["pending"]) == 1


class TestBundleFinish:
    @pytest.mark.asyncio
    async def test_finish_finalizes_all_rooms(self, env, monkeypatch):
        """«Завершить загрузку» → finalize_room_collect для всех
        комнат → panels + awaiting_mapping."""
        published = []

        async def fake_publish(channel, tournament, stats_ch, items,
                               room, mt, mi):
            published.append(room["room_index"])
            room["message_id"] = 5000 + room["room_index"]
            return True

        monkeypatch.setattr(up, "_publish_room_message", fake_publish)
        env.tournament.ocr_sessions.clear()
        m = _start_modal("3", tags=("r1", "r2", "r3"))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess

        view = _collect_view(env)
        finish_btn = _btns(view, "rooms_finish")[0]
        ix3 = _FakeInteraction(env)
        # _on_finish — bound callback кнопки
        await finish_btn.callback(ix3)
        by_idx = {r["room_index"]: r for r in sess["rooms"]}
        for i in (1, 2, 3):
            assert by_idx[i].get("panels")
            assert by_idx[i]["status"] == "awaiting_mapping"
        assert sess["status"] == "awaiting_mapping"
        assert sorted(published) == [1, 2, 3]
        assert any("Загрузка завершена" in m for m in ix3.followups)

    @pytest.mark.asyncio
    async def test_finish_blocked_when_incomplete(self, env):
        """Неполный комплект → сервер отклоняет finish даже при
        устаревшей View."""
        env.tournament.ocr_sessions.clear()
        m = _start_modal("2", tags=("r1",))
        ix = _FakeInteraction(env)
        await m.on_submit(ix)
        sess = env.tournament.ocr_sessions["qualifier_0"]
        env.session = sess
        # Принудительно создаём View с кнопкой finish (как будто
        # комплект был полон при рендере).
        view = up.RoomCollectView(
            1, 1, env.tournament, "qualifier", 0, sess["gen"],
        )
        # Кнопки finish нет — но вызовем _on_finish напрямую,
        # имитируя устаревший клик.
        ix2 = _FakeInteraction(env)
        await view._on_finish(ix2)
        assert any("неполный" in m for m in ix2.followups)
        assert sess["status"] == "collecting"

    @pytest.mark.asyncio
    async def test_no_finish_button_for_legacy_session(self, env):
        """Сессия без bundle_v2 → старые кнопки, finish нет."""
        env.session.pop("bundle_v2", None)
        view = _collect_view(env)
        assert _btns(view, "rooms_add")
        assert not _btns(view, "rooms_finish")


class TestBundleProgressText:
    def test_progress_shows_room_and_file_counters(self, env):
        sess = env.session
        sess["rooms_expected"] = 2
        sess["rooms"] = [
            {"room_index": 1, "status": "collecting_files",
             "pending": [_pending_entry()], "panels": {},
             "kind": None, "sha": None, "shas": []},
        ]
        text = up._rooms_progress_text(sess)
        assert "Комнаты: 1/2" in text
        assert "Скриншоты: 1/2" in text

    def test_legacy_session_no_bundle_line(self, env):
        env.session.pop("bundle_v2", None)
        env.session["rooms_expected"] = 2
        text = up._rooms_progress_text(env.session)
        assert "Скриншоты:" not in text


def _pending_entry():
    return {
        "sha": "s1", "kind": "classic", "filename": "s1.png",
        "pipe": {"panels": {"left": {"rows": [{}]},
                            "right": {"rows": [{}]}}},
        "shot_message_id": None,
    }
