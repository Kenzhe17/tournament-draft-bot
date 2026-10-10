from config import replace_emojis, get_emoji
"""View для системы кейсов."""

import asyncio
import discord
from storage.case_store import case_store
from storage.user_balance_store import user_balance_store


class CaseOpenButton(discord.ui.Button):
    """Кнопка открытия кейса."""

    def __init__(self, case_id: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"case_open:{case_id}"
        )
        self.case_id = case_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Открыть кейс с анимацией и reactions."""
        case = case_store.get_case(self.case_id)
        if not case:
            await interaction.response.send_message(replace_emojis("❌ Кейс не найден."), ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < case.price:
            await interaction.response.send_message(
                replace_emojis(f"cross Недостаточно монет. Нужно: {case.price} {get_emoji('money')}"),
                ephemeral=True
            )
            return

        # Начать анимацию
        msg = await interaction.response.send_message(
            replace_emojis("dice Вращаем..."),
            ephemeral=True
        )

        # Получить сообщение для reactions
        msg = await interaction.original_response()

        # Добавить reactions для визуального эффекта
        try:
            await msg.add_reaction(replace_emojis("dice"))
            await asyncio.sleep(1)
            await msg.add_reaction(replace_emojis("flash"))
            await asyncio.sleep(1)
            await msg.remove_reaction(replace_emojis("dice"), interaction.guild.me)
        except Exception:
            # Fallback если reactions не работают
            pass

        # Этап 2
        await interaction.edit_original_response(
            content=replace_emojis("dice Выбираем редкость..."),
        )

        try:
            await msg.add_reaction(replace_emojis("a_sparkle"))
            await asyncio.sleep(1)
            await msg.remove_reaction(replace_emojis("flash"), interaction.guild.me)
        except Exception:
            pass

        # Открыть кейс
        result = await case_store.open_case(
            interaction.guild_id,
            interaction.user.id,
            self.case_id,
            interaction.guild
        )

        # Формировать результат
        if result["type"] == "nothing":
            message = "😢 Ничего не выпало!"
            color = discord.Color.dark_red()
            reaction_emoji = "cross"
        elif result["type"] == "coins":
            message = f"{get_emoji('money')} Выпало {result['value']} {get_emoji('money')}!",
            color = discord.Color.dark_gold()
            reaction_emoji = "money"
        elif result["type"] == "item":
            item_name = result['value']
            item_rarity = result.get('rarity', 'common')
            message = f"{replace_emojis('a_star')} Выпало: **{item_name}** ({item_rarity})!"
            color = discord.Color.dark_green()
            reaction_emoji = "a_star"
        else:
            message = replace_emojis("cross Ошибка при открытии."),
            color = discord.Color.dark_red()
            reaction_emoji = "cross"

        # Показать результат
        # Determine emoji for case name
        name_emoji = "emoji_basic_case"
        if "Basic" in case.name:
            name_emoji = "emoji_basic_case"
        elif "Premium" in case.name:
            name_emoji = "emoji_premium_case"
        elif "Elite" in case.name:
            name_emoji = "emoji_elite_case"
        elif "Special" in case.name:
            name_emoji = "emoji_special_case"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Результат открытия {replace_emojis(name_emoji)} {case.name}",
            description=message,
            color=color
        )

        await interaction.edit_original_response(
            content="🎉",
            embed=embed
        )

        # Добавить reaction результата
        try:
            await msg.add_reaction(reaction_emoji)
        except Exception:
            pass


class CasesMainView(discord.ui.View):
    """Главное меню кейсов."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CaseSelect())


class CaseSelect(discord.ui.Select):
    """Выпадающее меню выбора кейса."""

    def __init__(self):
        cases = case_store.get_all_cases()
        options = []

        for case in cases:
            options.append(
                discord.SelectOption(
                    label=case.name,
                    value=case.id,
                    description=f"Цена: {case.price} {get_emoji('money')} - {case.description}"
                )
            )

        super().__init__(
            placeholder="Выберите кейс для открытия...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать выбор кейса."""
        case_id = self.values[0]
        case = case_store.get_case(case_id)

        if not case:
            await interaction.response.send_message(replace_emojis("❌ Кейс не найден."), ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < case.price:
            await interaction.response.send_message(
                replace_emojis(f"cross Недостаточно монет. Нужно: {case.price} {get_emoji('money')}"),
                ephemeral=True
            )
            return

        # Начать анимацию
        msg = await interaction.response.send_message(
            replace_emojis("dice Вращаем..."),
            ephemeral=True
        )

        # Получить сообщение для reactions
        msg = await interaction.original_response()

        # Добавить reactions для визуального эффекта
        try:
            await msg.add_reaction(replace_emojis("dice"))
            await asyncio.sleep(1)
            await msg.add_reaction(replace_emojis("flash"))
            await asyncio.sleep(1)
            await msg.remove_reaction(replace_emojis("dice"), interaction.guild.me)
        except Exception:
            # Fallback если reactions не работают
            pass

        # Этап 2
        await interaction.edit_original_response(
            content=replace_emojis("dice Выбираем редкость..."),
        )

        try:
            await msg.add_reaction(replace_emojis("a_sparkle"))
            await asyncio.sleep(1)
            await msg.remove_reaction(replace_emojis("flash"), interaction.guild.me)
        except Exception:
            pass

        # Открыть кейс
        result = await case_store.open_case(
            interaction.guild_id,
            interaction.user.id,
            case_id,
            interaction.guild
        )

        # Формировать результат
        if result["type"] == "nothing":
            message = "😢 Ничего не выпало!"
            color = discord.Color.dark_red()
            reaction_emoji = "cross"
        elif result["type"] == "coins":
            message = f"{get_emoji('money')} Выпало {result['value']} {get_emoji('money')}!",
            color = discord.Color.dark_gold()
            reaction_emoji = "money"
        elif result["type"] == "item":
            item_name = result['value']
            item_rarity = result.get('rarity', 'common')
            message = f"{replace_emojis('a_star')} Выпало: **{item_name}** ({item_rarity})!"
            color = discord.Color.dark_green()
            reaction_emoji = "a_star"
        else:
            message = replace_emojis("cross Ошибка при открытии."),
            color = discord.Color.dark_red()
            reaction_emoji = "cross"

        # Показать результат
        # Determine emoji for case name
        name_emoji = "emoji_basic_case"
        if "Basic" in case.name:
            name_emoji = "emoji_basic_case"
        elif "Premium" in case.name:
            name_emoji = "emoji_premium_case"
        elif "Elite" in case.name:
            name_emoji = "emoji_elite_case"
        elif "Special" in case.name:
            name_emoji = "emoji_special_case"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Результат открытия {replace_emojis(name_emoji)} {case.name}",
            description=message,
            color=color
        )

        await interaction.edit_original_response(
            content="🎉",
            embed=embed
        )

        # Добавить reaction результата
        try:
            await msg.add_reaction(reaction_emoji)
        except Exception:
            pass


class OpenAgainButton(discord.ui.Button):
    """Кнопка 'Открыть ещё 1'."""

    def __init__(self, case_id: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="🎲 Открыть ещё 1",
            custom_id=f"case_open_again:{case_id}"
        )
        self.case_id = case_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Открыть ещё один кейс."""
        # Delegate to CaseOpenButton
        button = CaseOpenButton(self.case_id, "Открыть")
        await button.callback(interaction)
