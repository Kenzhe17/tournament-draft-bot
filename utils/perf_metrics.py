"""Лёгкий мониторинг производительности r!z3.

Один reporter-loop (60с) пишет одну JSON-строку в лог —
Railway собирает её как structured log. Включён по умолчанию,
отключается env ENABLE_PERF_METRICS=false|0|no.

Принципы:
- минимальная нагрузка: один таск, sleep 1с для lag-сэмплов,
  отчёт раз в 60с;
- ограниченные буферы: deque с maxlen, счётчики — фиксированный
  набор ключей;
- мониторинг никогда не должен ронять бота: все публичные
  функции проглатывают собственные ошибки;
- никаких токенов/ключей/персональных данных в метриках.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("ENABLE_PERF_METRICS", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Счётчики и тайминги (ограниченные)
# ---------------------------------------------------------------------------

_COUNTERS: dict[str, int] = {}
# name -> deque of duration samples in ms (maxlen ограничивает память)
_TIMINGS: dict[str, "deque[float]"] = {}
_TIMING_MAXLEN = 1000

# Gauge-провайдеры: name -> callable() -> int|float|None.
# Регистрируются ботом/сторaми (не тянут зависимости сюда).
_GAUGES: dict[str, Callable[[], Any]] = {}

# Event-loop lag samples (секунды), окно ~5 минут при 1с периоде.
_LAG_SAMPLES: "deque[float]" = deque(maxlen=300)

_report_task: "asyncio.Task | None" = None
_lag_task: "asyncio.Task | None" = None
_wrapped_http = False


def incr(name: str, n: int = 1) -> None:
    """Инкремент счётчика. No-op при выключенном мониторинге."""
    try:
        if not _enabled():
            return
        _COUNTERS[name] = _COUNTERS.get(name, 0) + n
    except Exception:
        pass


def record(name: str, duration_ms: float) -> None:
    """Записать длительность операции в мс."""
    try:
        if not _enabled():
            return
        dq = _TIMINGS.get(name)
        if dq is None:
            dq = deque(maxlen=_TIMING_MAXLEN)
            _TIMINGS[name] = dq
        dq.append(float(duration_ms))
    except Exception:
        pass


def timing(name: str):
    """Sync context manager: with timing("x"): ..."""
    try:
        enabled = _enabled()
    except Exception:
        enabled = False

    class _T:
        def __enter__(self):
            self.t0 = time.perf_counter()
            return self

        def __exit__(self, *exc):
            record(name, (time.perf_counter() - self.t0) * 1000.0)
            return False

    return _T() if enabled else _NullTimer()


class _NullTimer:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def async_timing(name: str):
    """Async context manager для замера await-операций."""
    try:
        enabled = _enabled()
    except Exception:
        enabled = False

    class _AT:
        def __init__(self):
            self.t0 = time.perf_counter()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            record(name, (time.perf_counter() - self.t0) * 1000.0)
            return False

    return _AT() if enabled else _ANullTimer()


class _ANullTimer:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def register_gauge(name: str, fn: Callable[[], Any]) -> None:
    """Зарегистрировать gauge (активные задачи/сессии и т.п.)."""
    _GAUGES[name] = fn


def _percentile(samples: list[float], pct: float) -> float | None:
    """p-тый перцентиль; None при пустом списке."""
    if not samples:
        return None
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def _timing_stats(dq: "deque[float]") -> dict[str, float]:
    """avg/p95/max по окну сэмплов (мс)."""
    if not dq:
        return {}
    vals = list(dq)
    out = {
        "avg_ms": round(sum(vals) / len(vals), 2),
        "max_ms": round(max(vals), 2),
        "n": len(vals),
    }
    p95 = _percentile(vals, 95)
    if p95 is not None and len(vals) >= 20:
        # p95 обещаем только при достаточной выборке
        out["p95_ms"] = round(p95, 2)
    return out


def _process_stats() -> dict[str, float]:
    """CPU% и RSS через psutil (fallback — resource)."""
    out: dict[str, float] = {}
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        out["cpu_pct"] = round(proc.cpu_percent(interval=None), 1)
        out["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        try:
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss: Linux — KB, macOS — bytes, Windows — bytes
            rss = ru.ru_maxrss
            if rss > 1 << 40:
                rss //= 1024 * 1024  # bytes → MB
            elif rss > 1 << 30:
                rss //= 1024  # KB → MB
            out["rss_mb"] = round(float(rss), 1)
        except Exception:
            pass
    return out


async def _lag_loop() -> None:
    expected = time.monotonic() + 1.0
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        _LAG_SAMPLES.append(max(0.0, now - expected))
        expected = now + 1.0


async def _report_loop(interval: float = 60.0) -> None:
    """Раз в interval секунд — одна JSON-строка PERF-метрик."""
    while True:
        try:
            await asyncio.sleep(interval)
            await _emit_report()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # мониторинг не должен ронять бота
            logger.warning("perf_metrics report failed: %s", exc)


async def _emit_report() -> None:
    lag = list(_LAG_SAMPLES)
    lag_stats: dict[str, Any] = {}
    if lag:
        lag_stats = {
            "loop_lag_avg_ms": round(1000 * sum(lag) / len(lag), 1),
            "loop_lag_max_ms": round(1000 * max(lag), 1),
            "loop_lag_p95_ms": (
                round(1000 * _percentile(lag, 95), 1)
                if _percentile(lag, 95) is not None and len(lag) >= 20
                else None
            ),
            "lag_gt_100ms": sum(1 for x in lag if x > 0.1),
            "lag_gt_500ms": sum(1 for x in lag if x > 0.5),
        }

    metrics: dict[str, Any] = {
        "event": "perf_report",
        "ts": int(time.time()),
        **_process_stats(),
        **lag_stats,
        "asyncio_tasks": _safe(lambda: len(asyncio.all_tasks())),
    }

    # Счётчики за интервал: сбрасываем после чтения (rate, не total)
    counters = dict(_COUNTERS)
    _COUNTERS.clear()
    metrics["counters"] = counters

    # Тайминги — кумулятивное окно (deque), не сбрасываем
    timings = {k: _timing_stats(v) for k, v in _TIMINGS.items()}
    metrics["timings_ms"] = timings

    # Gauges
    gauges: dict[str, Any] = {}
    for name, fn in _GAUGES.items():
        try:
            gauges[name] = fn()
        except Exception:
            gauges[name] = None
    metrics["gauges"] = gauges

    try:
        logger.info("PERF %s", json.dumps(metrics, ensure_ascii=False))
    except Exception:
        pass


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _wrap_discord_http(bot) -> None:
    """Подсчитать Discord API вызовы и 429 через bot.http.request.

    Не меняет rate-limit поведение discord.py — только наблюдает
    за ответами. Один раз на процесс.
    """
    global _wrapped_http
    if _wrapped_http:
        return
    http = getattr(bot, "http", None)
    if http is None:
        return
    orig = getattr(http, "request", None)
    if orig is None or getattr(orig, "_rz3_wrapped", False):
        return

    async def wrapped(*args, **kwargs):
        try:
            resp = await orig(*args, **kwargs)
        except Exception:
            incr("discord_http_errors")
            raise
        status = getattr(resp, "status", None)
        incr("discord_api_calls")
        if status == 429:
            incr("discord_429")
        elif status is not None and status >= 400:
            incr("discord_http_errors")
        return resp

    wrapped._rz3_wrapped = True  # type: ignore[attr-defined]
    try:
        http.request = wrapped
        _wrapped_http = True
    except Exception:
        pass


def start(bot=None) -> None:
    """Запустить мониторинг (lag-сэмплер + reporter). Идемпотентно."""
    global _report_task, _lag_task
    if not _enabled():
        return
    try:
        if bot is not None:
            _wrap_discord_http(bot)
        if _report_task is None or _report_task.done():
            _report_task = asyncio.get_running_loop().create_task(
                _report_loop()
            )
            _report_task.set_name("perf_metrics_report")
        if _lag_task is None or _lag_task.done():
            _lag_task = asyncio.get_running_loop().create_task(_lag_loop())
            _lag_task.set_name("perf_metrics_lag")
    except Exception as exc:
        logger.warning("perf_metrics start failed: %s", exc)


async def stop() -> None:
    """Остановить мониторинг при shutdown."""
    global _report_task, _lag_task
    for task in (_report_task, _lag_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    _report_task = _lag_task = None
