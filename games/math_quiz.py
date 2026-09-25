"""Математическая викторина - PvP и PvE игра."""

import random
import discord
from views.quiz_view import QuizBetModal


class MathQuizGame:
    """Логика математической викторины."""

    def __init__(self):
        self.problems = [
            # Easy problems
            (lambda a, b: a + b, 10, 50, "addition"),
            (lambda a, b: a - b, 10, 50, "subtraction"),
            (lambda a, b: a * b, 20, 80, "multiplication"),
            # Medium problems
            (lambda a, b: a + b, 15, 70, "addition_medium"),
            (lambda a, b: a * b, 25, 100, "multiplication_medium"),
            (lambda a, b: a * b, 30, 120, "multiplication_hard"),
        ]

    def generate_problem(self, difficulty: str = "easy") -> tuple[str, int]:
        """Сгенерировать математическую задачу."""
        if difficulty == "easy":
            a = random.randint(1, 20)
            b = random.randint(1, 20)
            operation = random.choice(["+", "-", "×"])
        else:
            a = random.randint(10, 50)
            b = random.randint(1, 20)
            operation = random.choice(["+", "-", "×"])

        if operation == "+":
            answer = a + b
            question = f"{a} + {b} = ?"
        elif operation == "-":
            # Ensure positive result
            if a < b:
                a, b = b, a
            answer = a - b
            question = f"{a} - {b} = ?"
        else:  # ×
            answer = a * b
            question = f"{a} × {b} = ?"

        return question, answer

    def generate_round(self, difficulty: str = "easy") -> tuple[str, int]:
        """Сгенерировать раунд викторины."""
        return self.generate_problem(difficulty)


class MathQuizModal(discord.ui.Modal, title="Математическая викторина"):
    """Модал для ставки в математическую викторину."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

        self.bet = discord.ui.TextInput(
            label="Ставка (🪙)",
            placeholder="Введите сумму ставки",
            min_length=1,
            max_length=10,
            required=True
        )

        self.difficulty = discord.ui.TextInput(
            label="Сложность",
            placeholder="easy или medium",
            default="easy",
            max_length=10,
            required=False
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Начать игру с указанной ставкой."""
        from storage.user_balance_store import user_balance_store
        from storage.minigame_store import minigame_store

        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ставка должна быть числом!",
                ephemeral=True
            )
            return

        difficulty = self.difficulty.value or "easy"
        if difficulty not in ["easy", "medium"]:
            difficulty = "easy"

        # Проверить баланс
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить лимиты ставок
        from config import MIN_BET, MAX_BET
        if bet < MIN_BET or bet > MAX_BET:
            await interaction.response.send_message(
                f"❌ Ставка должна быть между {MIN_BET} и {MAX_BET} 🪙",
                ephemeral=True
            )
            return

        # Списать ставку
        await user_balance_store.subtract_balance(self.guild_id, self.user_id, bet)

        # Создать сессию игры
        game = MathQuizGame()
        question, answer = game.generate_round(difficulty)

        # Создать embed с вопросом
        embed = discord.Embed(
            title="🧠 Математическая викторина",
            description=f"**Сложность:** {difficulty}\n**Ставка:** {bet} 🪙\n\n**Вопрос:** {question}",
            color=discord.Color.purple()
        )
        embed.add_field(
            name="⏱️ Время",
            value="30 секунд на ответ",
            inline=False
        )

        # Создать view для ответа
        from views.math_quiz_view import MathQuizView
        view = MathQuizView(self.guild_id, self.user_id, bet, question, answer, difficulty)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
