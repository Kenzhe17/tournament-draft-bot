"""Модели для вопросов викторин."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Question:
    """Вопрос для викторины."""

    id: str
    question: str
    answer: str
    category: str  # math, word, riddle, trivia, logic, etc.
    difficulty: str  # easy, medium, hard
    hints: list[str]  # Список подсказок
    options: list[str] | None = None  # Варианты ответов (для multiple choice)
    is_active: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "difficulty": self.difficulty,
            "hints": self.hints,
            "options": self.options,
            "is_active": self.is_active
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Question":
        """Десериализация из словаря."""
        return cls(
            id=data.get("id", ""),
            question=data.get("question", ""),
            answer=data.get("answer", ""),
            category=data.get("category", ""),
            difficulty=data.get("difficulty", "medium"),
            hints=data.get("hints", []),
            options=data.get("options"),
            is_active=data.get("is_active", True)
        )


@dataclass
class QuestionHistory:
    """История вопросов игрока (защита от запоминания)."""

    guild_id: int
    user_id: int
    question_id: str
    answered_at: str  # ISO timestamp
    was_correct: bool

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "question_id": self.question_id,
            "answered_at": self.answered_at,
            "was_correct": self.was_correct
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuestionHistory":
        """Десериализация из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            user_id=data.get("user_id", 0),
            question_id=data.get("question_id", ""),
            answered_at=data.get("answered_at", ""),
            was_correct=data.get("was_correct", False)
        )
