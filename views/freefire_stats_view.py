"""Components V2 статистика Free Fire для /ff stats.

LayoutView с вкладками всех режимов (BR Карьера/Обычные/Рейтинг,
CS Карьера/Рейтинг) и кнопкой «✕», удаляющей сообщение. Данные
каждого режима запрашиваются у API лениво при первом открытии
вкладки и кэшируются в состоянии View — повторные переключения
бесплатны.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from services.freefire_service import FreeFireError
from utils import ff_profile_formatter as fmt
from utils import ff_stats_formatter as sfmt
from utils.design.containers import chunk_text

logger = logging.getLogger(__name__)

VIEW_TIMEOUT = 300.0

FetchFn = Callable[[str], Awaitable[dict[str, Any]]]


def _td(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def _sep() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


class FreeFireStatsView(discord.ui.LayoutView):
    """Интерактивная статистика /ff stats на Components V2.

    :param data: сырой ответ get_stats для начального режима
        (может быть None — тогда вкладка покажет «загрузка»).
    :param fetch: async callable(mode_key) -> сырой ответ API.
    :param expired: отрисовать отключённым (после on_timeout).
    """

    def __init__(
        self,
        *,
        uid: str,
        region: str,
        owner_id: int,
        data: dict[str, Any] | None,
        fetch: FetchFn | None = None,
        mode: str = "br_career",
        timeout: float | None = VIEW_TIMEOUT,
        expired: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.uid = uid
        self.region = region
        self.owner_id = owner_id
        self._fetch = fetch
        self.mode = mode if mode in sfmt.MODE_BY_KEY else "br_career"
        self._expired = expired
        self._notice = ""
        self._cache: dict[str, dict[str, Any]] = {}
        self._loading: set[str] = set()
        self._fetch_lock = asyncio.Lock()
        self.bound_message: discord.Message | None = None
        if data is not None:
            self._cache[self.mode] = {"ok": True, "data": data}
        self._render()

    # ------------------------------------------------------------------ utils

    def _nav_rows(self, *, disabled: bool) -> list[discord.ui.ActionRow]:
        row = discord.ui.ActionRow()
        for key, _, _, label in sfmt.STATS_MODES:
            btn = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if key == self.mode
                    else discord.ButtonStyle.secondary
                ),
                custom_id=f"ffs:{key}",
                disabled=disabled,
            )
            btn.callback = self._on_button
            row.add_item(btn)
        row2 = discord.ui.ActionRow()
        close = discord.ui.Button(
            label="Закрыть",
            style=discord.ButtonStyle.danger,
            custom_id="ffs:close",
            disabled=disabled,
        )
        close.callback = self._on_button
        row2.add_item(close)
        return [row, row2]

    def _fit(self, items: list[discord.ui.Item]) -> list[discord.ui.Item]:
        """Лимит 10 детей Container (8 контентных + 2 ActionRow)."""
        while len(items) > 8:
            for i in range(len(items) - 1):
                a, b = items[i], items[i + 1]
                if (
                    isinstance(a, discord.ui.TextDisplay)
                    and isinstance(b, discord.ui.TextDisplay)
                    and len(a.content) + len(b.content) + 1 <= 2000
                ):
                    items[i : i + 2] = [_td(a.content + "\n" + b.content)]
                    break
                if isinstance(a, discord.ui.Separator):
                    items.pop(i)
                    break
            else:
                for i in range(len(items) - 1, -1, -1):
                    if not isinstance(items[i], discord.ui.ActionRow):
                        items.pop(i)
                        break
        return items

    def _render(self) -> None:
        self.clear_items()
        container = discord.ui.Container()
        items = self._build_items()
        if self._expired:
            items.insert(
                0,
                _td("-# ⌛ Срок действия профиля истёк — вызовите `/ff stats` снова."),
            )
        for item in self._fit(items):
            container.add_item(item)
        for row in self._nav_rows(disabled=self._expired):
            container.add_item(row)
        self.add_item(container)

    def _build_items(self) -> list[discord.ui.Item]:
        _, _, label = sfmt.MODE_BY_KEY[self.mode]
        items: list[discord.ui.Item] = [
            _td(f"-# СТАТИСТИКА · {label.upper()}"),
        ]
        entry = self._cache.get(self.mode)
        if entry is None or self.mode in self._loading:
            items.append(_td("-# загрузка данных…"))
        elif not entry.get("ok"):
            items.append(
                _td(f"Не удалось загрузить статистику: {entry.get('error', 'ошибка')}")
            )
        else:
            stats = entry["data"].get("stats") or entry["data"]
            text = sfmt.stats_markdown(stats)
            if len(text) > 1800:
                text = text[:1800].rstrip() + "\n-# …"
            for chunk in chunk_text(text):
                items.append(_td(chunk))
        items.append(_sep())
        notice = f"{self._notice} · " if self._notice else ""
        items.append(
            _td(
                f"-# {notice}ID `{self.uid}` · {fmt.region_label(self.region)}"
                f" · обновлено <t:{int(time.time())}:R>"
            )
        )
        return items

    # ------------------------------------------------------------- callbacks

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Этой статистикой управляет только участник, вызвавший `/ff stats`.",
            ephemeral=True,
        )

    async def _on_button(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(interaction):
            await self._deny(interaction)
            return
        if self._expired:
            await interaction.response.send_message(
                "Срок действия профиля истёк — вызовите `/ff stats` снова.",
                ephemeral=True,
            )
            return

        custom_id = str(interaction.data.get("custom_id") or "")
        if custom_id == "ffs:close":
            await self._do_close(interaction)
            return
        if custom_id.startswith("ffs:") and custom_id != "ffs:close":
            mode = custom_id.split(":", 1)[1]
            if mode in sfmt.MODE_BY_KEY:
                self._notice = ""
                await self._switch_mode(interaction, mode)
            return

    async def _switch_mode(
        self, interaction: discord.Interaction, mode: str
    ) -> None:
        self.mode = mode
        entry = self._cache.get(mode)
        if entry is not None and entry.get("ok"):
            self._render()
            await interaction.response.edit_message(view=self)
            return
        if mode in self._loading:
            self._render()
            await interaction.response.edit_message(view=self)
            return
        if entry is not None and not entry.get("ok"):
            # Ошибка кэшируется — показываем её без повторного запроса
            self._render()
            await interaction.response.edit_message(view=self)
            return
        await self._fetch_mode(interaction, mode, defer=True)

    async def _fetch_mode(
        self, interaction: discord.Interaction, mode: str, *, defer: bool
    ) -> None:
        """Запросить режим у API и отрисовать результат."""
        if self._fetch is None:
            self._cache[mode] = {"ok": False, "error": "API недоступен"}
            self.mode = mode
            self._render()
            await interaction.response.edit_message(view=self)
            return
        self._loading.add(mode)
        self.mode = mode
        self._render()
        if defer:
            await interaction.response.defer()
        try:
            async with self._fetch_lock:
                data = await self._fetch(mode)
            self._cache[mode] = {"ok": True, "data": data}
        except FreeFireError as exc:
            logger.warning("FF stats fetch %s failed: %s", mode, exc)
            self._cache[mode] = {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("FF stats fetch %s unexpected error", mode)
            self._cache[mode] = {"ok": False, "error": "внутренняя ошибка"}
        finally:
            self._loading.discard(mode)
        self._render()
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            logger.exception("FF stats edit_original_response failed")
            try:
                await interaction.followup.edit_message(view=self)
            except Exception:
                logger.exception("FF stats followup edit failed")

    async def _do_close(self, interaction: discord.Interaction) -> None:
        """Удалить сообщение со статистикой."""
        message = getattr(interaction, "message", None) or self.bound_message
        try:
            if message is not None:
                await message.delete()
            else:
                await interaction.delete_original_response()
        except Exception:
            logger.debug("FF stats close failed", exc_info=True)
            try:
                await interaction.response.send_message(
                    "Не удалось удалить сообщение.", ephemeral=True
                )
            except Exception:
                logger.debug("FF stats close fallback failed", exc_info=True)

    async def on_timeout(self) -> None:
        expired = FreeFireStatsView(
            uid=self.uid,
            region=self.region,
            owner_id=self.owner_id,
            data=None,
            fetch=None,
            mode=self.mode,
            timeout=None,
            expired=True,
        )
        expired._cache = dict(self._cache)
        message = self.bound_message
        if message is not None:
            try:
                await message.edit(view=expired)
            except Exception:
                logger.debug("FF stats timeout edit failed", exc_info=True)
