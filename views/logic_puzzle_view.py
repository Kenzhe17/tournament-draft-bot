"""View для Логические задачи."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class LogicPuzzleView(discord.ui.View):
    """View для Логические задачи."""

    def __init__(self, guild_id: int, user_id: int, bet: int, question: str, answer: str, hints: list[str]):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.question = question
        self.answer = answer
        self.hints = hints
        self.attempts = 3
        self.hint_index = 0

    @discord.ui.button(label="Ответить", style=discord.ButtonStyle.primary, custom_id="logic_answer")
    async def answer_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Показать модал для ввода ответа."""
        await interaction.response.send_modal(LogicAnswerModal(self.guild_id, self.user_id, self.bet, self.question, self.answer, self.hints, self.attempts, self.hint_index))

    @discord.ui.button(label="💡 Подсказка", style=discord.ButtonStyle.secondary, custom_id="logic_hint")
    async def hint_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Показать подсказку."""
        if self.hint_index < len(self.hints):
            hint = self.hints[self.hint_index]
            self.hint_index += 1

            embed = discord.Embed(
                title="🧩 Логические задачи",
                description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 4x\n\n**Задача:**\n{self.question}",
                color=discord.Color.purple()
            )
            embed.add_field(
                name="💡 Подсказка",
                value=hint,
                inline=False
            )
            embed.add_field(
                name="⏱️ Время",
                value=f"Осталось попыток: {self.attempts}",
                inline=False
            )

            # Обновить view
            view = LogicPuzzleView(self.guild_id, self.user_id, self.bet, self.question, self.answer, self.hints)
            view.attempts = self.attempts
            view.hint_index = self.hint_index

            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                "❌ Все подсказки использованы!",
                ephemeral=True
            )


class LogicAnswerModal(discord.ui.Modal, title="Ваш ответ"):
    """Модал для ввода ответа."""

    def __init__(self, guild_id: int, user_id: int, bet: int, question: str, answer: str, hints: list[str], attempts: int, hint_index: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.question = question
        self.answer = answer
        self.hints = hints
        self.attempts = attempts
        self.hint_index = hint_index

        self.answer_input = discord.ui.TextInput(
            label="Ответ",
            placeholder="Введите ответ",
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Проверить ответ."""
        user_answer = self.answer_input.value.lower().strip()
        correct_answer = self.answer.lower()

        # Проверить ответ
        is_correct = user_answer == correct_answer

        # Рассчитать множитель
        multiplier = 4.0

        if is_correct:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Задача:** {self.question}\n**Ответ:** {self.answer}\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            self.attempts -= 1

            if self.attempts > 0:
                # Ещё есть попытки
                embed = discord.Embed(
                    title="❌ Неправильно!",
                    description=f"**Ваш ответ:** {user_answer}\n**Осталось попыток:** {self.attempts}",
                    color=discord.Color.orange()
                )

                # Показать подсказку если есть
                if self.hint_index < len(self.hints):
                    embed.add_field(
                        name="💡 Подсказка",
                        value=self.hints[self.hint_index],
                        inline=False
                    )
                    self.hint_index += 1

                # Продолжить игру
                view = LogicPuzzleView(self.guild_id, self.user_id, self.bet, self.question, self.answer, self.hints)
                view.attempts = self.attempts
                view.hint_index = self.hint_index
                await interaction.response.edit_message(embed=embed, view=view)
                return
            else:
                # Попытки закончились
                embed = discord.Embed(
                    title="❌ Попытки закончились!",
                    description=f"**Ваш ответ:** {user_answer}\n**Правильный ответ:** {self.answer}\n**Потеря:** {self.bet} 🪙",
                    color=discord.Color.red()
                )

        # Обновить статистику
        game_id = "logic_puzzle"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if is_correct:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + self.bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if is_correct else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet) if is_correct else -self.bet
        else:
            # Создать новую статистику
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if is_correct else 0,
                "total_bet": self.bet,
                "total_won": winnings if is_correct else 0,
                "net_profit": winnings - self.bet if is_correct else -self.bet
            }
            minigame_stats.append(stats)

        # Сохранить статистику
        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.edit_message(embed=embed, view=None)
