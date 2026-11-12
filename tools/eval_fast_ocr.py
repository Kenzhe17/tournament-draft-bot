"""Eval Fast OCR на наборе реальных скриншотов с эталонной разметкой.

Использование:
    python tools/eval_fast_ocr.py <img_dir> --ref <ref.json> [--iters N]

ref.json: {"<basename>": {"team_a":[{kills,deaths,assists,dmg}x4],
"team_b":[...], "score":[a,b]}}. Сопоставление по имени файла;
если не найдено — по совпадению aspect ratio (size в ref может быть
нормализованным).

Для каждого уникального изображения:
- fast parse + full parse, время, fallback;
- точность 32 числовых значений (8 K/D/A + 8 DMG) и счёта;
- имена (для ручной сверки — эталона по именам нет).

N итераций после warmup; отдельно fast_success / fallback_e2e /
full_only / e2e latency.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _load_refs(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _match_ref(name: str, refs: dict, img_size) -> tuple[str | None, dict | None]:
    if name in refs:
        return name, refs[name]
    # по aspect ratio (size в ref может быть даунскейлнут)
    if img_size:
        ar = img_size[0] / img_size[1]
        cands = []
        for k, v in refs.items():
            s = v.get("size")
            if s and abs(s[0] / s[1] - ar) < 0.01:
                cands.append(k)
        if len(cands) == 1:
            return cands[0], refs[cands[0]]
    return None, None


def _stats(players, key):
    return [
        (
            p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg")
        )
        for p in (players or {}).get(key) or []
    ]


def _ref_stats(entry):
    out = {}
    for key in ("team_a", "team_b"):
        out[key] = [
            (p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg"))
            for p in entry.get(key) or []
        ]
    return out


def _names(players, key):
    return [p.get("name") for p in (players or {}).get(key) or []]


def _pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("img_dir")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--iters", type=int, default=5)
    args = ap.parse_args()

    from PIL import Image

    from services import cs_parser, fast_cs_ocr
    from services.ocr_service import _run_ocr, warmup

    warmup()
    refs = _load_refs(Path(args.ref))
    files = sorted(
        p for p in Path(args.img_dir).iterdir()
        if p.suffix.lower() in IMG_EXT
    )

    rows = []
    all_fast, all_fb, all_full, all_e2e = [], [], [], []
    tot_num_ok = tot_num = 0
    n_fast_ok = n_fb = 0

    for p in files:
        data = p.read_bytes()
        with Image.open(p) as im:
            size = im.size
        ref_key, ref = _match_ref(p.name, refs, size)
        fast_t, fb_t, full_t, e2e_t = [], [], [], []
        num_ok = num_tot = 0
        score_ok = None
        last_fast = last_full = None
        for _ in range(args.iters):
            t0 = time.perf_counter()
            fp = fast_cs_ocr._extract_parsed(data)
            tf = time.perf_counter() - t0
            t0 = time.perf_counter()
            boxes = _run_ocr(data) or []
            pf = cs_parser.parse_cs_results(boxes) if boxes else None
            tl = time.perf_counter() - t0
            last_fast, last_full = fp, pf
            full_t.append(tl)
            if fp is None:
                n_fb += 1
                fb_t.append(tf + tl)
                e2e_t.append(tf + tl)
            else:
                n_fast_ok += 1
                fast_t.append(tf)
                e2e_t.append(tf)
            if ref:
                want = _ref_stats(ref)
                for key in ("team_a", "team_b"):
                    got = _stats(fp or {}, key)
                    for i in range(4):
                        for f in range(4):
                            num_tot += 1
                            g = got[i][f] if i < len(got) else None
                            w = want[key][i][f] if i < len(want[key]) else None
                            if g == w:
                                num_ok += 1
                if fp and ref.get("score") and fp.get("score") == ref["score"]:
                    score_ok = True
                elif fp is None:
                    score_ok = False
        tot_num_ok += num_ok
        tot_num += num_tot

        fb_rate = len(fb_t) / args.iters
        rows.append(
            {
                "file": p.name,
                "size": f"{size[0]}x{size[1]}",
                "ref": ref_key or "-",
                "fast_ok": n_fast_ok and last_fast is not None,
                "fast_p50": _pct(fast_t, 50) if fast_t else None,
                "fb_e2e_p50": _pct(fb_t, 50) if fb_t else None,
                "full_p50": _pct(full_t, 50),
                "e2e_p50": _pct(e2e_t, 50),
                "fb_rate": fb_rate,
                "num_acc": f"{num_ok}/{num_tot}" if ref else "-",
                "score": score_ok,
                "names_a": _names(last_fast or last_full or {}, "team_a"),
                "names_b": _names(last_fast or last_full or {}, "team_b"),
            }
        )
        all_fast += fast_t
        all_fb += fb_t
        all_full += full_t
        all_e2e += e2e_t

    print(f"{'file':<58} {'size':<11} {'fast':<5} {'full':<5} {'fb%':<5} "
          f"{'num':<11} {'fast_p50':<9} {'e2e_p50':<9} {'full_p50':<9}")
    for r in rows:
        print(
            f"{r['file']:<58} {r['size']:<11} "
            f"{'Y' if r['fast_ok'] else 'N':<5} "
            f"{'Y' if r['fast_ok'] or r['fb_rate']==0 else '?':<5} "
            f"{r['fb_rate']*100:>4.0f}% "
            f"{r['num_acc']:<11} "
            f"{(f'{r['fast_p50']:.2f}' if r['fast_p50'] is not None else '-'):<9} "
            f"{r['e2e_p50']:.2f}{'':<6} "
            f"{r['full_p50']:.2f}"
        )
        print(f"    ref={r['ref']} score_ok={r['score']}")
        print(f"    A: {r['names_a']}")
        print(f"    B: {r['names_b']}")

    n = args.iters * len(files)
    print(f"\n=== TOTAL {len(files)} unique images × {args.iters} iters ===")
    print(f"fast success : {n_fast_ok}/{n}  fallback: {n_fb}/{n} "
          f"({n_fb/n*100:.0f}%)")
    print(f"numeric acc  : {tot_num_ok}/{tot_num}")
    for name, vals in (
        ("fast_success", all_fast), ("fallback_e2e", all_fb),
        ("full_only", all_full), ("e2e", all_e2e),
    ):
        if vals:
            print(f"{name:14}: p50={_pct(vals,50):.2f}s "
                  f"p95={_pct(vals,95):.2f}s max={max(vals):.2f}s "
                  f"mean={sum(vals)/len(vals):.2f}s n={len(vals)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
