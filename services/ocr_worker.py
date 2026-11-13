"""Изолированный OCR worker-процесс.

Мотив: RapidOCR (det+cls+rec ONNX-сессии, кириллическая модель,
fast-det) держит ~500+ МБ RSS в основном процессе бота. Вся
ONNX-инференс-нагрузка переносится в дочерний процесс; родитель
получает только pickle-able результаты.

Гарантии:
- не более одного активного OCR-запроса (threading.Lock);
- воркер завершается после OCR_WORKER_IDLE_S простоя и
  перезапускается при следующем запросе;
- reaper не убивает воркер во время выполнения задачи;
- при падении/таймауте воркера вызывающий код получает
  OcrWorkerError и может откатиться на локальный инференс;
- задачи не теряются: idle-kill возможен только в простое.

Отключение: OCR_WORKER_DISABLE=1 → всё работает в процессе,
как раньше.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_CHILD_ENV = "Z3_OCR_WORKER_CHILD"
_IDLE_S = float(os.environ.get("OCR_WORKER_IDLE_S", "300"))
_REAPER_S = float(os.environ.get("OCR_WORKER_REAPER_S", "30"))
_TIMEOUT_S = float(os.environ.get("OCR_WORKER_TIMEOUT_S", "180"))


class OcrWorkerError(RuntimeError):
    """Worker недоступен/упал/таймаут — caller решает fallback."""


def is_worker_child() -> bool:
    return os.environ.get(_CHILD_ENV) == "1"


def worker_enabled() -> bool:
    return (
        os.environ.get("OCR_WORKER_DISABLE") != "1"
        and not is_worker_child()
    )


# --- parent side -----------------------------------------------------

_proc: mp.Process | None = None
_task_q: mp.Queue | None = None
_res_q: mp.Queue | None = None
_seq = itertools.count(1)
_lock = threading.Lock()          # один запрос за раз
_last_used = 0.0
_reaper_started = False


def _ensure_proc() -> None:
    """Spawn worker при необходимости. Вызывать под _lock."""
    global _proc, _task_q, _res_q
    if _proc is not None and _proc.is_alive():
        return
    ctx = mp.get_context("spawn")
    _task_q = ctx.Queue()
    _res_q = ctx.Queue()
    _proc = ctx.Process(
        target=_child_main,
        args=(_task_q, _res_q),
        name="z3-ocr-worker",
        daemon=True,
    )
    _proc.start()
    _start_reaper()
    logger.info("OCR worker spawned pid=%s", _proc.pid)


def _kill_proc() -> None:
    global _proc, _task_q, _res_q
    proc = _proc
    _proc = _task_q = _res_q = None
    if proc is not None and proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()


def call(
    kind: str,
    *args: Any,
    timeout: float | None = None,
    retries: int = 1,
) -> Any:
    """Синхронный вызов worker'а. Блокирует до результата.

    Вызывать из executor-потока (OCR_EXECUTOR), не из event loop.

    retries — число ПОВТОРНЫХ попыток после сбоя: при таймауте,
    падении child или ошибке задачи воркер убивается и
    перезапускается, затем запрос повторяется. ONNX-модели в
    основной процесс не загружаются — при недоступности worker'а
    caller получает OcrWorkerError.
    """
    global _last_used
    if not worker_enabled():
        raise OcrWorkerError("worker disabled")
    if is_worker_child():
        # Защита от рекурсии: в child диспатчим напрямую.
        return _dispatch(kind, args)
    attempts = max(0, retries) + 1
    last_exc: OcrWorkerError | None = None
    for attempt in range(attempts):
        try:
            with _lock:
                _ensure_proc()
                seq = next(_seq)
                _task_q.put((seq, kind, args))
                _last_used = time.time()
                try:
                    got_seq, ok, payload = _res_q.get(
                        timeout=timeout or _TIMEOUT_S
                    )
                except queue.Empty:
                    raise OcrWorkerError(
                        f"worker timeout kind={kind}"
                    )
                if got_seq != seq:
                    # Рассинхрон (не должно случаться при одном
                    # клиенте) — считаем worker битым.
                    raise OcrWorkerError("worker seq mismatch")
                _last_used = time.time()
                if not ok:
                    raise OcrWorkerError(str(payload))
                return payload
        except OcrWorkerError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            logger.warning(
                "OCR worker call failed (%s) — restart+retry %d/%d",
                exc, attempt + 1, attempts,
            )
            # Убиваем возможно зависший child и повторяем.
            with _lock:
                _kill_proc()
            time.sleep(0.2 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def warmup() -> None:
    """Прогрев worker'а: загружает ONNX-модели в дочерний процесс."""
    if not worker_enabled():
        return
    try:
        call("warmup", timeout=300)
    except OcrWorkerError:
        logger.warning("OCR worker warmup failed", exc_info=True)


def shutdown() -> None:
    """Остановить worker (например, при shutdown бота)."""
    global _reaper_started
    with _lock:
        if _task_q is not None:
            try:
                _task_q.put(None)
            except Exception:
                pass
        _kill_proc()


def _reaper_loop() -> None:
    while True:
        # Короткий шаг: интервал перечитывается каждую итерацию,
        # длинный sleep «заморозил» бы подстроенные в тестах
        # _REAPER_S/_IDLE_S до конца текущего цикла.
        time.sleep(min(_REAPER_S, 1.0))
        if not worker_enabled():
            continue
        # Неблокирующий захват: занят → пропускаем тик.
        if not _lock.acquire(blocking=False):
            continue
        try:
            proc = _proc
            if (
                proc is not None
                and proc.is_alive()
                and time.time() - _last_used > _IDLE_S
            ):
                logger.info(
                    "OCR worker idle %.0fs — terminating",
                    time.time() - _last_used,
                )
                _kill_proc()
        finally:
            _lock.release()


def _start_reaper() -> None:
    global _reaper_started
    if not _reaper_started:
        _reaper_started = True
        t = threading.Thread(
            target=_reaper_loop, name="ocr-worker-reaper", daemon=True
        )
        t.start()


# --- child side ------------------------------------------------------


def _dispatch(kind: str, args: tuple) -> Any:
    """Выполняется ВНУТРИ worker-процесса."""
    os.environ[_CHILD_ENV] = "1"

    if kind == "echo":
        # Тестовый/diagnostic kind: возвращает аргументы как есть.
        return args

    if kind == "warmup":
        from services import fast_cs_ocr, ocr_service

        ocr_service._get_ocr()
        ocr_service._get_cyrillic_ocr()
        fast_cs_ocr._get_fast_det()
        return True

    if kind == "cs_fast":
        from services import fast_cs_ocr

        t0 = time.perf_counter()
        reasons: list[str] = []
        parsed = fast_cs_ocr._extract_parsed(
            args[0], reason_out=reasons
        )
        wall_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "parsed": parsed,
            "reason": reasons[-1] if reasons else None,
            "wall_ms": wall_ms,
            "ok": parsed is not None,
        }

    if kind == "ocr_full":
        from services.ocr_service import _run_ocr_local

        return _run_ocr_local(*args)

    if kind == "ocr_cyr_region":
        from services.ocr_service import _run_ocr_cyrillic_region_local

        return _run_ocr_cyrillic_region_local(*args)

    if kind == "ocr_cyr_regions":
        from services.ocr_service import _run_ocr_cyrillic_regions_local

        return _run_ocr_cyrillic_regions_local(*args)

    raise ValueError(f"unknown OCR worker kind: {kind}")


def _child_main(task_q: mp.Queue, res_q: mp.Queue) -> None:
    os.environ[_CHILD_ENV] = "1"
    logger.info("OCR worker child started pid=%s", os.getpid())
    while True:
        try:
            item = task_q.get()
        except (EOFError, OSError):
            break
        if item is None:
            break
        seq, kind, args = item
        try:
            res_q.put((seq, True, _dispatch(kind, args)))
        except Exception as e:  # noqa: BLE001 — отчёт родителю
            logger.exception("OCR worker task failed kind=%s", kind)
            try:
                res_q.put((seq, False, f"{type(e).__name__}: {e}"))
            except Exception:
                break
    logger.info("OCR worker child exiting pid=%s", os.getpid())
