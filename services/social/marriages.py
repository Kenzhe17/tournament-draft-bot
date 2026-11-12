"""V2-карточка /браки: список действующих браков с пагинацией.

8 браков на страницу, кнопки «Назад»/«Далее» внутри Container.
Пагинацией может пользоваться только вызвавший команду.
"""

from __future__ import annotations

from datetime import UTC

import discord

from services.social.common import (
    BANNER_MARRIAGES,
    banner_file,
)

PAGE_SIZE = 5


def _fmt_date(dt) -> str:
    return dt.strftime("%d.%m.%Y") if dt else "—"


def _days_together(married_at) -> int | None:
    """Дней в браке на текущий момент."""
    if not married_at:
        return None
    from datetime import datetime
    now = datetime.now(UTC)
    if married_at.tzinfo is None:
        married_at = married_at.replace(tzinfo=UTC)
    return max(0, (now - married_at).days)


def _entries_text(entries: list[dict], offset: int) -> str:
    lines = []
    for i, e in enumerate(entries[offset:offset + PAGE_SIZE], start=offset + 1):
        days = _days_together(e.get("married_at"))
        together = f" · вместе {days} дн." if days is not None else ""
        lines.append(
            f"**{i:02d} · <@{e['user_a']}> & <@{e['user_b']}>**\n"
            f"Дата заключения: {_fmt_date(e['married_at'])}{together}"
        )
    return "\n\n".join(lines)


def _build_view(
    entries: list[dict],
    *,
    page: int,
    invoker_id: int,
    guild_id: int,
) -> MarriagesView:
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    view = MarriagesView(
        entries=entries, page=page, total_pages=total_pages,
        invoker_id=invoker_id, guild_id=guild_id,
        timeout=None,
    )
    return view


class MarriagesView(discord.ui.LayoutView):
    def __init__(
        self, *,
        entries: list[dict],
        page: int,
        total_pages: int,
        invoker_id: int,
        guild_id: int,
        timeout: float | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.entries = entries
        self.page = page
        self.total_pages = total_pages
        self.invoker_id = invoker_id
        self.guild_id = guild_id

        container = discord.ui.Container()
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=f"attachment://{BANNER_MARRIAGES}")
            )
        )
        from utils.design.containers import separator
        container.add_item(separator(large=True))

        if not entries:
            container.add_item(discord.ui.TextDisplay(
                content="**Браки участников r!z3**\n"
                        "Союзы, заключённые в нашем сообществе."
            ))
            container.add_item(separator())
            container.add_item(discord.ui.TextDisplay(
                content="Пока на сервере нет действующих союзов."
            ))
            container.add_item(separator())
            container.add_item(discord.ui.TextDisplay(
                content="*Возможно, первая история начнётся именно с тебя.*"
            ))
            self.add_item(container)
            return

        container.add_item(discord.ui.TextDisplay(
            content="**Браки участников r!z3**\n"
                    "Союзы, заключённые в нашем сообществе."
        ))
        container.add_item(separator())
        container.add_item(discord.ui.TextDisplay(
            content=_entries_text(entries, page * PAGE_SIZE)
        ))
        container.add_item(separator())
        container.add_item(discord.ui.TextDisplay(
            content=f"**Всего действующих браков: {len(entries)}**"
                    + (f" · стр. {page + 1}/{total_pages}" if total_pages > 1 else "")
        ))

        if total_pages > 1:
            container.add_item(separator())
            row = discord.ui.ActionRow()
            prev_btn = discord.ui.Button(
                label="Назад", style=discord.ButtonStyle.secondary,
                custom_id="marriages:prev", disabled=page == 0,
            )
            next_btn = discord.ui.Button(
                label="Далее", style=discord.ButtonStyle.secondary,
                custom_id="marriages:next",
                disabled=page >= total_pages - 1,
            )
            prev_btn.callback = self._on_prev
            next_btn.callback = self._on_next
            row.add_item(prev_btn)
            row.add_item(next_btn)
            container.add_item(row)

        self.add_item(container)

    async def _check_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Эту команду может использовать только вызвавший её участник.",
                ephemeral=True,
            )
            return False
        return True

    async def _go(self, interaction: discord.Interaction, page: int) -> None:
        if not await self._check_invoker(interaction):
            return
        new_view = _build_view(
            self.entries, page=page,
            invoker_id=self.invoker_id, guild_id=self.guild_id,
        )
        try:
            # V2-edit не сохраняет attachment:// автоматически —
            # баннер нужно переслать файлом (как в proposal._refresh).
            await interaction.response.edit_message(
                view=new_view,
                attachments=[marriages_file()],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("marriages page edit failed")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Не удалось обновить страницу. Попробуйте ещё раз.",
                    ephemeral=True,
                )

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, self.page - 1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._go(interaction, self.page + 1)


def build_marriages_view(
    entries: list[dict], *, invoker_id: int, guild_id: int,
) -> discord.ui.LayoutView:
    """Первая страница списка браков."""
    return _build_view(entries, page=0, invoker_id=invoker_id, guild_id=guild_id)


def marriages_file() -> discord.File:
    return banner_file(BANNER_MARRIAGES)
