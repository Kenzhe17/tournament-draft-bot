"""Тесты жизненного цикла OCR worker-процесса.

Не грузят ONNX-модели — используют kind="echo" для проверки
spawn/roundtrip/error-path, idle-kill репера и shutdown.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import ocr_worker


@pytest.fixture(autouse=True)
def _clean_worker():
    ocr_worker.shutdown()
    yield
    ocr_worker.shutdown()


def test_worker_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OCR_WORKER_DISABLE", "1")
    assert not ocr_worker.worker_enabled()
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("echo", 1)


def test_worker_roundtrip():
    res = ocr_worker.call("echo", 42, "привет", [1, 2])
    assert res == (42, "привет", [1, 2])


def test_worker_unknown_kind_raises():
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("no_such_kind")


def test_worker_idle_reaper_kills(monkeypatch):
    monkeypatch.setattr(ocr_worker, "_IDLE_S", 0.05)
    monkeypatch.setattr(ocr_worker, "_REAPER_S", 0.05)
    ocr_worker.call("echo", "warm")
    proc = ocr_worker._proc
    assert proc is not None and proc.is_alive()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not proc.is_alive():
            break
        time.sleep(0.05)
    assert not proc.is_alive(), "idle worker не завершён репером"


def test_worker_respawns_after_idle_kill(monkeypatch):
    monkeypatch.setattr(ocr_worker, "_IDLE_S", 0.05)
    monkeypatch.setattr(ocr_worker, "_REAPER_S", 0.05)
    ocr_worker.call("echo", 1)
    time.sleep(0.4)  # reaper убьёт
    # Следующий вызов — новый процесс, задача не теряется.
    res = ocr_worker.call("echo", 2)
    assert res == (2,)
    assert ocr_worker._proc is not None and ocr_worker._proc.is_alive()


def test_shutdown_terminates():
    ocr_worker.call("echo", "x")
    ocr_worker.shutdown()
    assert ocr_worker._proc is None
