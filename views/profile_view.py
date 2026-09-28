"""View для редактирования профиля."""
from config import replace_emojis

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
                replace_emojis("❌ Вы можете настраивать только свой профиль."),
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
            label=replace_emojis("✏️ Изменить профиль"),
            custom_id=f"profile_edit:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать модал для редактирования профиля."""
        # Проверить, что это владелец профиля
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы можете редактировать только свой профиль."),
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
        was_new_profile = stats is None

        # Создать профиль если его нет
        if not stats:
            from models.player_stats import PlayerStats
            stats = PlayerStats(
                guild_id=self.guild_id,
                user_id=self.user_id,
                name=interaction.user.display_name,
                elo=1000,
                wins=0,
                finals=0,
                games=0,
                current_streak=0,
                best_win_streak=0,
                best_loss_streak=0,
                total_kills=0,
                total_deaths=0,
                best_match_kills=0,
                total_elo_change=0,
                last_elo_change=0,
                xp=0,
                level=1,
                xp_to_next_level=100,
                total_earnings=0,
                tournament_participations=0,
                description=""
            )

        # Update stats - always save the values
        if nickname:
            stats.name = nickname
        stats.description = description  # Always save description (even if empty)
        # Don't reset avatar_url - keep existing avatar

        # Save stats (set for new profiles, update for existing)
        if was_new_profile:
            await player_stats_store.set(stats)
        else:
            await player_stats_store.update(self.guild_id, self.user_id, stats)

        # Regenerate profile embed with updated data
        from cogs.tournament import TournamentCog, get_rank_emoji
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store
        from utils.cosmetics import format_player_name

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        cosmetics = inventory_store.get_player_inventory(self.guild_id, self.user_id)
        inventory_count = len(cosmetics)

        # Get rank
        rank_title = get_rank_emoji(stats.level)
        current_xp, xp_needed = stats.get_level_progress()

        # Используем статистику турниров вместо мини-игр
        total_games_played = stats.games
        total_games_won = stats.wins

        # Винрейт
        win_rate = (total_games_won / total_games_played * 100) if total_games_played > 0 else 0

        # Last ELO Change
        elo_change = stats.last_elo_change if hasattr(stats, 'last_elo_change') else 0

        # Build description (new style)
        description_parts = ["Основная информация и статистика игрока:\n"]

        # Игровой профиль
        description_parts.append(f"{replace_emojis('⚪')} **Игровой профиль:**")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Ранг: {rank_title}")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} ELO: {int(stats.elo):,} `(Last: {elo_change:+d})`")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Уровень: Level {stats.level} `({current_xp:,} / {xp_needed:,} XP)`")
        description_parts.append("")

        # Экономика
        description_parts.append(f"{replace_emojis('⚪')} **Экономика:**")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Баланс: {balance:,} 💰")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Предметов: {inventory_count} шт.")
        description_parts.append("")

        # Статистика игр
        description_parts.append(f"{replace_emojis('⚪')} **Статистика игр:**")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Сыграно: {total_games_played} игр `(Побед: {total_games_won} | {win_rate:.1f}%)`")
        
        # Show K/D and kills only if player has played games
        if total_games_played > 0:
            description_parts.append(f"{replace_emojis('a_dot_smaller')} K/D Ratio: {stats.kd_ratio:.2f}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} AVG Kills: {stats.avg_kills:.2f}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Max Kills: {stats.best_match_kills}")
        else:
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Ещё не играл в турниры")
        
        description_parts.append("")

        # Био
        if stats.description:
            description_parts.append(f"{replace_emojis('⚪')} **О себе:**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} {stats.description}")
            description_parts.append("")

        description_parts.append(f"{replace_emojis('a_dot_smaller')} Данные обновляются в реальном времени")

        # Create new embed (new style)
        import discord
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} ПРОФИЛЬ - {stats.name}",
            description="\n".join(description_parts),
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url)

        # Update the original message
        await interaction.response.edit_message(embed=embed)
