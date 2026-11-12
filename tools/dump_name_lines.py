"""Full OCR → построчная разбивка name-колонки для всех 10 скринов.

Используется для ручной GT-разметки: full det разделяет две
текстовые строки игрока на отдельные bbox.
"""
import sys, json, hashlib
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from services.ocr_service import warmup, _run_ocr, _get_ocr
from services import fast_cs_ocr as F
import numpy as np, cv2

warmup()
D = Path(r"C:\Users\2006k\Downloads\скрины тест")
out = {}
for p in sorted(D.iterdir()):
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        continue
    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    # geometry из fast det
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    det = F._detect_boxes(img)
    if det is None:
        print(p.name, "no fast det"); continue
    _ip, _db, orig = det
    fboxes = [F._to_box_dict(orig[i], i) for i in range(len(orig))]
    boundary = F._find_panel_split(fboxes, img.shape[1], img.shape[0])
    if boundary is None:
        print(p.name, "no split"); continue
    # full OCR boxes для текстов
    full = _run_ocr(data)
    rec = F._RecCache(_get_ocr(), img, orig)
    geoms = []
    for side, pb in (("left",[b for b in fboxes if b["cx"]<boundary]),
                     ("right",[b for b in fboxes if b["cx"]>=boundary])):
        g = F._panel_geometry(pb, boundary, img.shape[1], img.shape[0], side, rec)
        geoms.append(g)
    rec.flush()
    # full boxes → текст по ближайшему fast box? нет — full boxes сами с текстом.
    # Для каждой строки игрока собираем full boxes в name-колонке.
    entry = {"sha256": sha, "size": list(img.shape[1::-1]), "left": [], "right": []}
    for pi,(side,g) in enumerate(zip(("left","right"),geoms)):
        cx0,cx1 = g["cols"]["name"]
        pitch = g["pitch"]
        for ri in range(4):
            row_cy = g["rows_cy"][ri]
            # full boxes в name col в пределах строки
            cands = [b for b in full
                     if cx0-30 <= b["cx"] <= cx1+30
                     and row_cy-0.55*pitch <= b["cy"] <= row_cy+0.55*pitch]
            # кластеризация по cy
            cands.sort(key=lambda b: b["cy"])
            lines = []
            for b in cands:
                if lines and abs(b["cy"]-lines[-1][-1]["cy"]) < 0.28*pitch:
                    lines[-1].append(b)
                else:
                    lines.append([b])
            row = {"row": ri+1, "lines": []}
            for ln in lines:
                ln.sort(key=lambda b: b["x0"])
                txt = " ".join(b["text"] for b in ln).strip()
                mh = max(b["y1"]-b["y0"] for b in ln)
                mcy = sum(b["cy"] for b in ln)/len(ln)
                row["lines"].append({"cy": round(mcy,1), "h": round(mh,1),
                                     "text": txt})
            entry[side].append(row)
    out[p.name] = entry
    print("done", p.name)
Path("tools/_name_lines.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("saved tools/_name_lines.json")
