"""Storage для вопросов викторин и защиты от запоминания."""

import json
from pathlib import Path
from typing import Any

from models.question import Question, QuestionHistory

DATA_DIR = Path("data")
QUESTIONS_FILE = DATA_DIR / "questions.json"
HISTORY_FILE = DATA_DIR / "question_history.json"


class QuestionStore:
    """Хранилище вопросов викторин."""

    def __init__(self) -> None:
        self._questions: dict[str, Question] = {}
        self._use_db = True  # TODO: переключить на PostgreSQL
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить вопросы из файла (fallback)."""
        if not QUESTIONS_FILE.exists():
            self._initialize_default_questions()
            return

        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for question_id, question_data in data.items():
                    self._questions[question_id] = Question.from_dict(question_data)
        except (json.JSONDecodeError, KeyError):
            self._initialize_default_questions()

    def save(self) -> None:
        """Сохранить вопросы в файл (fallback)."""
        data = {q_id: q.to_dict() for q_id, q in self._questions.items()}
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _initialize_default_questions(self) -> None:
        """Инициализировать вопросы по умолчанию."""
        # Математические вопросы
        self._questions["math_1"] = Question(
            id="math_1",
            question="Сколько будет 15 + 27?",
            answer="42",
            category="math",
            difficulty="easy",
            hints=["Подумайте о десятках", "10 + 20 = 30", "5 + 7 = 12"]
        )
        self._questions["math_2"] = Question(
            id="math_2",
            question="Сколько будет 8 × 7?",
            answer="56",
            category="math",
            difficulty="easy",
            hints=["7 × 8 = 56", "Умножение таблицы", "7 × 10 = 70, минус 14"]
        )
        self._questions["math_3"] = Question(
            id="math_3",
            question="Сколько будет 144 ÷ 12?",
            answer="12",
            category="math",
            difficulty="medium",
            hints=["12 × 12 = 144", "Подумайте о таблице умножения", "12 × 10 = 120"]
        )

        # Загадки
        self._questions["riddle_1"] = Question(
            id="riddle_1",
            question="Что можно держать, даже если это нельзя кидать?",
            answer="дыхание",
            category="riddle",
            difficulty="medium",
            hints=["Это не физический предмет", "Это связано с человеком", "Вы делаете это постоянно"]
        )
        self._questions["riddle_2"] = Question(
            id="riddle_2",
            question="Чем больше из неё берешь, тем больше она становится. Что это?",
            answer="яма",
            category="riddle",
            difficulty="medium",
            hints=["Это связано с землей", "Это может быть опасно", "Люди делают её специально"]
        )

        # Заголовки
        self._questions["trivia_1"] = Question(
            id="trivia_1",
            question="Какая планета ближе всего к Солнцу?",
            answer="Меркурий",
            category="trivia",
            difficulty="easy",
            hints=["Это не Земля", "Это самая маленькая планета", "Названа в честь римского бога"],
            options=["Меркурий", "Венера", "Марс", "Юпитер"]
        )
        self._questions["trivia_2"] = Question(
            id="trivia_2",
            question="Какой океан самый большой?",
            answer="Тихий океан",
            category="trivia",
            difficulty="easy",
            hints=["Это не Атлантический", "Находится между Азией и Америкой", "Площадь около 165 млн км²"],
            options=["Тихий океан", "Атлантический океан", "Индийский океан", "Северный Ледовитый"]
        )

        self.save()

    def get_question(self, question_id: str) -> Question | None:
        """Получить вопрос по ID."""
        return self._questions.get(question_id)

    def get_questions_by_category(self, category: str) -> list[Question]:
        """Получить вопросы по категории."""
        return [q for q in self._questions.values() if q.category == category and q.is_active]

    def get_random_question(self, category: str, exclude_ids: list[str] | None = None) -> Question | None:
        """Получить случайный вопрос из категории, исключая определенные ID."""
        import random

        questions = self.get_questions_by_category(category)
        if exclude_ids:
            questions = [q for q in questions if q.id not in exclude_ids]

        if not questions:
            return None

        return random.choice(questions)

    def add_question(self, question: Question) -> None:
        """Добавить новый вопрос."""
        self._questions[question.id] = question
        self.save()


class QuestionHistoryStore:
    """Хранилище истории вопросов для защиты от запоминания."""

    def __init__(self) -> None:
        self._history: list[QuestionHistory] = []
        self._use_db = True  # TODO: переключить на PostgreSQL
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить историю из файла (fallback)."""
        if not HISTORY_FILE.exists():
            return

        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._history = [QuestionHistory.from_dict(h) for h in data]
        except (json.JSONDecodeError, KeyError):
            self._history = []

    def save(self) -> None:
        """Сохранить историю в файл (fallback)."""
        data = [h.to_dict() for h in self._history]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_history(self, history: QuestionHistory) -> None:
        """Добавить запись в историю."""
        self._history.append(history)
        self.save()

    def get_recent_questions(self, guild_id: int, user_id: int, limit: int = 20) -> list[str]:
        """Получить ID последних вопросов игрока."""
        user_history = [
            h for h in self._history
            if h.guild_id == guild_id and h.user_id == user_id
        ]
        user_history.sort(key=lambda x: x.answered_at, reverse=True)
        return [h.question_id for h in user_history[:limit]]

    def get_recent_questions_for_cooldown(self, guild_id: int, user_id: int, hours: int = 24) -> list[str]:
        """Получить ID вопросов за последние N часов."""
        from datetime import datetime, timedelta

        cutoff_time = (datetime.now() - timedelta(hours=hours)).isoformat()

        user_history = [
            h for h in self._history
            if h.guild_id == guild_id and h.user_id == user_id and h.answered_at > cutoff_time
        ]
        return [h.question_id for h in user_history]


# Глобальные инстансы
question_store = QuestionStore()
question_history_store = QuestionHistoryStore()
