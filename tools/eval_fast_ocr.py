"""Eval Fast OCR на наборе реальных скриншотов с эталонной разметкой.

Использование:
    python tools/eval_fast_ocr.py <img_dir> --ref <ref.json> [--iters N]
        [--executor]

ref.json: {"<basename>": {"team_a":[{kills,deaths,assists,dmg}x4],
"team_b":[...], "score":[a,b], "size":[w,h]}}.

Сопоставление эталона:
- точное совпадение имени файла — основной путь;
- иначе — точное совпадение PIL-размера (size в ref);
- иначе — SKIP с явным сообщением (без тихого aspect-fallback).

Для каждого уникального изображения:
- fast parse и full parse отдельно (без чередования — исключаем
  взаимное влияние ONNX-инференсов на замеры);
- при fallback fast измеряется fast_attempt + full + parse
  (реальный end-to-end);
- точность 32 числовых значений и счёта — по ручному эталону;
  при fallback точность считается по результату full OCR
  (конечный результат pipeline);
- имена — для ручной сверки (эталона по именам нет).

--executor: дополнительно прогоняет parse_cs_fast через реальный
OCR_EXECUTOR (asyncio) с 1/2/4 параллельными задачами — включает
время ожидания в очереди executor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _load_refs(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _match_ref(
    name: str, refs: dict, img_size
) -> tuple[str | None, dict | None, str]:
    """→ (ref_key, entry, status). status: ok|skip."""
    if name in refs:
        return name, refs[name], "ok"
    if img_size:
        exact = [
            k for k, v in refs.items()
            if v.get("size") and list(v["size"]) == list(img_size)
        ]
        if len(exact) == 1:
            return exact[0], refs[exact[0]], "ok(size)"
        if len(exact) > 1:
            return None, None, f"skip: {len(exact)} ref с тем же size"
    return None, None, "skip: нет эталона"


def _stats(players, key):
    return [
        (
            p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg")
        )
        for p in (players or {}).get(key) or []
    ]


def _ref_stats(entry):
    return {
        key: [
            (p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg"))
            for p in entry.get(key) or []
        ]
        for key in ("team_a", "team_b")
    }


def _names(players, key):
    return [p.get("name") for p in (players or {}).get(key) or []]


def _pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))]


def _fmt(vals):
    if not vals:
        return "-"
    return (f"p50={_pct(vals,50):.2f} p95={_pct(vals,95):.2f} "
            f"max={max(vals):.2f}")


def _score_of(parsed):
    if not parsed:
        return None
    s = parsed.get("score")
    return tuple(s) if isinstance(s, (list, tuple)) else s


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("img_dir")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--iters", type=int, default=5)
    ap.add_argument("--executor", action="store_true")
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
    agg = {"fast": [], "fb": [], "full": [], "e2e": []}
    n_fast_ok = n_fb = n_full_ok = 0
    num_ok = num_tot = 0
    score_ok = score_tot = 0

    for p in files:
        data = p.read_bytes()
        with Image.open(p) as im:
            size = im.size
        ref_key, ref, st = _match_ref(p.name, refs, size)

        # --- fast iters (подряд, без full между ними) ---
        fast_t, fast_res = [], []
        for _ in range(args.iters):
            t0 = time.perf_counter()
            fp = fast_cs_ocr._extract_parsed(data)
            fast_t.append(time.perf_counter() - t0)
            fast_res.append(fp)
        last_fast = fast_res[-1]
        n_ok_iter = sum(1 for x in fast_res if x is not None)
        n_fast_ok += n_ok_iter
        n_fb += args.iters - n_ok_iter
        agg["fast"] += fast_t

        # --- full iters ---
        full_t, full_res = [], []
        for _ in range(args.iters):
            t0 = time.perf_counter()
            boxes = _run_ocr(data) or []
            pf = cs_parser.parse_cs_results(boxes) if boxes else None
            full_t.append(time.perf_counter() - t0)
            full_res.append(pf)
        last_full = full_res[-1]
        n_full_ok += sum(1 for x in full_res if x is not None)
        agg["full"] += full_t

        # --- e2e: fast_attempt + (fallback? full) ---
        e2e = []
        for i in range(args.iters):
            if fast_res[i] is None:
                e2e.append(fast_t[i] + full_t[i])
            else:
                e2e.append(fast_t[i])
        agg["e2e"] += e2e
        agg["fb"] += [fast_t[i] + full_t[i] for i in range(args.iters)
                      if fast_res[i] is None]

        # --- точность по финальному результату каждого iter ---
        fnum_ok = fnum_tot = 0
        fscore_ok = fscore_tot = 0
        if ref:
            want = _ref_stats(ref)
            for i in range(args.iters):
                final = fast_res[i] if fast_res[i] is not None else full_res[i]
                for key in ("team_a", "team_b"):
                    got = _stats(final or {}, key)
                    for r in range(4):
                        for f in range(4):
                            fnum_tot += 1
                            g = got[r][f] if r < len(got) else None
                            w = want[key][r][f] if r < len(want[key]) else None
                            if g == w:
                                fnum_ok += 1
                if ref.get("score"):
                    fscore_tot += 1
                    sc = _score_of(final)
                    if sc == tuple(ref["score"]):
                        fscore_ok += 1
            num_ok += fnum_ok
            num_tot += fnum_tot
            score_ok += fscore_ok
            score_tot += fscore_tot

        rows.append({
            "file": p.name, "size": f"{size[0]}x{size[1]}",
            "ref": ref_key or st, "iters": args.iters,
            "fast_ok": f"{n_ok_iter}/{args.iters}",
            "full_ok": f"{sum(1 for x in full_res if x is not None)}/{args.iters}",
            "fast": _fmt(fast_t), "fb_e2e": _fmt(
                [fast_t[i] + full_t[i] for i in range(args.iters)
                 if fast_res[i] is None]),
            "full": _fmt(full_t), "e2e": _fmt(e2e),
            "num": f"{fnum_ok}/{fnum_tot}" if ref else "-",
            "score": f"{fscore_ok}/{fscore_tot}" if ref else "-",
            "names_a": _names(last_fast or {}, "team_a"),
            "names_b": _names(last_fast or {}, "team_b"),
            "full_names_a": _names(last_full or {}, "team_a"),
            "full_names_b": _names(last_full or {}, "team_b"),
        })

    print(f"{'file':<56} {'size':<10} {'fast':<7} {'full':<7} "
          f"{'num':<9} {'score':<7} {'fast_p50':<7} {'e2e_p50':<7}")
    for r in rows:
        print(f"{r['file']:<56} {r['size']:<10} {r['fast_ok']:<7} "
              f"{r['full_ok']:<7} {r['num']:<9} {r['score']:<7} "
              f"{r['fast']:<22} {r['e2e']}")
        print(f"    ref={r['ref']}")
        print(f"    fast A: {r['names_a']}")
        print(f"    fast B: {r['names_b']}")
        if r["full_names_a"] != r["names_a"] or r["full_names_b"] != r["names_b"]:
            print(f"    full A: {r['full_names_a']}")
            print(f"    full B: {r['full_names_b']}")

    n_runs = args.iters * len(files)
    print(f"\n=== {len(files)} unique images × {args.iters} iters ===")
    print(f"fast ok: {n_fast_ok}/{n_runs}  fallback: {n_fb}/{n_runs} "
          f"({n_fb/n_runs*100:.0f}%)  full ok: {n_full_ok}/{n_runs}")
    print(f"final-result numeric acc: {num_ok}/{num_tot}  "
          f"score acc: {score_ok}/{score_tot}")
    for name in ("fast", "fb", "full", "e2e"):
        if agg[name]:
            print(f"{name:10}: {_fmt(agg[name])} n={len(agg[name])}")

    if args.executor:
        _run_executor_probe(args.img_dir)
    return 0


def _run_executor_probe(img_dir: str):
    """parse_cs_fast через реальный OCR_EXECUTOR: 1/2/4 параллельных.

    Включает ожидание в очереди executor — как в проде.
    """
    from services import fast_cs_ocr
    from services.ocr_service import warmup

    warmup()
    first = sorted(
        p for p in Path(img_dir).iterdir()
        if p.suffix.lower() in IMG_EXT
    )[0]
    data = first.read_bytes()
    print(f"executor probe on {first.name}")

    async def _probe(conc):
        async def one():
            t0 = time.perf_counter()
            r = await fast_cs_ocr.parse_cs_fast(data)
            return (time.perf_counter() - t0) * 1000, r is not None
        t0 = time.perf_counter()
        res = await asyncio.gather(*[one() for _ in range(conc)])
        wall = (time.perf_counter() - t0) * 1000
        return [r[0] for r in res], wall, all(r[1] for r in res)

    async def _main():
        for conc in (1, 2, 4):
            # прогрев
            await _probe(1)
            lat, wall, ok = await _probe(conc)
            print(f"executor conc={conc}: latencies={['%.0f'%x for x in lat]} "
                  f"wall={wall:.0f}ms ok={ok} "
                  f"(queue wait ≈ lat - solo)")

    asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
