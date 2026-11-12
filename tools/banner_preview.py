"""Локальный предпросмотр баннера: полный кадр + имитация обрезки Discord.

Discord показывает server banner полосой в шапке списка каналов — видна
центральная горизонтальная полоса, верхняя часть перекрыта именем сервера,
низ уходит в затемнение. Точные границы зависят от ширины окна, поэтому
полоса настраивается аргументами (по умолчанию — ~62% высоты по центру,
как для 1920x1080 это было y205-875).

Использование:
    .venv/Scripts/python.exe tools/banner_preview.py banner.png [out_dir]
        [--top 179] [--bottom 762]

Результат в out_dir (по умолчанию tools/banner_preview_out/):
    discord_preview.png        — видимая полоса Discord (safe zone)
    discord_preview_strict.png — более жёсткая центральная полоса (~40%)
    discord_diagnostic.png     — полный баннер с рамками видимых зон
    banner_full.png            — копия полного баннера
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_OUT = ROOT / "tools" / "banner_preview_out"

# доли высоты кадра для видимых полос Discord
BAND_TOP_F, BAND_BOT_F = 0.19, 0.81     # ~62% по центру
STRICT_TOP_F, STRICT_BOT_F = 0.315, 0.685  # ~37% — запасной вариант


# зоны из services/banner/renderer.py (для диагностической разметки)
def _element_boxes():
    try:
        from services.banner import renderer as r
    except Exception as e:  # noqa: BLE001 - превью не должно падать без пакета
        print(f"warn: element boxes unavailable: {e}")
        return []
    return [
        ("logo", 940, 210, 1350, 345),
        ("card", 775, 390, 1540, 740),
        ("avatar", r.AVATAR_CX - 75, r.AVATAR_CY - 75,
         r.AVATAR_CX + 75, r.AVATAR_CY + 75),
        ("nick", r.NICK_X, r.NICK_TOP, r.NICK_X + r.NICK_MAX_W, r.NICK_TOP + 70),
        ("stats", r.VOICE_X - 20, r.VOICE_TOP - 30, 1480, r.VOICE_TOP + 55),
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
    ap.add_argument("image", help="PNG баннера (16:9)")
    ap.add_argument("out_dir", nargs="?", default=str(DEFAULT_OUT))
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--bottom", type=int, default=None)
    args = ap.parse_args()

    src = Path(args.image)
    if not src.exists():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(src).convert("RGB")
    H = im.size[1]
    top = args.top if args.top is not None else int(H * BAND_TOP_F)
    bottom = args.bottom if args.bottom is not None else int(H * BAND_BOT_F)
    stop = int(H * STRICT_TOP_F)
    sbot = int(H * STRICT_BOT_F)

    shutil.copy2(src, out_dir / "banner_full.png")
    _band(im, top, bottom).save(out_dir / "discord_preview.png")
    _band(im, stop, sbot).save(out_dir / "discord_preview_strict.png")
    bands = [
        ("discord-band", top, bottom, (0, 220, 255, 255)),
        ("strict-40%", stop, sbot, (255, 210, 0, 255)),
    ]
    _diagnostic(im, bands).save(out_dir / "discord_diagnostic.png")

    print(f"out: {out_dir}  (band y{top}-{bottom}, strict y{stop}-{sbot})")
    for p in sorted(out_dir.iterdir()):
        print(f"  {p.name}  {p.stat().st_size} b")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
