"""View для редактирования профиля."""

import discord
from storage.player_stats_store import player_stats_store


class ProfileView(discord.ui.View):
    """View для профиля с кнопками."""

    def __init__(self, guild_id: int, user_id: int, is_owner: bool):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.is_owner = is_owner

        # Добавляем кнопку настроек только для владельца
        if is_owner:
            self.add_item(SettingsButton(guild_id, user_id))


class SettingsButton(discord.ui.Button):
    """Кнопка настроек."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="⚙️ Настройки",
            custom_id=f"profile_settings:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Открыть настройки профиля."""
        # Проверить, что это владелец
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Вы можете настраивать только свой профиль.",
                ephemeral=True
            )
            return

        # Показать модал редактирования
        await interaction.response.send_modal(ProfileEditModal(self.guild_id, self.user_id))


class ProfileEditButton(discord.ui.Button):
    """Кнопка редактирования профиля (устаревшая, сохранена для совместимости)."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="✏️ Изменить профиль",
            custom_id=f"profile_edit:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать модал для редактирования профиля."""
        # Проверить, что это владелец профиля
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Вы можете редактировать только свой профиль.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(ProfileEditModal(self.guild_id, self.user_id))


class ProfileEditModal(discord.ui.Modal, title="Редактирование профиля"):
    """Модал для редактирования ника и описания."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

        self.nickname = discord.ui.TextInput(
            label="Никнейм",
            placeholder="Введите ваш никнейм",
            max_length=32,
            required=False
        )

        self.description = discord.ui.TextInput(
            label="Описание",
            placeholder="Введите описание профиля",
            max_length=256,
            style=discord.TextStyle.paragraph,
            required=False
        )

        # Add items to modal
        self.add_item(self.nickname)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Сохранить изменения профиля."""
        nickname = self.nickname.value
        description = self.description.value

        # Get current stats
        stats = await player_stats_store.get(self.guild_id, self.user_id)

        if not stats:
            await interaction.response.send_message(
                "❌ Профиль не найден. Сначала сыграйте турнир.",
                ephemeral=True
            )
            return

        # Update stats - always save the values
        if nickname:
            stats.name = nickname
        stats.description = description  # Always save description (even if empty)
        
        # Reset avatar to Discord avatar
        stats.avatar_url = None

        # Save updated stats
        await player_stats_store.update(self.guild_id, self.user_id, stats)

        # Regenerate profile embed with updated data
        from cogs.tournament import TournamentCog
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store
        from utils.cosmetics import format_player_name

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        cosmetics = inventory_store.get_player_inventory(self.guild_id, self.user_id)
        inventory_count = len(cosmetics)

        # Get rank
        from cogs.tournament import get_rank_emoji
        rank_title = get_rank_emoji(stats.level)
        current_xp, xp_needed = stats.get_level_progress()

        # Используем статистику турниров вместо мини-игр
        total_games_played = stats.games
        total_games_won = stats.wins

        win_rate = (total_games_won / total_games_played * 100) if total_games_played > 0 else 0

        # Create new embed
        import discord
        embed = discord.Embed(
            title=f"👤 Профиль: {stats.name}",
            color=discord.Color.dark_blue()
        )
        
        # Always use Discord avatar
        embed.set_thumbnail(url=interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url)

        # Био (показываем сразу после заголовка, если есть)
        if stats.description:
            embed.description = stats.description

        # ELO
        embed.add_field(
            name="� ELO",
            value=f"{int(stats.elo)}",
            inline=True
        )

        # Уровень и опыт
        embed.add_field(
            name="📈 Level",
            value=f"{stats.level} | ⭐ Опыт: {current_xp:,} / {xp_needed:,}",
            inline=True
        )

        # Экономика
        embed.add_field(
            name="💵 Баланс",
            value=f"{balance:,} 🪙",
            inline=True
        )

        # Инвентарь
        embed.add_field(
            name="🎒 Предметов",
            value=f"{inventory_count} шт.",
            inline=True
        )

        # Игровая статистика
        embed.add_field(
            name="🎲 Сыграно игр",
            value=f"{total_games_played}",
            inline=True
        )

        embed.add_field(
            name="🏆 Побед",
            value=f"{total_games_won} ({win_rate:.1f}%)",
            inline=True
        )

        embed.add_field(
            name="🎯 AVG Kills",
            value=f"{stats.avg_kills:.2f}",
            inline=True
        )

        embed.add_field(
            name="⚔️ K/D Ratio",
            value=f"{stats.kd_ratio:.2f}",
            inline=True
        )

        embed.add_field(
            name="🔥 Max Kills",
            value=str(stats.best_match_kills),
            inline=True
        )

        elo_change = stats.last_elo_change if hasattr(stats, 'last_elo_change') else 0
        embed.add_field(
            name="📊 Last ELO Change",
            value=f"{elo_change:+d}",
            inline=True
        )

        # Update the original message
        await interaction.response.edit_message(embed=embed)
