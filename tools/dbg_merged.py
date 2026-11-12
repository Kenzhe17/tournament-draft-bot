import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import numpy as np, cv2
from services import fast_cs_ocr as F
from services.ocr_service import warmup, _get_ocr
warmup()
p = Path(r"C:\Users\2006k\Downloads\скрины тест\image0.jpg")
data = p.read_bytes()
img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
det = F._detect_boxes(img)
_ip, _db, orig = det
boxes = [F._to_box_dict(orig[i], i) for i in range(len(orig))]
boundary = F._find_panel_split(boxes, img.shape[1], img.shape[0])
eng = _get_ocr()
rec = F._RecCache(eng, img, orig)
geoms=[]
for side,pb in (("left",[b for b in boxes if b["cx"]<boundary]),("right",[b for b in boxes if b["cx"]>=boundary])):
    geoms.append(F._panel_geometry(pb,boundary,img.shape[1],img.shape[0],side,rec))
rec.flush()
# row1 left name members
for pi,(side,g) in enumerate(zip(("left","right"),geoms)):
    if side!="left": continue
    cx0,cx1=g["cols"]["name"]; row_cy=g["rows_cy"][0]; pitch=g["pitch"]
    print("pitch",pitch,"row_cy",row_cy)
    for b in g["panel_boxes"]:
        if cx0-2<=b["cx"]<=cx1+2 and abs(b["cy"]-row_cy)<=0.5*pitch:
            t,s=rec.get(b["idx"])
            print(f"  idx={b['idx']} cx={b['cx']:.0f} cy={b['cy']:.0f} y0={b['y0']:.0f} y1={b['y1']:.0f} h={b['y1']-b['y0']:.0f} s={s:.2f} {t!r}")
