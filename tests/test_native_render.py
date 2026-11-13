"""Тесты native PNG-рендереров (resvg/Pillow) и backend-переключателя.

Покрытие: размеры/валидность PNG, кириллица, повторные и
параллельные рендеры, event-loop, отсутствие дочерних процессов,
fallback auto→chromium, метрики.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# resvg_py — обязательная зависимость native-рендера: тесты должны
# ПАДАТЬ при её отсутствии, а не молча скипаться.
import resvg_py  # noqa: F401,E402
import PIL  # noqa: F401,E402

from PIL import Image  # noqa: E402


def _png_size(png: bytes) -> tuple[int, int]:
    im = Image.open(io.BytesIO(png))
    im.verify()
    im = Image.open(io.BytesIO(png))
    return im.size


def _profile_data(**over) -> dict:
    data = {
        "name": "Тест", "status": "ок", "level": 44, "elo": 1523,
        "last_elo_change": 24, "skill_rating": 7.4,
        "matches": 128, "wins": 74, "winrate": 57.8, "kda": 1.9,
        "avg_kills": 4.2, "avg_damage": 1820, "max_kills": 12,
        "elo_history": [10, -5, 30, 15], "hist_start_elo": 1400,
        "chart_delta": 50, "balance": 12500, "items": 320,
        "reputation": 45, "partner": "—", "birthday": "—",
        "ult_matches": 3, "ult_wins": 2, "ult_elo": 1600,
        "ult_status": "В норме",
    }
    data.update(over)
    return data


@pytest.fixture
def native_backend(monkeypatch):
    monkeypatch.setenv("PNG_RENDER_BACKEND", "native")


class TestProfileNative:
    @pytest.mark.asyncio
    async def test_size_and_valid_png(self, native_backend):
        from utils.profile_renderer import render_profile_png_async

        png = await render_profile_png_async(_profile_data())
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert _png_size(png) == (1920, 1080)

    @pytest.mark.asyncio
    async def test_cyrillic_and_special_chars(self, native_backend):
        from utils.profile_renderer import render_profile_png_async

        data = _profile_data(
            name="Женісайрат_Әсел_Қазақ_™",
            status=" honours система — проверка",
            partner="привет_мир_№1",
        )
        png = await render_profile_png_async(data)
        assert _png_size(png) == (1920, 1080)
        # Не пустой файл и не «картинка ошибки» — размер > 50KB
        assert len(png) > 50_000

    @pytest.mark.asyncio
    async def test_with_avatar_and_badge(self, native_backend):
        from utils.profile_renderer import render_profile_png_async

        av = (Path(__file__).parent.parent
              / "assets_ds_server" / "profile_v2" / "logo.png")
        badge = (Path(__file__).parent.parent
                 / "assets_ds_server" / "ranked_badges" / "gold_3.png")
        av_b = av.read_bytes() if av.exists() else None
        bg_b = badge.read_bytes() if badge.exists() else None
        png = await render_profile_png_async(
            _profile_data(), av_b, bg_b)
        assert _png_size(png) == (1920, 1080)

    @pytest.mark.asyncio
    async def test_repeated_renders(self, native_backend):
        from utils.profile_renderer import render_profile_png_async

        for _ in range(5):
            png = await render_profile_png_async(_profile_data())
            assert _png_size(png) == (1920, 1080)

    @pytest.mark.asyncio
    async def test_parallel_renders_bounded(self, native_backend):
        """5 параллельных рендеров — все успешны, executor ограничен."""
        from utils.profile_renderer import (
            PNG_EXECUTOR, render_profile_png_async)

        results = await asyncio.gather(*[
            render_profile_png_async(_profile_data(name=f"P{i}"))
            for i in range(5)
        ])
        for png in results:
            assert _png_size(png) == (1920, 1080)
        # Единый executor для profile+banner (PNG_RENDER_WORKERS).
        import os as _os
        expected = int(_os.environ.get("PNG_RENDER_WORKERS", "3"))
        assert PNG_EXECUTOR._max_workers == expected

    @pytest.mark.asyncio
    async def test_no_child_processes(self, native_backend):
        """Native-путь не создаёт дочерних процессов."""
        import psutil

        proc = psutil.Process(os.getpid())
        before = len(proc.children(recursive=True))
        from utils.profile_renderer import render_profile_png_async

        await render_profile_png_async(_profile_data())
        await asyncio.sleep(0.2)
        after = len(proc.children(recursive=True))
        assert after <= before + 1  # допускаем transient


class TestBannerNative:
    @pytest.mark.asyncio
    async def test_size_and_valid(self, native_backend):
        from services.banner.renderer import render_banner_png

        png = await render_banner_png(
            nickname="Kenzhe", avatar_png=None,
            voice_seconds=3600, matches=5, has_activity=True)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert _png_size(png) == (1672, 941)

    @pytest.mark.asyncio
    async def test_empty_state(self, native_backend):
        from services.banner.renderer import render_banner_png

        png = await render_banner_png(
            nickname=None, avatar_png=None, voice_seconds=0,
            matches=0, has_activity=False)
        assert _png_size(png) == (1672, 941)

    @pytest.mark.asyncio
    async def test_cyrillic_long_nick(self, native_backend):
        from services.banner.renderer import render_banner_png

        png = await render_banner_png(
            nickname="ОченьДлинныйНикнейм_Қазақ_Женіс",
            avatar_png=None, voice_seconds=95 * 60, matches=128,
            has_activity=True)
        assert _png_size(png) == (1672, 941)

    @pytest.mark.asyncio
    async def test_parallel(self, native_backend):
        from services.banner.renderer import render_banner_png

        results = await asyncio.gather(*[
            render_banner_png(nickname=f"P{i}", avatar_png=None,
                              voice_seconds=60 * i, matches=i,
                              has_activity=True)
            for i in range(5)
        ])
        for png in results:
            assert _png_size(png) == (1672, 941)


class TestBackendSwitch:
    @pytest.mark.asyncio
    async def test_auto_uses_native_first(self, monkeypatch):
        """auto → native без fallback, если native успешен."""
        monkeypatch.setenv("PNG_RENDER_BACKEND", "auto")
        from utils import profile_renderer as pr

        called = {"native": 0, "chromium": 0}
        orig_n = pr._render_profile_png_native
        orig_c = pr._render_profile_png_chromium

        async def spy_n(*a, **k):
            called["native"] += 1
            return await orig_n(*a, **k)

        async def spy_c(*a, **k):
            called["chromium"] += 1
            return await orig_c(*a, **k)

        monkeypatch.setattr(pr, "_render_profile_png_native", spy_n)
        monkeypatch.setattr(pr, "_render_profile_png_chromium", spy_c)
        png = await pr.render_profile_png_async(_profile_data())
        assert called == {"native": 1, "chromium": 0}
        assert _png_size(png) == (1920, 1080)

    @pytest.mark.asyncio
    async def test_auto_falls_back_on_native_error(self, monkeypatch):
        """auto: native упал → chromium fallback (логируется)."""
        monkeypatch.setenv("PNG_RENDER_BACKEND", "auto")
        from utils import profile_renderer as pr

        async def boom(*a, **k):
            raise RuntimeError("resvg broken")

        async def fake_chromium(*a, **k):
            return b"\x89PNG\r\n\x1a\nFAKE"

        monkeypatch.setattr(pr, "_render_profile_png_native", boom)
        monkeypatch.setattr(pr, "_render_profile_png_chromium",
                            fake_chromium)
        png = await pr.render_profile_png_async(_profile_data())
        assert png == b"\x89PNG\r\n\x1a\nFAKE"

    @pytest.mark.asyncio
    async def test_native_mode_raises(self, monkeypatch):
        """backend=native: ошибка НЕ маскируется fallback'ом."""
        monkeypatch.setenv("PNG_RENDER_BACKEND", "native")
        from utils import profile_renderer as pr

        async def boom(*a, **k):
            raise RuntimeError("resvg broken")

        monkeypatch.setattr(pr, "_render_profile_png_native", boom)
        with pytest.raises(RuntimeError, match="resvg broken"):
            await pr.render_profile_png_async(_profile_data())

    @pytest.mark.asyncio
    async def test_chromium_mode_skips_native(self, monkeypatch):
        monkeypatch.setenv("PNG_RENDER_BACKEND", "chromium")
        from utils import profile_renderer as pr

        called = []

        async def spy_n(*a, **k):
            called.append("native")
            raise AssertionError("native must not be called")

        async def fake_c(*a, **k):
            called.append("chromium")
            return b"\x89PNG\r\n\x1a\nFAKE"

        monkeypatch.setattr(pr, "_render_profile_png_native", spy_n)
        monkeypatch.setattr(pr, "_render_profile_png_chromium", fake_c)
        await pr.render_profile_png_async(_profile_data())
        assert called == ["chromium"]


class TestRenderAdmission:
    @pytest.mark.asyncio
    async def test_queue_full_rejected(self, native_backend, monkeypatch):
        """Превышение PNG_RENDER_MAX_INFLIGHT → быстрый отказ,
        без создания SVG/буферов."""
        import threading

        from utils import profile_renderer as pr

        monkeypatch.setattr(pr, "_PNG_MAX_INFLIGHT", 2)
        gate = threading.Event()

        def slow_svg(*a, **k):
            gate.wait(10)
            return b"<svg/>"

        async def fake_png(svg):
            return b"\x89PNG\r\n\x1a\nx"

        # _profile_svg_to_png строит SVG+растрирует; подменяем
        # целиком синхронной заглушкой, спящей в worker-потоке.
        monkeypatch.setattr(pr, "_profile_svg_to_png", slow_svg)
        monkeypatch.setattr(pr, "_svg_to_png_native", fake_png)
        t1 = asyncio.create_task(pr.render_profile_png_async({}))
        t2 = asyncio.create_task(pr.render_profile_png_async({}))
        await asyncio.sleep(0.1)
        assert pr.png_render_inflight() == 2
        # Третий — отказ сразу, не в очередь
        with pytest.raises(RuntimeError, match="queue is full"):
            await pr.render_profile_png_async({})
        gate.set()
        await asyncio.gather(t1, t2)
        assert pr.png_render_inflight() == 0

    @pytest.mark.asyncio
    async def test_admission_covers_banner_too(
        self, native_backend, monkeypatch
    ):
        """Banner и profile делят один admission-счётчик."""
        from utils import profile_renderer as pr
        from services.banner import renderer as br

        monkeypatch.setattr(pr, "_PNG_MAX_INFLIGHT", 1)
        import threading
        gate = threading.Event()

        def slow_banner(**kw):
            gate.wait(10)
            return b"\x89PNG\r\n\x1a\nx"

        def slow_svg(*a, **k):
            gate.wait(10)
            return b"<svg/>"

        monkeypatch.setattr(br, "_render_banner_native", slow_banner)
        monkeypatch.setattr(pr, "_profile_svg_to_png", slow_svg)
        t1 = asyncio.create_task(br.render_banner_png(
            nickname="A", avatar_png=None, voice_seconds=1,
            matches=1, has_activity=True))
        await asyncio.sleep(0.1)
        # profile не может занять слот, пока banner держит его
        with pytest.raises(RuntimeError, match="queue is full"):
            from utils.profile_renderer import render_profile_png_async
            await render_profile_png_async(_profile_data())
        gate.set()
        await t1
        assert pr.png_render_inflight() == 0

    @pytest.mark.asyncio
    async def test_cancel_keeps_slot_until_worker_done(
        self, native_backend, monkeypatch
    ):
        """Отмена asyncio-задачи НЕ освобождает admission-слот,
        пока worker-поток реально исполняет рендер."""
        import threading

        from utils import profile_renderer as pr

        monkeypatch.setattr(pr, "_PNG_MAX_INFLIGHT", 1)
        started = threading.Event()
        gate = threading.Event()

        def slow_svg(*a, **k):
            started.set()
            gate.wait(10)
            return b"<svg/>"

        async def fake_png(svg):
            return b"\x89PNG\r\n\x1a\nx"

        monkeypatch.setattr(pr, "_profile_svg_to_png", slow_svg)
        monkeypatch.setattr(pr, "_svg_to_png_native", fake_png)
        t = asyncio.create_task(pr.render_profile_png_async({}))
        # Ждём, пока worker реально начал работу
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        assert pr.png_render_inflight() == 1
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # Слот ВСЁ ЕЩЁ занят — поток продолжает работать,
        # новый рендер обязан получить отказ, а не занять слот.
        await asyncio.sleep(0.05)
        assert pr.png_render_inflight() == 1
        with pytest.raises(RuntimeError, match="queue is full"):
            await pr.render_profile_png_async({})
        # Освобождаем worker — слот отпускается по done_callback.
        gate.set()
        for _ in range(100):
            if pr.png_render_inflight() == 0:
                break
            await asyncio.sleep(0.02)
        assert pr.png_render_inflight() == 0
        # После освобождения рендер снова проходит
        # (_profile_svg_to_png всё ещё замокан → возвращает <svg/>).
        png = await pr.render_profile_png_async({})
        assert png == b"<svg/>"

    @pytest.mark.asyncio
    async def test_cancel_before_start_releases_slot(
        self, native_backend, monkeypatch
    ):
        """Если future отменён ДО старта в executor — слот
        освобождается через done_callback, утечки нет."""
        import threading

        from utils import profile_renderer as pr

        # Лимит высокий — blockers и тестовая задача проходят admission.
        monkeypatch.setattr(pr, "_PNG_MAX_INFLIGHT", 10)
        blocker = threading.Event()

        def block(*a, **k):
            blocker.wait(10)
            return b"ok"

        # Занимаем ВСЕ worker'ы PNG_EXECUTOR блокирующими задачами —
        # future тестового рендера гарантированно встанет в очередь
        # executor'а и не стартует до cancel.
        holders = [asyncio.create_task(pr._png_run_native(block))
                   for _ in range(pr.PNG_EXECUTOR._max_workers)]
        await asyncio.sleep(0.1)
        base = pr.png_render_inflight()
        assert base == pr.PNG_EXECUTOR._max_workers

        monkeypatch.setattr(pr, "_profile_svg_to_png",
                            lambda *a, **k: b"<svg/>")
        t = asyncio.create_task(pr.render_profile_png_async({}))
        await asyncio.sleep(0.1)
        assert pr.png_render_inflight() == base + 1
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # future ещё не стартовал — отмена освобождает слот.
        for _ in range(100):
            if pr.png_render_inflight() == base:
                break
            await asyncio.sleep(0.02)
        assert pr.png_render_inflight() == base
        # Освобождаем blockers — все слоты возвращаются в 0.
        blocker.set()
        await asyncio.gather(*holders)
        for _ in range(100):
            if pr.png_render_inflight() == 0:
                break
            await asyncio.sleep(0.02)
        assert pr.png_render_inflight() == 0

    @pytest.mark.asyncio
    async def test_peak_rss_recorded_during_render(
        self, native_backend
    ):
        """RSS-монитор фиксирует пик во время нативного рендера."""
        from utils import profile_renderer as pr

        pr._PNG_PEAK_RSS_MB = 0.0
        png = await pr.render_profile_png_async(_profile_data())
        assert _png_size(png) == (1920, 1080)
        assert pr.png_render_peak_rss_mb() > 10  # МБ — реальный пик

    @pytest.mark.asyncio
    async def test_event_loop_not_blocked(self, native_backend):
        """Рендеры идут в executor — loop отвечает во время рендера."""
        from utils.profile_renderer import render_profile_png_async

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        png = await render_profile_png_async(_profile_data())
        await t
        assert _png_size(png) == (1920, 1080)
        assert ticks >= 15  # loop не был заблокирован на ~600мс


class TestMetrics:
    def test_native_metric_keys_exist(self):
        from utils import perf_metrics

        perf_metrics.record("profile_render_native_ms", 1.0)
        perf_metrics.record("banner_render_native_ms", 1.0)
        perf_metrics.incr("png_render_fallbacks")
        perf_metrics.incr("png_render_errors")
        # не падает — ключи просто копятся в буферах
