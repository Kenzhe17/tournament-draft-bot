"""V2-интерфейс Зала славы: баннер + категория + StringSelect."""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from services.hall_of_fame.records import CATEGORIES, Record

BANNER_PATH = "assets/booyah/booyah.png"
BANNER_FILENAME = "booyah.png"


def banner_available() -> bool:
    from pathlib import Path
    return Path(BANNER_PATH).exists()


def banner_file() -> discord.File | None:
    if not banner_available():
        return None
    return discord.File(BANNER_PATH, filename=BANNER_FILENAME)


def _display_name(record: Record, guild: discord.Guild | None) -> str:
    """Имя игрока без упоминания (ping).

    Технические ID (<=0, тестовые боты) никогда не упоминаются.
    """
    uid = record.user_id
    if uid is None:
        return ""
    if guild is not None:
        member = guild.get_member(uid)
        if member is not None:
            return member.display_name
    if record.user_name:
        return record.user_name
    return f"ID {uid}" if uid > 0 else ""


def _record_block(rec: Record, guild: discord.Guild | None) -> str:
    lines = [f"**{rec.title}**"]
    if not rec.available:
        lines.append("*Рекорд пока не установлен.*")
        return "\n".join(lines)
    who = _display_name(rec, guild)
    value_line = f"`{rec.value}`" if rec.value else ""
    ctx = " · ".join(x for x in (who, rec.context) if x)
    lines.append(value_line)
    if ctx:
        lines.append(ctx)
    return "\n".join(lines)


class HallOfFameView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        guild_id: int,
        invoker_id: int,
        category_key: str = "combat",
        data: dict | None = None,
        guild: discord.Guild | None = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.invoker_id = invoker_id
        self.category_key = category_key
        self._data = data
        self._guild = guild
        self._render()

    def _render(self) -> None:
        container = discord.ui.Container()

        if banner_available():
            container.add_item(discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=f"attachment://{BANNER_FILENAME}")
            ))
            container.add_item(discord.ui.Separator(large=True))

        cat_title = next(t for k, t in CATEGORIES if k == self.category_key)
        container.add_item(discord.ui.TextDisplay(content="> ## ЗАЛ СЛАВЫ r!z3"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(content=f"**{cat_title.upper()}**"))
        container.add_item(discord.ui.Separator())

        cat = self._data.get(self.category_key) if self._data else None
        if cat and cat.records:
            blocks = [
                _record_block(r, self._guild) for r in cat.records
            ]
            container.add_item(discord.ui.TextDisplay(
                content="\n\n".join(blocks)
            ))
        else:
            container.add_item(discord.ui.TextDisplay(
                content="*Рекорды пока не установлены.*"
            ))

        container.add_item(discord.ui.Separator())

        select = discord.ui.Select(
            placeholder="Выберите категорию…",
            custom_id=f"hof_cat:{self.guild_id}",
            options=[
                discord.SelectOption(label=title, value=key)
                for key, title in CATEGORIES
            ],
        )
        for opt in select.options:
            opt.default = opt.value == self.category_key
        select.callback = self._on_select
        row = discord.ui.ActionRow()
        row.add_item(select)
        container.add_item(row)

        now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            content=f"*Обновлено: {now} · данные текущей эпохи статистики*"
        ))

        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Это меню доступно только участнику, вызвавшему `/booyah`.",
                ephemeral=True,
            )
            return
        key = interaction.data["values"][0]
        from services.hall_of_fame.cache import get_cached
        data = await get_cached(self.guild_id)
        new_view = HallOfFameView(
            guild_id=self.guild_id,
            invoker_id=self.invoker_id,
            category_key=key,
            data=data,
            guild=interaction.guild,
            timeout=None,
        )
        file = banner_file()
        kwargs = {"view": new_view}
        if file is not None:
            kwargs["file"] = file
        await interaction.response.edit_message(**kwargs)
