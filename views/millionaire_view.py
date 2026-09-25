"""View для Кто хочет стать миллионером."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class MillionaireView(discord.ui.View):
    """View для Кто хочет стать миллионером."""

    def __init__(self, guild_id: int, user_id: int, bet: int, questions: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.questions = questions
        self.current_question = 0
        self.current_prize = 0
        self.multiplier = 10.0

    async def show_question(self, interaction: discord.Interaction) -> None:
        """Показать текущий вопрос."""
        if self.current_question >= len(self.questions):
            # Все вопросы закончились - победа
            await self.finish_game(interaction, True)
            return

        question_data = self.questions[self.current_question]
        question = question_data["question"]
        options = question_data["options"]
        prize = question_data["prize"]

        embed = discord.Embed(
            title="💰 Кто хочет стать миллионером",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** {self.multiplier}x\n\n**Вопрос {self.current_question + 1}/{len(self.questions)}**\n{question}",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="💎 Текущий приз",
            value=f"{prize} 🪙",
            inline=False
        )

        # Создать view с опциями
        view = MillionaireQuestionView(self.guild_id, self.user_id, self.bet, self.questions, self.current_question, self.current_prize, self.multiplier)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def finish_game(self, interaction: discord.Interaction, is_win: bool) -> None:
        """Завершить игру."""
        if is_win:
            winnings = int(self.bet * self.multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Победа!",
                description=f"**Вы ответили на все вопросы!**\n**Выигрыш:** {winnings} 🪙 ({self.multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Проигрыш",
                description=f"**Проигрыш на вопросе {self.current_question + 1}**\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "millionaire"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if is_win:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + self.bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if is_win else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet) if is_win else -self.bet
        else:
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if is_win else 0,
                "total_bet": self.bet,
                "total_won": winnings if is_win else 0,
                "net_profit": winnings - self.bet if is_win else -self.bet
            }
            minigame_stats.append(stats)

        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


class MillionaireQuestionView(discord.ui.View):
    """View для отдельного вопроса."""

    def __init__(self, guild_id: int, user_id: int, bet: int, questions: list[dict], current_question: int, current_prize: int, multiplier: float):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.questions = questions
        self.current_question = current_question
        self.current_prize = current_prize
        self.multiplier = multiplier

        # Добавить кнопки с опциями
        question_data = questions[current_question]
        options = question_data["options"]
        for i, option in enumerate(options):
            self.add_item(MillionaireOptionButton(option, i, self))

    async def handle_answer(self, interaction: discord.Interaction, selected_option: str) -> None:
        """Обработать ответ."""
        question_data = self.questions[self.current_question]
        correct_answer = question_data["answer"]
        prize = question_data["prize"]

        is_correct = selected_option == correct_answer

        if is_correct:
            self.current_prize = prize

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Ответ:** {selected_option}\n**Приз:** {prize} 🪙",
                color=discord.Color.green()
            )

            # Перейти к следующему вопросу
            self.current_question += 1
            view = MillionaireView(self.guild_id, self.user_id, self.bet, self.questions)
            view.current_question = self.current_question
            view.current_prize = self.current_prize
            view.multiplier = self.multiplier

            await interaction.response.edit_message(embed=embed, view=view)

            # Показать следующий вопрос через 2 секунды
            import asyncio
            await asyncio.sleep(2)
            await view.show_question(interaction)
        else:
            # Проигрыш
            view = MillionaireView(self.guild_id, self.user_id, self.bet, self.questions)
            view.current_question = self.current_question
            view.current_prize = self.current_prize
            view.multiplier = self.multiplier

            await view.finish_game(interaction, False)


class MillionaireOptionButton(discord.ui.Button):
    """Кнопка для опции."""

    def __init__(self, option: str, index: int, view: MillionaireQuestionView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=option,
            custom_id=f"millionaire_option_{index}"
        )
        self.option = option
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на опцию."""
        await self.view.handle_answer(interaction, self.option)
