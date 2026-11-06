"""OcrService — клиент к battles-recon (github.com/TeamBattles/battles-recon).

Бот отправляет скриншот результата Free Fire в OCR-микросервис,
получает структурированный JSON (игроки, kills/deaths/assists/damage/placement)
и показывает организатору на подтверждение.

Требуется запущенный battles-recon (Docker) и env OCR_SERVICE_URL.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

OCR_SERVICE_URL = os.getenv("OCR_SERVICE_URL", "").rstrip("/")
OCR_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "120"))

# Профиль battles-recon для Free Fire (создаётся в profiles/freefire.json)
FF_GAME = os.getenv("FF_OCR_GAME", "freefire")
FF_MODE = os.getenv("FF_OCR_MODE", "squad")


class OcrError(Exception):
    pass


class OcrService:
    @property
    def enabled(self) -> bool:
        return bool(OCR_SERVICE_URL)

    async def health(self) -> dict | None:
        if not self.enabled:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{OCR_SERVICE_URL}/health",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return await r.json() if r.status == 200 else None
        except Exception as e:
            logger.warning("OCR health failed: %s", e)
            return None

    async def process_screenshot(self, image_bytes: bytes, filename: str,
                                 *, game: str = FF_GAME, mode: str = FF_MODE) -> dict:
        """Отправить скриншот в /process, вернуть распарсенные данные."""
        if not self.enabled:
            raise OcrError("OCR_SERVICE_URL не настроен")
        form = aiohttp.FormData()
        form.add_field("files", image_bytes, filename=filename,
                       content_type="image/png")
        form.add_field("game", game)
        form.add_field("mode", mode)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{OCR_SERVICE_URL}/process",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=OCR_TIMEOUT),
                ) as r:
                    if r.status != 200:
                        text = await r.text()
                        raise OcrError(f"OCR HTTP {r.status}: {text[:200]}")
                    return await r.json()
        except aiohttp.ClientError as e:
            raise OcrError(f"OCR недоступен: {e}") from e

    async def process_attachment(self, attachment) -> dict:
        """Скачать Discord attachment и распарсить."""
        data = await attachment.read()
        if len(data) > 10 * 1024 * 1024:
            raise OcrError("Файл больше 10MB")
        return await self.process_screenshot(
            data, attachment.filename or "screenshot.png",
        )


ocr_service = OcrService()


def normalize_results(raw: dict) -> list[dict]:
    """Привести ответ OCR к единому виду: [{name, placement, kills, deaths, assists, damage, points}]."""
    players: list[dict] = []
    # Формат ответа зависит от профиля; поддерживаем типовые ключи
    rows = raw.get("results") or raw.get("players") or raw.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("players", [])
    for row in rows if isinstance(rows, list) else []:
        players.append({
            "name": row.get("name") or row.get("player") or row.get("nickname") or "?",
            "placement": _int(row.get("placement") or row.get("pos") or row.get("position")),
            "kills": _int(row.get("kills") or row.get("k")),
            "deaths": _int(row.get("deaths") or row.get("d")),
            "assists": _int(row.get("assists") or row.get("a")),
            "damage": _int(row.get("damage") or row.get("dmg")),
            "points": _int(row.get("points") or row.get("score")),
        })
    return players


def _int(v: Any) -> int:
    try:
        return int(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return 0
