"""Диагностика панелей Fast OCR: печать линий det-боксов с rec-текстом.

Использование: python tools/_diag_panel.py <img> [left|right]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

import services.fast_cs_ocr as F
from services.ocr_service import _get_ocr, warmup


def main() -> None:
    path = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    warmup()
    data = Path(path).read_bytes()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    print("img", img.shape)
    det = F._detect_boxes(img)
    if det is None:
        print("no det")
        return
    img_padded, dtb, orig = det
    boxes = [F._to_box_dict(orig[i], i) for i in range(len(orig))]
    b = F._find_panel_split(boxes, img.shape[1], img.shape[0])
    print("boundary", b, "boxes", len(boxes))
    eng = _get_ocr()
    rec = F._RecCache(eng, img_padded, dtb)
    for side in ("left", "right"):
        if only and side != only:
            continue
        pboxes = (
            [x for x in boxes if x["cx"] < b]
            if side == "left"
            else [x for x in boxes if b is not None and x["cx"] >= b]
        )
        lines = F._cluster_lines(pboxes)
        for ln in lines:
            for m in ln["members"]:
                rec.want(m["idx"])
        rec.flush()
        print(f"--- {side} ({len(pboxes)} boxes, {len(lines)} lines)")
        for li, ln in enumerate(lines):
            txts = []
            for m in ln["members"]:
                t, s = rec.get(m["idx"])
                txts.append(f"{t!r}@{s:.2f}")
            print(
                f"  L{li} cy={ln['cy']:.0f} n={len(ln['members'])} | "
                + " ".join(txts)
            )


if __name__ == "__main__":
    main()
