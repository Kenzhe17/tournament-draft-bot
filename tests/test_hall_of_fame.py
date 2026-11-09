"""Тесты Зала славы: форматирование записей и view."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.hall_of_fame.records import Record, CATEGORIES
from services.hall_of_fame.view import _display_name, _record_block


def test_categories_seven():
    assert len(CATEGORIES) == 7
    assert [k for k, _ in CATEGORIES] == [
        "combat", "results", "rating", "tournaments",
        "ultimatum", "economy", "community",
    ]


def test_display_name_member_mention():
    class FakeMember:
        mention = "<@123>"
    class FakeGuild:
        def get_member(self, uid):
            return FakeMember() if uid == 123 else None
    rec = Record("T", "1", user_id=123, user_name="Name")
    assert _display_name(rec, FakeGuild()) == "<@123>"


def test_display_name_left_user_uses_stored_name():
    class FakeGuild:
        def get_member(self, uid):
            return None
    rec = Record("T", "1", user_id=999, user_name="PlayerName")
    assert _display_name(rec, FakeGuild()) == "`PlayerName`"


def test_display_name_no_user():
    rec = Record("T", "1")
    assert _display_name(rec, None) == ""


def test_record_block_unavailable():
    text = _record_block(Record("T", available=False), None)
    assert "Рекорд пока не установлен" in text
    assert "`" not in text.split("\n")[1]


def test_record_block_full():
    rec = Record("Best K/D", "5.50", user_id=555, user_name="Pro",
                 context="40 матчей")
    text = _record_block(rec, None)
    assert "**Best K/D**" in text
    assert "`5.50`" in text
    assert "`Pro`" in text
    assert "40 матчей" in text


def test_no_negative_id_mention():
    # Технические/отрицательные ID никогда не становятся mention
    rec = Record("T", "1", user_id=-123, user_name="Bot5")
    guild = type("G", (), {"get_member": lambda s, u: None})()
    assert _display_name(rec, guild) == "`Bot5`"
    rec2 = Record("T", "1", user_id=-123)
    assert _display_name(rec2, guild) == ""
