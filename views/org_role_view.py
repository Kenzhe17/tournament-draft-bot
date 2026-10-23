"""View для управления ролью организатора."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from config import ORG_ROLE_ID, replace_emojis, WELCOME_BANNER_URL

if TYPE_CHECKING:
    from bot import TournamentBot

# File for storing organizer slot state
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ORG_SLOT_FILE = DATA_DIR / "org_slot.json"
MAX_ORG_ROLE_HOLDERS = 3


class OrgSlotState:
    """Состояние слота организатора."""

    def __init__(self):
        self.organizers: list[int] = []  # List of organizer IDs
        self.guild_id = 0
        self.message_id = 0
        self.channel_id = 0

    def to_dict(self) -> dict:
        return {
            "organizers": self.organizers,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrgSlotState":
        state = cls()
        state.organizers = data.get("organizers", [])
        state.guild_id = data.get("guild_id", 0)
        state.message_id = data.get("message_id", 0)
        state.channel_id = data.get("channel_id", 0)
        return state


def load_state() -> OrgSlotState:
    """Загрузить состояние из файла."""
    if ORG_SLOT_FILE.exists():
        try:
            with open(ORG_SLOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return OrgSlotState.from_dict(data)
        except Exception:
            pass
    return OrgSlotState()


def save_state(state: OrgSlotState) -> None:
    """Сохранить состояние в файл."""
    try:
        with open(ORG_SLOT_FILE, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
    except Exception:
        pass


class TakeRoleButton(discord.ui.Button):
    """Кнопка для взятия роли организатора."""

    def __init__(self, state: OrgSlotState):
        super().__init__(
            label="Взять роль",
            style=discord.ButtonStyle.green,
            disabled=len(state.organizers) >= MAX_ORG_ROLE_HOLDERS,
            custom_id="org_take_role"
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        if interaction.user.id in state.organizers:
            await interaction.response.send_message(
                "❌ У вас уже есть роль организатора!",
                ephemeral=True
            )
            return

        if len(state.organizers) >= MAX_ORG_ROLE_HOLDERS:
            await interaction.response.send_message(
                f"❌ Достигнут лимит организаторов ({MAX_ORG_ROLE_HOLDERS})!",
                ephemeral=True
            )
            return

        if not ORG_ROLE_ID:
            await interaction.response.send_message(
                "❌ Роль организатора не настроена (ORG_ROLE_ID)",
                ephemeral=True
            )
            return

        try:
            # Give role to user
            role = interaction.guild.get_role(ORG_ROLE_ID)
            if not role:
                await interaction.response.send_message(
                    f"❌ Роль организатора не найдена на сервере (ID: {ORG_ROLE_ID})",
                    ephemeral=True
                )
                return

            await interaction.user.add_roles(role)

            # Update state
            state.organizers.append(interaction.user.id)
            state.guild_id = interaction.guild.id
            state.channel_id = interaction.channel.id
            state.message_id = interaction.message.id
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed (image is now embedded in main embed)
            new_embed = build_org_role_embed(interaction.guild, state)
            await interaction.response.edit_message(embed=new_embed, view=view)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет прав для выдачи ролей",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {str(e)}",
                ephemeral=True
            )


class ReleaseRoleButton(discord.ui.Button):
    """Кнопка для сдачи роли организатора."""

    def __init__(self, state: OrgSlotState):
        super().__init__(
            label="Сдать роль",
            style=discord.ButtonStyle.red,
            disabled=not state.organizers,
            custom_id="org_release_role"
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        # Check if user is an organizer or admin
        is_admin = interaction.user.guild_permissions.administrator
        is_organizer = interaction.user.id in state.organizers

        if not (is_admin or is_organizer):
            await interaction.response.send_message(
                "❌ Вы не являетесь организатором!",
                ephemeral=True
            )
            return

        if not ORG_ROLE_ID:
            await interaction.response.send_message(
                "❌ Роль организатора не настроена (ORG_ROLE_ID)",
                ephemeral=True
            )
            return

        try:
            # Remove role from user
            role = interaction.guild.get_role(ORG_ROLE_ID)
            if role and role in interaction.user.roles:
                await interaction.user.remove_roles(role)

            # Update state
            if interaction.user.id in state.organizers:
                state.organizers.remove(interaction.user.id)
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed (image is now embedded in main embed)
            new_embed = build_org_role_embed(interaction.guild, state)
            await interaction.response.edit_message(embed=new_embed, view=view)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет прав для снятия ролей",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {str(e)}",
                ephemeral=True
            )


class ResetButton(discord.ui.Button):
    """Кнопка для сброса роли (только для админов)."""

    def __init__(self):
        super().__init__(
            label="Сбросить",
            style=discord.ButtonStyle.secondary,
            custom_id="org_reset"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Эта кнопка только для Администраторов",
                ephemeral=True
            )
            return

        state = load_state()

        if not ORG_ROLE_ID:
            await interaction.response.send_message(
                "❌ Роль организатора не настроена (ORG_ROLE_ID)",
                ephemeral=True
            )
            return

        try:
            # Remove role from all members who have it
            role = interaction.guild.get_role(ORG_ROLE_ID)
            if role:
                for member in interaction.guild.members:
                    if role in member.roles:
                        await member.remove_roles(role)

            # Reset state
            state.organizers = []
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed (image is now embedded in main embed)
            new_embed = build_org_role_embed(interaction.guild, state)
            await interaction.response.edit_message(embed=new_embed, view=view)

            await interaction.followup.send(
                "✅ Роль организатора сброшена для всех пользователей",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет прав для снятия ролей",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {str(e)}",
                ephemeral=True
            )


class OrgRoleView(discord.ui.View):
    """View для управления ролью организатора."""

    def __init__(self, state: OrgSlotState):
        super().__init__(timeout=None)
        self.add_item(TakeRoleButton(state))
        self.add_item(ReleaseRoleButton(state))
        self.add_item(ResetButton())


def build_org_role_embed(guild: discord.Guild, state: OrgSlotState) -> discord.Embed:
    """Построить embed для управления ролью организатора."""
    # Build status section
    count = len(state.organizers)
    if count > 0:
        organizer_names = []
        for org_id in state.organizers:
            member = guild.get_member(org_id)
            name = member.display_name if member else f"<@{org_id}>"
            organizer_names.append(name)
        status_section = (
            f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Слот:** **Занят** ({count}/{MAX_ORG_ROLE_HOLDERS}) - {', '.join(organizer_names)}\n"
        )
    else:
        status_section = (
            f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Слот:** **Свободен** (0/{MAX_ORG_ROLE_HOLDERS})\n"
        )

    description = (
        f"{replace_emojis('white_arrow')} **Система бронирования роли Организатора**\n\n"
        f"Чтобы избежать багов и накладок, роль одновременно могут удерживать до **{MAX_ORG_ROLE_HOLDERS} Организаторов**.\n\n"
        f"{replace_emojis('a_sparkle')} **ПРАВИЛА ИСПОЛЬЗОВАНИЯ:**\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Нажмите **«Взять роль»**, чтобы забронировать время и получить роль.\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} После завершения турнира обязательно нажмите **«Сдать роль»**.\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Если кнопка заблокирована — слот полон ({MAX_ORG_ROLE_HOLDERS}).\n\n"
        f"{replace_emojis('a_sparkle')} **ТЕКУЩИЙ СТАТУС:**\n"
        f"{status_section}\n\n"
        f"{replace_emojis('a_sparkle')} *Администрация оставляет за собой право сбросить роль в любой момент.*"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} r1z3 | УПРАВЛЕНИЕ СЛОТОМ ОРГАНИЗАТОРА {replace_emojis('a_star')}",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_image(url=WELCOME_BANNER_URL)
    embed.set_footer(text="r1z3 Tournament System")

    return embed


async def setup_org_role_message(
    bot: TournamentBot,
    guild: discord.Guild,
    channel: discord.TextChannel
) -> None:
    """Настроить сообщение для управления ролью организатора."""
    state = load_state()

    # Create main embed (image is now embedded)
    main_embed = build_org_role_embed(guild, state)

    # Create view
    view = OrgRoleView(state)

    # Send message
    message = await channel.send(embed=main_embed, view=view)

    # Save message info
    state.message_id = message.id
    state.channel_id = channel.id
    state.guild_id = guild.id
    save_state(state)

    # Register view for persistence
    bot.add_view(view, message_id=message.id)
