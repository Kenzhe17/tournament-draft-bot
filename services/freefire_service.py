"""Free Fire Centralized API client (siambhau/FreeFireApi).

База: https://github.com/siambhau/FreeFireApi
Эндпоинты: http://siambhau69.eu.cc/<group>/<endpoint>?...&key=API_KEY
Ключ: FREEFIRE_API_KEY (бесплатный — Player Info group; premium — остальные).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

FF_API_BASE = os.getenv("FF_API_BASE", "http://siambhau69.eu.cc").rstrip("/")
FF_API_KEY = os.getenv("FREEFIRE_API_KEY", "")

# Регионы, поддерживаемые API (examples: BD, IND, SG...)
VALID_REGIONS = ("sg", "ind", "br", "bd", "ru", "id", "th", "tw", "us", "pk", "me", "cis")


class FreeFireError(Exception):
    """Ошибка Free Fire API."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class FreeFireService:
    """Async-клиент Free Fire Centralized API."""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or FF_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=20)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, params: dict[str, str],
                   raw: bool = False) -> Any:
        if not self.configured:
            raise FreeFireError(
                "API ключ не настроен. Установите FREEFIRE_API_KEY.", "NO_KEY"
            )
        params = {**params, "key": self.api_key}
        url = f"{FF_API_BASE}/{path}"

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    raise FreeFireError(
                        "Превышен лимит запросов API. Попробуйте позже.", "RATE_LIMIT"
                    )
                if resp.status in (401, 403):
                    raise FreeFireError(
                        "Неверный API ключ или нет доступа к эндпоинту.", "AUTH"
                    )
                if resp.status == 404:
                    raise FreeFireError("Игрок или объект не найден.", "NOT_FOUND")
                if resp.status >= 400:
                    raise FreeFireError(
                        f"Ошибка API (HTTP {resp.status})", "HTTP"
                    )
                if raw:
                    return await resp.read(), resp.headers.get("Content-Type", "")
                data = await resp.json(content_type=None)

        if isinstance(data, dict):
            err = data.get("error") or data.get("message")
            if err and not data.get("success", True):
                raise FreeFireError(str(err), "API")
            if isinstance(err, str) and "error" in data and len(data) <= 2:
                # {"error": "..."} без success-флага
                if "not found" in err.lower() or "required" in err.lower() \
                        or "failed" in err.lower():
                    raise FreeFireError(err, "API")
        return data if isinstance(data, dict) else {}

    # ---------------- endpoints ----------------

    async def get_info(self, region: str, uid: str) -> dict[str, Any]:
        """Полный профиль игрока (FREE tier)."""
        return await self._get("freefireinfo/bhau",
                               {"uid": uid, "region": region})

    async def get_stats(self, region: str, uid: str, *,
                        gamemode: str = "br",
                        matchmode: str = "CAREER") -> dict[str, Any]:
        """Статистика игрока. gamemode: br|cs, matchmode: CAREER|NORMAL|RANKED."""
        return await self._get("freefireinfo/stats",
                               {"uid": uid, "region": region,
                                "gamemode": gamemode, "matchmode": matchmode})

    async def get_ban(self, uid: str) -> dict[str, Any]:
        return await self._get("bancheck/bancheck", {"uid": uid})

    async def get_banner(self, region: str, uid: str) -> tuple[bytes, str]:
        """PNG-баннер профиля. Возвращает (bytes, content_type)."""
        return await self._get("banner/profile",
                               {"uid": uid, "region": region}, raw=True)

    async def get_wishlist(self, region: str, uid: str) -> dict[str, Any]:
        return await self._get("wish/wishlist",
                               {"uid": uid, "region": region})

    async def get_craftland(self, region: str, code: str,
                            lang: str = "en") -> dict[str, Any]:
        """Инфо о карте Craftland (quick info)."""
        return await self._get("craftlands/info",
                               {"map_code": code, "region": region, "lang": lang})

    async def get_craftland_details(self, region: str, code: str,
                                    lang: str = "en") -> dict[str, Any]:
        """Полные детали карты Craftland."""
        return await self._get("craftlands/map_details",
                               {"map_code": code, "region": region, "lang": lang})


freefire_service = FreeFireService()
