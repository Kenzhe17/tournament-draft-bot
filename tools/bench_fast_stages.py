"""Benchmark стадий Adaptive Fast OCR + thread-config sweep.

Usage:
    python tools/bench_fast_stages.py <img_dir> [--ref ref.json]
        [--iters N] [--threads T] [--det-threads T] [--conc C]

Печатает per-stage тайминги (из utils.perf_metrics) по каждому
изображению и сводку p50/p95. --threads пересоздаёт rec-движок
с intra_op_num_threads=T, --det-threads — det-only движок.
--conc N — N параллельных вызовов через OCR_EXECUTOR.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def _dump_timings(tag, perf):
    rows = []
    for name, dq in sorted(perf._TIMINGS.items()):
        if not name.startswith("fast_ocr_"):
            continue
        vals = list(dq)
        if not vals:
            continue
        rows.append(
            f"    {name:32s} n={len(vals):3d} "
            f"p50={_pct(vals,50):8.1f} p95={_pct(vals,95):8.1f} "
            f"max={max(vals):8.1f}"
        )
    if rows:
        print(f"  [{tag}]")
        print("\n".join(rows))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("img_dir")
    ap.add_argument("--iters", type=int, default=1)
    ap.add_argument("--threads", type=int, default=None,
                    help="intra_op_num_threads для rec/full движка")
    ap.add_argument("--det-threads", type=int, default=None)
    ap.add_argument("--conc", type=int, default=1)
    args = ap.parse_args()

    if args.threads is not None:
        import os
        os.environ["OCR_INTRA_OP_THREADS"] = str(args.threads)
    if args.det_threads is not None:
        import os
        os.environ["FAST_DET_THREADS"] = str(args.det_threads)

    from services import fast_cs_ocr, ocr_service
    from services.ocr_service import OCR_EXECUTOR, warmup
    from utils import perf_metrics

    warmup()

    files = sorted(
        p for p in Path(args.img_dir).iterdir()
        if p.suffix.lower() in IMG_EXT
    )
    if not files:
        print("no images")
        return

    async def one(p: Path):
        data = p.read_bytes()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            OCR_EXECUTOR, fast_cs_ocr._extract_parsed, data
        )

    # прогревочный прогон на первом файле
    await one(files[0])
    perf_metrics._TIMINGS.clear()

    t_all = []
    ok = 0
    for p in files:
        for _ in range(args.iters):
            t0 = time.perf_counter()
            res = await one(p)
            dt = (time.perf_counter() - t0) * 1000
            t_all.append(dt)
            ok += res is not None
    print(
        f"files={len(files)} iters={args.iters} conc={args.conc} "
        f"threads={args.threads} det_threads={args.det_threads} "
        f"ok={ok}/{len(t_all)} "
        f"total_ms p50={_pct(t_all,50):.0f} "
        f"p95={_pct(t_all,95):.0f} max={max(t_all):.0f}"
    )
    _dump_timings("stages", perf_metrics)

    # --- concurrency probe ---
    if args.conc > 1:
        perf_metrics._TIMINGS.clear()
        data = files[0].read_bytes()
        t0 = time.perf_counter()
        await asyncio.gather(
            *(one(files[0]) for _ in range(args.conc))
        )
        wall = (time.perf_counter() - t0) * 1000
        print(f"  [conc={args.conc} same-file] wall={wall:.0f}ms")
        _dump_timings("conc", perf_metrics)


if __name__ == "__main__":
    asyncio.run(main())
