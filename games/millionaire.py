"""Кто хочет стать миллионером - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class MillionaireGame:
    """Логика игры Кто хочет стать миллионером."""

    def __init__(self):
        self.questions = [
            {
                "question": "Какая планета ближе всего к Солнцу?",
                "options": ["Меркурий", "Венера", "Марс", "Юпитер"],
                "answer": "Меркурий",
                "prize": 50
            },
            {
                "question": "Какой океан самый большой?",
                "options": ["Тихий океан", "Атлантический океан", "Индийский океан", "Северный Ледовитый"],
                "answer": "Тихий океан",
                "prize": 100
            },
            {
                "question": "В каком году закончилась Вторая мировая война?",
                "options": ["1943", "1944", "1945", "1946"],
                "answer": "1945",
                "prize": 200
            },
            {
                "question": "Какой химический элемент имеет символ O?",
                "options": ["Золото", "Кислород", "Осмий", "Олово"],
                "answer": "Кислород",
                "prize": 500
            },
            {
                "question": "Какая столица Франции?",
                "options": ["Лондон", "Берлин", "Париж", "Мадрид"],
                "answer": "Париж",
                "prize": 1000
            },
            {
                "question": "Кто написал 'Войну и мир'?",
                "options": ["Достоевский", "Толстой", "Чехов", "Пушкин"],
                "answer": "Толстой",
                "prize": 2000
            },
            {
                "question": "Сколько костей в теле взрослого человека?",
                "options": ["186", "206", "226", "246"],
                "answer": "206",
                "prize": 5000
            },
            {
                "question": "В каком году был первый полёт человека в космос?",
                "options": ["1959", "1961", "1963", "1965"],
                "answer": "1961",
                "prize": 10000
            },
            {
                "question": "Какая самая длинная река в мире?",
                "options": ["Амазонка", "Нил", "Янцзы", "Миссисипи"],
                "answer": "Нил",
                "prize": 20000
            },
            {
                "question": "Сколько континентов на Земле?",
                "options": ["5", "6", "7", "8"],
                "answer": "7",
                "prize": 50000
            },
        ]

    def get_random_questions(self, count: int = 10) -> list[dict]:
        """Получить случайные вопросы."""
        return random.sample(self.questions, min(count, len(self.questions)))


class MillionaireModal(discord.ui.Modal, title="Кто хочет стать миллионером"):
    """Модал для ставки в Кто хочет стать миллионером."""

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
        game = MillionaireGame()
        questions = game.get_random_questions(10)

        # Создать view для игры
        from views.millionaire_view import MillionaireView
        view = MillionaireView(self.guild_id, self.user_id, bet, questions)

        # Показать первый вопрос
        await view.show_question(interaction)
