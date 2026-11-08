"""View для редактирования профиля."""
import logging

from config import replace_emojis

import discord
from utils.design import compose
from storage.player_stats_store import player_stats_store

logger = logging.getLogger(__name__)


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
            label="Настройки",
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
            label="Изменить профиль",
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
        try:
            await self._handle_submit(interaction)
        except Exception:
            logger.exception("ProfileEditModal.on_submit failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Не удалось сохранить профиль. Попробуйте ещё раз.",
                        ephemeral=True
                    )
            except Exception:
                pass

    async def _handle_submit(self, interaction: discord.Interaction) -> None:
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
                xp_to_next_level=250,
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

        # Regenerate profile embed with updated data (same card as /player profile)
        from utils.profile_card import build_profile_embed

        embed = await build_profile_embed(self.guild_id, interaction.user)

        # Update the original message with new embed and view
        view = ProfileView(self.guild_id, self.user_id, True)
        cv2_view = compose(embed, view)
        try:
            # Modal submit has no "original response" — edit the message the modal was opened from
            if interaction.message is not None:
                await interaction.message.edit(view=cv2_view)
                await interaction.response.send_message("✅ Профиль обновлён.", ephemeral=True)
            else:
                await interaction.response.send_message(view=cv2_view, ephemeral=True)
        except Exception:
            logger.exception("ProfileEditModal: failed to update profile message")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(view=cv2_view, ephemeral=True)
                except Exception:
                    pass
