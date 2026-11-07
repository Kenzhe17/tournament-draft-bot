"""Free Fire API client — siambhau/FreeFireApi (siambhau69.eu.cc).

Эндпоинты:
  GET /freefireinfo/bhau?uid=&region=&key=       — профиль
  GET /freefireinfo/stats?uid=&region=&gamemode=&matchmode=&key= — стата
  GET /bancheck/bancheck?uid=&key=               — бан
  GET /banner/profile?uid=&region=&key=          — баннер (binary)
  GET /wish/wishlist?uid=&region=&key=           — вишлист

Ключ: env FREEFIRE_API_KEY. База: env FREEFIRE_API_BASE (default ниже).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://siambhau69.eu.cc"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class FreeFireError(Exception):
    """Ошибка Free Fire API."""

    def __init__(self, message: str, code: str = "API_ERROR"):
        super().__init__(message)
        self.code = code


class FreeFireService:
    def __init__(self) -> None:
        self.api_key = os.getenv("FREEFIRE_API_KEY", "").strip()
        self.base_url = os.getenv(
            "FREEFIRE_API_BASE", DEFAULT_BASE_URL
        ).rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, path: str, params: dict[str, str],
                       *, binary: bool = False) -> Any:
        if not self.configured:
            raise FreeFireError(
                "Free Fire API не настроен (FREEFIRE_API_KEY).", "NO_KEY")

        session = await self._get_session()
        query = {**params, "key": self.api_key}
        url = f"{self.base_url}{path}"

        try:
            async with session.get(url, params=query) as resp:
                if resp.status == 429:
                    raise FreeFireError(
                        "Rate limit API. Попробуйте позже.", "RATE_LIMIT")
                if resp.status == 401:
                    raise FreeFireError(
                        "Неверный API-ключ Free Fire.", "BAD_KEY")
                if resp.status == 404:
                    raise FreeFireError(
                        "Данные не найдены (UID/регион?).", "NOT_FOUND")
                if resp.status >= 400:
                    raise FreeFireError(
                        f"API вернул ошибку {resp.status}.", "HTTP_ERROR")

                if binary:
                    data = await resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    return data, ctype

                try:
                    return await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    raise FreeFireError(
                        f"Некорректный ответ API: {text[:120]}", "BAD_JSON")
        except FreeFireError:
            raise
        except aiohttp.ClientError as e:
            logger.error("Free Fire API network error: %s", e)
            raise FreeFireError("Сетевая ошибка при запросе к API.", "NETWORK")

    # ------------------------------------------------------------- endpoints

    async def get_info(self, region: str, uid: str) -> dict[str, Any]:
        """Профиль игрока (/freefireinfo/bhau)."""
        return await self._request(
            "/freefireinfo/bhau", {"uid": uid, "region": region})

    async def get_stats(self, region: str, uid: str, *,
                        gamemode: str = "br", matchmode: str = "career"
                        ) -> dict[str, Any]:
        """Статистика (/freefireinfo/stats)."""
        return await self._request(
            "/freefireinfo/stats",
            {"uid": uid, "region": region,
             "gamemode": gamemode, "matchmode": matchmode})

    async def get_ban(self, uid: str) -> dict[str, Any]:
        """Проверка бана (/bancheck/bancheck)."""
        return await self._request("/bancheck/bancheck", {"uid": uid})

    async def get_banner(self, region: str, uid: str
                         ) -> tuple[bytes, str]:
        """Баннер профиля (/banner/profile) — бинарные данные."""
        return await self._request(
            "/banner/profile", {"uid": uid, "region": region}, binary=True)

    async def get_wishlist(self, region: str, uid: str) -> dict[str, Any]:
        """Вишлист (/wish/wishlist)."""
        return await self._request(
            "/wish/wishlist", {"uid": uid, "region": region})


freefire_service = FreeFireService()
