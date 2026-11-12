"""Phase: фикс RoomUploadModal — Label>FileUpload (discord.py 2.7),
чтение вложений через self.upload, обработка ошибок send_modal,
fallback rss_mb."""

from types import SimpleNamespace

import pytest

import discord
from discord.ui import FileUpload, Label

import cogs.ocr_upload as ou


class _Resp:
    def __init__(self, fail_modal=False):
        self.fail_modal = fail_modal
        self.messages = []
        self.modals = []

    async def defer(self, **kw):
        pass

    async def send_message(self, text=None, **kw):
        self.messages.append((text, kw))

    async def send_modal(self, modal):
        if self.fail_modal:
            raise RuntimeError("modal open failed")
        self.modals.append(modal)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append((text, kw))


def _interaction(resp=None, user_id=42):
    return SimpleNamespace(
        response=resp or _Resp(),
        followup=_Followup(),
        user=SimpleNamespace(
            id=user_id, display_name="Cap", name="cap",
            guild_permissions=SimpleNamespace(administrator=False),
        ),
        guild=SimpleNamespace(id=1, name="g"),
        channel=SimpleNamespace(id=2, name="c"),
    )


def _attachment(name: str, payload: bytes):
    async def _read():
        return payload

    return SimpleNamespace(filename=name, read=_read)


def _tournament_with_session(gen=1):
    key = ou._session_key("qualifier", 0)
    session = {
        "match_type": "qualifier",
        "match_index": 0,
        "mode": "rooms",
        "status": "collecting",
        "rooms_expected": 1,
        "rooms_received": 0,
        "rooms": [],
        "gen": gen,
        "uploaded_by": 42,
        "uploaded_by_name": "Cap",
        "teams": ["A", "B"],
        "session_id": "s1",
        "match_id": "m1",
        "winner_pending": True,
        "winning_team_index": None,
    }
    t = SimpleNamespace(
        channel_id=2, id=9, ocr_sessions={key: session},
        mm_search_channel_id=0,
    )
    return t, key, session


@pytest.fixture
def modal_env(monkeypatch):
    """Подменяем тяжёлые зависимости on_submit, оставляя
    реальный RoomUploadModal и чтение вложений."""
    tournament, key, session = _tournament_with_session()
    captured = {}

    async def fake_bundle(guild, t, k, s, user):
        return "ok", t, s

    async def fake_stats(interaction, t=None):
        return interaction.channel

    async def fake_process(**kw):
        captured["item"] = kw.get("item")
        captured["items"] = kw.get("items")
        return "ok"

    monkeypatch.setattr(ou, "store", SimpleNamespace(get=lambda g, c: tournament))
    monkeypatch.setattr(ou, "_rooms_file_submit_allowed", lambda *a: True)
    monkeypatch.setattr(ou, "_rooms_claim_bundle", fake_bundle)
    monkeypatch.setattr(ou, "_stats_channel", fake_stats)
    monkeypatch.setattr(ou, "process_room_file", fake_process)
    return tournament, key, session, captured


class TestRoomUploadModalReal:
    def test_constructs_real_label_fileupload(self):
        """Настоящий Modal: Label-обёртка над FileUpload,
        без mock — конструктор discord.py 2.7 не падает."""
        modal = ou.RoomUploadModal(1, 2, SimpleNamespace(), "qualifier", 0, 1, 1)
        assert modal.title == "Комната 1"
        upload = modal.upload
        assert isinstance(upload, Label)
        assert isinstance(upload.component, FileUpload)
        assert upload.component.min_values == 1
        # V5 sequential: строго один файл за загрузку
        assert upload.component.max_values == 1
        assert upload.component.required is True
        assert "1 файл" in upload.text
        # add_item сохранил Label как единственный child
        assert modal.children[0] is upload

    @pytest.mark.asyncio
    async def test_submit_reads_one_attachment(self, modal_env):
        _, key, session, captured = modal_env
        modal = ou.RoomUploadModal(1, 2, SimpleNamespace(), "qualifier", 0, 1, 1)
        modal.upload.component._values = [_attachment("room1.png", b"PNG1")]
        inter = _interaction()
        await modal.on_submit(inter)
        assert captured["item"] == {"filename": "room1.png", "data": b"PNG1"}
        assert inter.followup.sent  # финальное сообщение отправлено

    @pytest.mark.asyncio
    async def test_submit_passes_first_of_values(self, modal_env):
        """V5: модалка принимает 1 файл; даже если values вернул
        несколько — в process_room_file уходит только первый."""
        _, key, session, captured = modal_env
        modal = ou.RoomUploadModal(1, 2, SimpleNamespace(), "qualifier", 0, 2, 1)
        modal.upload.component._values = [
            _attachment("a.png", b"AAA"),
            _attachment("b.png", b"BBB"),
        ]
        inter = _interaction()
        await modal.on_submit(inter)
        assert captured["item"] == {"filename": "a.png", "data": b"AAA"}

    @pytest.mark.asyncio
    async def test_submit_no_files_notifies(self, modal_env):
        modal = ou.RoomUploadModal(1, 2, SimpleNamespace(), "qualifier", 0, 1, 1)
        inter = _interaction()
        await modal.on_submit(inter)
        assert any("Файлы не получены" in str(s[0]) for s in inter.followup.sent)


class TestSendModalFailure:
    @pytest.mark.asyncio
    async def test_on_add_modal_failure_keeps_session(self, monkeypatch):
        """Ошибка send_modal → ephemeral-ошибка, сессия остаётся
        collecting (не блокируется), файлы не теряются."""
        tournament, key, session = _tournament_with_session()
        monkeypatch.setattr(ou, "store", SimpleNamespace(get=lambda g, c: tournament))
        monkeypatch.setattr(ou, "_rooms_file_submit_allowed", lambda *a: True)

        view = ou.RoomCollectView(1, 2, tournament, "qualifier", 0, 1)
        resp = _Resp(fail_modal=True)
        inter = _interaction(resp)
        await view._on_add(inter)
        # Пользователь получил понятную ошибку
        assert any("Не удалось открыть" in str(m[0]) for m in resp.messages)
        # Сессия НЕ отменена и НЕ завершена — retry возможен
        assert session["status"] == "collecting"
        assert session["gen"] == 1
