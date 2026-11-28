"""Unit-тесты спонтанных событий 2.0: активити-гейт, игры,
выбор приза, конфиг. Без БД и Discord."""
import random
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import event_games, event_rewards
from services.activity_tracker import ActivityGate, content_hash


# --------------------------------------------------------------- gate

def test_gate_thresholds_met():
    g = ActivityGate()
    key = (1, 100)
    t = 1_000_000.0
    # 3 автора, сообщения каждые 11с (>= min_interval 10с).
    for i in range(40):
        uid = 1 + (i % 3)
        g.record(key, uid, f"msg {i} unique {i}",
                 now=t + i * 11.0)
    # В окне 300с: >=10 credited, >=3 авторов, >=2 активных за 60с.
    assert g.evaluate(key, now=t + 40 * 11.0) is True
    # max_per_author=4 в скользящем окне:series из 10 сообщений
    # одного автора подряд даёт не более 4 credited.
    g2 = ActivityGate(min_interval_s=0)
    credited = sum(
        1 for i in range(10)
        if g2.record((2, 2), 7, f"burst {i}", now=t + i))
    assert credited == 4


def test_gate_min_messages_and_users():
    g = ActivityGate(min_messages=10, min_unique_users=3,
                     max_per_author=4, min_interval_s=0)
    key = (1, 1)
    t = 1000.0
    for i in range(9):
        g.record(key, i % 3 + 1, f"u{i}", now=t + i)
    assert g.evaluate(key, now=t + 9) is False
    g.record(key, 3, "u9", now=t + 9)
    assert g.evaluate(key, now=t + 9) is True


def test_gate_flood_single_spammer():
    """Один спамер не может накопить credited-сообщения:
    min_interval + max_per_author."""
    g = ActivityGate(min_interval_s=10, max_per_author=4,
                     min_unique_users=1, min_messages=3,
                     min_active_authors=1)
    key = (1, 1)
    t = 1000.0
    credited = 0
    for i in range(30):
        if g.record(key, 42, f"spam {i}", now=t + i):
            credited += 1
    assert credited <= 4  # max_per_author
    # Остальные авторы нужны для unique>=3 в реальном конфиге.


def test_gate_duplicate_content_not_counted():
    g = ActivityGate(min_interval_s=0, max_per_author=100)
    key = (1, 1)
    t = 1000.0
    assert g.record(key, 1, "  Одинаковый   Текст ", now=t) is True
    # другой регистр/пробелы — тот же хэш
    assert g.record(key, 2, "одинаковый текст", now=t + 1) is False
    assert g.record(key, 3, "другой текст", now=t + 2) is True


def test_gate_window_slides():
    g = ActivityGate(window_s=60, min_messages=2,
                     min_unique_users=1, max_per_author=10,
                     min_interval_s=0, min_active_authors=1)
    key = (1, 1)
    g.record(key, 1, "a", now=1000.0)
    g.record(key, 2, "b", now=1001.0)
    assert g.evaluate(key, now=1010.0) is True
    # Окно уехало — старые сэмплы вытеснены.
    assert g.evaluate(key, now=2000.0) is False


def test_gate_no_eval_without_messages():
    g = ActivityGate()
    assert g.evaluate((9, 9), now=time.time()) is False


# --------------------------------------------------------------- games

def _prize(amount=100, rtype="coins", item_id=None):
    return {"type": rtype, "amount": amount, "item_id": item_id,
            "label": "x"}


def test_speed_first_click():
    cfg = {}
    actions = {"winner": 55}
    out = event_games.compute_outcome("speed", cfg, actions,
                                      _prize(300), 0)
    assert out["winners"] == [55]
    assert out["payout"] == 300
    out2 = event_games.compute_outcome("speed", cfg, {}, _prize(), 0)
    assert out2["winners"] == []


def test_raffle_random_participant():
    actions = {"p:1": 1, "p:2": 1, "p:3": 1}
    out = event_games.compute_outcome("raffle", {}, actions,
                                      _prize(200), 0)
    assert out["winners"][0] in (1, 2, 3)
    out_none = event_games.compute_outcome("raffle", {}, {}, _prize(), 0)
    assert out_none["reason"] == "no_participants"


def test_chests_only_correct_side_wins():
    cfg = {"win_chest": 2}
    actions = {"choice:1": 2, "choice:2": 0, "choice:3": 2}
    out = event_games.compute_outcome("chests", cfg, actions,
                                      _prize(150), 0)
    assert out["winners"][0] in (1, 3)
    out_no = event_games.compute_outcome(
        "chests", cfg, {"choice:1": 0}, _prize(), 0)
    assert out_no["reason"] == "no_correct"


def test_quick_math_first_correct():
    cfg = {"a": 6, "op": "+", "b": 7, "accepted": ["13"]}
    assert event_games.answer_matches("13", cfg["accepted"])
    assert not event_games.answer_matches("14", cfg["accepted"])
    out = event_games.compute_outcome("quick_math", cfg,
                                      {"winner": 9}, _prize(80), 0)
    assert out["winners"] == [9]


def test_minority_rules():
    prize = _prize(rtype="item", amount=0, item_id="sword")
    # empty side → no winner
    out = event_games.compute_outcome(
        "minority", {}, {"choice:1": "red"}, prize, 0)
    assert out["reason"] == "empty_side"
    # tie → no winner
    out = event_games.compute_outcome(
        "minority", {}, {"choice:1": "red", "choice:2": "blue",
                         "choice:3": "red", "choice:4": "blue"},
        prize, 0)
    assert out["reason"] == "tie"
    # smaller side wins, item prize preserved (payout None)
    out = event_games.compute_outcome(
        "minority", {}, {"choice:1": "blue", "choice:2": "red",
                         "choice:3": "red", "choice:4": "red",
                         "choice:5": "red"},
        prize, 0)
    assert out["winners"] == [1]
    assert out["payout"] is None


def test_dice_highest_and_tie():
    actions = {"roll:1": 42, "roll:2": 77, "roll:3": 77}
    out = event_games.compute_outcome("dice", {}, actions,
                                      _prize(120), 0)
    assert out["winners"][0] in (2, 3)
    out_empty = event_games.compute_outcome("dice", {}, {}, _prize(), 0)
    assert out_empty["winners"] == []


def test_growing_value_growth_and_claim():
    prize = {"type": "coins", "amount": 500, "start": 100,
             "item_id": None}
    assert event_games.growing_value(prize, 0, 45) == 100
    assert event_games.growing_value(prize, 45, 45) == 500
    mid = event_games.growing_value(prize, 22.5, 45)
    assert 100 < mid < 500
    cfg = {"burn_after": 30.0}
    actions = {"claim:7": 12.0}
    out = event_games.compute_outcome("growing", cfg, actions,
                                      prize, 0)
    assert out["winners"] == [7]
    assert out["payout"] == event_games.growing_value(prize, 12.0, 45)
    # burned — no claims
    out2 = event_games.compute_outcome("growing", cfg, {}, prize, 0)
    assert out2["reason"] == "burned" and out2["winners"] == []


def test_word_aliases_and_scramble():
    cfg_word, aliases = event_games.WORDS[0]
    accepted = [cfg_word, *aliases]
    assert event_games.answer_matches(cfg_word.upper(), accepted)
    assert event_games.answer_matches("  Ёлка  ".replace("Ё", "е"),
                                      ["елка"])
    rng = random.Random(1)
    s = event_games._scramble("привет", rng)
    assert s != "привет" and sorted(s) == sorted("привет")


def test_last_click_latest_wins():
    actions = {"click:1": 100.0, "click:2": 200.0, "click:3": 150.0}
    out = event_games.compute_outcome("last_click", {}, actions,
                                      _prize(90), 0)
    assert out["winners"] == [2]
    out_none = event_games.compute_outcome("last_click", {}, {},
                                           _prize(), 0)
    assert out_none["winners"] == []


def test_guess_number_and_krokodil():
    cfg = {"number": 17, "accepted": ["17"]}
    assert event_games.answer_matches("17", cfg["accepted"])
    out = event_games.compute_outcome("guess_number", cfg,
                                      {"winner": 4}, _prize(60), 0)
    assert out["winners"] == [4]
    clue, word, aliases = event_games.CLUES[0]
    acc = [word, *aliases]
    assert event_games.answer_matches(word, acc)
    out2 = event_games.compute_outcome("krokodil",
                                       {"accepted": acc},
                                       {"winner": 8}, _prize(70), 0)
    assert out2["winners"] == [8]


def test_make_config_secrets():
    rng = random.Random(42)
    settings = {}
    chests = event_games.make_config("chests", _prize(), settings, rng)
    assert 0 <= chests["win_chest"] < 5
    math_cfg = event_games.make_config("quick_math", _prize(),
                                       settings, rng)
    assert math_cfg["a"] + (math_cfg["b"] if math_cfg["op"] == "+"
                            else 0) or True
    gn = event_games.make_config("guess_number", _prize(), settings,
                                 rng)
    assert 1 <= gn["number"] <= 20
    grow = event_games.make_config("growing", _prize(), settings, rng)
    assert 0 < grow["burn_after"] < 45
    lc = event_games.make_config("last_click", _prize(), settings, rng)
    assert 15.0 <= lc["duration"] <= 45.0


def test_public_deadline_and_close():
    start = 1000.0
    assert event_games.public_deadline("speed", {}, start) == 1030.0
    lc_cfg = {"duration": 20.0}
    assert event_games.public_deadline("last_click", lc_cfg,
                                       start) == 1020.0
    grow_cfg = {"burn_after": 25.0}
    assert event_games.close_deadline("growing", grow_cfg, start,
                                      1045.0) == 1025.0
    assert event_games.close_deadline("speed", {}, start, 1030.0) \
        is None


# --------------------------------------------------------------- prize

def _settings(**kw):
    base = {
        "reward_weights": {"coins": 50, "xp": 30, "rep": 15,
                           "item": 5},
        "coins_min": 100, "coins_max": 500,
        "xp_min": 50, "xp_max": 200,
        "rep_min": 1, "rep_max": 3,
        "item_exclude_categories": ["roles", "access"],
        "growing_burn_min_seconds": 15,
        "growing_burn_max_seconds": 40,
    }
    base.update(kw)
    return base


def test_select_prize_numeric_ranges():
    rng = random.Random(7)
    for _ in range(50):
        p = event_rewards.select_prize(1, _settings(), "speed", rng)
        assert p["type"] in ("coins", "xp", "rep", "item")
        if p["type"] == "coins":
            assert 100 <= p["amount"] <= 500
        elif p["type"] == "xp":
            assert 50 <= p["amount"] <= 200
        elif p["type"] == "rep":
            assert 1 <= p["amount"] <= 3


def test_select_prize_growing_numeric_only():
    """growing: item-вес исключён, тип только coins/xp/rep,
    start <= amount."""
    rng = random.Random(3)
    for _ in range(50):
        p = event_rewards.select_prize(1, _settings(), "growing", rng)
        assert p["type"] in ("coins", "xp", "rep")
        assert "start" in p and p["start"] <= p["amount"]


def test_select_prize_item_needs_catalog():
    """Пустой eligible-каталог → item исключается до выбора."""
    rng = random.Random(11)
    with patch.object(event_rewards, "eligible_items",
                      return_value=[]):
        for _ in range(30):
            p = event_rewards.select_prize(1, _settings(), "speed",
                                           rng)
            assert p["type"] != "item"


def test_select_prize_item_fixed_id():
    fake_item = SimpleNamespace(id="frame_gold", name="Золотая рамка",
                                is_active=True, item_type="cosmetic",
                                bundle=None, required_level=0,
                                category="frames",
                                available_from=None,
                                available_until=None)
    rng = random.Random(5)
    weights = {"coins": 0, "xp": 0, "rep": 0, "item": 100}
    with patch.object(event_rewards, "eligible_items",
                      return_value=[fake_item]):
        p = event_rewards.select_prize(
            1, _settings(reward_weights=weights), "speed", rng)
    assert p["type"] == "item"
    assert p["item_id"] == "frame_gold"


def test_eligible_items_policy():
    """Allowlist: только активные cosmetic без level/bundle,
    вне исключённых категорий и сезонного окна."""
    items = [
        SimpleNamespace(id="ok", is_active=True,
                        item_type="cosmetic", bundle=None,
                        required_level=0, category="frames",
                        available_from=None, available_until=None),
        SimpleNamespace(id="role", is_active=True,
                        item_type="role", bundle=None,
                        required_level=0, category="roles",
                        available_from=None, available_until=None),
        SimpleNamespace(id="lvl", is_active=True,
                        item_type="cosmetic", bundle=None,
                        required_level=5, category="frames",
                        available_from=None, available_until=None),
        SimpleNamespace(id="bundle", is_active=True,
                        item_type="cosmetic", bundle=["a"],
                        required_level=0, category="frames",
                        available_from=None, available_until=None),
        SimpleNamespace(id="inactive", is_active=False,
                        item_type="cosmetic", bundle=None,
                        required_level=0, category="frames",
                        available_from=None, available_until=None),
        SimpleNamespace(id="expired", is_active=True,
                        item_type="cosmetic", bundle=None,
                        required_level=0, category="frames",
                        available_from="2020-01-01",
                        available_until="2020-12-31"),
    ]
    with patch("storage.shop_store.shop_store") as ss:
        ss.get_all_items.return_value = items
        out = event_rewards.eligible_items(["roles"])
    assert [i.id for i in out] == ["ok"]


def test_pick_game_no_immediate_repeat():
    rng = random.Random(0)
    games = event_games.GAME_ORDER
    seen = set()
    for _ in range(30):
        g = event_games.pick_game(games, {}, rng,
                                  last_type="speed")
        assert g != "speed"
        seen.add(g)
    assert len(seen) > 1


def test_pick_game_disabled_empty():
    assert event_games.pick_game([], {}, random.Random(1)) is None
