"""View для управления ролью организатора."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from config import replace_emojis, WELCOME_BANNER_URL, ORG_ROLE_MAP, TOURNAMENT_CHANNELS

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
        # Map role_id to list of user IDs who have that role
        self.role_holders: dict[int, list[int]] = {}
        self.guild_id = 0
        self.message_id = 0
        self.channel_id = 0

    def to_dict(self) -> dict:
        return {
            "role_holders": {str(k): v for k, v in self.role_holders.items()},
            "guild_id": self.guild_id,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrgSlotState":
        state = cls()
        state.role_holders = {int(k): v for k, v in data.get("role_holders", {}).items()}
        state.guild_id = data.get("guild_id", 0)
        state.message_id = data.get("message_id", 0)
        state.channel_id = data.get("channel_id", 0)
        return state

    def get_holders(self, role_id: int) -> list[int]:
        """Get list of users who have this role."""
        return self.role_holders.get(role_id, [])

    def add_holder(self, role_id: int, user_id: int) -> bool:
        """Add user to role holders. Returns True if successful."""
        if role_id not in self.role_holders:
            self.role_holders[role_id] = []
        if len(self.role_holders[role_id]) >= MAX_ORG_ROLE_HOLDERS:
            return False
        if user_id not in self.role_holders[role_id]:
            self.role_holders[role_id].append(user_id)
        return True

    def remove_holder(self, role_id: int, user_id: int) -> bool:
        """Remove user from role holders. Returns True if successful."""
        if role_id in self.role_holders and user_id in self.role_holders[role_id]:
            self.role_holders[role_id].remove(user_id)
            return True
        return False


def load_state() -> OrgSlotState:
    """Загрузить состояние из файла."""
    if ORG_SLOT_FILE.exists():
        try:
            with open(ORG_SLOT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                state = OrgSlotState.from_dict(data)
                # Migrate old format (organizers list) to new format (role_holders dict)
                if hasattr(state, 'organizers') and state.organizers:
                    from config import ORG_1_ROLE_ID
                    state.role_holders = {ORG_1_ROLE_ID: state.organizers}
                    delattr(state, 'organizers')
                return state
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

    def __init__(self, state: OrgSlotState, role_id: int, role_name: str):
        holders = state.get_holders(role_id)
        is_full = len(holders) >= MAX_ORG_ROLE_HOLDERS
        super().__init__(
            label=f"Взять {role_name}",
            style=discord.ButtonStyle.green if not is_full else discord.ButtonStyle.red,
            disabled=is_full,
            custom_id=f"org_take_role_{role_id}"
        )
        self.state = state
        self.role_id = role_id
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        if interaction.user.id in state.get_holders(self.role_id):
            await interaction.response.send_message(
                "❌ У вас уже есть эта роль!",
                ephemeral=True
            )
            return

        if len(state.get_holders(self.role_id)) >= MAX_ORG_ROLE_HOLDERS:
            await interaction.response.send_message(
                f"❌ Достигнут лимит для {self.role_name} ({MAX_ORG_ROLE_HOLDERS})!",
                ephemeral=True
            )
            return

        if not self.role_id:
            await interaction.response.send_message(
                "❌ Роль организатора не настроена",
                ephemeral=True
            )
            return

        try:
            # Give role to user
            role = interaction.guild.get_role(self.role_id)
            if not role:
                await interaction.response.send_message(
                    f"❌ Роль организатора не найдена на сервере (ID: {self.role_id})",
                    ephemeral=True
                )
                return

            await interaction.user.add_roles(role)

            # Update state
            state.add_holder(self.role_id, interaction.user.id)
            state.guild_id = interaction.guild.id
            state.channel_id = interaction.channel.id
            state.message_id = interaction.message.id
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed
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

    def __init__(self, state: OrgSlotState, role_id: int, role_name: str):
        has_role = interaction.user.id in state.get_holders(role_id) if hasattr(self, 'has_user_role') else False
        super().__init__(
            label=f"Сдать {role_name}",
            style=discord.ButtonStyle.red,
            custom_id=f"org_release_role_{role_id}"
        )
        self.state = state
        self.role_id = role_id
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        if interaction.user.id not in state.get_holders(self.role_id):
            await interaction.response.send_message(
                f"❌ У вас нет роли {self.role_name}!",
                ephemeral=True
            )
            return

        if not self.role_id:
            await interaction.response.send_message(
                "❌ Роль организатора не настроена",
                ephemeral=True
            )
            return

        try:
            # Remove role from user
            role = interaction.guild.get_role(self.role_id)
            if role and role in interaction.user.roles:
                await interaction.user.remove_roles(role)

            # Update state
            state.remove_holder(self.role_id, interaction.user.id)
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed
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

        try:
            # Remove all 3 organizer roles from all members
            from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID
            role_ids = [ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID]

            for role_id in role_ids:
                role = interaction.guild.get_role(role_id)
                if role:
                    for member in interaction.guild.members:
                        if role in member.roles:
                            await member.remove_roles(role)

            # Reset state
            state.role_holders = {}
            save_state(state)

            # Rebuild view with updated state
            view = OrgRoleView(state)

            # Update embed
            new_embed = build_org_role_embed(interaction.guild, state)
            await interaction.response.edit_message(embed=new_embed, view=view)

            await interaction.followup.send(
                "✅ Все роли организаторов сброшены для всех пользователей",
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
        # Add 3 take/release button pairs for each role
        from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID
        roles = [
            (ORG_1_ROLE_ID, "ORG-1"),
            (ORG_2_ROLE_ID, "ORG-2"),
            (ORG_3_ROLE_ID, "ORG-3"),
        ]
        for role_id, role_name in roles:
            self.add_item(TakeRoleButton(state, role_id, role_name))
            self.add_item(ReleaseRoleButton(state, role_id, role_name))
        self.add_item(ResetButton())


def build_org_role_embed(guild: discord.Guild, state: OrgSlotState) -> discord.Embed:
    """Построить embed для управления ролью организатора."""
    from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID

    # Build status section for each role
    status_section = ""
    roles = [
        (ORG_1_ROLE_ID, "ORG-1"),
        (ORG_2_ROLE_ID, "ORG-2"),
        (ORG_3_ROLE_ID, "ORG-3"),
    ]

    for role_id, role_name in roles:
        holders = state.get_holders(role_id)
        count = len(holders)
        if count > 0:
            holder_names = []
            for holder_id in holders:
                member = guild.get_member(holder_id)
                name = member.display_name if member else f"<@{holder_id}>"
                holder_names.append(name)
            status_section += (
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **{role_name}:** **Занят** ({count}/{MAX_ORG_ROLE_HOLDERS}) - {', '.join(holder_names)}\n"
            )
        else:
            status_section += (
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **{role_name}:** **Свободен** (0/{MAX_ORG_ROLE_HOLDERS})\n"
            )

    description = (
        f"{replace_emojis('white_arrow')} **Система бронирования ролей Организаторов**\n\n"
        f"Каждая роль (ORG-1, ORG-2, ORG-3) соответствует своему каналу турнира.\n"
        f"До **{MAX_ORG_ROLE_HOLDERS} человек** могут одновременно иметь одну роль.\n\n"
        f"{replace_emojis('a_sparkle')} **ПРАВИЛА ИСПОЛЬЗОВАНИЯ:**\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Нажмите **«Взять»** для нужной роли, чтобы получить доступ к каналу.\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} После завершения нажмите **«Сдать»**.\n"
        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Если кнопка красная — слот полон ({MAX_ORG_ROLE_HOLDERS}).\n\n"
        f"{replace_emojis('a_sparkle')} **ТЕКУЩИЙ СТАТУС:**\n"
        f"{status_section}\n\n"
        f"{replace_emojis('a_sparkle')} *Администрация оставляет за собой право сбросить роли в любой момент.*"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} r1z3 | УПРАВЛЕНИЕ РОЛЯМИ ОРГАНИЗАТОРОВ {replace_emojis('a_star')}",
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
