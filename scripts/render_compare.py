"""Генерация эталонных/нативных PNG для визуального сравнения.

Usage:
    python scripts/render_compare.py baseline   # chromium → baseline/
    python scripts/render_compare.py native     # resvg/Pillow → native/
    python scripts/render_compare.py diff       # сравнение baseline vs native
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
OUT = REPO / "render_compare_out"


def _avatar_png() -> bytes:
    """Синтетический аватар 256x256 (цветной круг с буквами)."""
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(256):
        t = y / 255
        d.line([(0, y), (255, y)], fill=(int(80 + 120 * t), 60, int(160 + 60 * t), 255))
    d.ellipse((30, 30, 225, 225), outline=(255, 255, 255, 200), width=8)
    d.text((105, 105), "Z3", fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _badge_png() -> bytes:
    p = REPO / "assets_ds_server" / "ranked_badges" / "gold_3.png"
    return p.read_bytes() if p.exists() else None


def _profiles() -> dict[str, dict]:
    base = {
        "name": "Kenzhe", "status": "играю каждый вечер",
        "level": 44, "elo": 1523, "last_elo_change": 24,
        "skill_rating": 7.4,
        "matches": 128, "wins": 74, "winrate": 57.8, "kda": 1.9,
        "avg_kills": 4.2, "avg_damage": 1820, "max_kills": 12,
        "elo_history": [24, -12, 40, 8, -30, 55, 12, -5, 33, 18],
        "hist_start_elo": 1400, "chart_delta": 123,
        "balance": 12500, "items": 320, "reputation": 45,
        "partner": "Алиса", "birthday": "12.03.2004",
        "ult_matches": 12, "ult_wins": 7, "ult_elo": 1600,
        "ult_status": "В норме",
    }
    variants = {
        "normal": dict(base),
        "long_name": {**base, "name": "ОченьДлинныйНикнейм_Игрока_Джонни_Ковбой_777"},
        "cyrillic": {**base, "name": "Женісайрат_Әсел_Қазақ"},
        "latin_digits": {**base, "name": "xX_Sniper_KZ_2007_Xx", "partner": "ProGamer_KZ_99"},
        "special": {**base, "name": "L3g3ndⓈ©®™🔥"},
        "no_avatar": {**base, "name": "БезАватара"},
        "low_elo": {**base, "elo": 812, "last_elo_change": -45,
                    "elo_history": [-30, -12, 8, -55, 20, -8, -22, 5, -40, -15],
                    "chart_delta": -149, "level": 7},
        "high_elo": {**base, "elo": 2487, "last_elo_change": 63,
                     "elo_history": [40, 55, 22, 61, 38, 70, 45, 52, 33, 60],
                     "chart_delta": 476, "level": 97,
                     "name": "GrandmasterPlayer"},
        "long_bio": {**base, "status": "очень длинный статус который должен обрезаться многоточием потому что не влезает в отведённую область карточки профиля"},
        "no_history": {**base, "elo_history": [], "matches": 0, "wins": 0,
                       "winrate": 0, "kda": 0, "avg_kills": 0,
                       "avg_damage": 0, "max_kills": 0, "chart_delta": 0,
                       "ult_matches": 0, "ult_wins": 0, "ult_status": "Пока нет сыгранных матчей"},
    }
    return variants


async def _gen_profiles(backend_dir: Path) -> None:
    from utils import profile_renderer as pr

    avatar = _avatar_png()
    badge = _badge_png()
    backend_dir.mkdir(parents=True, exist_ok=True)
    for name, data in _profiles().items():
        av = None if name == "no_avatar" else avatar
        t0 = __import__("time").monotonic()
        png = await pr.render_profile_png_async(data, av, badge)
        dt = __import__("time").monotonic() - t0
        (backend_dir / f"profile_{name}.png").write_bytes(png)
        print(f"  profile_{name}: {len(png)} bytes, {dt:.2f}s")


async def _gen_banners(backend_dir: Path) -> None:
    from services.banner import renderer as br

    avatar = _avatar_png()
    backend_dir.mkdir(parents=True, exist_ok=True)
    cases = {
        "active": dict(nickname="Kenzhe", avatar_png=avatar,
                       voice_seconds=4 * 3600 + 27 * 60, matches=37,
                       has_activity=True, report_day_label="Отчёт за 12.11"),
        "long_nick": dict(nickname="ОченьДлинныйНикнейм_Игрока_Джонни", avatar_png=avatar,
                          voice_seconds=95 * 60, matches=128,
                          has_activity=True, report_day_label="Отчёт за 12.11"),
        "empty": dict(nickname=None, avatar_png=None, voice_seconds=0,
                      matches=0, has_activity=False,
                      report_day_label="Отчёт за 12.11"),
        "no_avatar": dict(nickname="БезАватара", avatar_png=None,
                          voice_seconds=3600, matches=1,
                          has_activity=True, report_day_label="Отчёт"),
    }
    for name, kw in cases.items():
        t0 = __import__("time").monotonic()
        png = await br.render_banner_png(**kw)
        dt = __import__("time").monotonic() - t0
        (backend_dir / f"banner_{name}.png").write_bytes(png)
        print(f"  banner_{name}: {len(png)} bytes, {dt:.2f}s")


async def _gen(backend_dir: Path) -> None:
    await _gen_profiles(backend_dir)
    await _gen_banners(backend_dir)
    from utils.profile_renderer import shutdown_profile_renderer

    await shutdown_profile_renderer()


def _diff() -> None:
    """Сравнение baseline/ vs native/: размеры, mean|diff|, %пикселей>30."""
    import numpy as np
    from PIL import Image, ImageChops

    base_dir = OUT / "baseline"
    nat_dir = OUT / "native"
    dif_dir = OUT / "diff"
    dif_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for bp in sorted(base_dir.glob("*.png")):
        np_ = nat_dir / bp.name
        if not np_.exists():
            rows.append((bp.name, "MISSING", "", ""))
            continue
        a = Image.open(bp).convert("RGB")
        b = Image.open(np_).convert("RGB")
        if a.size != b.size:
            rows.append((bp.name, f"SIZE {a.size}!={b.size}", "", ""))
            continue
        arr_a = np.asarray(a, dtype=np.int16)
        arr_b = np.asarray(b, dtype=np.int16)
        d = np.abs(arr_a - arr_b)
        mean = float(d.mean())
        pct = float((d.max(axis=2) > 30).mean() * 100)
        # diff-картинка: красным — отличия
        diff_img = Image.fromarray(
            np.clip(d * 4, 0, 255).astype(np.uint8)
        )
        diff_img.save(dif_dir / bp.name)
        # overlay 50/50
        overlay = Image.blend(a, b, 0.5)
        overlay.save(dif_dir / ("overlay_" + bp.name))
        rows.append((bp.name, f"{mean:.2f}", f"{pct:.2f}%", ""))
    print(f"{'file':38} {'mean|d|':>8} {'%px>30':>8}")
    for r in rows:
        print(f"{r[0]:38} {r[1]:>8} {r[2]:>8}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if mode == "baseline":
        asyncio.run(_gen(OUT / "baseline"))
    elif mode == "native":
        asyncio.run(_gen(OUT / "native"))
    elif mode == "diff":
        _diff()
