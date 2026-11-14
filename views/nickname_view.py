"""CV2-сценарий платной смены ника (услуга nickname_change).

Поток: карточка услуги → Modal «Новый ник» → ephemeral
предпросмотр → «Подтвердить за 1 000» / «Отмена» → результат.
Все этапы ephemeral, в общий канал ничего не публикуется.
"""

from __future__ import annotations

import logging

import discord

from config import get_emoji, replace_emojis
from services import nickname_service
from services.nickname_service import (
    NICKNAME_CHANGE_PRICE,
    purchase_nickname_change,
    record_nickname_audit,
    validate_nickname,
)
from storage.user_balance_store import user_balance_store
from utils.design import compose

logger = logging.getLogger(__name__)


def _money() -> str:
    return get_emoji("money")


async def _balance(guild_id: int, user_id: int) -> int:
    try:
        return await user_balance_store.get_balance(guild_id, user_id)
    except Exception:  # noqa: BLE001
        return 0


def _embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.from_rgb(69, 69, 69),
    )
    return embed


# ------------------------------------------------------------- service card


class ChangeNickButton(discord.ui.Button):
    """Кнопка «Изменить ник» на карточке услуги."""

    def __init__(self, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"Изменить ник за {price}",
            custom_id="nickname_change:open",
        )
        self.price = price

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            NicknameChangeModal(self.price)
        )


class NicknameServiceCardView(discord.ui.View):
    def __init__(self, price: int):
        super().__init__(timeout=180)
        self.add_item(ChangeNickButton(price))
        self.add_item(ShopBackToListButton())


class ShopBackToListButton(discord.ui.Button):
    """Назад к списку ролей/услуг."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Назад",
            custom_id="shop:back_roles",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.shop_view import show_roles_list
        await show_roles_list(interaction)


async def show_nickname_service_card(
    interaction: discord.Interaction, item
) -> None:
    """Карточка услуги «Смена ника» с кнопкой перехода к Modal."""
    balance = await _balance(interaction.guild_id, interaction.user.id)
    embed = _embed(
        f"УСЛУГА | {item.name}",
        f"{item.description}\n\n"
        f"**Стоимость:** {item.price} {_money()}\n"
        f"**Баланс:** {balance:,} {_money()}\n\n"
        "Нажмите «Изменить ник», чтобы задать новый серверный ник.",
    )
    view = NicknameServiceCardView(item.price)
    await interaction.response.edit_message(view=compose(embed, view))


# -------------------------------------------------------------------- modal


class NicknameChangeModal(discord.ui.Modal, title="Смена ника"):
    new_nick = discord.ui.TextInput(
        label="Новый ник",
        placeholder="Введите новый ник (1–32 символа)",
        min_length=1,
        max_length=32,
        required=True,
    )

    def __init__(self, price: int):
        super().__init__()
        self.price = price

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_nick = str(self.new_nick.value or "").strip()

        member, err = await nickname_service.check_preconditions(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        err = nickname_service.check_nickname_rules(member, new_nick)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        balance = await _balance(interaction.guild_id, interaction.user.id)
        old_nick = member.nick or member.display_name
        after = balance - self.price

        embed = _embed(
            "СМЕНА НИКА",
            f"**Текущий ник:** {old_nick}\n"
            f"**Новый ник:** {new_nick}\n\n"
            f"**Стоимость:** {self.price} {_money()}\n"
            f"**Баланс:** {balance:,} {_money()}\n"
            f"**После покупки:** {after:,} {_money()}",
        )
        view = NicknameConfirmView(new_nick, self.price)
        await interaction.response.send_message(
            view=compose(embed, view), ephemeral=True
        )


# ------------------------------------------------------------- confirm view


class NicknameConfirmView(discord.ui.View):
    def __init__(self, new_nick: str, price: int):
        super().__init__(timeout=180)
        self.new_nick = new_nick
        self.price = price
        self.add_item(NicknameConfirmButton(new_nick, price))
        self.add_item(NicknameCancelButton())


class NicknameConfirmButton(discord.ui.Button):
    def __init__(self, new_nick: str, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"Подтвердить за {price}",
            custom_id="nickname_change:confirm",
        )
        self.new_nick = new_nick
        self.price = price

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        result, payload = await purchase_nickname_change(
            interaction, self.new_nick, self.price
        )

        await record_nickname_audit(
            interaction,
            op_id=payload.get("op_id") or "-",
            old_nick=payload.get("old_nick") or "",
            new_nick=self.new_nick,
            price=self.price,
            result=result,
        )

        if result == "success":
            balance = payload.get("balance")
            balance_text = (
                f"{balance:,} {_money()}" if balance is not None else "—"
            )
            embed = _embed(
                "НИК ИЗМЕНЁН",
                f"**Предыдущий:** {payload.get('old_nick')}\n"
                f"**Новый:** {payload.get('new_nick')}\n"
                f"**Списано:** {self.price} {_money()}\n"
                f"**Остаток:** {balance_text}",
            )
        else:
            embed = _embed(
                "ОШИБКА",
                payload.get("message", "Не удалось выполнить покупку."),
            )

        try:
            await interaction.edit_original_response(
                view=compose(embed)
            )
        except Exception:  # noqa: BLE001
            try:
                await interaction.followup.send(
                    view=compose(embed), ephemeral=True
                )
            except Exception:  # noqa: BLE001
                pass


class NicknameCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Отмена",
            custom_id="nickname_change:cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        embed = _embed("ОТМЕНЕНО", "Покупка смены ника отменена.")
        try:
            await interaction.response.edit_message(view=compose(embed))
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(
                "Отменено.", ephemeral=True
            )
