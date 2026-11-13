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


def test_worker_retry_restarts_child():
    """Ошибка задачи → kill+respawn+retry; после retries — OcrWorkerError."""
    ocr_worker.call("echo", "warm")
    pid1 = ocr_worker._proc.pid
    # no_such_kind: child отвечает ok=False → OcrWorkerError → retry.
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("no_such_kind", retries=1)
    # После retry-попыток child был перезапущен.
    assert ocr_worker._proc is not None
    assert ocr_worker._proc.pid != pid1
    # Воркер жив и отвечает после рестарта.
    assert ocr_worker.call("echo", "after") == ("after",)


def test_call_retries_on_transport_failure(monkeypatch):
    """Искусственный транспортный сбой → автоматический retry."""
    calls = {"n": 0}
    real_call = ocr_worker.call

    # Имитируем: первый вызой падает по таймауту через подмену _res_q.get
    # — проще проверить счётчик перезапусков через kill.
    killed = []
    real_kill = ocr_worker._kill_proc

    def spy_kill():
        killed.append(1)
        real_kill()

    monkeypatch.setattr(ocr_worker, "_kill_proc", spy_kill)
    # no_such_kind с retries=2 → 3 попытки, ≥2 kill.
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("no_such_kind", retries=2)
    assert len(killed) >= 2


def test_final_transport_failure_kills_child(monkeypatch):
    """Исчерпание retry на таймауте → зависший child убит,
    IPC-ресурсы освобождены (_proc/_task_q/_res_q = None) —
    не ждём idle-репера."""
    import queue as _queue

    real_ensure = ocr_worker._ensure_proc

    class _HungResQ:
        def put(self, *a, **k):
            pass

        def get(self, timeout=None):
            raise _queue.Empty()

    def fake_ensure():
        real_ensure()
        ocr_worker._res_q = _HungResQ()

    monkeypatch.setattr(ocr_worker, "_ensure_proc", fake_ensure)
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("echo", "x", retries=1)
    assert ocr_worker._proc is None
    assert ocr_worker._task_q is None
    assert ocr_worker._res_q is None


def test_task_error_keeps_healthy_child():
    """Ошибка уровня задачи (ok=False) — НЕ транспортный сбой:
    после финальной неудачи здоровый child НЕ убивается."""
    ocr_worker.call("echo", "warm")
    proc = ocr_worker._proc
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_worker.call("no_such_kind", retries=0)
    # child жив — следующий вызов не платит за respawn+загрузку.
    assert ocr_worker._proc is proc
    assert ocr_worker.call("echo", "after") == ("after",)


def test_run_ocr_propagates_worker_error(monkeypatch):
    """OcrWorkerError не маскируется пустым результатом:
    [] — валидный ответ OCR, недоступный worker — исключение."""
    from services import ocr_service

    monkeypatch.setattr(ocr_worker, "worker_enabled", lambda: True)

    def boom(*a, **k):
        raise ocr_worker.OcrWorkerError("dead")

    monkeypatch.setattr(ocr_worker, "call", boom)
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_service._run_ocr(b"png")
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_service._run_ocr_cyrillic_region(b"png", (0, 0, 1, 1))
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_service._run_ocr_cyrillic_regions(b"png", [(0, 0, 1, 1)])


def test_run_ocr_empty_result_still_valid(monkeypatch):
    """Пустой список от worker'а — валидный результат, не ошибка."""
    from services import ocr_service

    monkeypatch.setattr(ocr_worker, "worker_enabled", lambda: True)
    monkeypatch.setattr(ocr_worker, "call", lambda *a, **k: [])
    assert ocr_service._run_ocr(b"png") == []


def test_ocr_executor_queue_bounded(monkeypatch):
    """OCR_EXECUTOR: не более OCR_MAX_PENDING задач
    (выполняемых + ожидающих) — избыток = OcrWorkerError."""
    import threading

    from services import ocr_service

    monkeypatch.setattr(ocr_service, "_OCR_MAX_PENDING", 1)
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return []

    f = ocr_service.OCR_EXECUTOR.submit(slow)
    try:
        with pytest.raises(
            ocr_worker.OcrWorkerError, match="queue is full"
        ):
            ocr_service.OCR_EXECUTOR.submit(lambda: [])
    finally:
        gate.set()
        f.result(timeout=10)
    # Слот освобождён по done_callback — новые задачи проходят.
    f2 = ocr_service.OCR_EXECUTOR.submit(lambda: 1)
    assert f2.result(timeout=10) == 1


def test_call_stats_filled():
    """stats получает lock_wait_ms/ipc_ms по итогам вызова."""
    stats: dict = {}
    res = ocr_worker.call("echo", 1, stats=stats)
    assert res == (1,)
    assert "lock_wait_ms" in stats and stats["lock_wait_ms"] >= 0
    assert "ipc_ms" in stats and stats["ipc_ms"] >= 0


def test_waiters_bounded(monkeypatch):
    """Очередь ожидающих ограничена: второй ждущий → OcrWorkerBusy."""
    import threading

    monkeypatch.setattr(ocr_worker, "_MAX_WAITERS", 1)
    ocr_worker._lock.acquire()  # worker «занят» — вызовы ждут
    done: list = []
    errors: list = []

    def first():
        try:
            ocr_worker.call("echo", "a")
            done.append("first")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=first, daemon=True)
    t1.start()
    time.sleep(0.3)  # первый уже в _WAITING
    with pytest.raises(ocr_worker.OcrWorkerBusy):
        ocr_worker.call("echo", "b", retries=0)
    ocr_worker._lock.release()
    t1.join(timeout=10)
    assert done == ["first"] and not errors


def test_low_priority_waits_for_high(monkeypatch):
    """Low-priority вызов не проходит admission, пока ждёт high."""
    import threading

    order: list[str] = []
    ocr_worker._lock.acquire()

    def low():
        ocr_worker.call("echo", "low", priority=-1, retries=0)
        order.append("low")

    def high():
        ocr_worker.call("echo", "high", priority=1, retries=0)
        order.append("high")

    # High заходит в ожидание ПЕРВЫМ — low должен ждать в
    # _wait_cond, пока _HI_WAITING > 0, и не конкурировать за _lock.
    th = threading.Thread(target=high, daemon=True)
    th.start()
    time.sleep(0.2)
    tl = threading.Thread(target=low, daemon=True)
    tl.start()
    time.sleep(0.3)
    ocr_worker._lock.release()
    th.join(timeout=15)
    tl.join(timeout=15)
    assert order == ["high", "low"]


def test_ocr_regions_single_worker_call(monkeypatch):
    """_run_ocr_regions → один worker-вызов kind=ocr_regions
    на весь batch (не N round-trip'ов)."""
    from services import ocr_service

    calls: list = []

    def fake_call(kind, *args, **kwargs):
        calls.append((kind, args))
        return [[{"text": "x"}] for _ in args[1]]

    monkeypatch.setattr(ocr_worker, "worker_enabled", lambda: True)
    monkeypatch.setattr(ocr_worker, "call", fake_call)
    regions = [(0, 0, 10, 10), (20, 20, 30, 30)]
    res = ocr_service._run_ocr_regions(b"img", regions, priority=-1)
    assert len(calls) == 1
    assert calls[0][0] == "ocr_regions"
    assert calls[0][1] == (b"img", regions)
    assert calls[0] and res == [[{"text": "x"}], [{"text": "x"}]]


def test_ocr_regions_bad_payload_raises(monkeypatch):
    from services import ocr_service

    monkeypatch.setattr(ocr_worker, "worker_enabled", lambda: True)
    monkeypatch.setattr(
        ocr_worker, "call", lambda *a, **k: "not-a-list"
    )
    with pytest.raises(ocr_worker.OcrWorkerError):
        ocr_service._run_ocr_regions(b"img", [(0, 0, 1, 1)])
