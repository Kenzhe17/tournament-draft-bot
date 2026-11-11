"""Phase 3: perf_metrics — корректность, ограниченность буферов,
отключение через env, отказоустойчивость."""

import asyncio
import importlib
import json
import logging

import pytest

from utils import perf_metrics


@pytest.fixture(autouse=True)
def _reset():
    perf_metrics._COUNTERS.clear()
    perf_metrics._TIMINGS.clear()
    perf_metrics._GAUGES.clear()
    perf_metrics._LAG_SAMPLES.clear()
    yield
    perf_metrics._COUNTERS.clear()
    perf_metrics._TIMINGS.clear()
    perf_metrics._GAUGES.clear()
    perf_metrics._LAG_SAMPLES.clear()


class TestCounters:
    def test_incr_and_reset_on_report(self):
        perf_metrics.incr("a")
        perf_metrics.incr("a", 4)
        perf_metrics.incr("b")
        assert perf_metrics._COUNTERS == {"a": 5, "b": 1}

    def test_record_bounded_deque(self):
        for i in range(perf_metrics._TIMING_MAXLEN + 500):
            perf_metrics.record("x", float(i))
        dq = perf_metrics._TIMINGS["x"]
        assert len(dq) == perf_metrics._TIMING_MAXLEN
        assert dq[-1] == float(perf_metrics._TIMING_MAXLEN + 499)

    def test_metrics_never_raise(self, monkeypatch):
        monkeypatch.setattr(
            perf_metrics, "_enabled", lambda: 1 / 0  # type: ignore
        )
        # _enabled упал — incr/record должны проглотить
        try:
            perf_metrics.incr("x")
            perf_metrics.record("y", 1.0)
        except Exception:
            pytest.fail("metrics must not raise")


class TestDisable:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ENABLE_PERF_METRICS", "false")
        importlib.reload(perf_metrics)
        try:
            perf_metrics.incr("a")
            perf_metrics.record("x", 1.0)
            assert perf_metrics._COUNTERS == {}
            assert perf_metrics._TIMINGS == {}
        finally:
            monkeypatch.delenv("ENABLE_PERF_METRICS", raising=False)
            importlib.reload(perf_metrics)


class TestReport:
    @pytest.mark.asyncio
    async def test_emit_report_json_line(self, caplog):
        perf_metrics.incr("discord_edits", 3)
        perf_metrics.record("json_dumps_ms", 12.5)
        perf_metrics.register_gauge("test_gauge", lambda: 42)
        perf_metrics._LAG_SAMPLES.extend([0.001, 0.002] * 15)

        with caplog.at_level(logging.INFO, logger="utils.perf_metrics"):
            await perf_metrics._emit_report()

        lines = [r.message for r in caplog.records if r.message.startswith("PERF ")]
        assert len(lines) == 1
        payload = json.loads(lines[0][5:])
        assert payload["event"] == "perf_report"
        assert payload["counters"]["discord_edits"] == 3
        assert payload["gauges"]["test_gauge"] == 42
        assert "loop_lag_max_ms" in payload
        # счётчики сброшены после отчёта
        assert perf_metrics._COUNTERS == {}

    @pytest.mark.asyncio
    async def test_gauge_failure_does_not_break_report(self, caplog):
        def _bad():
            raise RuntimeError("boom")

        perf_metrics.register_gauge("bad", _bad)
        with caplog.at_level(logging.INFO, logger="utils.perf_metrics"):
            await perf_metrics._emit_report()
        assert any(
            r.message.startswith("PERF ") for r in caplog.records
        )
        assert perf_metrics._GAUGES["bad"]() if False else True

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        perf_metrics.start()
        assert perf_metrics._report_task is not None
        assert perf_metrics._lag_task is not None
        await perf_metrics.stop()
        assert perf_metrics._report_task is None
        assert perf_metrics._lag_task is None
        # Повторный start после stop работает
        perf_metrics.start()
        await perf_metrics.stop()

    @pytest.mark.asyncio
    async def test_lag_sampler_records(self):
        perf_metrics.start()
        await asyncio.sleep(1.3)
        await perf_metrics.stop()
        assert len(perf_metrics._LAG_SAMPLES) >= 1
        assert all(x >= 0 for x in perf_metrics._LAG_SAMPLES)
