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
            await interaction.response.send_message("❌ Кейс не найден.", ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < case.price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. Нужно: {case.price} 🪙",
                ephemeral=True
            )
            return

        # Начать анимацию
        msg = await interaction.response.send_message(
            "🎲 Вращаем...",
            ephemeral=True
        )

        # Получить сообщение для reactions
        msg = await interaction.original_response()

        # Добавить reactions для визуального эффекта
        try:
            await msg.add_reaction("🎲")
            await asyncio.sleep(1)
            await msg.add_reaction("⚡")
            await asyncio.sleep(1)
            await msg.remove_reaction("🎲", interaction.guild.me)
        except Exception:
            # Fallback если reactions не работают
            pass

        # Этап 2
        await interaction.edit_original_response(
            content="🎲 Выбираем редкость..."
        )

        try:
            await msg.add_reaction("✨")
            await asyncio.sleep(1)
            await msg.remove_reaction("⚡", interaction.guild.me)
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
            reaction_emoji = "😢"
        elif result["type"] == "coins":
            message = f"💰 Выпало {result['value']} 🪙!"
            color = discord.Color.dark_gold()
            reaction_emoji = "💰"
        elif result["type"] == "item":
            item = result["value"]
            message = f"🎉 Выпало: **{item.name}** ({item.rarity.value})!"
            color = discord.Color.dark_green()
            reaction_emoji = "🎉"
        else:
            message = "❌ Ошибка при открытии."
            color = discord.Color.dark_red()
            reaction_emoji = "❌"

        # Показать результат
        embed = discord.Embed(
            title=f"🎉 Результат открытия {case.name}",
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
                    description=f"Цена: {case.price} 🪙 - {case.description}",
                    emoji="🎲"
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
            await interaction.response.send_message("❌ Кейс не найден.", ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < case.price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. Нужно: {case.price} 🪙",
                ephemeral=True
            )
            return

        # Начать анимацию
        msg = await interaction.response.send_message(
            "🎲 Вращаем...",
            ephemeral=True
        )

        # Получить сообщение для reactions
        msg = await interaction.original_response()

        # Добавить reactions для визуального эффекта
        try:
            await msg.add_reaction("🎲")
            await asyncio.sleep(1)
            await msg.add_reaction("⚡")
            await asyncio.sleep(1)
            await msg.remove_reaction("🎲", interaction.guild.me)
        except Exception:
            # Fallback если reactions не работают
            pass

        # Этап 2
        await interaction.edit_original_response(
            content="🎲 Выбираем редкость..."
        )

        try:
            await msg.add_reaction("✨")
            await asyncio.sleep(1)
            await msg.remove_reaction("⚡", interaction.guild.me)
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
            reaction_emoji = "😢"
        elif result["type"] == "coins":
            message = f"💰 Выпало {result['value']} 🪙!"
            color = discord.Color.dark_gold()
            reaction_emoji = "💰"
        elif result["type"] == "item":
            item = result["value"]
            message = f"🎉 Выпало: **{item.name}** ({item.rarity.value})!"
            color = discord.Color.dark_green()
            reaction_emoji = "🎉"
        else:
            message = "❌ Ошибка при открытии."
            color = discord.Color.dark_red()
            reaction_emoji = "❌"

        # Показать результат
        embed = discord.Embed(
            title=f"🎉 Результат открытия {case.name}",
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
