"""Cog: диагностическая slash-команда /ocr для Adaptive Fast OCR.

Только для ручного тестирования на тестовом сервере: прогоняет
скриншот результата CS 4x4 через fast/full/compare режимы и показывает
Embed со сравнением. Ничего не пишет в турниры, ELO, экономику,
PostgreSQL и каналы результатов — чистая диагностика.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from config import ADMIN_USER_IDS, guild_allowed

logger = logging.getLogger(__name__)

MAX_OCR_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_MODES = ("fast", "full", "compare")
# Не более 2 параллельных диагностических прогонов — OCR executor
# общий с боевым pipeline, не перегружаем его.
_OCR_DIAG_SLOTS = asyncio.Semaphore(2)
_PROCESS_TIMEOUT = 180.0

_IMG_EXTS = {".png", ".jpg", ".jpeg"}


def _is_allowed(interaction: discord.Interaction) -> bool:
    """Owner / extra admins / guild-admin, только разрешённая гильдия."""
    if interaction.guild is None:
        return False
    if not guild_allowed(interaction.guild_id):
        return False
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _attachment_ok(att: discord.Attachment) -> str | None:
    """None — ок; иначе причина отказа."""
    ctype = (att.content_type or "").lower()
    if not ctype.startswith("image/"):
        return "Вложение не является изображением (content-type)."
    if att.size > MAX_OCR_IMAGE_BYTES:
        return "Файл больше 10 МБ."
    name = (att.filename or "").lower()
    if not any(name.endswith(e) for e in _IMG_EXTS):
        return "Поддерживаются только PNG/JPG/JPEG."
    return None


def _verify_image(data: bytes) -> tuple[int, int] | None:
    """Реальное содержимое через PIL → (w, h) или None."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _row_view(p: dict) -> str:
    name = p.get("name") or "?"
    k, d, a = p.get("kills"), p.get("deaths"), p.get("assists")
    dmg = p.get("dmg")
    kda = f"{k}/{d}/{a}" if None not in (k, d, a) else "?/?/?"
    dmg_s = str(dmg) if dmg is not None else "?"
    return f"{name} — {kda} — {dmg_s}"


def _panels_to_embed_lines(panels: dict) -> list[str]:
    lines: list[str] = []
    for side in ("left", "right"):
        panel = panels.get(side) or {}
        pname = panel.get("name") or ("Левая" if side == "left" else "Правая")
        lines.append(f"**{pname}**")
        for p in panel.get("rows") or []:
            lines.append(_row_view(p))
        lines.append("")
    return lines


def _score_str(score) -> str:
    if isinstance(score, (list, tuple)) and len(score) == 2:
        return f"{score[0]} — {score[1]}"
    return "не распознан"


def _result_summary(res: dict | None) -> dict | None:
    """_classic_pipeline result → плоский dict для embed/сравнения."""
    if not res or "panels" not in res:
        return None
    panels = res["panels"]
    return {
        "panels": panels,
        "score": res.get("score"),
        "banner": res.get("banner"),
        "top5": res.get("top5"),
        "squad": False,
    }


def _fast_to_summary(parsed: dict | None) -> dict | None:
    """raw parsed fast-пути → тот же формат, что _result_summary."""
    if not parsed or "team_a" not in parsed:
        return None
    panels = {}
    for side, name_key, pk in (
        ("left", "team_a_name", "team_a"),
        ("right", "team_b_name", "team_b"),
    ):
        panels[side] = {
            "name": parsed.get(name_key),
            "rows": [
                {
                    "name": p.get("name"),
                    "kills": p.get("kills"),
                    "deaths": p.get("deaths"),
                    "assists": p.get("assists"),
                    "dmg": p.get("dmg"),
                }
                for p in parsed.get(pk) or []
            ],
        }
    return {
        "panels": panels,
        "score": parsed.get("score"),
        "banner": parsed.get("banner"),
        "top5": parsed.get("top5"),
        "squad": False,
    }


def _diff_results(fast: dict | None, full: dict | None) -> list[str]:
    """Построчные различия fast vs full. Full — НЕ эталон."""
    if not fast or not full:
        return []
    diffs: list[str] = []
    fs, gs = fast.get("score"), full.get("score")
    if fs != gs:
        diffs.append(f"score: fast {_score_str(fs)} vs full {_score_str(gs)}")
    for side in ("left", "right"):
        fp = (fast["panels"].get(side) or {}).get("rows") or []
        gp = (full["panels"].get(side) or {}).get("rows") or []
        for i in range(max(len(fp), len(gp))):
            fr = fp[i] if i < len(fp) else None
            gr = gp[i] if i < len(gp) else None
            if fr is None or gr is None:
                diffs.append(f"{side}[{i}]: fast={'нет' if fr is None else 'есть'} full={'нет' if gr is None else 'есть'}")
                continue
            for f in ("name", "kills", "deaths", "assists", "dmg"):
                if fr.get(f) != gr.get(f):
                    diffs.append(
                        f"{side}[{i}].{f}: fast={fr.get(f)!r} full={gr.get(f)!r}"
                    )
    return diffs


class OCRDiagCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="ocr",
        description="Диагностика OCR скриншота CS 4x4 (fast/full/compare)",
    )
    @app_commands.describe(
        image="Скриншот результата матча (PNG/JPG)",
        mode="Режим: fast, full или compare (по умолчанию)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="fast", value="fast"),
            app_commands.Choice(name="full", value="full"),
            app_commands.Choice(name="compare", value="compare"),
        ]
    )
    async def ocr(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        mode: str = "compare",
    ) -> None:
        if not _is_allowed(interaction):
            await interaction.response.send_message(
                "⛔ Команда доступна только администраторам тестового сервера.",
                ephemeral=True,
            )
            return
        mode_v = mode or "compare"
        err = _attachment_ok(image)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed = await asyncio.wait_for(
                self._run(interaction, image, mode_v), _PROCESS_TIMEOUT
            )
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="🔬 OCR диагностика",
                description=f"⏱️ Превышен таймаут {_PROCESS_TIMEOUT:.0f}с.",
                color=0xE74C3C,
            )
        except Exception:
            logger.exception("/ocr diag failed")
            embed = discord.Embed(
                title="🔬 OCR диагностика",
                description="❌ Внутренняя ошибка OCR. См. логи.",
                color=0xE74C3C,
            )
        await interaction.edit_original_response(embed=embed)

    async def _run(
        self, interaction: discord.Interaction, att: discord.Attachment, mode: str
    ) -> discord.Embed:
        t_dl = time.perf_counter()
        data = await att.read()
        dl_ms = (time.perf_counter() - t_dl) * 1000.0
        size = _verify_image(data)
        if size is None:
            return discord.Embed(
                title="🔬 OCR диагностика",
                description="❌ Файл не является корректным изображением.",
                color=0xE74C3C,
            )
        img_w, img_h = size

        from cogs.ocr_upload import _classic_pipeline, _fast_parse_cs
        from services import fast_cs_ocr
        from services.ocr_service import ocr_score_fallback, OCR_EXECUTOR

        loop = asyncio.get_running_loop()
        timings: dict[str, float] = {}
        fast_status = "skipped"
        fast_reason = ""
        fast_sum = None
        full_sum = None
        score_fb_ms = 0.0

        async def _fast_once() -> dict | None:
            nonlocal fast_status, fast_reason, score_fb_ms
            t0 = time.perf_counter()
            parsed = await _fast_parse_cs(data)
            timings["fast_ms"] = (time.perf_counter() - t0) * 1000.0
            if parsed is None:
                fast_status = "fallback"
                fast_reason = fast_cs_ocr.last_fallback_reason() or "validation_failed"
                return None
            # Как в боевом pipeline: досчитать счёт при промахе.
            if parsed.get("score") is None:
                t1 = time.perf_counter()
                sc = await loop.run_in_executor(
                    OCR_EXECUTOR, ocr_score_fallback, data,
                    parsed.get("_rec_boxes") or [],
                )
                score_fb_ms = (time.perf_counter() - t1) * 1000.0
                if sc:
                    parsed["score"] = list(sc)
            fast_status = "ok"
            return _fast_to_summary(parsed)

        async def _full_once() -> dict | None:
            t0 = time.perf_counter()
            res = await _classic_pipeline(data, need_score=True, fast=False)
            timings["full_ms"] = (time.perf_counter() - t0) * 1000.0
            return _result_summary(res)

        t_e2e = time.perf_counter()
        if mode == "fast":
            fast_sum = await _fast_once()
            if fast_sum is None:
                full_sum = await _full_once()
        elif mode == "full":
            full_sum = await _full_once()
        else:  # compare — независимые прогоны в одинаковых условиях
            fast_sum = await _fast_once()
            full_sum = await _full_once()
        e2e_ms = (time.perf_counter() - t_e2e) * 1000.0

        # Экран/команды — из успешного результата (fast приоритетнее,
        # т.к. это проверяемый путь; full — для сравнения).
        primary = fast_sum or full_sum
        embed = discord.Embed(title="🔬 OCR диагностика", color=0x3498DB)
        embed.add_field(name="Изображение", value=f"{img_w}×{img_h} px\n{att.filename}", inline=True)
        embed.add_field(name="Режим", value=mode, inline=True)
        embed.add_field(name="Загрузка", value=f"{dl_ms:.0f} мс", inline=True)

        if primary is None:
            embed.add_field(
                name="Результат",
                value="❌ Ни fast, ни full не распознали таблицу. Возможно, это не экран CS 4x4.",
            )
        else:
            panels = primary["panels"]
            if primary.get("squad"):
                embed.add_field(name="Тип экрана", value="squad (один отряд)")
            else:
                embed.add_field(name="Тип экрана", value="CS 4x4 (две таблицы)")
            tnames = []
            for side in ("left", "right"):
                nm = (panels.get(side) or {}).get("name")
                tnames.append(nm or ("Левая" if side == "left" else "Правая"))
            embed.add_field(name="Команды", value=" vs ".join(tnames))
            embed.add_field(name="Счёт", value=_score_str(primary.get("score")))
            if primary.get("banner"):
                embed.add_field(name="Баннер", value=primary["banner"])
            lines = _panels_to_embed_lines(panels)
            embed.add_field(
                name="Игроки (Fast)" if fast_sum else "Игроки (Full)",
                value="\n".join(lines)[:1024] or "—",
            )

        # Статус fast
        if mode != "full":
            if fast_status == "ok":
                fb = f"✅ success — {timings.get('fast_ms', 0):.0f} мс"
            elif fast_status == "fallback":
                fb = f"⚠️ fallback ({fast_reason}) — {timings.get('fast_ms', 0):.0f} мс"
                if score_fb_ms:
                    fb += f" +score {score_fb_ms:.0f} мс"
            embed.add_field(name="Fast OCR", value=fb, inline=False)
        if mode != "fast" or full_sum is not None:
            fv = "не запускался"
            if "full_ms" in timings:
                fv = f"{timings['full_ms']:.0f} мс"
                if full_sum is None:
                    fv += " — не распознан"
            embed.add_field(name="Full OCR", value=fv, inline=False)
        embed.add_field(name="E2E (executor+parse)", value=f"{e2e_ms:.0f} мс", inline=False)

        if mode == "compare" and fast_sum and full_sum:
            diffs = _diff_results(fast_sum, full_sum)
            if diffs:
                embed.add_field(
                    name="⚠️ Расхождения Fast vs Full",
                    value="\n".join(diffs)[:1024],
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Сравнение", value="✅ Fast и Full совпали по всем полям."
                )
        return embed


async def setup(bot) -> None:
    await bot.add_cog(OCRDiagCog(bot))
