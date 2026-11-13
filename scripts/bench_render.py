"""Benchmark PNG-рендереров: chromium vs native.

Меряет: first render, avg/30, p95, cpu_time, RSS до/после,
параллелизм 1/2/5. Запуск: python scripts/bench_render.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import psutil

proc = psutil.Process(os.getpid())


def rss() -> float:
    return proc.memory_info().rss / 1024 / 1024


def stats(times):
    times = sorted(times)
    n = len(times)
    return {
        "avg": sum(times) / n,
        "p95": times[int(n * 0.95) - 1],
        "min": times[0],
        "max": times[-1],
    }


async def bench_profile(backend: str, n: int = 30):
    os.environ["PNG_RENDER_BACKEND"] = backend
    from utils import profile_renderer as pr
    from scripts.render_compare import _profiles, _avatar_png, _badge_png

    data = _profiles()["normal"]
    av, badge = _avatar_png(), _badge_png()

    # прогрев
    await pr.render_profile_png_async(data, av, badge)
    await pr.render_profile_png_async(data, av, badge)

    rss0 = rss()
    cpu0 = time.process_time()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        await pr.render_profile_png_async(data, av, badge)
        times.append(time.perf_counter() - t0)
    cpu_dt = time.process_time() - cpu0
    s = stats(times)
    print(f"[profile/{backend}] n={n} avg={s['avg']*1000:.0f}ms "
          f"p95={s['p95']*1000:.0f}ms min={s['min']*1000:.0f} "
          f"max={s['max']*1000:.0f} cpu={cpu_dt:.2f}s "
          f"rss {rss0:.0f}->{rss():.0f}MB")
    return times


async def bench_banner(backend: str, n: int = 30):
    os.environ["PNG_RENDER_BACKEND"] = backend
    from services.banner import renderer as br
    from scripts.render_compare import _avatar_png

    kw = dict(nickname="Kenzhe", avatar_png=_avatar_png(),
              voice_seconds=4 * 3600 + 27 * 60, matches=37,
              has_activity=True, report_day_label="Отчёт за 12.11")
    await br.render_banner_png(**kw)
    await br.render_banner_png(**kw)
    rss0 = rss()
    cpu0 = time.process_time()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        await br.render_banner_png(**kw)
        times.append(time.perf_counter() - t0)
    cpu_dt = time.process_time() - cpu0
    s = stats(times)
    print(f"[banner/{backend}] n={n} avg={s['avg']*1000:.0f}ms "
          f"p95={s['p95']*1000:.0f}ms min={s['min']*1000:.0f} "
          f"max={s['max']*1000:.0f} cpu={cpu_dt:.2f}s "
          f"rss {rss0:.0f}->{rss():.0f}MB")
    return times


async def bench_concurrent(backend: str, conc: int):
    os.environ["PNG_RENDER_BACKEND"] = backend
    from services.banner import renderer as br
    from scripts.render_compare import _avatar_png

    kw = dict(nickname="Kenzhe", avatar_png=_avatar_png(),
              voice_seconds=3600, matches=5, has_activity=True)
    await br.render_banner_png(**kw)  # прогрев
    t0 = time.perf_counter()
    await asyncio.gather(*[br.render_banner_png(**kw)
                           for _ in range(conc)])
    dt = time.perf_counter() - t0
    print(f"[banner/{backend} x{conc}] wall={dt*1000:.0f}ms "
          f"rss={rss():.0f}MB children={len(proc.children(recursive=True))}")


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("all", "profile"):
        await bench_profile("chromium")
        await bench_profile("native")
    if mode in ("all", "banner"):
        await bench_banner("chromium")
        await bench_banner("native")
    if mode in ("all", "conc"):
        for c in (1, 2, 5):
            await bench_concurrent("native", c)
        for c in (1, 2, 5):
            await bench_concurrent("chromium", c)
    from utils.profile_renderer import shutdown_profile_renderer
    await shutdown_profile_renderer()
    print(f"final rss={rss():.0f}MB "
          f"children={len(proc.children(recursive=True))}")


if __name__ == "__main__":
    asyncio.run(main())
