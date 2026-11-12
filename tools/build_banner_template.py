"""Пересборка assets_ds_server/server_banner/template_clean.png под обрезку Discord.

Что делает:
1. Удаляет описание под логотипом (inpaint Telea + feather).
2 Вырезает блок логотипа и опускает его ниже (к центру безопасной зоны).
3. Вырезает карточку участника дня, поднимает её и слегка уменьшает (scale<1),
   сохраняя пропорции; старое место заполняется inpaint'ом.
4. Все склейки — с feathered alpha, без жёстких швов.

Запуск: .venv/Scripts/python.exe tools/build_banner_template.py
Исходник шаблона должен быть исходным (до правок) — скрипт идемпотентен
только от чистого template_clean.png; перед запуском восстановите бэкап
(template_clean.png.bak), если он есть.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "assets_ds_server" / "server_banner" / "template_clean.png"
BACKUP = TEMPLATE.with_suffix(".png.bak")

# --- Зоны (координаты исходного template_clean.png 1920x1080) ---
# Описание под логотипом: 2 строки, x~930-1810, y~318-425
DESC_RECT = (930, 315, 1815, 424)          # x0,y0,x1,y1

# Блок логотипа: глифы R!Z3 + свечение, x~1040-1745, y~100-250
# (луна выше y~95 не затрагивается)
LOGO_RECT = (1040, 100, 1745, 250)         # x0,y0,x1,y1
LOGO_DY = 88                               # сдвиг вниз

# Карточка участника дня: рамка x~837-1853 y~447-962 + свечение
CARD_RECT = (790, 432, 1905, 990)          # x0,y0,x1,y1
CARD_SCALE = 0.95
CARD_BOTTOM = 868                          # низ карточки после переноса
CARD_CENTER_X = 1347                       # сохранить горизонтальный центр

FEATHER = 14                               # ширина feathered-склейки


def _mask_for(rect, shape):
    x0, y0, x1, y1 = rect
    m = np.zeros(shape[:2], np.uint8)
    m[y0:y1, x0:x1] = 255
    return m


def inpaint(img: np.ndarray, rect) -> np.ndarray:
    """Telea-inpaint прямоугольника с мягкими краями маски."""
    x0, y0, x1, y1 = rect
    mask = _mask_for(rect, img.shape)
    # soften mask edges, чтобы не было резкой границы inpaint-зоны
    k = 31
    soft = cv2.GaussianBlur(mask, (k, k), 0)
    mask = np.where(soft > 60, 255, 0).astype(np.uint8)
    out = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
    # лёгкое размытие внутри зоны для однородности с фоном
    blur = cv2.GaussianBlur(out, (0, 0), 6)
    a = (mask.astype(np.float32) / 255.0)[..., None]
    return (out * (1 - a) + blur * a).astype(np.uint8)


def feathered_paste(dst: np.ndarray, patch: np.ndarray,
                    x: int, y: int) -> np.ndarray:
    """Вклеить patch в dst в позицию (x,y) с feathered краями."""
    ph, pw = patch.shape[:2]
    H, W = dst.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + pw), min(H, y + ph)
    if x1 <= x0 or y1 <= y0:
        return dst
    px0, py0 = x0 - x, y0 - y
    region = patch[py0:py0 + (y1 - y0), px0:px0 + (x1 - x0)]

    alpha = np.ones((y1 - y0, x1 - x0, 1), np.float32)
    f = FEATHER
    # линейный ramp по краям региона
    for i in range(min(f, alpha.shape[0])):
        t = i / f
        alpha[i, :] *= t
        alpha[alpha.shape[0] - 1 - i, :] *= t
    for j in range(min(f, alpha.shape[1])):
        t = j / f
        alpha[:, j] *= t
        alpha[:, alpha.shape[1] - 1 - j] *= t
    alpha = np.clip(alpha, 0, 1)

    out = dst.copy()
    out[y0:y1, x0:x1] = (
        region.astype(np.float32) * alpha
        + dst[y0:y1, x0:x1].astype(np.float32) * (1 - alpha)
    ).astype(np.uint8)
    return out


def extract(img: np.ndarray, rect):
    x0, y0, x1, y1 = rect
    return img[y0:y1, x0:x1].copy()


def main() -> None:
    if BACKUP.exists():
        img = cv2.imread(str(BACKUP), cv2.IMREAD_COLOR)
    else:
        img = cv2.imread(str(TEMPLATE), cv2.IMREAD_COLOR)
        shutil.copy2(TEMPLATE, BACKUP)
    assert img is not None, "template not found"

    # 1. описание
    img = inpaint(img, DESC_RECT)

    # 2. логотип: вырезать -> inpaint дырки -> вклеить ниже
    logo = extract(img, LOGO_RECT)
    img = inpaint(img, LOGO_RECT)
    lx, ly = LOGO_RECT[0], LOGO_RECT[1] + LOGO_DY
    img = feathered_paste(img, logo, lx, ly)

    # 3. карточка: вырезать -> inpaint дырки -> уменьшить и поднять
    card = extract(img, CARD_RECT)
    img = inpaint(img, CARD_RECT)
    new_w = int(round(card.shape[1] * CARD_SCALE))
    new_h = int(round(card.shape[0] * CARD_SCALE))
    card_r = cv2.resize(card, (new_w, new_h), interpolation=cv2.INTER_AREA)
    cx = CARD_CENTER_X - new_w // 2
    cy = CARD_BOTTOM - new_h
    img = feathered_paste(img, card_r, cx, cy)

    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(TEMPLATE)
    print(f"saved {TEMPLATE}")
    print(f"  desc inpaint {DESC_RECT}")
    print(f"  logo moved dy=+{LOGO_DY} -> y {LOGO_RECT[1]+LOGO_DY}-{LOGO_RECT[3]+LOGO_DY}")
    print(f"  card scale={CARD_SCALE} -> {new_w}x{new_h} at ({cx},{cy}) "
          f"bottom={cy+new_h}")


if __name__ == "__main__":
    main()
