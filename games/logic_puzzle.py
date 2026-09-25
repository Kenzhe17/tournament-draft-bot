"""Логические задачи - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class LogicPuzzleGame:
    """Логика игры Логические задачи."""

    def __init__(self):
        self.puzzles = [
            {
                "question": "У отца Мэри есть 5 дочерей: Чача, Чече, Чичи, Чочо. Как зовут пятую дочь?",
                "answer": "Мэри",
                "hints": ["Прочитайте вопрос внимательно", "Имя уже есть в вопросе", "Это не Чача"]
            },
            {
                "question": "Если вы идете в кино с друзьями, и на пути вы встречаете 5 человек, у каждого из которых 5 яблок. Сколько всего яблок у всех?",
                "answer": "25",
                "hints": ["Умножьте людей на яблоки", "5 человек × 5 яблок", "Это простая математика"]
            },
            {
                "question": "Что можно держать, даже если это нельзя кидать?",
                "answer": "дыхание",
                "hints": ["Это не физический предмет", "Это связано с человеком", "Вы делаете это постоянно"]
            },
            {
                "question": "Какой месяц имеет 28 дней?",
                "answer": "все",
                "hints": ["Не только февраль", "У каждого месяца есть минимум 28 дней", "Ответ не конкретный месяц"]
            },
            {
                "question": "У человека есть 3 сына. У каждого сына есть сестра. Сколько детей у человека?",
                "answer": "4",
                "hints": ["3 сына + 1 дочь", "У всех сыновей одна общая сестра", "3 + 1 = 4"]
            },
        ]

    def get_random_puzzle(self) -> dict:
        """Получить случайную логическую задачу."""
        return random.choice(self.puzzles)


class LogicPuzzleModal(discord.ui.Modal, title="Логические задачи"):
    """Модал для ставки в Логические задачи."""

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

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Начать игру с указанной ставкой."""
        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ставка должна быть числом!",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить лимиты ставок
        if bet < MIN_BET or bet > MAX_BET:
            await interaction.response.send_message(
                f"❌ Ставка должна быть между {MIN_BET} и {MAX_BET} 🪙",
                ephemeral=True
            )
            return

        # Списать ставку
        await user_balance_store.subtract_balance(self.guild_id, self.user_id, bet)

        # Создать сессию игры
        game = LogicPuzzleGame()
        puzzle_data = game.get_random_puzzle()
        question = puzzle_data["question"]
        answer = puzzle_data["answer"]
        hints = puzzle_data["hints"]

        # Создать embed с задачей
        embed = discord.Embed(
            title="🧩 Логические задачи",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 4x\n\n**Задача:**\n{question}",
            color=discord.Color.purple()
        )

        embed.add_field(
            name="⏱️ Время",
            value="120 секунд на ответ",
            inline=False
        )

        # Создать view для ответа
        from views.logic_puzzle_view import LogicPuzzleView
        view = LogicPuzzleView(self.guild_id, self.user_id, bet, question, answer, hints)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
