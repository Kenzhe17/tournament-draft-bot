"""View для редактирования профиля."""

import discord
from storage.player_stats_store import player_stats_store


class ProfileEditButton(discord.ui.Button):
    """Кнопка редактирования профиля."""

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

        # Get current stats to pre-fill fields
        stats = await player_stats_store.get(guild_id, user_id)
        if stats:
            default_name = stats.name
            default_description = stats.description if hasattr(stats, 'description') else ''
        else:
            default_name = ""
            default_description = ""

        self.nickname = discord.ui.TextInput(
            label="Никнейм",
            placeholder="Введите ваш никнейм",
            default=default_name,
            max_length=32,
            required=False
        )

        self.description = discord.ui.TextInput(
            label="Описание",
            placeholder="Введите описание профиля",
            default=default_description,
            max_length=256,
            style=discord.TextStyle.paragraph,
            required=False
        )

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

        # Update stats
        if nickname:
            stats.name = nickname
        if description:
            stats.description = description

        # Save updated stats
        await player_stats_store.update(self.guild_id, self.user_id, stats)

        await interaction.response.send_message(
            "✅ Профиль успешно обновлен!",
            ephemeral=True
        )
