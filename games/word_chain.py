"""Словесные цепочки - PvP игра (упрощенная PvE версия)."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class WordChainGame:
    """Логика игры Словесные цепочки."""

    def __init__(self):
        self.letters = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        self.words = {
            "а": ["арбуз", "ананас", "апельсин", "автомобиль"],
            "б": ["банан", "барсук", "бабочка", "береза"],
            "в": ["волк", "василек", "волейбол", "весна"],
            "г": ["гриб", "гора", "гусь", "гитара"],
            "д": ["дерево", "дом", "день", "дождь"],
            "е": ["ель", "ежик", "енот", "ерунда"],
            "ж": ["жук", "жираф", "журнал", "железо"],
            "з": ["заяц", "зебра", "зима", "завод"],
            "и": ["игра", "искра", "изюм", "интернет"],
            "й": ["йогурт", "йога", "йод", "йод"],
            "к": ["кот", "карта", "книга", "компьютер"],
            "л": ["лиса", "лес", "лампа", "лимон"],
            "м": ["мышь", "мир", "море", "музыка"],
            "н": ["нос", "небо", "ночь", "новости"],
            "о": ["озеро", "облако", "океан", "огонь"],
            "п": ["пес", "птица", "планета", "путь"],
            "р": ["рыба", "река", "роза", "ручка"],
            "с": ["солнце", "собака", "сад", "слово"],
            "т": ["телефон", "трава", "тень", "трактор"],
            "у": ["улица", "утка", "ураган", "уровень"],
            "ф": ["футбол", "фонтан", "фонарь", "фрукт"],
            "х": ["хлеб", "хвост", "хор", "химия"],
            "ц": ["цирк", "цветок", "цифра", "центр"],
            "ч": ["человек", "часы", "чашка", "черепаха"],
            "ш": ["школа", "шар", "шапка", "шоколад"],
            "щ": ["щука", "щетка", "щит", "щекотка"],
            "ъ": ["объект", "съезд", "подъезд", "отъезд"],
            "ы": ["сыр", "мыло", "рынок", "клуб"],
            "ь": ["ручь", "конь", "медь", "сыр"],
            "э": ["экран", "электрон", "эпидемия", "экономика"],
            "ю": ["юг", "юбка", "юла", "юноша"],
            "я": ["яблоко", "ягода", "язык", "ящик"],
        }

    def get_random_letter(self) -> str:
        """Получить случайную букву."""
        return random.choice(self.letters)

    def check_word(self, word: str, letter: str) -> bool:
        """Проверить, начинается ли слово с нужной буквы."""
        return word.lower().startswith(letter.lower())

    def get_bot_word(self, letter: str) -> str:
        """Получить слово бота."""
        words = self.words.get(letter.lower(), ["слово"])
        return random.choice(words)


class WordChainModal(discord.ui.Modal, title="Словесные цепочки"):
    """Модал для ставки в Словесные цепочки."""

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
        game = WordChainGame()
        letter = game.get_random_letter()

        # Создать embed с буквой
        embed = discord.Embed(
            title="🔤 Словесные цепочки",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 2x\n\n**Буква:** {letter.upper()}\n\nНазовите слово, которое начинается на эту букву!",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="⏱️ Время",
            value="30 секунд на ответ",
            inline=False
        )

        # Создать view для ответа
        from views.word_chain_view import WordChainView
        view = WordChainView(self.guild_id, self.user_id, bet, letter, game)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
