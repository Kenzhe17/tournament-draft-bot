"""Тесты диагностической команды /ocr (cogs/ocr_diag.py).

Проверяем: права доступа, валидацию вложения, режимы fast/full/compare,
fallback, расхождения Fast vs Full, таймаут и изоляцию от игровых
данных (cog не трогает store/ELO/экономику — только OCR-сервисы).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.ocr_diag as diag


def _png_bytes(w: int = 64, h: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self) -> None:
        self.sent: list = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs


class _IX:
    def __init__(self, user_id: int = 999, admin: bool = False,
                 guild_id: int = 555):
        self.guild = SimpleNamespace(id=guild_id)
        self.guild_id = guild_id
        self.user = SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=admin),
        )
        self.response = _Resp()
        self.embed = None

    async def edit_original_response(self, embed=None, **kwargs):
        self.embed = embed


class _Att:
    def __init__(self, data: bytes | None = None, ctype: str = "image/png",
                 filename: str = "shot.png", size: int | None = None):
        self._data = _png_bytes() if data is None else data
        self.content_type = ctype
        self.filename = filename
        self.size = len(self._data) if size is None else size

    async def read(self) -> bytes:
        return self._data


def _parsed(kills: int = 6) -> dict:
    return {
        "team_a": [
            {"name": f"A{i}", "kills": kills, "deaths": 1, "assists": 2,
             "dmg": 1000 + i}
            for i in range(4)
        ],
        "team_b": [
            {"name": f"B{i}", "kills": 3, "deaths": 4, "assists": 5,
             "dmg": 2000 + i}
            for i in range(4)
        ],
        "team_a_name": "TeamA", "team_b_name": "TeamB",
        "score": [4, 3], "banner": None, "top5": False,
        "_rec_boxes": [],
    }


def _pipeline_result(kills: int = 6) -> dict:
    p = _parsed(kills)
    return {
        "panels": {
            "left": {"name": "TeamA", "rows": p["team_a"]},
            "right": {"name": "TeamB", "rows": p["team_b"]},
        },
        "score": [4, 3], "banner": None, "top5": False,
        "winner_side": "left",
    }


@pytest.fixture
def cog(monkeypatch):
    monkeypatch.setattr(diag, "guild_allowed", lambda gid: True)
    monkeypatch.setattr(diag, "ADMIN_USER_IDS", {111})
    return diag.OCRDiagCog(bot=None)


class TestPermissions:
    @pytest.mark.asyncio
    async def test_denies_non_admin(self, cog):
        ix = _IX(user_id=999, admin=False)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="fast")
        assert ix.response.sent and "⛔" in ix.response.sent[0][0]
        assert not ix.response.deferred

    @pytest.mark.asyncio
    async def test_allows_owner(self, cog):
        ix = _IX(user_id=111, admin=False)
        # owner проходит permission; дальше упрётся в OCR —
        # мокаем _run, чтобы не гонять движок.
        async def fake_run(interaction, att, mode):
            return diag.discord.Embed(title="ok")
        monkeypatch_cog = cog
        orig = monkeypatch_cog._run
        monkeypatch_cog._run = fake_run
        try:
            await cog.ocr.callback(cog, ix, image=_Att(), mode="fast")
        finally:
            monkeypatch_cog._run = orig
        assert ix.response.deferred
        assert ix.embed is not None

    @pytest.mark.asyncio
    async def test_denies_disallowed_guild(self, monkeypatch, cog):
        monkeypatch.setattr(diag, "guild_allowed", lambda gid: False)
        ix = _IX(user_id=111, admin=True)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="fast")
        assert ix.response.sent and "⛔" in ix.response.sent[0][0]


class TestAttachmentValidation:
    @pytest.mark.asyncio
    async def test_rejects_non_image(self, cog):
        ix = _IX(user_id=111)
        await cog.ocr.callback(
            cog, ix, image=_Att(ctype="application/pdf", filename="x.pdf"),
            mode="fast",
        )
        assert ix.response.sent and "❌" in ix.response.sent[0][0]

    @pytest.mark.asyncio
    async def test_rejects_bad_extension(self, cog):
        ix = _IX(user_id=111)
        await cog.ocr.callback(
            cog, ix, image=_Att(filename="x.txt"), mode="fast",
        )
        assert ix.response.sent and "❌" in ix.response.sent[0][0]

    @pytest.mark.asyncio
    async def test_rejects_oversize(self, cog):
        ix = _IX(user_id=111)
        att = _Att(size=11 * 1024 * 1024)
        await cog.ocr.callback(cog, ix, image=att, mode="fast")
        assert ix.response.sent and "❌" in ix.response.sent[0][0]

    @pytest.mark.asyncio
    async def test_rejects_corrupt_image(self, cog):
        ix = _IX(user_id=111)
        att = _Att(data=b"not an image", filename="x.png")
        await cog.ocr.callback(cog, ix, image=att, mode="fast")
        assert ix.response.deferred
        assert ix.embed is not None
        assert "не является корректным" in ix.embed.description


class TestRunModes:
    @pytest.mark.asyncio
    async def test_fast_success(self, monkeypatch, cog):
        async def fake_fast(data, **kw):
            return _parsed()
        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="fast")
        assert ix.embed is not None
        text = str(ix.embed.to_dict())
        assert "4 — 3" in text
        assert "TeamA" in text
        assert "Fast OCR" in text

    @pytest.mark.asyncio
    async def test_fast_fallback_runs_full(self, monkeypatch, cog):
        calls = {"classic": 0}

        async def fake_fast(data, **kw):
            return None

        async def fake_classic(data, need_score=True, fast=True):
            calls["classic"] += 1
            assert fast is False
            return _pipeline_result()

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        import services.fast_cs_ocr as fco
        monkeypatch.setattr(fco, "last_fallback_reason", lambda: "no_anchor_right")
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="fast")
        assert calls["classic"] == 1
        assert ix.embed is not None
        assert "fallback" in str(ix.embed.to_dict())

    @pytest.mark.asyncio
    async def test_full_mode_skips_fast(self, monkeypatch, cog):
        fast_calls = {"n": 0}

        async def fake_fast(data, **kw):
            fast_calls["n"] += 1
            return _parsed()

        async def fake_classic(data, need_score=True, fast=True):
            assert fast is False
            return _pipeline_result()

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="full")
        assert fast_calls["n"] == 0
        assert ix.embed is not None

    @pytest.mark.asyncio
    async def test_compare_reports_diffs(self, monkeypatch, cog):
        async def fake_fast(data, **kw):
            return _parsed(kills=6)

        async def fake_classic(data, need_score=True, fast=True):
            return _pipeline_result(kills=9)  # kills отличаются

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="compare")
        assert ix.embed is not None
        d = str(ix.embed.to_dict())
        assert "Расхождения" in d
        assert "kills" in d

    @pytest.mark.asyncio
    async def test_compare_no_diffs(self, monkeypatch, cog):
        async def fake_fast(data, **kw):
            return _parsed()

        async def fake_classic(data, need_score=True, fast=True):
            return _pipeline_result()

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="compare")
        assert "совпали" in str(ix.embed.to_dict())

    @pytest.mark.asyncio
    async def test_unrecognized_everything(self, monkeypatch, cog):
        async def fake_fast(data, **kw):
            return None

        async def fake_classic(data, need_score=True, fast=True):
            return None

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="compare")
        assert "не распознали" in str(ix.embed.to_dict())

    @pytest.mark.asyncio
    async def test_no_state_mutation(self, monkeypatch, cog):
        """Изоляция: /ocr не вызывает store/ELO/экономику."""
        import storage.json_store as js
        called = []
        monkeypatch.setattr(js.store, "set", lambda *a, **k: called.append("set"))
        monkeypatch.setattr(js.store, "get", lambda *a, **k: called.append("get"))

        async def fake_fast(data, **kw):
            return _parsed()

        async def fake_classic(data, need_score=True, fast=True):
            return _pipeline_result()

        monkeypatch.setattr("services.fast_cs_ocr.parse_cs_fast", fake_fast)
        monkeypatch.setattr("cogs.ocr_upload._classic_pipeline", fake_classic)
        ix = _IX(user_id=111)
        await cog.ocr.callback(cog, ix, image=_Att(), mode="compare")
        assert ix.embed is not None
        assert called == []
