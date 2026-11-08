"""View предложения руки и сердца (/player marry)."""

import logging

import discord
from utils.design import compose, text_card

from storage.player_stats_store import player_stats_store

logger = logging.getLogger(__name__)


async def _get_or_create_stats(guild_id: int, user_id: int, display_name: str):
    """Загрузить статистику игрока или создать дефолтную."""
    from views.profile_view import _default_stats

    stats = await player_stats_store.get(guild_id, user_id)
    if stats is None:
        stats = _default_stats(guild_id, user_id, display_name)
        await player_stats_store.set(stats)
    return stats


class MarriageProposalView(discord.ui.View):
    """Кнопки принятия/отклонения предложения."""

    def __init__(self, guild_id: int, proposer: discord.Member, target: discord.Member):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.proposer = proposer
        self.target = target
        self.add_item(MarryAcceptButton(self))
        self.add_item(MarryDeclineButton(self))


class _ProposalButton(discord.ui.Button):
    def __init__(self, proposal: MarriageProposalView, label: str, style: discord.ButtonStyle):
        super().__init__(style=style, label=label)
        self.proposal = proposal

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.proposal.target.id:
            await interaction.response.send_message(
                "❌ Это предложение адресовано не вам.", ephemeral=True
            )
            return False
        return True


class MarryAcceptButton(_ProposalButton):
    def __init__(self, proposal: MarriageProposalView):
        super().__init__(proposal, "Принять", discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        for item in self.proposal.children:
            item.disabled = True

        proposal = self.proposal
        try:
            p_stats = await _get_or_create_stats(
                proposal.guild_id, proposal.proposer.id, proposal.proposer.display_name
            )
            t_stats = await _get_or_create_stats(
                proposal.guild_id, proposal.target.id, proposal.target.display_name
            )

            if p_stats.spouse_id or t_stats.spouse_id:
                await interaction.response.edit_message(
                    view=text_card("❌ Кто-то из вас уже состоит в браке. Предложение отклонено.")
                )
                return

            p_stats.spouse_id = proposal.target.id
            t_stats.spouse_id = proposal.proposer.id
            await player_stats_store.set(p_stats)
            await player_stats_store.set(t_stats)

            embed = discord.Embed(
                title=" БРАК | /player marry",
                description=(
                    f" {proposal.proposer.mention} и {proposal.target.mention}\n\n"
                    " Теперь вы муж и жена! "
                ),
                color=discord.Color.from_rgb(69, 69, 69),
            )
            await interaction.response.edit_message(
                view=compose(embed, self.proposal)
            )
        except Exception:
            logger.exception("MarryAcceptButton failed")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Не удалось заключить брак. Попробуйте ещё раз.", ephemeral=True
                )


class MarryDeclineButton(_ProposalButton):
    def __init__(self, proposal: MarriageProposalView):
        super().__init__(proposal, "Отклонить", discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        for item in self.proposal.children:
            item.disabled = True
        await interaction.response.edit_message(
            view=text_card(f"❌ {interaction.user.mention} отклонил(а) предложение {self.proposal.proposer.mention}.")
        )
