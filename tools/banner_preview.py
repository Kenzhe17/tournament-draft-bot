"""Локальный предпросмотр баннера: полный кадр + имитация обрезки Discord.

Discord показывает server banner полосой в шапке списка каналов — видна
центральная горизонтальная полоса, верхняя часть перекрыта именем сервера,
низ уходит в затемнение. Точные границы зависят от ширины окна, поэтому
полоса настраивается аргументами.

Использование:
    .venv/Scripts/python.exe tools/banner_preview.py banner.png [out_dir]
        [--top 205] [--bottom 875]

Результат в out_dir (по умолчанию tools/banner_preview_out/):
    discord_preview.png        — видимая полоса Discord (safe zone)
    discord_preview_strict.png — более жёсткая центральная полоса (~40%)
    discord_diagnostic.png     — полный баннер с рамками видимых зон
    banner_full.png            — копия полного баннера 1920x1080
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tools" / "banner_preview_out"

# оценка видимой полосы Discord (y-координаты в кадре 1920x1080)
DEFAULT_TOP, DEFAULT_BOTTOM = 205, 875
STRICT_TOP, STRICT_BOTTOM = 330, 760   # ~40% по центру — запасной вариант

# зоны из services/banner/renderer.py (для диагностической разметки)
def _element_boxes():
    try:
        from services.banner import renderer as r
    except Exception as e:  # noqa: BLE001 - превью не должно падать без пакета
        print(f"warn: element boxes unavailable: {e}")
        return []
    return [
        ("logo", 1040, 150, 1760, 330),
        ("card", 818, 340, 1877, 875),
        ("avatar", r.AVATAR_CX - 90, r.AVATAR_CY - 90,
         r.AVATAR_CX + 90, r.AVATAR_CY + 90),
        ("nick", r.NICK_X, r.NICK_TOP, r.NICK_X + r.NICK_MAX_W, r.NICK_TOP + 70),
        ("stats", r.VOICE_X - 20, r.VOICE_TOP, 1780, r.VOICE_TOP + 70),
    ]


def _band(im: Image.Image, top: int, bottom: int) -> Image.Image:
    return im.crop((0, top, im.width, bottom))


def _diagnostic(im: Image.Image, bands) -> Image.Image:
    out = im.convert("RGBA")
    d = ImageDraw.Draw(out, "RGBA")
    W = out.size[0]
    for name, top, bottom, color in bands:
        d.rectangle([0, top, W - 1, bottom - 1], outline=color, width=4)
        d.rectangle([0, top, W - 1, min(top + 26, bottom)],
                    fill=color[:3] + (90,), outline=None)
        d.text((12, top + 4), f"{name}: y{top}-{bottom}", fill=(255, 255, 255, 255))
    for name, x0, y0, x1, y1 in _element_boxes():
        d.rectangle([x0, y0, x1, y1], outline=(0, 255, 120, 255), width=2)
        d.text((x0 + 4, y0 + 2), name, fill=(0, 255, 120, 255))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="PNG баннера 1920x1080")
    ap.add_argument("out_dir", nargs="?", default=str(DEFAULT_OUT))
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--bottom", type=int, default=DEFAULT_BOTTOM)
    args = ap.parse_args()

    src = Path(args.image)
    if not src.exists():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(src).convert("RGB")
    if im.size != (1920, 1080):
        print(f"warning: size {im.size}, expected 1920x1080")

    shutil.copy2(src, out_dir / "banner_full.png")
    _band(im, args.top, args.bottom).save(out_dir / "discord_preview.png")
    _band(im, STRICT_TOP, STRICT_BOTTOM).save(
        out_dir / "discord_preview_strict.png")
    bands = [
        ("discord-band", args.top, args.bottom, (0, 220, 255, 255)),
        ("strict-40%", STRICT_TOP, STRICT_BOTTOM, (255, 210, 0, 255)),
    ]
    _diagnostic(im, bands).save(out_dir / "discord_diagnostic.png")

    print(f"out: {out_dir}")
    for p in sorted(out_dir.iterdir()):
        print(f"  {p.name}  {p.stat().st_size} b")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
