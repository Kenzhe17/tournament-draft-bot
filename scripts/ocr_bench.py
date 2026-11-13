"""Bench Fast/Full OCR внутри контейнера (Railway SSH).

Использование (внутри сервиса core-staging):

    railway ssh --service core-staging -- \
        python scripts/ocr_bench.py --image /app/test_1920.png --n 5

Переменные окружения ONNX читаются при первом вызове worker'а —
задавать ДО запуска скрипта:

    OCR_INTRA_OP_THREADS=2 OCR_INTER_OP_THREADS=2 \
    FAST_DET_THREADS=2 python scripts/ocr_bench.py ...

Холодный прогон = первый вызов (spawn worker + load моделей);
последующие --n — тёплые. Печатает stage-тайминги каждого
прогона и cgroup memory.current/peak.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _cgroup() -> dict:
    out = {}
    for name, paths in (
        ("memory.current", ("/sys/fs/cgroup/memory.current",)),
        ("memory.peak", ("/sys/fs/cgroup/memory.peak",)),
        ("memory.max", ("/sys/fs/cgroup/memory.max",)),
    ):
        for p in paths:
            try:
                out[name] = Path(p).read_text().strip()
                break
            except OSError:
                continue
    return out


def _proc_rss_mb() -> float:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return -1.0


async def _run_once(data: bytes, mode: str) -> dict:
    if mode == "fast":
        from services import fast_cs_ocr

        t0 = time.perf_counter()
        parsed = await fast_cs_ocr.parse_cs_fast(data)
        wall = (time.perf_counter() - t0) * 1000
        return {
            "wall_ms": wall,
            "ok": parsed is not None,
            "timing": (parsed or {}).get("_ocr_timing") or {},
        }
    # full/compare — через боевой pipeline без Discord-частей
    from cogs.ocr_upload import _classic_pipeline

    t0 = time.perf_counter()
    res = await _classic_pipeline(
        data, need_score=True, fast=(mode == "compare")
    )
    wall = (time.perf_counter() - t0) * 1000
    return {
        "wall_ms": wall,
        "ok": res is not None,
        "timing": (res or {}).get("ocr_timing") or {},
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--mode", default="fast",
                    choices=["fast", "full", "compare"])
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    data = Path(args.image).read_bytes()
    print(f"image={args.image} bytes={len(data)} mode={args.mode} n={args.n}")
    print("env threads:", {
        k: os.environ.get(k, "-1")
        for k in (
            "OCR_INTRA_OP_THREADS", "OCR_INTER_OP_THREADS",
            "FAST_DET_THREADS", "OCR_CYR_INTRA_OP_THREADS",
            "OCR_CYR_INTER_OP_THREADS", "OCR_WORKER_DISABLE",
        )
    })
    print("cgroup@start:", _cgroup())

    runs = []
    for i in range(args.n):
        t0 = time.perf_counter()
        r = await _run_once(data, args.mode)
        r["iter_wall_ms"] = (time.perf_counter() - t0) * 1000
        runs.append(r)
        t = r["timing"]
        print(
            f"[{i}] ok={r['ok']} wall={r['iter_wall_ms']:.0f}ms "
            f"queue={t.get('executor_queue_ms', 0) + t.get('worker_lock_wait_ms', 0):.0f} "
            f"det={(t.get('decode_ms') or 0) + (t.get('det_ms') or 0):.0f} "
            f"rec={t.get('recognition_ms')} "
            f"parse={t.get('parse_ms')} "
            f"rtt={t.get('worker_roundtrip_ms')} "
            f"refine={t.get('refine_ms')}"
        )
        print("    cgroup:", _cgroup())

    # summary P50/P95 по warm-прогонам (пропускаем первый — cold)
    warm = [r["iter_wall_ms"] for r in runs[1:]] or [
        r["iter_wall_ms"] for r in runs
    ]
    warm.sort()

    def pct(p: float) -> float:
        if not warm:
            return 0.0
        k = min(len(warm) - 1, int(round(p / 100 * (len(warm) - 1))))
        return warm[k]

    summary = {
        "mode": args.mode,
        "n": len(runs),
        "cold_ms": runs[0]["iter_wall_ms"],
        "warm_p50_ms": pct(50),
        "warm_p95_ms": pct(95),
        "warm_max_ms": warm[-1] if warm else 0,
        "proc_maxrss_mb": _proc_rss_mb(),
        "cgroup": _cgroup(),
        "runs": runs,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"},
                     ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("saved:", args.out)


if __name__ == "__main__":
    asyncio.run(main())
