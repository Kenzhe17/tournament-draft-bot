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
import os
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
    sec = p.get("secondary_text")
    if sec:
        name = f"{name} _[{sec}]_"
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


def _ms(v) -> str:
    return f"{v:.0f}" if isinstance(v, (int, float)) else "—"


def _timing_field(t: dict | None, title: str) -> str | None:
    """Поэтапные замеры OCR → строки для embed.

    Очередь = executor wait + worker lock + IPC;
    Детекция = decode+det; Распознавание = sum rec-прогонов;
    Парсинг = residual после известных стадий.
    """
    if not t:
        return None
    queue = sum(
        t.get(k) or 0.0
        for k in ("executor_queue_ms", "worker_lock_wait_ms", "worker_ipc_ms")
    )
    det = (t.get("decode_ms") or 0.0) + (t.get("det_ms") or 0.0)
    rec = t.get("recognition_ms")
    if rec is None:
        rec = sum(
            v for k, v in t.items() if k.endswith("_rec_ms")
        )
    parse_ = t.get("parse_ms")
    if parse_ is None:
        known = ("decode_ms", "det_ms", "anchors_ms", "cells_ms",
                 "bands_ms", "score_ms")
        spent = sum(t.get(k) or 0.0 for k in known)
        base = t.get("total_ms") or t.get("worker_wall_ms") or 0.0
        parse_ = max(0.0, base - spent) if base else None
    total = t.get("total_wall_ms") or t.get("pipeline_ms") or t.get("total_ms")
    lines = [
        f"{title}",
        f"Ожидание очереди: {_ms(queue)} мс",
        f"Детекция текста: {_ms(det)} мс",
        f"Распознавание: {_ms(rec)} мс",
        f"Парсинг: {_ms(parse_)} мс",
        f"Общее время: {_ms(total)} мс",
    ]
    extra = []
    for k, label in (
        ("anchors_ms", "anchors"), ("cells_ms", "cells"),
        ("bands_ms", "bands"), ("score_ms", "score"),
        ("refine_ms", "refine"), ("ocr_boxes_ms", "full_ocr"),
    ):
        if t.get(k):
            extra.append(f"{label}={t[k]:.0f}")
    if extra:
        lines.append("детали: " + ", ".join(extra))
    return "\n".join(lines)


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
        "ocr_timing": res.get("ocr_timing"),
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
                    "secondary_text": p.get("secondary_text"),
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
        "ocr_timing": parsed.get("_ocr_timing"),
    }


_DIFF_FIELDS = (
    ("name", "Игровые ники"),
    ("secondary_text", "Клан-теги"),
    ("kills", "K/D/A"), ("deaths", "K/D/A"), ("assists", "K/D/A"),
    ("dmg", "Damage"),
)


def _diff_results(fast: dict | None, full: dict | None) -> list[str]:
    """Построчные различия fast vs full, сгруппированные по
    категориям (ники / клан-теги / K/D/A / damage / счёт).
    Full — НЕ эталон точности."""
    if not fast or not full:
        return []
    groups: dict[str, list[str]] = {}
    fs, gs = fast.get("score"), full.get("score")
    if fs != gs:
        groups.setdefault("Счёт", []).append(
            f"fast {_score_str(fs)} vs full {_score_str(gs)}"
        )
    for side in ("left", "right"):
        fp = (fast["panels"].get(side) or {}).get("rows") or []
        gp = (full["panels"].get(side) or {}).get("rows") or []
        for i in range(max(len(fp), len(gp))):
            fr = fp[i] if i < len(fp) else None
            gr = gp[i] if i < len(gp) else None
            if fr is None or gr is None:
                groups.setdefault("Строки", []).append(
                    f"{side}[{i}]: fast={'нет' if fr is None else 'есть'} "
                    f"full={'нет' if gr is None else 'есть'}"
                )
                continue
            for f, cat in _DIFF_FIELDS:
                if fr.get(f) != gr.get(f):
                    groups.setdefault(cat, []).append(
                        f"{side}[{i}].{f}: fast={fr.get(f)!r} "
                        f"full={gr.get(f)!r}"
                    )
    out: list[str] = []
    for cat in ("Игровые ники", "Клан-теги", "K/D/A", "Damage",
                "Счёт", "Строки"):
        items = groups.get(cat)
        if items:
            out.append(f"**{cat}**")
            out.extend(f"  {s}" for s in items)
    return out


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

        from cogs.ocr_upload import _classic_pipeline
        from services import fast_cs_ocr
        from services.ocr_service import ocr_score_fallback, OCR_EXECUTOR

        loop = asyncio.get_running_loop()
        timings: dict[str, float] = {}
        fast_status = "skipped"
        fast_reason = ""
        fast_sum = None
        full_sum = None
        score_fb_ms = 0.0
        fast_enabled = os.environ.get("OCR_FAST_ENABLED", "1") == "1"

        async def _fast_once() -> dict | None:
            nonlocal fast_status, fast_reason, score_fb_ms
            if not fast_enabled:
                fast_status = "disabled"
                fast_reason = "OCR_FAST_ENABLED=0"
                return None
            reason_box: list[str] = []
            t0 = time.perf_counter()
            parsed = await fast_cs_ocr.parse_cs_fast(
                data, reason_out=reason_box, priority=-1
            )
            timings["fast_ms"] = (time.perf_counter() - t0) * 1000.0
            if parsed is None:
                fast_status = "fallback"
                fast_reason = (
                    reason_box[0]
                    if reason_box
                    else fast_cs_ocr.last_fallback_reason()
                    or "validation_failed"
                )
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
            res = await _classic_pipeline(
                data, need_score=True, fast=False, priority=-1
            )
            timings["full_ms"] = (time.perf_counter() - t0) * 1000.0
            return _result_summary(res)

        t_e2e = time.perf_counter()
        # Семафор: не более 2 параллельных диагностических прогонов —
        # OCR executor общий с боевым pipeline.
        async with _OCR_DIAG_SLOTS:
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
            elif fast_status == "disabled":
                fb = f"⏭️ disabled ({fast_reason})"
            else:
                fb = "—"
            embed.add_field(name="Fast OCR", value=fb, inline=False)
            tf = _timing_field(
                (fast_sum or {}).get("ocr_timing"), "По этапам (fast)"
            )
            if tf:
                embed.add_field(name="Тайминги", value=tf, inline=False)
        if mode != "fast" or full_sum is not None:
            fv = "не запускался"
            if "full_ms" in timings:
                fv = f"{timings['full_ms']:.0f} мс"
                if full_sum is None:
                    fv += " — не распознан"
            embed.add_field(name="Full OCR", value=fv, inline=False)
            fu = (full_sum or {}).get("ocr_timing") or {}
            tf2 = _timing_field(fu, "По этапам (full)")
            if tf2:
                embed.add_field(name="Тайминги", value=tf2, inline=False)
        embed.add_field(name="E2E (executor+parse)", value=f"{e2e_ms:.0f} мс", inline=False)

        if mode == "compare":
            embed.add_field(
                name="ℹ️ Compare",
                value="Compare запускает fast и full OCR **последовательно** — "
                "общее время = сумма двух независимых прогонов.",
                inline=False,
            )
        if mode == "compare" and fast_sum and full_sum:
            diffs = _diff_results(fast_sum, full_sum)
            if diffs:
                embed.add_field(
                    name="⚠️ Расхождения Fast vs Full",
                    value="Full OCR — НЕ эталон: сверяйте с фактическими "
                    "значениями на скриншоте.\n" + "\n".join(diffs)[:900],
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Сравнение", value="✅ Fast и Full совпали по всем полям."
                )
        return embed


async def setup(bot) -> None:
    await bot.add_cog(OCRDiagCog(bot))
