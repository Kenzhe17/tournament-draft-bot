"""Тест на эрудицию - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class TriviaGame:
    """Логика игры Тест на эрудицию."""

    def __init__(self):
        self.questions = [
            {
                "question": "Какая планета ближе всего к Солнцу?",
                "options": ["Меркурий", "Венера", "Марс", "Юпитер"],
                "answer": "Меркурий",
                "category": "Астрономия"
            },
            {
                "question": "Какой океан самый большой?",
                "options": ["Тихий океан", "Атлантический океан", "Индийский океан", "Северный Ледовитый"],
                "answer": "Тихий океан",
                "category": "География"
            },
            {
                "question": "В каком году закончилась Вторая мировая война?",
                "options": ["1943", "1944", "1945", "1946"],
                "answer": "1945",
                "category": "История"
            },
            {
                "question": "Какой химический элемент имеет символ O?",
                "options": ["Золото", "Кислород", "Осмий", "Олово"],
                "answer": "Кислород",
                "category": "Химия"
            },
            {
                "question": "Какая столица Франции?",
                "options": ["Лондон", "Берлин", "Париж", "Мадрид"],
                "answer": "Париж",
                "category": "География"
            },
            {
                "question": "Кто написал 'Войну и мир'?",
                "options": ["Достоевский", "Толстой", "Чехов", "Пушкин"],
                "answer": "Толстой",
                "category": "Литература"
            },
            {
                "question": "Сколько костей в теле взрослого человека?",
                "options": ["186", "206", "226", "246"],
                "answer": "206",
                "category": "Биология"
            },
            {
                "question": "В каком году был первый полёт человека в космос?",
                "options": ["1959", "1961", "1963", "1965"],
                "answer": "1961",
                "category": "История"
            },
            {
                "question": "Какая самая длинная река в мире?",
                "options": ["Амазонка", "Нил", "Янцзы", "Миссисипи"],
                "answer": "Нил",
                "category": "География"
            },
            {
                "question": "Сколько континентов на Земле?",
                "options": ["5", "6", "7", "8"],
                "answer": "7",
                "category": "География"
            },
            {
                "question": "Какой газ наиболее распространён в атмосфере Земли?",
                "options": ["Кислород", "Азот", "Углекислый газ", "Аргон"],
                "answer": "Азот",
                "category": "Химия"
            },
            {
                "question": "В каком году была написана первая Конституция США?",
                "options": ["1776", "1787", "1789", "1791"],
                "answer": "1787",
                "category": "История"
            },
        ]

    def get_random_questions(self, count: int = 10) -> list[dict]:
        """Получить случайные вопросы."""
        return random.sample(self.questions, min(count, len(self.questions)))


class TriviaModal(discord.ui.Modal, title="Тест на эрудицию"):
    """Модал для ставки в Тест на эрудицию."""

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
        game = TriviaGame()
        questions = game.get_random_questions(10)

        # Создать view для игры
        from views.trivia_view import TriviaView
        view = TriviaView(self.guild_id, self.user_id, bet, questions)

        # Показать первый вопрос
        await view.show_question(interaction)
