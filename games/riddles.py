"""Загадки - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class RiddlesGame:
    """Логика игры Загадки."""

    def __init__(self):
        self.riddles = [
            {
                "question": "Что можно держать, даже если это нельзя кидать?",
                "answer": "дыхание",
                "hints": ["Это не физический предмет", "Это связано с человеком", "Вы делаете это постоянно"]
            },
            {
                "question": "Чем больше из неё берешь, тем больше она становится. Что это?",
                "answer": "яма",
                "hints": ["Это связано с землей", "Это может быть опасно", "Люди делают её специально"]
            },
            {
                "question": "Что всегда идёт, но никуда не приходит?",
                "answer": "время",
                "hints": ["Это не человек", "Неостановимо", "У вас никогда не бывает достаточно"]
            },
            {
                "question": "Что имеет голову, но не имеет тела?",
                "answer": "монета",
                "hints": ["Это металлическое", "Есть ценность", "В кармане"]
            },
            {
                "question": "Что принадлежит вам, но другие используют это больше?",
                "answer": "имя",
                "hints": ["Это слово", "При рождении получают", "Люди называют вас"]
            },
            {
                "question": "Что становится мокрым, когда сушат?",
                "answer": "полотенце",
                "hints": ["Это предмет в ванной", "Используется после мытья", "Поглощает воду"]
            },
            {
                "question": "Что уходит, когда приходит?",
                "answer": "сон",
                "hints": ["Это состояние", "Ночью", "Снишься сны"]
            },
            {
                "question": "Что можно разбить, но нельзя держать?",
                "answer": "обещание",
                "hints": ["Это абстрактное", "Люди дают его", "Можно нарушить"]
            },
        ]

    def get_random_riddle(self) -> dict:
        """Получить случайную загадку с подсказками."""
        return random.choice(self.riddles)


class RiddlesModal(discord.ui.Modal, title="Загадки"):
    """Модал для ставки в Загадки."""

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
        game = RiddlesGame()
        riddle_data = game.get_random_riddle()
        question = riddle_data["question"]
        answer = riddle_data["answer"]
        hints = riddle_data["hints"]

        # Создать embed с загадкой
        embed = discord.Embed(
            title="🤔 Загадки",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 3x\n\n**Загадка:**\n{question}",
            color=discord.Color.orange()
        )

        embed.add_field(
            name="⏱️ Время",
            value="90 секунд на ответ",
            inline=False
        )

        # Создать view для ответа
        from views.riddles_view import RiddlesView
        view = RiddlesView(self.guild_id, self.user_id, bet, question, answer, hints)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
