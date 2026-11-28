"""Events Content Expansion — unit-тесты.

Покрытие без PG:
- validate_catalog: схема, дубликаты, clue-vs-answer;
- normalize_answer / scramble_word / answer_matches
  (ё/е, регистр, дефисы, алиасы, «комп» != «компьютер»);
- bundled-каталоги: оба файла валидны, ровно 1000 активных
  записей, scramble сохраняет мультимножество букв;
- make_config(content=...): word/krokodil берут запись
  каталога и фиксируют content_id; legacy-путь без content
  работает как раньше;
- pick_game/enabled_games не изменились.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import event_content, event_games

# ------------------------------------------------------- validate_catalog

def _word_entry(**kw):
    e = {"id": "w1", "word": "котёл", "category": "Быт",
         "difficulty": "easy", "enabled": True}
    e.update(kw)
    return e


def _krok_entry(**kw):
    e = {"id": "k1", "answer": "котёл", "clue": "Варят в нём Sup",
         "accepted": ["котёл"], "category": "Быт",
         "difficulty": "easy", "enabled": True}
    e.update(kw)
    return e


def _cat(game, entries):
    return {"schema_version": 1, "content_version": "t",
            "locale": "ru", "game": game, "entries": entries}


def test_validate_word_ok():
    assert event_content.validate_catalog(
        _cat("word", [_word_entry()]), "word") == []


def test_validate_krok_ok():
    assert event_content.validate_catalog(
        _cat("krokodil", [_krok_entry()]), "krokodil") == []


def test_validate_rejects_bad():
    errs = event_content.validate_catalog(
        _cat("word", [
            _word_entry(id="a"), _word_entry(id="a"),       # dup id
            _word_entry(id="b", word="ab"),                  # короткое
            _word_entry(id="c", word="a b"),                 # пробел
            _word_entry(id="d", word="word"),                # не кириллица
            _word_entry(id="e", word="ёжик", difficulty="x"),
        ]), "word")
    assert errs  # есть ошибки
    assert any("duplicate id" in e for e in errs)


def test_validate_clue_contains_answer():
    errs = event_content.validate_catalog(
        _cat("krokodil", [
            _krok_entry(clue="Котёл варит Sup на плите"),
        ]), "krokodil")
    assert any("clue contains answer" in e for e in errs)


def test_validate_clue_contains_alias():
    errs = event_content.validate_catalog(
        _cat("krokodil", [
            _krok_entry(accepted=["котёл", "казан"],
                        clue="Казан варит Sup на плите"),
        ]), "krokodil")
    assert any("clue contains alias" in e for e in errs)


def test_validate_accepted_first_is_answer():
    errs = event_content.validate_catalog(
        _cat("krokodil", [
            _krok_entry(accepted=["другое", "котёл"]),
        ]), "krokodil")
    assert any("accepted[0]" in e for e in errs)


def test_validate_game_mismatch():
    errs = event_content.validate_catalog(
        _cat("word", []), "krokodil")
    assert any("game mismatch" in e for e in errs)


# ------------------------------------------------------- нормализация

def test_normalize_answer():
    n = event_content.normalize_answer
    assert n("  Плей-Офф ") == "плей офф"
    assert n("ЁЖИК") == "ежик"
    assert n("веб-камера") == "веб камера"
    assert n("самолёт") == "самолет"


def test_answer_matches():
    m = event_content.answer_matches
    acc = ["плейофф", "плей-офф"]
    assert m("ПЛЕЙ-ОФФ", acc)
    assert m("плей офф", acc)
    assert m("плейофф", acc)
    # «комп» не должен засчитываться за «компьютер»
    assert not m("комп", ["компьютер"])
    # ё/е
    assert m("ежик", ["ёжик"])
    assert m("ЁЖИК", ["ежик"])
    # пустой ответ
    assert not m("   ", ["кот"])


def test_scramble_word_preserves_letters():
    rng = random.Random(42)
    for _ in range(50):
        w = "программирование"
        s = event_content.scramble_word(w, rng)
        assert s != w
        assert Counter(s) == Counter(w)
    # слово с ё и повторяющимися буквами
    for _ in range(20):
        s = event_content.scramble_word("ёжик", rng)
        assert Counter(s) == Counter("ёжик") and s != "ёжик"


# ------------------------------------------------------- bundled

def test_bundled_catalogs_valid():
    for game in event_content.GAME_FILES:
        data = event_content.bundled_catalog(game)
        assert data is not None, f"{game} catalog failed to load"
        entries = [e for e in data["entries"] if e.get("enabled")]
        assert len(entries) == 1000, \
            f"{game}: {len(entries)} enabled entries"
        # 1000 уникальных id
        assert len({e["id"] for e in entries}) == 1000
        # равномерность сложности — не обязана быть точной,
        # но уровни должны присутствовать все три
        diffs = {e["difficulty"] for e in entries}
        assert diffs == {"easy", "medium", "hard"}


def test_bundled_scramble_all_words():
    """Каждое bundled-слово можно перемешать с сохранением
    букв и отличием от оригинала."""
    rng = random.Random(1)
    entries = event_content.bundled_entries("word")
    for e in entries[:200]:  # выборка — все 1000 в PG-тестах
        w = e["word"]
        s = event_content.scramble_word(w, rng)
        assert Counter(s) == Counter(w), w
        assert s != w, w


# ------------------------------------------------------- make_config

def test_make_config_word_with_content():
    rng = random.Random(7)
    entry = {"id": "word_x", "word": "вертолёт",
             "accepted": ["вертолет", "вертушка"],
             "difficulty": "medium", "category": "Транспорт"}
    cfg = event_games.make_config(
        "word", {"type": "coins", "amount": 100}, {}, rng,
        content=entry)
    assert cfg["word"] == "вертолёт"
    assert cfg["content_id"] == "word_x"
    assert cfg["content_difficulty"] == "medium"
    assert Counter(cfg["scrambled"]) == Counter("вертолёт")
    assert cfg["scrambled"] != "вертолёт"
    # accepted включает канонический + алиасы, без дублей
    assert "вертолёт" in cfg["accepted"]
    assert "вертолет" in cfg["accepted"]
    assert len(cfg["accepted"]) == len(set(cfg["accepted"]))
    # ответ матчится через answer_matches
    assert event_games.answer_matches("ВЕРТОЛЕТ", cfg["accepted"])
    assert event_games.answer_matches("вертушка", cfg["accepted"])


def test_make_config_krokodil_with_content():
    rng = random.Random(3)
    entry = {"id": "krok_y", "answer": "аквариум",
             "clue": "В ней живут рыбы.",
             "accepted": ["аквариум", "аква"],
             "difficulty": "easy", "category": "Быт"}
    cfg = event_games.make_config(
        "krokodil", {"type": "coins", "amount": 50}, {}, rng,
        content=entry)
    assert cfg["word"] == "аквариум"
    assert cfg["clue"] == "В ней живут рыбы."
    assert "аквариум" in cfg["accepted"]
    assert "аква" in cfg["accepted"]


def test_make_config_legacy_still_works():
    """Без content — legacy-списки WORDS/CLUES (старые тесты,
    fallback)."""
    rng = random.Random(5)
    cfg = event_games.make_config(
        "word", {"type": "coins", "amount": 1}, {}, rng)
    assert cfg["word"] and cfg["scrambled"]
    assert "content_id" not in cfg
    cfg2 = event_games.make_config(
        "krokodil", {"type": "coins", "amount": 1}, {}, rng)
    assert cfg2["word"] and cfg2["clue"]


# ------------------------------------------------------- игры не тронуты

def test_other_games_unchanged():
    """Остальные типы не зависят от каталога."""
    rng = random.Random(9)
    for g in ("speed", "raffle", "chests", "quick_math",
              "minority", "dice", "growing", "last_click",
              "guess_number"):
        cfg = event_games.make_config(
            g, {"type": "coins", "amount": 10}, {}, rng)
        assert isinstance(cfg, dict)
    assert event_games.GAME_ORDER == (
        "speed", "raffle", "chests", "quick_math", "minority",
        "dice", "growing", "word", "last_click",
        "guess_number", "krokodil")


# ------------------------------------------------------- cog wiring

@pytest.mark.asyncio
async def test_pick_content_passes_settings_and_handles_errors():
    """_pick_content: weights/categories из settings, ошибка
    стора → None (тип недоступен, бот не падает)."""
    from cogs.coin_drop import CoinDropCog

    calls = {}

    class _Store:
        async def pick(self, game, **kw):
            calls.update(kw)
            calls["game"] = game
            return {"id": "x1", "word": "слово"}

    class _Self:
        _rng = random.Random(0)
        _content_ready = True
        _content_store_obj = _Store()

        def _content_store(self):
            return self._content_store_obj

        async def _ensure_content_ready(self):
            pass

    settings = {
        "content_word_difficulty_weights": {"easy": 1},
        "content_word_disabled_categories": ["Спорт"],
    }
    entry = await CoinDropCog._pick_content(
        _Self(), "word", 123, settings, "evt1")
    assert entry == {"id": "x1", "word": "слово"}
    assert calls["game"] == "word"
    assert calls["guild_id"] == "123"
    assert calls["event_id"] == "evt1"
    assert calls["difficulty_weights"] == {"easy": 1}
    assert calls["disabled_categories"] == {"Спорт"}

    class _BadStore:
        async def pick(self, game, **kw):
            raise RuntimeError("db down")

    _Self._content_store_obj = _BadStore()
    assert await CoinDropCog._pick_content(
        _Self(), "word", 1, {}, "e") is None


def test_reveal_answer_only_for_answer_games():
    """Ответ виден только в финальной карточке и только для
    игр с ответом."""
    from cogs.coin_drop import _EventSession, _reveal_answer

    def _sess(game, cfg):
        return _EventSession.from_row({
            "event_id": "e", "guild_id": 1, "channel_id": 2,
            "game_type": game, "prize": {}, "config": cfg,
            "actions": {}, "message_id": None})

    assert _reveal_answer(_sess("word", {"word": "котёл"})) \
        == "котёл"
    assert _reveal_answer(_sess("krokodil",
                                {"word": "аквариум"})) == "аквариум"
    assert _reveal_answer(_sess("guess_number",
                                {"number": 7})) == "7"
    assert _reveal_answer(_sess("speed", {})) is None
    assert _reveal_answer(_sess("word", {})) is None
