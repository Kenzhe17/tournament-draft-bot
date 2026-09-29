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


class OrgSlotState:
    """Состояние слота организатора."""

    def __init__(self):
        self.occupied = False
        self.organizer_id = 0
        self.guild_id = 0
        self.message_id = 0
        self.channel_id = 0

    def to_dict(self) -> dict:
        return {
            "occupied": self.occupied,
            "organizer_id": self.organizer_id,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrgSlotState":
        state = cls()
        state.occupied = data.get("occupied", False)
        state.organizer_id = data.get("organizer_id", 0)
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
            disabled=state.occupied,
            custom_id="org_take_role"
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        if state.occupied:
            await interaction.response.send_message(
                f"❌ Слот организатора сейчас занят пользователем <@{state.organizer_id}>!",
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
                    "❌ Роль организатора не найдена на сервере",
                    ephemeral=True
                )
                return

            await interaction.user.add_roles(role)

            # Update state
            state.occupied = True
            state.organizer_id = interaction.user.id
            state.guild_id = interaction.guild.id
            state.channel_id = interaction.channel.id
            state.message_id = interaction.message.id
            save_state(state)

            # Update view
            view = interaction.message.view
            if view:
                for item in view.children:
                    if isinstance(item, TakeRoleButton):
                        item.disabled = True
                    elif isinstance(item, ReleaseRoleButton):
                        item.disabled = False

            # Update embed
            embed = interaction.message.embeds[0]
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
            disabled=not state.occupied,
            custom_id="org_release_role"
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        # Check if user is the current organizer or admin
        is_admin = interaction.user.guild_permissions.administrator
        is_organizer = interaction.user.id == state.organizer_id

        if not (is_admin or is_organizer):
            await interaction.response.send_message(
                "❌ Вы не являетесь текущим организатором!",
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
            # Remove role from current organizer
            if state.organizer_id > 0:
                organizer = interaction.guild.get_member(state.organizer_id)
                if organizer:
                    role = interaction.guild.get_role(ORG_ROLE_ID)
                    if role:
                        await organizer.remove_roles(role)

            # Reset state
            state.occupied = False
            state.organizer_id = 0
            save_state(state)

            # Update view
            view = interaction.message.view
            if view:
                for item in view.children:
                    if isinstance(item, TakeRoleButton):
                        item.disabled = False
                    elif isinstance(item, ReleaseRoleButton):
                        item.disabled = True

            # Update embed
            embed = interaction.message.embeds[0]
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
            state.occupied = False
            state.organizer_id = 0
            save_state(state)

            # Update view
            view = interaction.message.view
            if view:
                for item in view.children:
                    if isinstance(item, TakeRoleButton):
                        item.disabled = False
                    elif isinstance(item, ReleaseRoleButton):
                        item.disabled = True

            # Update embed
            embed = interaction.message.embeds[0]
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
    if state.occupied:
        status_section = (
            f"{replace_emojis('dot')} {replace_emojis('white_arrow')} **Слот:** `ЗАНЯТ`\n"
            f"{replace_emojis('dot')} {replace_emojis('white_arrow')} **Организатор:** <@{state.organizer_id}>"
        )
    else:
        status_section = (
            f"{replace_emojis('dot')} {replace_emojis('white_arrow')} **Слот:** `СВОБОДЕН`\n"
            f"{replace_emojis('dot')} {replace_emojis('white_arrow')} **Организатор:** `Никого`"
        )

    description = (
        f"{replace_emojis('white_arrow')} **Система бронирования роли Организатора**\n\n"
        f"Чтобы избежать багов и накладок, роль одновременно может удерживать только **1 Организатор**.\n\n"
        f"{replace_emojis('a_sparkle')} **ПРАВИЛА ИСПОЛЬЗОВАНИЯ:**\n"
        f"{replace_emojis('dot')} {replace_emojis('white_arrow')} Нажмите **«Взять роль»**, чтобы забронировать время и получить права орга.\n"
        f"{replace_emojis('dot')} {replace_emojis('white_arrow')} После завершения турнира обязательно нажмите **«Сдать роль»**.\n"
        f"{replace_emojis('dot')} {replace_emojis('white_arrow')} Если кнопка заблокирована — слот занят другим организатором.\n\n"
        f"{replace_emojis('a_sparkle')} **ТЕКУЩИЙ СТАТУС:**\n"
        f"{status_section}\n\n"
        f"{replace_emojis('a_sparkle')} *Администрация оставляет за собой право сбросить роль в любой момент.*"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} r1z3 | УПРАВЛЕНИЕ СЛОТОМ ОРГАНИЗАТОРА {replace_emojis('a_star')}",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_footer(text="r1z3 Tournament System")

    return embed


async def setup_org_role_message(
    bot: TournamentBot,
    guild: discord.Guild,
    channel: discord.TextChannel
) -> None:
    """Настроить сообщение для управления ролью организатора."""
    state = load_state()

    # Create image embed
    image_embed = discord.Embed(color=discord.Color.from_rgb(69, 69, 69))
    image_embed.set_image(url=WELCOME_BANNER_URL)

    # Create main embed
    main_embed = build_org_role_embed(guild, state)

    # Create view
    view = OrgRoleView(state)

    # Send message
    message = await channel.send(embeds=[image_embed, main_embed], view=view)

    # Save message info
    state.message_id = message.id
    state.channel_id = channel.id
    state.guild_id = guild.id
    save_state(state)

    # Register view for persistence
    bot.add_view(view, message_id=message.id)
