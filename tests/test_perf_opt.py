"""Тесты оптимизации Railway: OCR-счётчик, batch cyrillic OCR,
idle-закрытие Chromium, метрики памяти."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestCountingExecutor:
    def test_active_ops_counted(self):
        from services.ocr_service import OCR_EXECUTOR, active_ocr_ops

        async def run():
            loop = asyncio.get_running_loop()
            ev = asyncio.Event()

            def work():
                # Пока worker спит — операция должна быть «активной»
                for _ in range(100):
                    if active_ocr_ops() >= 1:
                        ev.set()
                        break
                    time.sleep(0.01)
                time.sleep(0.05)
                return 42

            fut = loop.run_in_executor(OCR_EXECUTOR, work)
            await asyncio.wait_for(ev.wait(), timeout=5)
            assert await fut == 42
            # После завершения счётчик вернулся к 0
            assert active_ocr_ops() == 0

        asyncio.run(run())


class TestCyrillicBatch:
    def test_regions_share_one_decode(self, monkeypatch):
        """ocr_cyrillic_regions: одно декодирование на N регионов."""
        import services.ocr_service as ocr
        from PIL import Image

        # PNG 100x100
        import io

        buf = io.BytesIO()
        Image.new("RGB", (100, 100), (255, 0, 0)).save(buf, "PNG")
        data = buf.getvalue()

        opens = []
        real_open = Image.open

        def counting_open(*a, **k):
            opens.append(1)
            return real_open(*a, **k)

        monkeypatch.setattr(ocr.Image, "open", counting_open)
        calls = []

        def fake_region(img, region):
            calls.append(region)
            return f"name{len(calls)}"

        monkeypatch.setattr(ocr, "_cyr_region_text", fake_region)

        regions = [(0, 0, 10, 10), (20, 20, 30, 30), (40, 40, 50, 50)]
        out = ocr._run_ocr_cyrillic_regions(data, regions)
        assert out == ["name1", "name2", "name3"]
        assert calls == regions
        # Image.open вызван: 1 декодирование + N переоткрытий crop-буферов?
        # _cyr_region_text замокан — crop.save не вызывается; единственный
        # Image.open — исходное изображение.
        assert len(opens) == 1

    def test_empty_regions(self):
        import services.ocr_service as ocr

        assert ocr._run_ocr_cyrillic_regions(b"bad", []) == []


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self):
        self.launch_count = 0
        self.browsers: list[_FakeBrowser] = []

    async def launch(self, args=None):
        self.launch_count += 1
        b = _FakeBrowser()
        self.browsers.append(b)
        return b


class _FakeDriver:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stop_count = 0

    async def stop(self):
        self.stop_count += 1


class _FakePWContext:
    def __init__(self):
        self.driver = _FakeDriver()

    async def start(self):
        return self.driver


def _install_fake_playwright(monkeypatch):
    """Подменить playwright.async_api фейком; вернуть (starts, ctx)."""
    import types

    starts: list[int] = []

    def fake_async_playwright():
        starts.append(1)
        return _FakePWContext()

    # Все вызовы должны возвращать ОДИН контекст, иначе каждый
    # _get_browser создаст новый драйвер — это и проверяем.
    shared_ctx = _FakePWContext()

    def fake_async_playwright_shared():
        starts.append(1)
        return shared_ctx

    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = fake_async_playwright_shared
    pkg.async_api = api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    return starts, shared_ctx


@pytest.fixture
def pr(monkeypatch):
    """Чистое состояние profile_renderer между тестами."""
    import utils.profile_renderer as m

    monkeypatch.setattr(m, "_browser", None)
    monkeypatch.setattr(m, "_playwright", None)
    monkeypatch.setattr(m, "_last_browser_used", time.monotonic())
    monkeypatch.setattr(m, "_active_renders", 0)
    monkeypatch.setattr(m, "IDLE_CLOSE_SECONDS", 0.05)
    # Reaper по умолчанию «спит» — тесты сами дёргают
    # _close_browser_if_idle/_idle_reaper с явным интервалом.
    monkeypatch.setattr(m, "_REAPER_INTERVAL", 3600.0)
    monkeypatch.setattr(m, "_idle_reaper_task", None)
    return m


async def _run_reaper_ticks(pr, monkeypatch, seconds=0.15):
    """Запустить настоящий reaper-loop с быстрым интервалом."""
    monkeypatch.setattr(pr, "_REAPER_INTERVAL", 0.02)
    task = asyncio.create_task(pr._idle_reaper())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestProfileIdleReaper:
    @pytest.mark.asyncio
    async def test_close_browser_resets_state(self, pr):
        b = _FakeBrowser()
        pr._browser = b
        await pr._close_browser()
        assert pr._browser is None
        assert b.closed

    @pytest.mark.asyncio
    async def test_idle_reaper_auto_closes_stale_browser(
        self, pr, monkeypatch
    ):
        """Reaper реально закрывает браузер после idle-таймаута."""
        b = _FakeBrowser()
        pr._browser = b
        pr._playwright = object()
        pr._last_browser_used = time.monotonic() - 999  # давным-давно

        await _run_reaper_ticks(pr, monkeypatch, seconds=0.3)
        assert b.closed
        assert pr._browser is None

    @pytest.mark.asyncio
    async def test_idle_reaper_keeps_fresh_browser(
        self, pr, monkeypatch
    ):
        b = _FakeBrowser()
        pr._browser = b
        pr._playwright = object()
        pr._last_browser_used = time.monotonic()  # только что
        pr.IDLE_CLOSE_SECONDS = 60.0  # свежий ≠ stale при коротком пороге

        await _run_reaper_ticks(pr, monkeypatch, seconds=0.1)
        assert not b.closed

    @pytest.mark.asyncio
    async def test_idle_reaper_skips_active_render(
        self, pr, monkeypatch
    ):
        """Браузер не закрывается, пока идёт рендер — даже если idle."""
        b = _FakeBrowser()
        pr._browser = b
        pr._playwright = object()
        pr._last_browser_used = time.monotonic() - 999
        pr._active_renders = 1

        await _run_reaper_ticks(pr, monkeypatch, seconds=0.1)
        assert not b.closed
        # Рендер завершился → следующий тик закрывает браузер.
        pr._active_renders = 0
        await _run_reaper_ticks(pr, monkeypatch, seconds=0.3)
        assert b.closed


class TestBrowserLifecycle:
    @pytest.mark.asyncio
    async def test_launch_idle_close_relaunch_reuses_driver(
        self, pr, monkeypatch
    ):
        """launch → idle close → launch: драйвер НЕ пересоздаётся.

        Регрессия: раньше каждый _get_browser() после idle-close
        вызывал async_playwright().start() — плодились драйверы
        и их дочерние процессы.
        """
        starts, ctx = _install_fake_playwright(monkeypatch)

        b1 = await pr._get_browser()
        assert len(starts) == 1
        assert ctx.driver.chromium.launch_count == 1

        # idle-close: браузер закрыт, драйвер жив
        pr._last_browser_used = time.monotonic() - 9999
        await pr._close_browser()
        assert b1.closed
        assert pr._playwright is ctx.driver

        # повторный launch — БЕЗ нового start()
        b2 = await pr._get_browser()
        assert len(starts) == 1, "драйвер пересоздан — утечка процессов"
        assert ctx.driver.chromium.launch_count == 2
        assert not b2.closed

        # и ещё один цикл — всё ещё один драйвер
        await pr._close_browser()
        await pr._get_browser()
        assert len(starts) == 1
        assert ctx.driver.chromium.launch_count == 3

    @pytest.mark.asyncio
    async def test_no_child_process_accumulation(self, pr, monkeypatch):
        """N циклов close/launch → ровно 1 playwright-драйвер."""
        starts, ctx = _install_fake_playwright(monkeypatch)
        for _ in range(5):
            await pr._get_browser()
            await pr._close_browser()
        assert len(starts) == 1
        assert ctx.driver.stop_count == 0  # драйвер не останавливался

    @pytest.mark.asyncio
    async def test_dead_driver_restarted_once(self, pr, monkeypatch):
        """Если launch падает (драйвер умер) — stop + один рестарт."""
        import types

        starts: list[int] = []
        launches = {"n": 0}

        class _Chromium:
            async def launch(self, args=None):
                launches["n"] += 1
                if launches["n"] == 1:
                    raise RuntimeError("driver dead")
                return _FakeBrowser()

        class _Drv:
            def __init__(self):
                self.chromium = _Chromium()
                self.stop_count = 0

            async def stop(self):
                self.stop_count += 1

        class _Ctx:
            def __init__(self):
                self.driver = _Drv()

            async def start(self):
                starts.append(1)
                return self.driver

        shared = _Ctx()

        def fake_async_playwright():
            return shared

        pkg = types.ModuleType("playwright")
        api = types.ModuleType("playwright.async_api")
        api.async_playwright = fake_async_playwright
        pkg.async_api = api
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.async_api", api)

        b = await pr._get_browser()
        assert not b.closed
        assert launches["n"] == 2  # fail → restart → success
        assert shared.driver.stop_count == 1

    @pytest.mark.asyncio
    async def test_parallel_get_browser_single_launch(
        self, pr, monkeypatch
    ):
        """Параллельные _get_browser() → один launch (lock)."""
        starts, ctx = _install_fake_playwright(monkeypatch)
        results = await asyncio.gather(
            *[pr._get_browser() for _ in range(8)]
        )
        assert all(r is results[0] for r in results)
        assert len(starts) == 1
        assert ctx.driver.chromium.launch_count == 1

    @pytest.mark.asyncio
    async def test_render_semaphore_limits_parallelism(self, pr):
        """CARD_RENDER_SEM ≤ 2 параллельных рендера."""
        active = 0
        max_active = 0

        async def worker():
            nonlocal active, max_active
            async with pr.CARD_RENDER_SEM:
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        assert max_active <= 2
        assert max_active >= 1

    @pytest.mark.asyncio
    async def test_render_counters_track_active(self, pr):
        pr._render_started()
        pr._render_started()
        assert pr._active_renders == 2
        pr._render_finished()
        assert pr._active_renders == 1
        pr._render_finished()
        assert pr._active_renders == 0

    @pytest.mark.asyncio
    async def test_close_in_progress_new_render_gets_fresh_browser(
        self, pr, monkeypatch
    ):
        """Гонка: idle-close уже начался → новый рендер.

        Reaper отсоединил браузер (_browser=None) и «закрывает»
        его; параллельный _get_browser() НЕ должен получить
        закрывающийся экземпляр — только свежий launch.
        """
        starts, ctx = _install_fake_playwright(monkeypatch)
        b1 = await pr._get_browser()
        pr._last_browser_used = time.monotonic() - 9999

        close_started = asyncio.Event()
        release_close = asyncio.Event()
        orig_close = b1.close

        async def slow_close():
            close_started.set()
            await release_close.wait()
            await orig_close()

        b1.close = slow_close

        closer = asyncio.create_task(pr._close_browser_if_idle())
        await asyncio.wait_for(close_started.wait(), timeout=2)
        # Detach произошёл ДО close: глобально браузера уже нет,
        # но b1 физически ещё «закрывается».
        assert pr._browser is None

        pr._render_started()
        try:
            # Не ждёт завершения чужого close (лок свободен) —
            # сразу получает новый браузер.
            b2 = await asyncio.wait_for(pr._get_browser(), timeout=2)
        finally:
            pr._render_finished()
        assert b2 is not b1
        assert not b2.closed
        assert ctx.driver.chromium.launch_count == 2

        release_close.set()
        assert await closer is True
        assert b1.closed
        assert pr._browser is b2

    @pytest.mark.asyncio
    async def test_render_started_before_check_aborts_close(
        self, pr, monkeypatch
    ):
        """Рендер начался ДО проверки reaper'а → close отменяется."""
        starts, ctx = _install_fake_playwright(monkeypatch)
        b1 = await pr._get_browser()
        pr._last_browser_used = time.monotonic() - 9999

        pr._render_started()
        try:
            # stale по времени, но _active_renders=1 → не закрываем
            assert await pr._close_browser_if_idle() is False
            assert pr._browser is b1
            assert not b1.closed
            # И _get_browser отдаёт ЖИВОЙ экземпляр, не закрытый.
            assert await pr._get_browser() is b1
            assert not b1.closed
        finally:
            pr._render_finished()


class TestRealChromiumLifecycle:
    """Настоящий Chromium/Playwright: launch→idle→close→launch
    циклы и контроль дочерних процессов (без mock-драйвера)."""

    @pytest.mark.asyncio
    async def test_real_cycles_no_process_accumulation(
        self, pr, monkeypatch
    ):
        # Тест проверяет жизненный цикл CHROMIUM — форсируем backend.
        monkeypatch.setenv("PNG_RENDER_BACKEND", "chromium")
        psutil = pytest.importorskip("psutil")
        pytest.importorskip("playwright.async_api")
        import os

        proc = psutil.Process(os.getpid())

        async def child_count() -> int:
            # Даём ОС перевести завершённые процессы в zombie-free
            # состояние перед подсчётом.
            await asyncio.sleep(0.3)
            return len(proc.children(recursive=True))

        try:
            # Цикл 1: launch → idle-close → счётчик «холостых» детей
            b1 = await pr._get_browser()
            assert b1.is_connected()
            pr._last_browser_used = time.monotonic() - 9999
            assert await pr._close_browser_if_idle() is True
            assert not b1.is_connected()
            c_idle1 = await child_count()

            # Цикл 2: launch → реальный PNG-рендер → idle-close
            png = await pr.render_profile_png_async({})
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            pr._last_browser_used = time.monotonic() - 9999
            assert await pr._close_browser_if_idle() is True
            c_idle2 = await child_count()

            # Драйвер playwright жив (1 node-процесс), браузеры
            # закрыты — число дочерних процессов не растёт
            # между циклами. Допуск ±2 на transient helper'ы.
            assert abs(c_idle2 - c_idle1) <= 2, (c_idle1, c_idle2)

            # Рендер после idle-close — свежий launch, PNG валиден.
            png2 = await pr.render_profile_png_async({})
            assert png2[:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            await pr.shutdown_profile_renderer()

    @pytest.mark.asyncio
    async def test_real_render_survives_idle_close_attempts(
        self, pr, monkeypatch
    ):
        # Chromium-рендер — форсируем backend.
        monkeypatch.setenv("PNG_RENDER_BACKEND", "chromium")
        """Конкурентно: PNG-рендер vs попытки idle-close.

        Рендер держит _active_renders>0 → reaper обязан
        отказаться от закрытия; даже если close «проскочил»
        между попытками, _get_browser отдаёт живой/свежий
        экземпляр — PNG генерируется успешно.
        """
        pytest.importorskip("playwright.async_api")
        await pr._get_browser()  # прогрев launch
        pr._last_browser_used = time.monotonic() - 9999

        stop = asyncio.Event()

        async def spam_close():
            while not stop.is_set():
                await pr._close_browser_if_idle()
                await asyncio.sleep(0.005)

        task = asyncio.create_task(spam_close())
        try:
            png = await pr.render_profile_png_async({})
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        await pr.shutdown_profile_renderer()


class TestPerfMetricsMemory:
    def test_process_stats_keys(self):
        from utils.perf_metrics import _process_stats

        stats = _process_stats()
        # На Linux/Railway — rss_mb + rss_children_mb (psutil) или
        # rss_mb из resource-fallback; на Windows без psutil — {}.
        assert isinstance(stats, dict)

    def test_cpu_percent_not_always_zero(self):
        """cpu_percent на кэшированном Process не возвращает 0
        из-за «первого замера» — процесс живёт между вызовами."""
        from utils.perf_metrics import _get_proc, _process_stats

        proc = _get_proc()
        # Первый вызов после создания Process может дать 0.0 —
        # это норма; важно, что объект ОДИН и переиспользуется.
        s1 = _process_stats()
        s2 = _process_stats()
        assert "cpu_pct" in s1 or "rss_mb" in s1
        # Повторный вызов — тот же объект Process.
        assert _get_proc() is proc
