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
        """Открыть кейс с анимацией."""
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
        await interaction.response.send_message(
            "🎲 Вращаем...",
            ephemeral=True
        )

        # Ждём 2 секунды
        await asyncio.sleep(2)

        # Обновить сообщение
        await interaction.edit_original_response(
            content="🎲 Выбираем редкость..."
        )

        # Ждём ещё 1 секунду
        await asyncio.sleep(1)

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
            color = discord.Color.red()
        elif result["type"] == "coins":
            message = f"💰 Выпало {result['value']} 🪙!"
            color = discord.Color.gold()
        elif result["type"] == "item":
            item = result["value"]
            message = f"🎉 Выпало: **{item.name}** ({item.rarity.value})!"
            color = discord.Color.green()
        else:
            message = "❌ Ошибка при открытии."
            color = discord.Color.red()

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


class CasesMainView(discord.ui.View):
    """Главное меню кейсов."""

    def __init__(self):
        super().__init__(timeout=None)
        cases = case_store.get_all_cases()
        for case in cases:
            self.add_item(CaseOpenButton(case.id, f"{case.name} - {case.price} 🪙"))


class OpenAgainButton(discord.ui.Button):
    """Кнопка 'Открыть ещё 1'."""

    def __init__(self, case_id: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Открыть ещё 1",
            custom_id=f"case_open_again:{case_id}"
        )
        self.case_id = case_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Открыть ещё один кейс."""
        # Delegate to CaseOpenButton
        button = CaseOpenButton(self.case_id, "Открыть")
        await button.callback(interaction)
