"""Чистый шаблон серверного баннера на базе нового дизайна
(Neon Purple Temple Participant Banner.png, 1672x941).

Удаляет динамические зоны (inpaint Telea + feather), оставляя
статичный дизайн: лого, рамку карточки, лейблы «УЧАСТНИК ДНЯ»,
«ЗА ВЧЕРА», «В ГОЛОСОВОМ», «Сыграно»-стиль и всю графику.

Результат: assets_ds_server/server_banner/template_clean.png
(перезаписывает предыдущий 1920x1080 вариант — он остаётся в .bak и git).

Запуск: .venv/Scripts/python.exe tools/build_banner_template_v2.py
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(r"C:\Users\2006k\Downloads\banner-auto"
           r"\Neon Purple Temple Participant Banner.png")
OUT = ROOT / "assets_ds_server" / "server_banner" / "template_clean.png"

W, H = 1672, 941

# --- Динамические зоны (координаты примера 1672x941) ---
# Аватар: диск внутри неонового кольца (центр из HoughCircles)
AVATAR_CX, AVATAR_CY, AVATAR_R = 908, 536, 52
# Ник + строка «Самый активный за прошедший день»
NICK_RECT = (985, 472, 1505, 600)
# Значение голоса под лейблом «В ГОЛОСОВОМ»
VOICE_RECT = (910, 648, 1020, 712)
# Значение матчей «28 матчей» (одна строка)
MATCHES_RECT = (1220, 630, 1470, 692)


def _mask_rect(shape, rect):
    x0, y0, x1, y1 = rect
    m = np.zeros(shape[:2], np.uint8)
    m[y0:y1, x0:x1] = 255
    return m


def inpaint(img, mask):
    soft = cv2.GaussianBlur(mask, (31, 31), 0)
    mask = np.where(soft > 60, 255, 0).astype(np.uint8)
    out = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
    blur = cv2.GaussianBlur(out, (0, 0), 6)
    a = (mask.astype(np.float32) / 255.0)[..., None]
    return (out * (1 - a) + blur * a).astype(np.uint8)


def main() -> None:
    img = cv2.imread(str(SRC), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[:2] == (H, W), img.shape if img is not None else "no img"

    masks = []
    # аватар — круглая маска
    m = np.zeros((H, W), np.uint8)
    cv2.circle(m, (AVATAR_CX, AVATAR_CY), AVATAR_R, 255, -1)
    masks.append(m)
    masks.append(_mask_rect(img.shape, NICK_RECT))
    masks.append(_mask_rect(img.shape, VOICE_RECT))
    masks.append(_mask_rect(img.shape, MATCHES_RECT))

    total = np.zeros((H, W), np.uint8)
    for m in masks:
        total = cv2.bitwise_or(total, m)
    img = inpaint(img, total)

    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(OUT)
    print(f"saved {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
