"""Панель организатора — единое CV2-сообщение с ролями ORG-1/2/3.

Одна постоянная панель: баннер (MediaGallery) + статусы ролей +
динамические кнопки «Взять/Сдать» + «Сбросить роли» (с подтверждением).
Без промежуточных экранов; состояние — OrgSlotState в org_slot.json,
фактические Discord-роли перепроверяются при каждом действии.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import discord

from config import (
    ADMIN_USER_IDS,
    BOT_OWNER_ID,
    DATA_DIR,
    MANAGER_ROLE_ID,
    WELCOME_BANNER_URL,
)

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# File for storing organizer slot state
DATA_DIR.mkdir(exist_ok=True)
ORG_SLOT_FILE = DATA_DIR / "org_slot.json"
MAX_ORG_ROLE_HOLDERS = 1

# Упоминания владельцев ролей — без пингов
_NO_MENTIONS = discord.AllowedMentions.none()

# Сериализация take/release/reset: файл состояния один, гонки
# двух одновременных «Взять» исключаются блокировкой в процессе.
_PANEL_LOCK = asyncio.Lock()

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


def _sep(*, large: bool = False) -> discord.ui.Separator:
    spacing = (
        discord.SeparatorSpacing.large
        if large
        else discord.SeparatorSpacing.small
    )
    return discord.ui.Separator(spacing=spacing)


def _holders_map(
    guild: discord.Guild | None, state: OrgSlotState
) -> dict[int, set[int]]:
    """Эффективные держатели ролей: state ∪ фактические Discord-роли.

    Скан guild.members — один проход на все ORG-роли; расхождения
    state/Discord показываются объединением (self-heal при действии).
    """
    roles = _org_roles()
    holders: dict[int, set[int]] = {
        rid: set(state.get_holders(rid)) for rid, _ in roles
    }
    if guild is not None:
        wanted = {rid for rid, _ in roles if rid}
        for member in getattr(guild, "members", []) or []:
            member_rids = {r.id for r in getattr(member, "roles", [])}
            hit = member_rids & wanted
            if not hit:
                continue
            for rid in hit:
                holders[rid].add(member.id)
    return holders


def _display_name(guild: discord.Guild | None, user_id: int) -> str:
    """Короткое имя держателя для статуса — без Discord-упоминаний."""
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return f"@{getattr(member, 'display_name', None) or member.name}"
    return f"@{user_id}"


def _can_reset(user, guild: discord.Guild | None) -> bool:
    """Права на «Сбросить роли»: владелец/админы/менеджер-сервер.

    ORG-1/2/3 держатели сбрасывать чужие роли не могут — для этого
    есть кнопка «Сдать» у владельца роли.
    """
    uid = getattr(user, "id", 0)
    if uid and (uid == BOT_OWNER_ID or uid in ADMIN_USER_IDS):
        return True
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    if guild is not None and MANAGER_ROLE_ID:
        user_rids = {r.id for r in getattr(user, "roles", [])}
        if MANAGER_ROLE_ID in user_rids:
            return True
    return False


async def _fresh_member(guild: discord.Guild, user) -> object:
    """Свежий Member с актуальными ролями (fallback — interaction.user)."""
    fetch = getattr(guild, "fetch_member", None)
    if fetch is not None:
        try:
            return await fetch(user.id)
        except Exception:
            pass
    return guild.get_member(user.id) or user


async def _handle_role_button(
    interaction: discord.Interaction, slot: int
) -> None:
    """Единый callback кнопок ролей: взять свободную / сдать свою."""
    roles = _org_roles()
    if slot < 0 or slot >= len(roles):
        await interaction.response.send_message(
            "Неизвестная роль.", ephemeral=True
        )
        return
    role_id, role_name = roles[slot]
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Панель работает только на сервере.", ephemeral=True
        )
        return
    uid = interaction.user.id
    result_text = ""

    async with _PANEL_LOCK:
        state = load_state()
        member = await _fresh_member(guild, interaction.user)
        member_role_ids = {
            r.id for r in getattr(member, "roles", [])
        }
        holders = set(state.get_holders(role_id))
        # Фактические Discord-держатели этой роли (перепроверка)
        discord_holders = {
            m.id
            for m in getattr(guild, "members", []) or []
            if role_id
            and role_id in {r.id for r in getattr(m, "roles", [])}
        }
        occupied_by = (holders | discord_holders) - {uid}

        is_own = uid in holders or uid in discord_holders or (
            bool(role_id) and role_id in member_role_ids
        )

        if is_own:
            # --- Сдать свою роль ---
            if not role_id:
                await interaction.response.send_message(
                    "Роль организатора не настроена.", ephemeral=True
                )
                return
            discord_role = guild.get_role(role_id)
            if (
                discord_role is not None
                and role_id in member_role_ids
            ):
                try:
                    await member.remove_roles(discord_role)
                except discord.Forbidden:
                    await interaction.response.send_message(
                        "У бота нет прав для снятия ролей.",
                        ephemeral=True,
                    )
                    return
            while state.remove_holder(role_id, uid):
                pass
            state.guild_id = guild.id
            state.channel_id = interaction.channel_id
            state.message_id = interaction.message.id
            save_state(state)
            result_text = f"{role_name} сдана."
        elif occupied_by:
            await interaction.response.send_message(
                "Роль уже занята.", ephemeral=True
            )
            return
        else:
            # --- Взять свободную роль ---
            if not role_id:
                await interaction.response.send_message(
                    "Роль организатора не настроена.", ephemeral=True
                )
                return
            discord_role = guild.get_role(role_id)
            if discord_role is None:
                await interaction.response.send_message(
                    f"Роль {role_name} не найдена на сервере.",
                    ephemeral=True,
                )
                return
            # Если у пользователя уже есть другая ORG-роль —
            # сначала снимаем её, затем выдаём новую.
            for other_id, other_name in roles:
                if not other_id or other_id == role_id:
                    continue
                had_state = uid in state.get_holders(other_id)
                had_discord = other_id in member_role_ids
                if not (had_state or had_discord):
                    continue
                other_role = guild.get_role(other_id)
                if other_role is not None and other_id in member_role_ids:
                    try:
                        await member.remove_roles(other_role)
                    except discord.Forbidden:
                        pass
                while state.remove_holder(other_id, uid):
                    pass
            try:
                await member.add_roles(discord_role)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "У бота нет прав для выдачи ролей.", ephemeral=True
                )
                return
            state.add_holder(role_id, uid)
            state.guild_id = guild.id
            state.channel_id = interaction.channel_id
            state.message_id = interaction.message.id
            save_state(state)
            result_text = f"{role_name} взята."

        new_view = OrgPanelView(guild=guild, state=state)

    try:
        await interaction.response.edit_message(
            view=new_view,
            attachments=[],
            allowed_mentions=_NO_MENTIONS,
        )
    except Exception:
        logger.exception("org panel edit failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                result_text, ephemeral=True
            )
            return
    # edit_message — это и есть ответ на interaction; отдельный
    # ephemeral не нужен, но подтверждение действия полезно.
    try:
        await interaction.followup.send(result_text, ephemeral=True)
    except Exception:
        pass


class _RoleSlotButton(discord.ui.Button):
    """Кнопка слота ORG-N. Label/style задаются при сборке панели."""

    def __init__(self, slot: int, *, label: str, occupied: bool):
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.secondary
                if occupied
                else discord.ButtonStyle.primary
            ),
            custom_id=f"org_panel_role_{slot}",
            disabled=not _org_roles()[slot][0],
        )
        self.slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_role_button(interaction, self.slot)


class _ResetRolesButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Сбросить роли",
            style=discord.ButtonStyle.danger,
            custom_id="org_panel_reset",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_reset(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Сброс доступен только владельцу, администратору "
                "или менеджеру сервера.",
                ephemeral=True,
            )
            return
        confirm = discord.ui.View(timeout=60)
        confirm.add_item(_ResetConfirmButton())
        confirm.add_item(_ResetCancelButton())
        await interaction.response.send_message(
            "Сбросить все организаторские роли (ORG-1/2/3) "
            "у всех пользователей?",
            view=confirm,
            ephemeral=True,
        )


class _ResetConfirmButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Подтвердить",
            style=discord.ButtonStyle.danger,
            custom_id="org_panel_reset_yes",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_reset(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Нет прав на сброс.", ephemeral=True
            )
            return
        guild = interaction.guild
        async with _PANEL_LOCK:
            state = load_state()
            role_ids = [rid for rid, _ in _org_roles() if rid]
            discord_roles = {
                rid: guild.get_role(rid) for rid in role_ids
            }
            for member in getattr(guild, "members", []) or []:
                member_rids = {
                    r.id for r in getattr(member, "roles", [])
                }
                for rid, role in discord_roles.items():
                    if role is not None and rid in member_rids:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
                        except Exception:
                            logger.exception(
                                "org reset: remove_roles failed"
                            )
            state.role_holders = {}
            state.guild_id = guild.id
            save_state(state)
            new_view = OrgPanelView(guild=guild, state=state)

        # Обновить публичную панель (не ephemeral-карточку подтверждения)
        try:
            channel_id = state.channel_id or interaction.channel_id
            channel = guild.get_channel(channel_id) or interaction.channel
            if channel is not None and state.message_id:
                message = await channel.fetch_message(state.message_id)
                await message.edit(
                    view=new_view,
                    attachments=[],
                    allowed_mentions=_NO_MENTIONS,
                )
        except Exception:
            logger.exception("org panel refresh after reset failed")

        await interaction.response.send_message(
            "Все организаторские роли сброшены.", ephemeral=True
        )


class _ResetCancelButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Отмена",
            style=discord.ButtonStyle.secondary,
            custom_id="org_panel_reset_no",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Сброс отменён.", ephemeral=True
        )


class OrgPanelView(discord.ui.LayoutView):
    """Единая CV2-панель организатора (persistent, timeout=None).

    Структура: MediaGallery(баннер) → заголовок/описание →
    статусы ORG-1/2/3 → ряд кнопок ролей → «Сбросить роли».
    """

    def __init__(
        self,
        guild: discord.Guild | None = None,
        state: OrgSlotState | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        state = state if state is not None else load_state()
        roles = _org_roles()
        holders = _holders_map(guild, state)

        container = discord.ui.Container()

        if WELCOME_BANNER_URL:
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=WELCOME_BANNER_URL)
                )
            )
            container.add_item(_sep(large=True))

        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    "**ПАНЕЛЬ ОРГАНИЗАТОРА**\n"
                    "Управление организаторскими ролями сервера."
                )
            )
        )
        container.add_item(_sep())

        status_lines = []
        for role_id, role_name in roles:
            role_holders = sorted(holders.get(role_id, ()))
            if role_holders:
                names = ", ".join(
                    _display_name(guild, uid) for uid in role_holders
                )
                status_lines.append(
                    f"`{role_name}` — {names} · "
                    f"{len(role_holders)}/{MAX_ORG_ROLE_HOLDERS}"
                )
            else:
                status_lines.append(
                    f"`{role_name}` — Свободна · "
                    f"0/{MAX_ORG_ROLE_HOLDERS}"
                )
        container.add_item(
            discord.ui.TextDisplay(
                content="**ОРГАНИЗАТОРСКИЕ РОЛИ**\n"
                + "\n".join(status_lines)
            )
        )
        container.add_item(_sep())
        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    "Выберите свободную роль. "
                    "После завершения работы сдайте её."
                )
            )
        )

        row = discord.ui.ActionRow()
        for slot, (role_id, role_name) in enumerate(roles):
            occupied = bool(holders.get(role_id))
            row.add_item(
                _RoleSlotButton(
                    slot,
                    label=(
                        f"Сдать {role_name}"
                        if occupied
                        else f"Взять {role_name}"
                    ),
                    occupied=occupied,
                )
            )
        container.add_item(row)

        row2 = discord.ui.ActionRow()
        row2.add_item(_ResetRolesButton())
        container.add_item(row2)

        self.add_item(container)
        self._guild = guild
        self._state = state


async def setup_org_role_message(
    bot: TournamentBot,
    guild: discord.Guild,
    channel: discord.TextChannel
) -> None:
    """Создать/обновить постоянную панель организатора."""
    state = load_state()

    view = OrgPanelView(guild=guild, state=state)

    message = await channel.send(
        view=view,
        allowed_mentions=_NO_MENTIONS,
    )

    # Save message info
    state.message_id = message.id
    state.channel_id = channel.id
    state.guild_id = guild.id
    save_state(state)

    # Register view for persistence
    bot.add_view(view, message_id=message.id)
