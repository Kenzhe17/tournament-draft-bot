"""Сверка Fast OCR ников со ручной разметкой ocr_nicknames_reference.json.

Сравнение: exact и normalized (lower, только буквы/цифры).
ambiguous=true записи показываются отдельно и не входят в счёт.
"""
import sys, json, hashlib, re, unicodedata
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from services import fast_cs_ocr as F
from services.ocr_service import warmup

warmup()
D = Path(r"C:\Users\2006k\Downloads\скрины тест")
GT = json.loads(Path("tests/fixtures/ocr_nicknames_reference.json").read_text(encoding="utf-8"))

def norm(s):
    if not s: return ""
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^0-9a-zа-яё一-鿿]", "", s)

tot_n = tot_ok = tot_okx = amb_n = amb_ok = 0
sec_tot = sec_ok = 0
for p in sorted(D.iterdir()):
    if p.suffix.lower() not in (".png",".jpg",".jpeg"): continue
    gt = GT.get(p.name)
    if not gt: print("NO GT", p.name); continue
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    if sha != gt["sha256"]:
        print("SHA MISMATCH", p.name); continue
    r = F._extract_parsed(p.read_bytes())
    got = {"left": r["team_a"], "right": r["team_b"]} if r else {"left": [], "right": []}
    print("=====", p.name)
    for side in ("left",".right".strip(".")):
        for i, g in enumerate(gt[side]):
            pl = got[side][i] if i < len(got[side]) else {}
            gn, gs = pl.get("name",""), pl.get("secondary_text")
            ok = norm(gn) == norm(g["nickname"])
            okx = (gn or "").strip().lower() == (g["nickname"] or "").strip().lower()
            flag = "AMB" if g["ambiguous"] else "   "
            tot_n += 1; tot_ok += ok; tot_okx += okx
            if g["ambiguous"]: amb_n += 1; amb_ok += ok
            # secondary
            gs_n = norm(gs); gt_s = norm(g["secondary_text"])
            if g["secondary_text"] is not None or gs:
                sec_tot += 1; sec_ok += (gs_n == gt_s and bool(gs_n) == bool(gt_s))
            mark = "OK " if ok else "MISS"
            print(f"  {flag} {side[0]}{i+1} {mark} gt={g['nickname']!r}/{g['secondary_text']!r} got={gn!r}/{gs!r}")
print(f"\nNICK exact={tot_okx}/{tot_n} norm={tot_ok}/{tot_n} "
      f"({100*tot_ok/tot_n:.1f}%)  amb={amb_ok}/{amb_n}")
print(f"SECONDARY match={sec_ok}/{sec_tot}")
