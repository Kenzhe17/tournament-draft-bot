"""View для Тест на эрудицию."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class TriviaView(discord.ui.View):
    """View для Тест на эрудицию."""

    def __init__(self, guild_id: int, user_id: int, bet: int, questions: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.questions = questions
        self.current_question = 0
        self.correct_answers = 0
        self.required_correct = 7  # Нужно 7 из 10 правильных

    async def show_question(self, interaction: discord.Interaction) -> None:
        """Показать текущий вопрос."""
        if self.current_question >= len(self.questions):
            # Все вопросы закончились
            await self.finish_game(interaction)
            return

        question_data = self.questions[self.current_question]
        question = question_data["question"]
        options = question_data["options"]
        category = question_data["category"]

        embed = discord.Embed(
            title="🧠 Тест на эрудицию",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 5x\n\n**Вопрос {self.current_question + 1}/{len(self.questions)}** ({category})\n\n{question}",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="📊 Прогресс",
            value=f"Правильных: {self.correct_answers}/{self.required_correct} (нужно)",
            inline=False
        )

        # Создать view с опциями
        view = TriviaQuestionView(self.guild_id, self.user_id, self.bet, self.questions, self.current_question, self.correct_answers)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def finish_game(self, interaction: discord.Interaction) -> None:
        """Завершить игру."""
        multiplier = 5.0
        is_win = self.correct_answers >= self.required_correct

        if is_win:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Победа!",
                description=f"**Правильных ответов:** {self.correct_answers}/{len(self.questions)}\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Проигрыш",
                description=f"**Правильных ответов:** {self.correct_answers}/{len(self.questions)} (нужно {self.required_correct})\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "trivia"
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


class TriviaQuestionView(discord.ui.View):
    """View для отдельного вопроса."""

    def __init__(self, guild_id: int, user_id: int, bet: int, questions: list[dict], current_question: int, correct_answers: int):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.questions = questions
        self.current_question = current_question
        self.correct_answers = correct_answers

        # Добавить кнопки с опциями
        question_data = questions[current_question]
        options = question_data["options"]
        for i, option in enumerate(options):
            self.add_item(OptionButton(option, i, self))

    async def handle_answer(self, interaction: discord.Interaction, selected_option: str) -> None:
        """Обработать ответ."""
        question_data = self.questions[self.current_question]
        correct_answer = question_data["answer"]

        is_correct = selected_option == correct_answer
        if is_correct:
            self.correct_answers += 1

        # Показать результат и перейти к следующему вопросу
        if is_correct:
            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Ответ:** {selected_option}",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Ваш ответ:** {selected_option}\n**Правильный ответ:** {correct_answer}",
                color=discord.Color.red()
            )

        # Перейти к следующему вопросу
        self.current_question += 1
        view = TriviaView(self.guild_id, self.user_id, self.bet, self.questions)
        view.current_question = self.current_question
        view.correct_answers = self.correct_answers

        await interaction.response.edit_message(embed=embed, view=view)

        # Показать следующий вопрос через 2 секунды
        import asyncio
        await asyncio.sleep(2)
        await view.show_question(interaction)


class OptionButton(discord.ui.Button):
    """Кнопка для опции."""

    def __init__(self, option: str, index: int, view: TriviaQuestionView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=option,
            custom_id=f"option_{index}"
        )
        self.option = option
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на опцию."""
        await self.view.handle_answer(interaction, self.option)
