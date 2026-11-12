"""Components V2 профиль игрока Free Fire для /ff info.

LayoutView с четырьмя вкладками (Обзор / Ранги / Экипировка / Аккаунт)
и кнопкой «✕», удаляющей сообщение. Данные API хранятся в состоянии
View — переключение вкладок не выполняет повторных запросов.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord

from services import ff_catalog
from utils import ff_profile_formatter as fmt

logger = logging.getLogger(__name__)

VIEW_TIMEOUT = 300.0     # время жизни интерактивного профиля
MAX_GALLERY_ITEMS = 10   # лимит MediaGallery

TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Обзор"),
    ("ranks", "Ранги"),
    ("gear", "Экипировка"),
    ("account", "Аккаунт"),
)


def _td(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def _section(text: str, thumbnail: str | None = None) -> discord.ui.Item:
    """Section с thumbnail-аксессуаром; без картинки — обычный TextDisplay."""
    if thumbnail:
        return discord.ui.Section(
            _td(text), accessory=discord.ui.Thumbnail(media=thumbnail)
        )
    return _td(text)


def _sep() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


class FreeFireProfileView(discord.ui.LayoutView):
    """Интерактивный профиль /ff info на Components V2.

    :param data: сырой ответ freefire_service.get_info.
    :param expired: отрисовать уже отключённым (после on_timeout).
    """

    def __init__(
        self,
        *,
        uid: str,
        region: str,
        owner_id: int,
        data: dict[str, Any],
        timeout: float | None = VIEW_TIMEOUT,
        expired: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.uid = uid
        self.region = region
        self.owner_id = owner_id
        self.result = data if isinstance(data, dict) else {}
        self.tab = "overview"
        self._expired = expired
        self.bound_message: discord.Message | None = None
        self._render()

    # ------------------------------------------------------------------ utils

    def _data(self) -> dict[str, Any]:
        return fmt.normalize_profile(self.result)

    def _fields(self) -> dict[str, Any]:
        return fmt.basic_fields(self._data())

    def _nav_rows(self, *, disabled: bool) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for tab_id, label in TABS:
            active = tab_id == self.tab
            btn = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if active
                    else discord.ButtonStyle.secondary
                ),
                custom_id=f"ffp:{tab_id}",
                disabled=disabled,
            )
            btn.callback = self._on_button
            row.add_item(btn)
        close = discord.ui.Button(
            label="✕",
            style=discord.ButtonStyle.danger,
            custom_id="ffp:close",
            disabled=disabled,
        )
        close.callback = self._on_button
        row.add_item(close)
        return row

    def _footer_text(self) -> str:
        parts = [f"обновлено <t:{int(time.time())}:R>"]
        season = self._fields().get("season")
        if season:
            parts.append(f"сезон {season}")
        return "-# " + " · ".join(parts)

    def _fit(self, items: list[discord.ui.Item]) -> list[discord.ui.Item]:
        """Уложиться в лимит 10 детей Container (9 + ActionRow навигации):
        склеить текстовые блоки или убрать разделители."""
        while len(items) > 9:
            # склеить два соседних TextDisplay или убрать Separator
            for i in range(len(items) - 1):
                a, b = items[i], items[i + 1]
                if (
                    isinstance(a, discord.ui.TextDisplay)
                    and isinstance(b, discord.ui.TextDisplay)
                    and len(a.content) + len(b.content) + 1 <= 2000
                ):
                    items[i : i + 2] = [
                        _td(a.content + "\n" + b.content)
                    ]
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
        builder = getattr(self, f"_tab_{self.tab}", self._tab_overview)
        items = builder()
        if self._expired:
            items.insert(
                0,
                _td("-# ⌛ Срок действия профиля истёк — вызовите `/ff info` снова."),
            )
        for item in self._fit(items):
            container.add_item(item)
        container.add_item(self._nav_rows(disabled=self._expired))
        self.add_item(container)

    # ------------------------------------------------------------- вкладки

    def _tab_overview(self) -> list[discord.ui.Item]:
        data = self._data()
        f = self._fields()
        items: list[discord.ui.Item] = [
            _td("-# FREE FIRE · PLAYER PROFILE"),
        ]

        avatar_id = data["profile"].get("avatarId")
        avatar_url = None
        if avatar_id and ff_catalog.get_item(avatar_id):
            avatar_url = ff_catalog.icon_url(avatar_id)

        header_text = (
            f"## {f['nickname']}\n"
            f"UID `{self.uid}` · {fmt.region_label(f['region'] or self.region)}"
            f" · Ур. {fmt.fmt_num(f['level'])}"
        )
        items.append(_section(header_text, thumbnail=avatar_url))

        sig = fmt.signature_text(data["social"])
        if sig:
            items.append(_td(f"*{sig}*"))

        cs = fmt.cs_rank(f["cs_points"])
        br = fmt.br_rank(f["rank_points"])
        items.append(_sep())
        items.append(
            _td(
                "**CLASH SQUAD**\n"
                f"{fmt.rank_label(f['cs_rank_id'], cs)}\n"
                f"{fmt.fmt_num(f['cs_points'])} очков\n\n"
                "**BATTLE ROYALE**\n"
                f"{fmt.rank_label(f['rank_id'], br)}\n"
                f"{fmt.fmt_num(f['rank_points'])} очков"
            )
        )

        items.append(_sep())
        items.append(
            _td(
                f"Лайки **{fmt.fmt_num(f['liked'])}**\n"
                f"Бейджи **{fmt.fmt_num(f['badge_cnt'])}**\n"
                f"EXP **{fmt.fmt_num(f['exp'])}**"
            )
        )
        items.append(_sep())
        items.append(_td(self._footer_text()))
        return items

    def _tab_ranks(self) -> list[discord.ui.Item]:
        f = self._fields()
        items: list[discord.ui.Item] = [
            _td("-# РЕЙТИНГ · RANKS"),
        ]

        cs = fmt.cs_rank(f["cs_points"])
        br = fmt.br_rank(f["rank_points"])

        def _rank_block(title: str, rank_id: Any, resolved: dict[str, Any],
                        points: Any, unit: str, max_id: Any) -> discord.ui.Item:
            try:
                max_label = f"ID {int(max_id)}" if max_id is not None else "—"
            except (TypeError, ValueError):
                max_label = str(max_id) if max_id is not None else "—"
            text = (
                f"**{title}**\n"
                f"{fmt.rank_label(rank_id, resolved)}\n"
                f"{fmt.fmt_num(points)} {unit}\n"
                f"Макс. ранг: {max_label}"
            )
            return _section(text, thumbnail=resolved.get("badge"))

        items.append(
            _rank_block("CLASH SQUAD", f["cs_rank_id"], cs, f["cs_points"],
                        "очков", f["cs_max_rank_id"])
        )
        items.append(
            _rank_block("BATTLE ROYALE", f["rank_id"], br, f["rank_points"],
                        "очков", f["max_rank_id"])
        )
        items.append(_sep())
        items.append(_td(self._footer_text()))
        return items

    def _tab_gear(self) -> list[discord.ui.Item]:
        data = self._data()
        groups = fmt.group_clothes(data["profile"])
        items: list[discord.ui.Item] = [_td("-# ЭКИПИРОВКА · OUTFIT")]

        all_entries = [e for _, entries in groups for e in entries]
        if not all_entries:
            items.append(_td("Экипировка не отображается."))
        else:
            gallery_items = []
            for e in all_entries[:MAX_GALLERY_ITEMS]:
                if not e["icon"]:
                    continue
                desc = f"{e['name']} · {e['type'] or 'Предмет'}"
                if e["rarity"]:
                    desc += f" · {e['rarity']}"
                gallery_items.append(
                    discord.MediaGalleryItem(
                        media=e["icon"], description=desc[:100]
                    )
                )
            if gallery_items:
                items.append(discord.ui.MediaGallery(*gallery_items))
            if len(all_entries) > MAX_GALLERY_ITEMS:
                items.append(
                    _td(f"-# показаны первые {MAX_GALLERY_ITEMS} из {len(all_entries)}")
                )
            # Компактный список только для предметов без иконки
            no_icon = [
                e["name"] for e in all_entries[:MAX_GALLERY_ITEMS] if not e["icon"]
            ]
            if no_icon:
                names = " · ".join(no_icon)
                if len(names) > 300:
                    names = names[:300].rstrip() + "…"
                items.append(_td(f"-# без иконок: {names}"))

        pet = fmt.pet_entry(data["pet"])
        if pet:
            pet_text = (
                f"**Питомец** — {pet['name']}\n"
                f"Уровень {fmt.fmt_num(pet['level'])}"
                + (f" · EXP {fmt.fmt_num(pet['exp'])}" if pet["exp"] else "")
            )
            items.append(_sep())
            items.append(_section(pet_text, thumbnail=pet["icon"]))

        items.append(_sep())
        items.append(_td(self._footer_text()))
        return items

    def _tab_account(self) -> list[discord.ui.Item]:
        data = self._data()
        social = data["social"]
        credit = data["credit"]
        diamond = data["diamond"]
        basic = data["basic"]
        prime = basic.get("primeInfo") or basic.get("primePrivilegeDetail") or {}

        f = self._fields()
        items: list[discord.ui.Item] = [_td("-# АККАУНТ · ACCOUNT")]

        info_lines = [
            f"Создан: {fmt.fmt_ts(f['create_at'])}",
            f"Последний вход: {fmt.fmt_ts(f['last_login'])}",
            f"Регион: {fmt.region_label(f['region'] or self.region)}",
            f"Язык: {fmt.language_label(social)}",
            f"Любимый режим: {fmt.mode_prefer(social)}",
            f"Версия FF: {f['version'] or '—'} · Сезон: {fmt.fmt_num(f['season'])}",
        ]
        items.append(_td("\n".join(info_lines)))

        extra_lines = [
            f"Credit Score: **{fmt.fmt_num(credit.get('creditScore'))}**",
        ]
        if prime.get("primeLevel"):
            extra_lines.append(f"Prime: **{fmt.fmt_num(prime.get('primeLevel'))}**")
        if diamond.get("diamondCost") is not None:
            extra_lines.append(
                f"Diamond Cost: **{fmt.fmt_num(diamond.get('diamondCost'))}**"
            )
        items.append(_sep())
        items.append(_td("\n".join(extra_lines)))

        clan_name = (
            data["clan"].get("clanName")
            or data["guild"].get("name")
            or data["guild"].get("guildName")
        )
        team_lines: list[str] = []
        if clan_name:
            kind = "Клан" if data["clan"].get("clanName") else "Гильдия"
            team_lines.append(f"**{kind}:** {fmt.safe_text(clan_name, limit=60)}")
        members = data["clan"].get("memberNum")
        capacity = data["clan"].get("capacity")
        if members is not None:
            team_lines.append(
                f"Участники: **{fmt.fmt_num(members)}**/{fmt.fmt_num(capacity)}"
            )
        level = data["clan"].get("clanLevel")
        if level is not None:
            team_lines.append(f"Уровень клана: **{fmt.fmt_num(level)}**")
        if team_lines:
            items.append(_sep())
            items.append(_td("\n".join(team_lines)))

        items.append(_sep())
        items.append(_td(self._footer_text()))
        return items

    # ------------------------------------------------------------- callbacks

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Этот профиль управляет только участник, вызвавший `/ff info`.",
            ephemeral=True,
        )

    async def _on_button(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(interaction):
            await self._deny(interaction)
            return
        if self._expired:
            await interaction.response.send_message(
                "Срок действия профиля истёк — вызовите `/ff info` снова.",
                ephemeral=True,
            )
            return

        custom_id = str(interaction.data.get("custom_id") or "")
        if custom_id == "ffp:close":
            await self._do_close(interaction)
            return
        if custom_id.startswith("ffp:"):
            tab = custom_id.split(":", 1)[1]
            if tab in dict(TABS):
                self.tab = tab
                self._render()
                await interaction.response.edit_message(view=self)

    async def _do_close(self, interaction: discord.Interaction) -> None:
        """Удалить сообщение с профилем."""
        message = getattr(interaction, "message", None) or self.bound_message
        try:
            if message is not None:
                await message.delete()
            else:
                await interaction.delete_original_response()
        except Exception:
            logger.debug("FF profile close failed", exc_info=True)
            try:
                await interaction.response.send_message(
                    "Не удалось удалить сообщение.", ephemeral=True
                )
            except Exception:
                logger.debug("FF profile close fallback failed", exc_info=True)

    async def on_timeout(self) -> None:
        """Отключить кнопки и попросить вызвать команду заново."""
        expired = FreeFireProfileView(
            uid=self.uid,
            region=self.region,
            owner_id=self.owner_id,
            data=self.result,
            timeout=None,
            expired=True,
        )
        message = self.bound_message
        if message is not None:
            try:
                await message.edit(view=expired)
            except Exception:
                logger.debug("FF profile timeout edit failed", exc_info=True)
