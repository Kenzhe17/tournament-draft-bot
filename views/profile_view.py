"""View для редактирования профиля."""
import logging
from datetime import datetime

from config import replace_emojis

import discord
from utils.design import compose, swap_view
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
    """Кнопка настроек — открывает меню редактирования."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Настройки",
            custom_id=f"profile_settings:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Открыть меню настроек профиля."""
        # Проверить, что это владелец
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы можете настраивать только свой профиль."),
                ephemeral=True
            )
            return

        view = ProfileSettingsView(self.guild_id, self.user_id)
        try:
            # Меню живёт на том же сообщении профиля — модалки потом
            # смогут отредактировать именно его через interaction.message
            await interaction.response.edit_message(
                view=swap_view(interaction.message, view)
            )
        except Exception:
            logger.exception("SettingsButton: failed to open settings menu")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Не удалось открыть настройки. Попробуйте ещё раз.",
                    ephemeral=True
                )


class ProfileSettingsView(discord.ui.View):
    """Меню настроек профиля: День рождения / Никнейм / Описание."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=180)
        self.add_item(BirthdayButton(guild_id, user_id))
        self.add_item(NicknameButton(guild_id, user_id))
        self.add_item(DescriptionButton(guild_id, user_id))
        self.add_item(BackToProfileButton(guild_id, user_id))


class _SettingsFieldButton(discord.ui.Button):
    """Базовая кнопка поля настроек."""

    modal_cls = None
    label_text = ""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=self.label_text,
            custom_id=f"profile_field:{type(self).__name__}:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы можете настраивать только свой профиль."),
                ephemeral=True
            )
            return
        await interaction.response.send_modal(self.modal_cls(self.guild_id, self.user_id))


class _CooldownFieldButton(_SettingsFieldButton):
    """Кнопка поля с кулдауном (раз в 7 дней) — предупреждает до открытия модалки."""

    field = ""

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы можете настраивать только свой профиль."),
                ephemeral=True
            )
            return

        from storage.profile_cooldowns import format_remaining, profile_cooldowns

        left = profile_cooldowns.seconds_left(self.guild_id, self.user_id, self.field)
        if left > 0:
            await interaction.response.send_message(
                replace_emojis(
                    f"⏳ Это поле можно менять **раз в 7 дней**.\n"
                    f"Осталось: **{format_remaining(left)}**"
                ),
                ephemeral=True
            )
            return

        await interaction.response.send_modal(self.modal_cls(self.guild_id, self.user_id))


class BirthdayButton(_CooldownFieldButton):
    modal_cls = None  # задаётся после объявления BirthdayModal
    label_text = "День рождения"
    field = "birthday"


class NicknameButton(_SettingsFieldButton):
    modal_cls = None
    label_text = "Никнейм"


class DescriptionButton(_CooldownFieldButton):
    modal_cls = None
    label_text = "Описание"
    field = "description"


class BackToProfileButton(discord.ui.Button):
    """Вернуться к обычному виду профиля."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Назад",
            custom_id=f"profile_back:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Это не ваш профиль."),
                ephemeral=True
            )
            return

        import io

        from services.profile_card import build_profile_png

        # Рендер ~3с — defer, чтобы interaction не протух
        await interaction.response.defer()
        png = await build_profile_png(
            interaction.guild, interaction.user
        )
        view = ProfileView(self.guild_id, self.user_id, True)
        await interaction.edit_original_response(
            view=view,
            attachments=[discord.File(io.BytesIO(png), filename="profile.png")],
        )


def _default_stats(guild_id: int, user_id: int, display_name: str):
    from models.player_stats import PlayerStats
    return PlayerStats(
        guild_id=guild_id,
        user_id=user_id,
        name=display_name,
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


async def _check_change_cooldown(
    interaction: discord.Interaction,
    guild_id: int,
    user_id: int,
    field: str,
    new_value: str,
) -> bool:
    """Проверить кулдаун поля (раз в 7 дней). True — изменение разрешено."""
    stats = await player_stats_store.get(guild_id, user_id)
    current = getattr(stats, field, "") if stats else ""
    if str(new_value) == str(current):
        return True

    from storage.profile_cooldowns import format_remaining, profile_cooldowns

    left = profile_cooldowns.seconds_left(guild_id, user_id, field)
    if left > 0:
        label = {"birthday": "День рождения", "description": "Описание"}.get(field, field)
        await interaction.response.send_message(
            replace_emojis(
                f"⏳ {label} можно менять **раз в 7 дней**.\n"
                f"Осталось: **{format_remaining(left)}**"
            ),
            ephemeral=True
        )
        return False

    profile_cooldowns.mark_changed(guild_id, user_id, field)
    return True


async def _save_and_refresh(interaction: discord.Interaction, guild_id: int, user_id: int, **fields) -> None:
    """Сохранить поля профиля и перерисовать карточку на месте."""
    stats = await player_stats_store.get(guild_id, user_id)
    was_new_profile = stats is None

    if not stats:
        stats = _default_stats(guild_id, user_id, interaction.user.display_name)

    for key, value in fields.items():
        setattr(stats, key, value)

    if was_new_profile:
        await player_stats_store.set(stats)
    else:
        await player_stats_store.update(guild_id, user_id, stats)

    import io

    from services.profile_card import build_profile_png

    # Рендер ~3с — defer модалки сразу, карточку правим отдельным edit
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    png = await build_profile_png(interaction.guild, interaction.user)
    view = ProfileView(guild_id, user_id, True)
    file = discord.File(io.BytesIO(png), filename="profile.png")
    cv2_view = view
    attachments = [file]

    try:
        if interaction.message is not None:
            await interaction.message.edit(view=cv2_view, attachments=attachments)
            await interaction.followup.send("✅ Профиль обновлён.", ephemeral=True)
        else:
            await interaction.followup.send(view=cv2_view, attachments=attachments, ephemeral=True)
    except Exception:
        logger.exception("Profile modal: failed to update profile message")
        try:
            await interaction.followup.send(view=cv2_view, attachments=attachments, ephemeral=True)
        except Exception:
            pass


class _ProfileFieldModal(discord.ui.Modal):
    """Базовая модалка одного поля профиля."""

    def __init__(self, guild_id: int, user_id: int, **kwargs):
        super().__init__(**kwargs)
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self._handle_submit(interaction)
        except Exception:
            logger.exception(f"{type(self).__name__}.on_submit failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Не удалось сохранить профиль. Попробуйте ещё раз.",
                        ephemeral=True
                    )
            except Exception:
                pass

    async def _handle_submit(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError


class NicknameModal(_ProfileFieldModal, title="Никнейм"):
    """Модалка смены никнейма."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(guild_id, user_id)
        self.nickname = discord.ui.TextInput(
            label="Никнейм",
            placeholder="Введите ваш никнейм",
            max_length=32,
            required=False
        )
        self.add_item(self.nickname)

    async def _handle_submit(self, interaction: discord.Interaction) -> None:
        nickname = self.nickname.value.strip()
        if nickname:
            await _save_and_refresh(interaction, self.guild_id, self.user_id, name=nickname)
        else:
            await interaction.response.send_message(
                "❌ Никнейм не может быть пустым.", ephemeral=True
            )


class DescriptionModal(_ProfileFieldModal, title="Описание"):
    """Модалка смены описания."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(guild_id, user_id)
        self.description = discord.ui.TextInput(
            label="Описание",
            placeholder="Введите описание профиля",
            max_length=256,
            style=discord.TextStyle.paragraph,
            required=False
        )
        self.add_item(self.description)

    async def _handle_submit(self, interaction: discord.Interaction) -> None:
        new_value = self.description.value
        if not await _check_change_cooldown(
            interaction, self.guild_id, self.user_id, "description", new_value
        ):
            return
        await _save_and_refresh(
            interaction, self.guild_id, self.user_id,
            description=new_value
        )


class BirthdayModal(_ProfileFieldModal, title="День рождения"):
    """Модалка установки дня рождения."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__(guild_id, user_id)
        self.birthday = discord.ui.TextInput(
            label="Дата (ДД.ММ.ГГГГ)",
            placeholder="Например: 25.12.2004",
            max_length=10,
            required=False
        )
        self.add_item(self.birthday)

    async def _handle_submit(self, interaction: discord.Interaction) -> None:
        raw = self.birthday.value.strip()
        if raw:
            normalized = self._parse_date(raw)
            if not normalized:
                await interaction.response.send_message(
                    "❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ (например, 25.12.2004).",
                    ephemeral=True
                )
                return
            new_value = normalized
        else:
            new_value = ""

        if not await _check_change_cooldown(
            interaction, self.guild_id, self.user_id, "birthday", new_value
        ):
            return

        await _save_and_refresh(
            interaction, self.guild_id, self.user_id, birthday=new_value
        )

    @staticmethod
    def _parse_date(raw: str) -> str | None:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%d.%m.%Y")
            except ValueError:
                continue
        return None


# Привязка модалок к кнопкам меню (модалки объявлены ниже кнопок)
BirthdayButton.modal_cls = BirthdayModal
NicknameButton.modal_cls = NicknameModal
DescriptionButton.modal_cls = DescriptionModal


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
        """Открыть меню настроек профиля."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы можете редактировать только свой профиль."),
                ephemeral=True
            )
            return

        view = ProfileSettingsView(self.guild_id, self.user_id)
        try:
            await interaction.response.edit_message(
                view=swap_view(interaction.message, view)
            )
        except Exception:
            logger.exception("ProfileEditButton: failed to open settings menu")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Не удалось открыть настройки. Попробуйте ещё раз.",
                    ephemeral=True
                )
