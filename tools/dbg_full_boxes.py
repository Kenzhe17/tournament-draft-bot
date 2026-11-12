"""Full RapidOCR boxes в колонке ИМЯ — разделяет ли det две строки."""
import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from services.ocr_service import warmup, _run_ocr
warmup()
for name in ["image0.jpg"]:
    p = Path(r"C:\Users\2006k\Downloads\скрины тест") / name
    boxes = _run_ocr(p.read_bytes())
    print("===", name, len(boxes), "boxes")
    # name col left x 500-750, right x 1590-1830; y 540-990
    for b in sorted(boxes, key=lambda b:(b["cy"],b["cx"])):
        if not (520 <= b["cy"] <= 990): continue
        if (500 <= b["cx"] <= 760) or (1580 <= b["cx"] <= 1840):
            print(f"  cx={b['cx']:7.1f} cy={b['cy']:7.1f} x0={b['x0']:6.1f} y0={b['y0']:6.1f} x1={b['x1']:6.1f} y1={b['y1']:6.1f} h={b['y1']-b['y0']:5.1f} {b['text']!r}")
