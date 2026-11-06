"""Free Fire Community API client.

Документация: https://docs.freefirecommunity.com
Ключ: FREEFIRE_API_KEY (получить на developers.freefirecommunity.com)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

FF_API_BASE = "https://developers.freefirecommunity.com/api/v1"
FF_API_KEY = os.getenv("FREEFIRE_API_KEY", "")

VALID_REGIONS = ("sg", "ind", "br")


class FreeFireError(Exception):
    """Ошибка Free Fire API."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class FreeFireService:
    """Async-клиент Free Fire Community API."""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or FF_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=15)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.configured:
            raise FreeFireError(
                "API ключ не настроен. Установите FREEFIRE_API_KEY.", "NO_KEY"
            )

        headers = {"x-api-key": self.api_key}
        url = f"{FF_API_BASE}/{path}"

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    raise FreeFireError(
                        "Превышен лимит запросов API. Попробуйте позже.", "RATE_LIMIT"
                    )
                if resp.status in (401, 403):
                    raise FreeFireError(
                        "Неверный или просроченный API ключ.", "AUTH"
                    )
                if resp.status == 404:
                    raise FreeFireError("Игрок или объект не найден.", "NOT_FOUND")
                if resp.status >= 400:
                    raise FreeFireError(
                        f"Ошибка API (HTTP {resp.status})", "HTTP"
                    )
                data = await resp.json(content_type=None)

        if isinstance(data, dict) and data.get("success") is False:
            raise FreeFireError(
                data.get("message") or "Ошибка API", data.get("code", "API")
            )
        return data if isinstance(data, dict) else {}

    async def get_info(self, region: str, uid: str) -> dict[str, Any]:
        return await self._get("info", {"region": region, "uid": uid})

    async def get_stats(self, region: str, uid: str) -> dict[str, Any]:
        return await self._get("stats", {"region": region, "uid": uid})

    async def get_ban(self, uid: str, lang: str = "ru") -> dict[str, Any]:
        return await self._get("bancheck", {"uid": uid, "lang": lang})

    async def get_wishlist(self, region: str, uid: str) -> dict[str, Any]:
        return await self._get("wishlist", {"region": region, "uid": uid})

    async def get_craftland(self, region: str, code: str) -> dict[str, Any]:
        return await self._get("craftland", {"region": region, "code": code})


freefire_service = FreeFireService()
