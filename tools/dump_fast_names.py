import sys, json, hashlib
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from services import fast_cs_ocr as F
from services.ocr_service import warmup
warmup()
D = Path(r"C:\Users\2006k\Downloads\скрины тест")
out = {}
for p in sorted(D.iterdir()):
    if p.suffix.lower() not in (".png",".jpg",".jpeg"): continue
    data = p.read_bytes()
    r = F._extract_parsed(data)
    e = {"sha256": hashlib.sha256(data).hexdigest(), "left": [], "right": []}
    if r:
        for side,key in (("left","team_a"),("right","team_b")):
            for pl in r[key]:
                e[side].append({"name": pl["name"], "secondary_text": pl.get("secondary_text")})
    out[p.name] = e
    print("==", p.name)
    for side in ("left","right"):
        for i,pl in enumerate(e[side]):
            print(f"  {side[0]}{i+1}: {pl['name']!r}  sec={pl['secondary_text']!r}")
Path("tools/_fast_names.json").write_text(json.dumps(out,ensure_ascii=False,indent=1),encoding="utf-8")
