"""V2-предложение руки и сердца: баннер + кнопки Принять/Отклонить.

Состояния: pending / accepted / declined / expired / failed.
Кнопки активны только в pending и только для адресата.
Брак записывается атомарно через player_stats_store.try_marry,
уведомление дедуплицируется через marriage_store.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import discord

from services.social.common import (
    BANNER_MARRIAGE, BANNER_PROPOSAL, banner_file, social_view,
)
from utils.design.containers import separator
from storage.marriage_store import marriage_store
from storage.player_stats_store import player_stats_store

logger = logging.getLogger(__name__)

_TIMEOUT = 300  # как в прежней реализации
_STATE_TEXT = {
    "pending": None,  # рендерится динамически
    "accepted": "*Предложение принято.*",
    "declined": "*Предложение отклонено.*",
    "expired": "*Срок предложения истёк.*",
    "failed": "*Не удалось заключить брак.*",
}


async def _get_or_create_stats(guild_id: int, user_id: int, display_name: str):
    from views.profile_view import _default_stats
    stats = await player_stats_store.get(guild_id, user_id)
    if stats is None:
        stats = _default_stats(guild_id, user_id, display_name)
        await player_stats_store.set(stats)
    return stats


def build_proposal_view(
    proposer: discord.Member,
    target: discord.Member,
    *,
    state: str = "pending",
    guild_id: int | None = None,
) -> "ProposalView":
    view = ProposalView(
        guild_id=guild_id or proposer.guild.id,
        proposer=proposer,
        target=target,
        state=state,
        timeout=_TIMEOUT if state == "pending" else None,
    )
    return view


class ProposalView(discord.ui.LayoutView):
    """LayoutView предложения: Container(баннер, текст, ActionRow)."""

    def __init__(
        self,
        *,
        guild_id: int,
        proposer: discord.Member,
        target: discord.Member,
        state: str = "pending",
        timeout: float | None = _TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.proposer = proposer
        self.target = target
        self.state = state
        self._lock = asyncio.Lock()

        container = discord.ui.Container()
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media=f"attachment://{BANNER_PROPOSAL}"
                )
            )
        )
        container.add_item(separator(large=True))
        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    "**Предложение руки и сердца**\n\n"
                    f"**{proposer.mention}** предлагает **{target.mention}** "
                    "связать свои судьбы на r!z3.\n\n"
                    "**Примешь ли ты это предложение?**"
                )
            )
        )
        container.add_item(separator())
        status = _STATE_TEXT.get(state)
        if status is None:
            status = f"*Ожидаем решения {target.mention}.*"
        container.add_item(discord.ui.TextDisplay(content=status))

        row = discord.ui.ActionRow()
        row.add_item(_AcceptButton(self))
        row.add_item(_DeclineButton(self))
        for btn in row.children:
            btn.disabled = state != "pending"
        container.add_item(row)
        self.add_item(container)

    async def on_timeout(self) -> None:
        if self.state != "pending":
            return
        self.state = "expired"
        msg = getattr(self, "message", None)
        if msg is None:
            return
        try:
            new_view = build_proposal_view(
                self.proposer, self.target, state="expired",
                guild_id=self.guild_id)
            kwargs = {}
            if msg.attachments:
                kwargs["attachments"] = list(msg.attachments)
            await msg.edit(view=new_view, **kwargs)
        except Exception:
            logger.debug("proposal timeout edit failed", exc_info=True)


class _ProposalButton(discord.ui.Button):
    def __init__(self, view: ProposalView, label: str, style) -> None:
        super().__init__(style=style, label=label)
        self.proposal = view

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.proposal.target.id:
            await interaction.response.send_message(
                "Это предложение адресовано другому участнику.",
                ephemeral=True,
            )
            return False
        return True


class _AcceptButton(_ProposalButton):
    def __init__(self, view: ProposalView) -> None:
        super().__init__(view, "Принять", discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        p = self.proposal
        if not await self._authorized(interaction):
            return
        async with p._lock:
            if p.state != "pending":
                await interaction.response.send_message(
                    "Это предложение уже обработано.", ephemeral=True)
                return
            p.state = "accepted"
            try:
                ok = await _accept(p)
            except Exception:
                logger.exception("marry accept failed")
                ok = False
            if not ok:
                p.state = "failed"
        await _refresh(interaction, p)
        if p.state == "accepted":
            await _send_marriage_notice(interaction, p)


class _DeclineButton(_ProposalButton):
    def __init__(self, view: ProposalView) -> None:
        super().__init__(view, "Отклонить", discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        p = self.proposal
        if not await self._authorized(interaction):
            return
        async with p._lock:
            if p.state != "pending":
                await interaction.response.send_message(
                    "Это предложение уже обработано.", ephemeral=True)
                return
            p.state = "declined"
        await _refresh(interaction, p)


async def _accept(p: ProposalView) -> bool:
    """Атомарно сохранить брак + запись в marriages."""
    proposer = p.proposer
    target = p.target
    p_stats = await _get_or_create_stats(
        p.guild_id, proposer.id, proposer.display_name)
    t_stats = await _get_or_create_stats(
        p.guild_id, target.id, target.display_name)
    if p_stats.spouse_id or t_stats.spouse_id:
        return False
    if not await player_stats_store.try_marry(
        p.guild_id, proposer.id, target.id
    ):
        return False
    p_stats.spouse_id = target.id
    t_stats.spouse_id = proposer.id
    is_new, married_at = await marriage_store.record_marriage(
        p.guild_id, proposer.id, target.id)
    p._married_at = married_at
    p._marriage_is_new = is_new
    return True


async def _refresh(interaction: discord.Interaction, p: ProposalView) -> None:
    """Обновить сообщение: пересобрать view с новым состоянием."""
    new_view = build_proposal_view(
        p.proposer, p.target, state=p.state, guild_id=p.guild_id)
    try:
        await interaction.response.edit_message(
            view=new_view, attachments=[interaction.message.attachments[0]]
            if interaction.message.attachments else []
        )
    except Exception:
        logger.exception("proposal refresh failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Предложение обработано.", ephemeral=True)


async def _send_marriage_notice(
    interaction: discord.Interaction, p: ProposalView
) -> None:
    """Торжественное уведомление о браке (один раз на пару)."""
    if not getattr(p, "_marriage_is_new", True):
        return
    married_at = getattr(p, "_married_at", None) or datetime.now().astimezone()
    date_str = married_at.strftime("%d.%m.%Y")

    view = social_view(BANNER_MARRIAGE, timeout=None)
    view.text("**Новый союз на r!z3**\nНа сервере появилась новая пара!")
    view.sep()
    view.text(
        f"**{p.proposer.mention} & {p.target.mention}**\n"
        "Официально связали свои судьбы."
    )
    view.sep()
    view.text(f"**ДАТА БРАКА**\n{date_str}")
    view.sep()
    view.text("*Пусть ваша история будет долгой и счастливой.*")

    try:
        channel = interaction.channel or await interaction.client.fetch_channel(
            interaction.channel_id)
        await channel.send(view=view, file=banner_file(BANNER_MARRIAGE))
    except Exception:
        logger.exception("marriage notice failed")
