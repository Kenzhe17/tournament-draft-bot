"""View для управления ролью организатора."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from utils.design import compose

if TYPE_CHECKING:
    from bot import TournamentBot

from config import DATA_DIR

# File for storing organizer slot state
DATA_DIR.mkdir(exist_ok=True)
ORG_SLOT_FILE = DATA_DIR / "org_slot.json"
MAX_ORG_ROLE_HOLDERS = 1

# Упоминания владельцев ролей в embed — без пингов
_NO_MENTIONS = discord.AllowedMentions.none()

_ORG_ROLES = None  # lazy: (role_id, "ORG-N") из config


def _org_roles() -> list[tuple[int, str]]:
    global _ORG_ROLES
    if _ORG_ROLES is None:
        from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID
        _ORG_ROLES = [
            (ORG_1_ROLE_ID, "ORG-1"),
            (ORG_2_ROLE_ID, "ORG-2"),
            (ORG_3_ROLE_ID, "ORG-3"),
        ]
    return _ORG_ROLES


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
    def from_dict(cls, data: dict) -> OrgSlotState:
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


async def _edit_panel(
    interaction: discord.Interaction,
    state: OrgSlotState,
    view: discord.ui.View,
) -> None:
    """Обновить существующее сообщение панели без баннера и пингов."""
    await interaction.response.edit_message(
        view=compose(build_org_role_embed(interaction.guild, state), view),
        attachments=[],
        allowed_mentions=_NO_MENTIONS,
    )


class TakeRoleButton(discord.ui.Button):
    """Кнопка для взятия роли организатора."""

    def __init__(
        self, state: OrgSlotState, role_id: int, role_name: str,
        slot: int = 0,
    ):
        holders = state.get_holders(role_id)
        is_full = len(holders) >= MAX_ORG_ROLE_HOLDERS
        super().__init__(
            label=f"Взять {role_name}",
            style=discord.ButtonStyle.primary,
            disabled=is_full,
            # slot-суффикс: при совпадающих/нулевых ORG_*_ROLE_ID
            # custom_id всё равно уникален — Discord не отклоняет
            # сообщение целиком.
            custom_id=f"org_take_role_{role_id}_{slot}"
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

            # Stay on role-selection view: кнопки обновляются
            # под текущего пользователя (Взять -> Сдать).
            view = RoleSelectionView(state, interaction.guild, interaction.user.id)
            await _edit_panel(interaction, state, view)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет прав для выдачи ролей",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {e!s}",
                ephemeral=True
            )


class ReleaseRoleButton(discord.ui.Button):
    """Кнопка для сдачи роли организатора."""

    def __init__(
        self, state: OrgSlotState, role_id: int, role_name: str,
        slot: int = 0,
    ):
        super().__init__(
            label=f"Сдать {role_name}",
            style=discord.ButtonStyle.danger,
            custom_id=f"org_release_role_{role_id}_{slot}"
        )
        self.state = state
        self.role_id = role_id
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()

        member = interaction.user
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        has_discord_role = self.role_id in member_role_ids
        # Check if user has the role or is admin
        is_holder = interaction.user.id in state.get_holders(self.role_id)
        is_admin = interaction.user.guild_permissions.administrator

        if not is_holder and not has_discord_role and not is_admin:
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
            # Remove role from user (if admin, remove from the holder; if user, remove from self)
            holders = state.get_holders(self.role_id)
            if not holders and not has_discord_role:
                await interaction.response.send_message(
                    f"❌ Роль {self.role_name} никому не назначена",
                    ephemeral=True
                )
                return

            # If admin, remove from the first holder; if user, remove from self
            target_user_id = (
                interaction.user.id
                if (is_holder or has_discord_role)
                else holders[0]
            )
            target_member = interaction.guild.get_member(target_user_id)

            if target_member:
                role = interaction.guild.get_role(self.role_id)
                if role and role in target_member.roles:
                    await target_member.remove_roles(role)

            # Update state
            state.remove_holder(self.role_id, target_user_id)
            save_state(state)

            # Stay on role-selection view: кнопки обновляются
            # под текущего пользователя.
            view = RoleSelectionView(state, interaction.guild, interaction.user.id)
            await _edit_panel(interaction, state, view)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет прав для снятия ролей",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {e!s}",
                ephemeral=True
            )


class OccupiedRoleButton(discord.ui.Button):
    """Занятая роль: неактивная кнопка-индикатор для чужих пользователей."""

    def __init__(self, role_id: int, role_name: str, slot: int = 0):
        super().__init__(
            label=f"{role_name} · Занята",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id=f"org_occupied_{role_id}_{slot}",
        )


class BackButton(discord.ui.Button):
    """Кнопка для возврата к главному меню."""

    def __init__(self):
        super().__init__(
            label="Назад",
            style=discord.ButtonStyle.secondary,
            custom_id="org_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()
        view = OrgRoleView(state)
        await _edit_panel(interaction, state, view)


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

            # Rebuild main view with updated state
            view = OrgRoleView(state)

            # Update embed
            await _edit_panel(interaction, state, view)

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
                f"❌ Ошибка: {e!s}",
                ephemeral=True
            )


class OrgRoleView(discord.ui.View):
    """View для управления ролью организатора."""

    def __init__(self, state: OrgSlotState):
        super().__init__(timeout=None)
        # Add main button to open role selection
        self.add_item(RoleSelectionButton(state))
        # Add reset button for admins
        self.add_item(ResetButton())


class RoleSelectionButton(discord.ui.Button):
    """Main button to open role selection menu."""

    def __init__(self, state: OrgSlotState):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Взять роль",
            custom_id="org_role_select"
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        state = load_state()
        view = RoleSelectionView(state, interaction.guild, interaction.user.id)
        await _edit_panel(interaction, state, view)


class RoleSelectionView(discord.ui.View):
    """View with 3 role buttons (Take, Release or Occupied)."""

    def __init__(self, state: OrgSlotState, guild: discord.Guild, user_id: int):
        super().__init__(timeout=None)
        self.state = state
        self.guild = guild
        self.user_id = user_id

        # Get user's roles
        user = guild.get_member(user_id)
        user_role_ids = [role.id for role in user.roles] if user else []
        is_admin = user.guild_permissions.administrator if user else False

        for slot, (role_id, role_name) in enumerate(_org_roles()):
            holders = state.get_holders(role_id)
            is_full = len(holders) >= MAX_ORG_ROLE_HOLDERS

            # has_role: запись в state ИЛИ фактическая Discord-роль
            # (защита от рассинхрона state/Discord после рестарта).
            has_role = (
                role_id in user_role_ids
                or user_id in holders
            )

            if has_role or (is_admin and len(holders) > 0):
                # Пользователь владеет ролью (или админ может снять
                # чужую) — показываем действие «Сдать».
                self.add_item(
                    ReleaseRoleButton(state, role_id, role_name, slot)
                )
            elif is_full:
                # Роль занята другим — неактивный индикатор.
                self.add_item(
                    OccupiedRoleButton(role_id, role_name, slot)
                )
            else:
                self.add_item(
                    TakeRoleButton(state, role_id, role_name, slot)
                )

        # Add back button
        self.add_item(BackButton())


def build_org_role_embed(guild: discord.Guild, state: OrgSlotState) -> discord.Embed:
    """Построить компактный embed панели организатора."""
    lines = []
    for role_id, role_name in _org_roles():
        holders = state.get_holders(role_id)
        count = len(holders)
        if count > 0:
            holder_mentions = ", ".join(
                f"<@{holder_id}>" for holder_id in holders
            )
            lines.append(
                f"`{role_name}` — Занята · {count}/{MAX_ORG_ROLE_HOLDERS}"
                f" — {holder_mentions}"
            )
        else:
            lines.append(
                f"`{role_name}` — Свободна · 0/{MAX_ORG_ROLE_HOLDERS}"
            )

    description = (
        "Управление организаторскими ролями\n\n"
        "**ОРГАНИЗАТОРСКИЕ РОЛИ**\n"
        + "\n".join(lines)
        + "\n\nВыберите свободную роль. "
        "После завершения работы сдайте её."
    )

    embed = discord.Embed(
        title="ПАНЕЛЬ ОРГАНИЗАТОРА",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69),
    )
    embed.set_footer(text="r!z3 • Tournament Management")

    return embed


async def setup_org_role_message(
    bot: TournamentBot,
    guild: discord.Guild,
    channel: discord.TextChannel
) -> None:
    """Настроить сообщение для управления ролью организатора."""
    state = load_state()

    # Create main embed (без баннера — панель компактная)
    main_embed = build_org_role_embed(guild, state)

    # Create view
    view = OrgRoleView(state)

    # Send message
    message = await channel.send(
        view=compose(main_embed, view),
        allowed_mentions=_NO_MENTIONS,
    )

    # Save message info
    state.message_id = message.id
    state.channel_id = channel.id
    state.guild_id = guild.id
    save_state(state)

    # Register view for persistence
    bot.add_view(view, message_id=message.id)
