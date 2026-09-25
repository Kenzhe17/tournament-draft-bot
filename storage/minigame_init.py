"""Initialize mini-games in the database."""

from storage.db import get_pool
from storage.minigame_store import minigame_store


async def initialize_minigames() -> None:
    """Initialize all mini-games in the database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Games on luck (Priority 1)
        games = [
            # 1. Rock-Paper-Scissors
            {
                "id": "rps",
                "name": "Камень-Ножницы-Бумага",
                "description": "Классическая игра. Камень бьёт ножницы, ножницы бьют бумагу, бумага бьёт камень.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 10,
                "max_bet": 1000,
                "multiplier": 2.0,
                "is_pvp": True,
                "is_pve": True,
            },
            # 2. Guess Number
            {
                "id": "guess_number",
                "name": "Угадай число",
                "description": "Бот загадывает число от 1 до 100. У вас есть 7 попыток.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 20,
                "max_bet": 500,
                "multiplier": 5.0,
                "is_pvp": False,
                "is_pve": True,
            },
            # 3. Coin Flip
            {
                "id": "coin_flip",
                "name": "Монетка",
                "description": "Орел или решка. Кто угадал тот и выиграл.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 10,
                "max_bet": 1000,
                "multiplier": 2.0,
                "is_pvp": True,
                "is_pve": True,
            },
            # 4. Dice Roll
            {
                "id": "dice_roll",
                "name": "Кубик",
                "description": "Бросьте кубик. У кого больше выпадет тот и победил.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 10,
                "max_bet": 1000,
                "multiplier": 2.0,
                "is_pvp": True,
                "is_pve": True,
            },
            # 5. Guess Emoji
            {
                "id": "guess_emoji",
                "name": "Угадай эмодзи",
                "description": "Бот показывает скрытый эмодзи с подсказками.",
                "category": "luck",
                "difficulty": "medium",
                "min_bet": 15,
                "max_bet": 300,
                "multiplier": 3.0,
                "is_pvp": False,
                "is_pve": True,
            },
            # 6. Wheel of Fortune
            {
                "id": "wheel",
                "name": "Колесо фортуны",
                "description": "Крутите колесо с разными множителями.",
                "category": "luck",
                "difficulty": "medium",
                "min_bet": 20,
                "max_bet": 500,
                "multiplier": 5.0,
                "is_pvp": False,
                "is_pve": True,
            },
            # 7. Guess Color
            {
                "id": "guess_color",
                "name": "Угадай цвет",
                "description": "Бот загадывает один из 3 цветов.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 10,
                "max_bet": 200,
                "multiplier": 3.0,
                "is_pvp": False,
                "is_pve": True,
            },
            # 8. Tic-Tac-Toe
            {
                "id": "tictactoe",
                "name": "Крестики-Нолики",
                "description": "Классическая игра 3x3. Кто первый соберёт 3 в ряд - победитель.",
                "category": "luck",
                "difficulty": "medium",
                "min_bet": 20,
                "max_bet": 500,
                "multiplier": 2.0,
                "is_pvp": True,
                "is_pve": True,
            },
            # 9. Reflex Test
            {
                "id": "reflex_test",
                "name": "Быстрый тест",
                "description": "Нажмите кнопку как можно быстрее при появлении эмодзи.",
                "category": "luck",
                "difficulty": "easy",
                "min_bet": 10,
                "max_bet": 200,
                "multiplier": 2.0,
                "is_pvp": False,
                "is_pve": True,
            },
            # 10. Spin Bottle
            {
                "id": "spin_bottle",
                "name": "Бутылочка",
                "description": "Крутите бутылочку. На кого она укажет - тот выполняет вызов.",
                "category": "luck",
                "difficulty": "medium",
                "min_bet": 25,
                "max_bet": 500,
                "multiplier": 2.0,
                "is_pvp": True,
                "is_pve": False,
            },
        ]

        for game in games:
            await conn.execute(
                """
                INSERT INTO minigames (id, name, description, category, difficulty, min_bet, max_bet, multiplier, is_pvp, is_pve, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, TRUE)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    difficulty = EXCLUDED.difficulty,
                    min_bet = EXCLUDED.min_bet,
                    max_bet = EXCLUDED.max_bet,
                    multiplier = EXCLUDED.multiplier,
                    is_pvp = EXCLUDED.is_pvp,
                    is_pve = EXCLUDED.is_pve,
                    is_active = TRUE
                """,
                game["id"],
                game["name"],
                game["description"],
                game["category"],
                game["difficulty"],
                game["min_bet"],
                game["max_bet"],
                game["multiplier"],
                game["is_pvp"],
                game["is_pve"],
            )
