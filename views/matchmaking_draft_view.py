"""View для драфта в matchmaking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ui import View, Select

from matchmaking.manager import matchmaking_manager

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class PlayerSelect(Select):
    """Select Menu с доступными игроками для драфта."""

    def __init__(self, guild_id: int, session, available_players: list[int]):
        self.guild_id = guild_id
        self.session = session

        # Создаем опции с именами игроков
        options = []
        for player_id in available_players:
            name = session.match.player_names.get(player_id, "Unknown")
            options.append(discord.SelectOption(label=name, value=str(player_id)))

        super().__init__(
            placeholder="Выберите игрока...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"mm_draft_select:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработка выбора игрока."""
        session = matchmaking_manager.get_session(self.guild_id)
        if not session:
            await interaction.response.send_message("❌ Сессия не найдена", ephemeral=True)
            return

        draft_data = session.match.draft_data
        current_picker = draft_data["current_picker"]
        pick_index = draft_data["pick_index"]

        # Проверяем, чей сейчас ход
        captain_id = session.match.teams[current_picker].captain_id

        # В тестовом режиме разрешаем выбирать любому
        # В обычном режиме только капитан может выбирать
        if interaction.user.id != captain_id:
            captain_name = session.match.player_names[captain_id]
            await interaction.response.send_message(
                f"❌ Сейчас выбирает капитан {captain_name}",
                ephemeral=True
            )
            return

        # Получаем выбранного игрока
        selected_player_id = int(self.values[0])

        # Проверяем, доступен ли игрок
        if selected_player_id not in draft_data["available"]:
            await interaction.response.send_message("❌ Этот игрок уже выбран", ephemeral=True)
            return

        # Добавляем игрока в команду
        session.match.teams[current_picker].players.append(selected_player_id)

        # Удаляем из доступных
        draft_data["available"].remove(selected_player_id)

        # Переходим к следующему выбору
        draft_data["pick_index"] += 1

        # Проверяем, завершен ли драфт
        if draft_data["pick_index"] >= len(draft_data["pick_order"]):
            # Драфт завершен
            await self.complete_draft(interaction, session)
            return

        # Переходим к следующему капитану
        next_picker = draft_data["pick_order"][draft_data["pick_index"]]
        draft_data["current_picker"] = next_picker

        # Обновляем embed
        await self.update_draft_embed(interaction, session)

    async def update_draft_embed(self, interaction: discord.Interaction, session):
        """Обновить embed драфта."""
        from storage.player_stats_store import player_stats_store

        draft_data = session.match.draft_data
        current_picker = draft_data["current_picker"]
        captain_name = session.match.player_names[session.match.teams[current_picker].captain_id]

        # Получаем ELO для доступных игроков
        player_elos = []
        for player_id in draft_data["available"]:
            stats = await player_stats_store.get_stats(session.guild_id, player_id)
            elo = stats.elo if stats else 1000
            player_elos.append((player_id, elo))

        # Сортируем по ELO
        player_elos.sort(key=lambda x: x[1], reverse=True)

        embed = discord.Embed(
            title="🎲 Драфт",
            description=f"Капитан {captain_name} выбирает.",
            color=discord.Color.blue()
        )

        # Показываем доступных игроков с ELO
        available_text = ""
        for i, (pid, elo) in enumerate(player_elos):
            name = session.match.player_names[pid]
            available_text += f"{i + 1}. {name} ({elo} ELO)\n"

        embed.add_field(name="Доступные игроки:", value=available_text, inline=False)

        # Показываем текущие команды
        team0_names = [session.match.player_names[pid] for pid in session.match.teams[0].players]
        team1_names = [session.match.player_names[pid] for pid in session.match.teams[1].players]

        embed.add_field(name=f"{session.match.teams[0].name}:", value="\n".join(team0_names), inline=True)
        embed.add_field(name=f"{session.match.teams[1].name}:", value="\n".join(team1_names), inline=True)

        # Создаем новый view с обновленными опциями
        view = MatchmakingDraftView(session.guild_id, session)

        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.NotFound:
            pass

    async def complete_draft(self, interaction: discord.Interaction, session):
        """Завершить драфт и перейти к настройке команд."""
        # Показываем результаты драфта
        team0_names = [session.match.player_names[pid] for pid in session.match.teams[0].players]
        team1_names = [session.match.player_names[pid] for pid in session.match.teams[1].players]

        embed = discord.Embed(
            title="✅ Драфт завершен",
            color=discord.Color.green()
        )

        embed.add_field(
            name=f"{session.match.teams[0].name}",
            value="\n".join(team0_names),
            inline=True
        )
        embed.add_field(
            name=f"{session.match.teams[1].name}",
            value="\n".join(team1_names),
            inline=True
        )

        await interaction.response.edit_message(embed=embed, view=None)

        # Переходим к фазе настройки команд
        session.start_team_setup()

        # Показываем TeamSetupView
        from views.team_setup_view import TeamSetupView
        view = TeamSetupView(session.guild_id, session)

        setup_embed = discord.Embed(
            title="⚙️ Настройка команд",
            description="Капитаны могут изменить название команды и нажать Ready когда будут готовы.",
            color=discord.Color.orange()
        )

        await interaction.followup.send(embed=setup_embed, view=view)

        # Добавляем кнопки для ставок
        from views.bet_views import BetButton, ViewBetsButton, ToggleBettingButton
        matches = [(0, 1)]  # Одна игра между командой 0 и командой 1

        betting_view = discord.ui.View(timeout=None)
        betting_view.add_item(BetButton(session.guild_id, session.match, matches, "matchmaking"))
        betting_view.add_item(ViewBetsButton(session.guild_id, session.match, matches, "matchmaking"))
        betting_view.add_item(ToggleBettingButton(session.guild_id, session.match.betting_open))

        betting_embed = discord.Embed(
            title="💰 Ставки",
            description="Делайте ставки на матч! Ставки закроются когда обе команды нажмут Ready.",
            color=discord.Color.gold()
        )

        await interaction.followup.send(embed=betting_embed, view=betting_view)


class MatchmakingDraftView(View):
    """View для драфта в matchmaking."""

    def __init__(self, guild_id: int, session):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.session = session

        draft_data = session.match.draft_data
        available_players = draft_data["available"]

        if available_players:
            self.add_item(PlayerSelect(guild_id, session, available_players))
