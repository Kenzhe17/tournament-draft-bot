"""Benchmark: anchor-based fast OCR vs полный RapidOCR pipeline.

Использование:
    python tools/bench_fast_ocr.py [--iters N] [--ref FILE] img [img...]

--ref FILE — JSON с эталонной статистикой (см.
tests/fixtures/ocr_reference.json): {"<basename>": {"team_a":[...],
"team_b":[...]}} — поля kills/deaths/assists/dmg сравниваются
по строкам. Без --ref точность оценивается совпадением с полным
парсером (full parse может сам ошибаться — см. отчёт).

Метрики на итерацию:
- fast_success: время _extract_parsed при успехе;
- fallback_e2e: fast_attempt + full_ocr + parse (реальный
  end-to-end при промахе fast-пути);
- full_only: время полного pipeline;
- e2e: фактическое end-to-end (fast ИЛИ fast+full).

Отчёт: p50/p95/max, доля fallback, точность K/D/A+DMG.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _players(parsed, key):
    out = []
    for p in parsed.get(key) or []:
        out.append(
            {
                "name": p.get("name"),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "dmg": p.get("dmg"),
            }
        )
    return out


def _stats_only(players):
    return [
        (p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg"))
        for p in players
    ]


def _accuracy(parsed, ref_entry) -> tuple[int, int, str]:
    """(совпало, всего, note) по 16 значениям K/D/A+DMG."""
    if parsed is None:
        return 0, 16, "fallback"
    ok = total = 0
    diffs = []
    for key in ("team_a", "team_b"):
        got = _players(parsed, key)
        want = ref_entry.get(key) or []
        for i in range(4):
            for field in ("kills", "deaths", "assists", "dmg"):
                total += 1
                g = got[i].get(field) if i < len(got) else None
                w = want[i].get(field) if i < len(want) else None
                if g == w:
                    ok += 1
                elif len(diffs) < 4:
                    diffs.append(f"{key}[{i}].{field}={g}!={w}")
    return ok, total, "; ".join(diffs)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(round((q / 100) * (len(s) - 1))))
    return s[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--ref", default="")
    args = ap.parse_args()

    refs = {}
    if args.ref:
        refs = json.loads(Path(args.ref).read_text(encoding="utf-8"))

    from services import cs_parser, fast_cs_ocr
    from services.ocr_service import _run_ocr, warmup

    warmup()

    fast_ok_t: list[float] = []
    fb_e2e_t: list[float] = []
    full_t: list[float] = []
    e2e_t: list[float] = []
    n_fb = n_ok = n_acc_ok = n_acc_total = 0

    for p in args.images:
        path = Path(p)
        data = path.read_bytes()
        ref_entry = refs.get(path.name)
        img_fast, img_fb, img_full, img_e2e = [], [], [], []
        for _ in range(args.iters):
            # fast attempt
            t0 = time.perf_counter()
            parsed_fast = fast_cs_ocr._extract_parsed(data)
            t_fast = time.perf_counter() - t0

            # full pipeline (нужен и для e2e-fallback, и для full_only)
            t0 = time.perf_counter()
            boxes = _run_ocr(data) or []
            parsed_full = (
                cs_parser.parse_cs_results(boxes) if boxes else None
            )
            t_full = time.perf_counter() - t0

            if parsed_fast is None:
                n_fb += 1
                img_fb.append(t_fast + t_full)
                img_e2e.append(t_fast + t_full)
                acc = (0, 16, "fallback")
            else:
                n_ok += 1
                img_fast.append(t_fast)
                img_e2e.append(t_fast)
                if ref_entry:
                    acc = _accuracy(parsed_fast, ref_entry)
                else:
                    same = _stats_only(_players(parsed_fast, "team_a")) == _stats_only(
                        _players(parsed_full or {}, "team_a")
                    ) and _stats_only(_players(parsed_fast, "team_b")) == _stats_only(
                        _players(parsed_full or {}, "team_b")
                    )
                    acc = (16 if same else 0, 16, "vs-full" if same else "DIFF")
            n_acc_ok += acc[0]
            n_acc_total += acc[1]
            if acc[2] not in ("", "fallback") and acc[0] < acc[1]:
                print(f"  {path.name}: acc {acc[0]}/{acc[1]} {acc[2]}")
            img_full.append(t_full)

        def _stat(name, vals):
            if not vals:
                return "-"
            return (f"p50={_pct(vals,50):.2f} p95={_pct(vals,95):.2f} "
                    f"max={max(vals):.2f} mean={sum(vals)/len(vals):.2f}")

        print(f"{path.name}:")
        print(f"  fast_success : {_stat('', img_fast)}  (n={len(img_fast)})")
        print(f"  fallback_e2e : {_stat('', img_fb)}  (n={len(img_fb)})")
        print(f"  full_only    : {_stat('', img_full)}")
        print(f"  e2e          : {_stat('', img_e2e)}")
        fast_ok_t += img_fast
        fb_e2e_t += img_fb
        full_t += img_full
        e2e_t += img_e2e

    n = n_ok + n_fb
    print(f"\n=== TOTAL ({n} runs) ===")
    print(f"fallback rate : {n_fb}/{n} = {n_fb/n*100:.0f}%"
          if n else "no runs")
    print(f"accuracy      : {n_acc_ok}/{n_acc_total} values")
    for name, vals in (
        ("fast_success", fast_ok_t), ("fallback_e2e", fb_e2e_t),
        ("full_only", full_t), ("e2e", e2e_t),
    ):
        if vals:
            print(f"{name:14}: p50={_pct(vals,50):.2f}s "
                  f"p95={_pct(vals,95):.2f}s max={max(vals):.2f}s "
                  f"mean={sum(vals)/len(vals):.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
