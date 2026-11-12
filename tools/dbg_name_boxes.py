"""Dump name-column det boxes (cy/y0/y1/h/text) per player row."""
import sys, io
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import numpy as np, cv2
from services import fast_cs_ocr as F
from services.ocr_service import warmup, _get_ocr

warmup()
name = sys.argv[1] if len(sys.argv) > 1 else "image0.jpg"
p = Path(r"C:\Users\2006k\Downloads\скрины тест") / name
data = p.read_bytes()
img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
det = F._detect_boxes(img)
if det is None:
    print("no det"); sys.exit()
img_pad, dt_boxes, orig = det
boxes = [F._to_box_dict(poly, i) for i, poly in enumerate(orig)]
eng = _get_ocr()
rec = F._RecCache(eng, img, orig)

# replicate split + geometry minimally by calling internals
# find panels
boundary = F._find_panel_split(boxes, img.shape[1], img.shape[0])
print("boundary", boundary, "img", img.shape)
left_boxes = [b for b in boxes if b["cx"] < boundary]
right_boxes = [b for b in boxes if b["cx"] >= boundary]
geoms = []
for side, pboxes in (("left", left_boxes), ("right", right_boxes)):
    g = F._panel_geometry(pboxes, boundary, img.shape[1], img.shape[0], side, rec)
    geoms.append(g)
rec.flush()
for pi, (side, g) in enumerate(zip(("left","right"), geoms)):
    print(f"== {side} header_cy={g['header_cy']:.0f} pitch={g['pitch']:.0f} cols={ {k:(round(v[0]),round(v[1])) for k,v in g['cols'].items()} }")
    for ri in range(4):
        row_cy = g["rows_cy"][ri]; pitch=g["pitch"]
        y_lo,y_hi = g["bounds"][ri]
        print(f"  row{ri+1} cy={row_cy:.0f} bounds=({y_lo:.0f},{y_hi:.0f})")
        cx0,cx1 = g["cols"]["name"]
        cands=[]
        for b in g["panel_boxes"]:
            if not (cx0-2 <= b["cx"] <= cx1+2): continue
            if b["cy"] < row_cy - 0.9*pitch or b["cy"] > row_cy + 0.95*pitch: continue
            t,s = rec.get(b["idx"])
            cands.append((b["cy"], b["y0"], b["y1"], b["y1"]-b["y0"], round(b["cx"]), round(s,2), t))
        for c in sorted(cands):
            print("    cy=%5.0f y0=%5.0f y1=%5.0f h=%3.0f cx=%4.0f s=%.2f %r" % (c[0],c[1],c[2],c[3],c[4],c[5],c[6]))
