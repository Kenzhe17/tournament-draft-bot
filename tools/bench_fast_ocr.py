"""Benchmark: полный RapidOCR pipeline vs anchor-based fast OCR.

Использование:
    python tools/bench_fast_ocr.py screen1.png screen2.png ...

Для каждого изображения:
- полный путь: _run_ocr (det+cls+rec) + parse_cs_results;
- fast путь: fast_cs_ocr._extract_parsed;
- сравнение: время, число боксов/rec-вызовов, совпадение игроков
  (имя/kills/deaths/assists/dmg по строкам).

Запускать на машине с прогретыми моделями (warmup вызывается сам).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _players(parsed, key):
    out = []
    for p in parsed.get(key) or []:
        out.append(
            (
                p.get("name"),
                p.get("kills"),
                p.get("deaths"),
                p.get("assists"),
                p.get("dmg"),
            )
        )
    return out


def _agree(full, fast) -> tuple[bool, str]:
    if fast is None:
        return False, "fast→fallback"
    diffs = []
    for side, fk, kk in (("A", "team_a", "team_a"), ("B", "team_b", "team_b")):
        a = _players(full, fk)
        b = _players(fast, kk)
        if len(a) != len(b):
            diffs.append(f"{side}: rows {len(a)}!={len(b)}")
            continue
        for i, (pa, pb) in enumerate(zip(a, b)):
            if pa != pb:
                diffs.append(f"{side}[{i}] {pa}!={pb}")
    score_f = full.get("score")
    score_x = fast.get("score")
    if score_f != score_x:
        diffs.append(f"score {score_f}!={score_x}")
    return (not diffs), "; ".join(diffs[:4])


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print("usage: bench_fast_ocr.py <img> [<img>...]")
        return 2

    from services import cs_parser, fast_cs_ocr
    from services.ocr_service import _run_ocr, warmup

    warmup()
    print(f"{'image':38} {'full_s':>7} {'fast_s':>7} {'spd':>5} "
          f"{'boxes':>5} {'agree':>5}  note")
    tot_full = tot_fast = 0.0
    n_ok = n_fb = 0
    for p in paths:
        data = Path(p).read_bytes()
        # полный путь
        t0 = time.perf_counter()
        boxes = _run_ocr(data) or []
        t_det_rec = time.perf_counter() - t0
        parsed_full = cs_parser.parse_cs_results(boxes) if boxes else None
        t_full = time.perf_counter() - t0

        # fast путь
        t0 = time.perf_counter()
        parsed_fast = fast_cs_ocr._extract_parsed(data)
        t_fast = time.perf_counter() - t0

        tot_full += t_full
        tot_fast += t_fast
        if parsed_fast is None:
            n_fb += 1
            agree, note = False, "fallback"
        else:
            n_ok += 1
            if parsed_full is None:
                agree, note = False, "full=parse fail"
            else:
                agree, note = _agree(parsed_full, parsed_fast)
        spd = t_full / t_fast if t_fast > 0 else 0
        print(
            f"{Path(p).name:38} {t_full:7.2f} {t_fast:7.2f} "
            f"{spd:5.2f} {len(boxes):5} {str(agree):>5}  {note}"
        )
    n = len(paths)
    print(f"\nTOTAL: full={tot_full:.2f}s fast={tot_fast:.2f}s "
          f"speedup={tot_full / tot_fast if tot_fast else 0:.2f}x "
          f"ok={n_ok} fallback={n_fb}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
