"""Игровые типы спонтанных событий 2.0 — чистая логика.

11 типов (ровно столько, других нет):

    speed        Быстрый дроп — первый валидный клик, 30 c
    raffle       Розыгрыш — случайный уникальный участник, 60 c
    chests       Таинственные сундуки — 5 кнопок, один выбор,
                 случайный победитель среди верных, 30 c
    quick_math   Быстрый счёт — модалка, первый верный ответ, 30 c
    minority     Меньшинство — скрытый выбор red/blue, 30 c,
                 обе стороны непусты, при ничьей нет победителя,
                 случайный из меньшей стороны (фикс. приз, вкл. item)
    dice         Счастливый кубик — один RNG 1–100 на юзера, 30 c,
                 максимум побеждает, ничья → случайный из лидеров
    growing      Растущий куш — до 45 c, видимая растущая сумма,
                 скрытый дедлайн сгорания, первый валидный клейм,
                 иначе без выплаты; только числовые награды
    word         Перепутанное слово — curated слова, модалка,
                 первый верный (вкл. алиасы), 30 c
    last_click   Последний клик — скрытый дедлайн 15–45 c,
                 один принятый клик на юзера, побеждает последний
    guess_number Угадай число — скрытое int 1–20, одна попытка
                 модалкой, первый верный, 30 c
    krokodil     Крокодил — curated подсказка без раскрытия слова,
                 модалка, первый верный (вкл. алиасы), 45 c

Формат ``actions`` (плоский JSONB, ключи с префиксами):
    ``p:<uid>``      — участник (raffle)
    ``choice:<uid>`` — выбор (chests → int, minority → "red"/"blue")
    ``roll:<uid>``   — кубик (int 1..100)
    ``click:<uid>``  — unix-время последнего клика (last_click)
    ``gclaim``       — {"uid","elapsed"} единственный клейм (growing);
                       ``claim:<uid>`` — legacy-формат до gclaim
    ``winner``       — int user_id (игры «первый верный»)
    ``answer``       — str (опционально, для отображения)

Секреты игры (выигрышный сундук, число, слово, burn-дедлайн)
лежат в ``config`` строки события и не попадают в публичную
карточку.
"""

from __future__ import annotations

import random
import string
import time
import unicodedata
from typing import Any

# ------------------------------------------------------------------
# Метаданные типов
# ------------------------------------------------------------------

GAME_DEFS: dict[str, dict[str, Any]] = {
    "speed":        {"title": "Быстрый дроп", "duration": 30,
                     "kind": "first_click"},
    "raffle":       {"title": "Розыгрыш", "duration": 60,
                     "kind": "raffle"},
    "chests":       {"title": "Таинственные сундуки", "duration": 30,
                     "kind": "chests", "chests": 5},
    "quick_math":   {"title": "Быстрый счёт", "duration": 30,
                     "kind": "modal_answer"},
    "minority":     {"title": "Меньшинство", "duration": 30,
                     "kind": "minority"},
    "dice":         {"title": "Счастливый кубик", "duration": 30,
                     "kind": "dice"},
    "growing":      {"title": "Растущий куш", "duration": 45,
                     "kind": "growing"},
    "word":         {"title": "Перепутанное слово", "duration": 30,
                     "kind": "modal_answer"},
    "last_click":   {"title": "Последний клик", "duration": 45,
                     "kind": "last_click"},
    "guess_number": {"title": "Угадай число", "duration": 30,
                     "kind": "modal_answer"},
    "krokodil":     {"title": "Крокодил", "duration": 45,
                     "kind": "modal_answer"},
}

GAME_ORDER: tuple[str, ...] = tuple(GAME_DEFS)


def enabled_games(disabled: list[str] | None,
                  weights: dict[str, float] | None,
                  exclude: set[str] | None = None
                  ) -> list[str]:
    """Доступные типы: минус disabled и exclude (напр. 'growing'
    для item-приза не исключается — growing и так только
    числовой; exclude используется для выбора типа под приз)."""
    disabled_set = set(disabled or [])
    out = [g for g in GAME_ORDER
           if g not in disabled_set and g not in (exclude or set())]
    return out


def pick_game(games: list[str], weights: dict[str, float] | None,
              rng: random.Random, last_type: str | None = None
              ) -> str | None:
    """Случайный тип по весам; равные веса по умолчанию.
    Без немедленного повтора, если есть альтернативы."""
    if not games:
        return None
    pool = [g for g in games if g != last_type] or list(games)
    w = weights or {}
    weights_list = []
    for g in pool:
        try:
            weights_list.append(max(0.0, float(w.get(g, 1.0) or 0.0)))
        except (TypeError, ValueError):
            weights_list.append(0.0)
    if sum(weights_list) <= 0:
        weights_list = [1.0] * len(pool)
    return rng.choices(pool, weights=weights_list, k=1)[0]


# ------------------------------------------------------------------
# Секретная конфигурация игр (генерируется ДО публикации карточки)
# ------------------------------------------------------------------

def _scramble(word: str, rng: random.Random) -> str:
    """Перемешать буквы; гарантируем, что результат != слову."""
    letters = list(word)
    for _ in range(20):
        rng.shuffle(letters)
        s = "".join(letters)
        if s != word:
            return s
    return word[::-1]


def _math_problem(rng: random.Random) -> dict[str, Any]:
    op = rng.choice(["+", "-", "*"])
    if op == "+":
        a, b = rng.randint(2, 30), rng.randint(2, 30)
        ans = a + b
    elif op == "-":
        a = rng.randint(5, 40)
        b = rng.randint(1, a)
        ans = a - b
    else:
        a, b = rng.randint(2, 12), rng.randint(2, 12)
        ans = a * b
    return {"a": a, "b": b, "op": op, "answer": ans}


# (слово, алиасы) — ответ проверяется по normalize ниже.
WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("самолёт", ("самолет", "аэроплан", "самолёт")),
    ("вертолёт", ("вертолет", "вертушка")),
    ("компьютер", ("комп", "пк", "компьютер")),
    ("программирование", ("программирование", "кодинг", "програмирование")),
    ("торт", ("пирожное", "тортик", "десерт")),
    ("кошка", ("кот", "киска", "кошка")),
    ("собака", ("пёс", "пес", "собачка", "друг")),
    ("холодильник", ("холодильник", "холодос")),
    ("велосипед", ("велик", "байк", "велосипед")),
    ("гитара", ("гитара", "гитарка")),
    ("библиотека", ("библиотека", "библиотек")),
    ("музей", ("музей", "музеюшка")),
    ("аэропорт", ("аэропорт", "аэродром")),
    ("подводная лодка", ("подлодка", "подводная лодка", "субмарина")),
    ("ракета", ("ракета", "космический корабль")),
    ("пирамида", ("пирамида", "египет")),
    ("туман", ("туман", "мгла", "дымка")),
    ("радуга", ("радуга", "радужка")),
    ("северное сияние", ("сияние", "полярное сияние", "северное сияние")),
    ("компас", ("компас", "навигация")),
    ("фонарь", ("фонарь", "светильник")),
    ("зонт", ("зонт", "зонтик")),
    ("чемодан", ("чемодан", "багаж")),
    ("подарок", ("подарок", "сюрприз", "дар")),
    ("фейерверк", ("фейерверк", "салют", "хлопушки")),
    ("картина", ("картина", "холст", "полотно")),
    ("скрипка", ("скрипка", "скрипочка")),
    ("барабан", ("барабан", "литавры")),
    ("молния", ("молния", "гром", "гроза")),
    ("снегопад", ("снег", "метель", "снегопад")),
)

# (подсказка, слово, алиасы) — подсказка описывает слово, не раскрывая.
CLUES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Им этим пользуются, чтобы летать по воздуху.",
     "самолёт", ("самолет", "аэроплан")),
    ("Кухонный прибор для приготовления пищи на огне.",
     "плита", ("плита", "конфорка", "духовка")),
    ("Этим пишут текст на компьютере.",
     "клавиатура", ("клава", "клавиатура", "keyboard")),
    ("В него кладут деньги и карты, чтобы не потерять.",
     "кошелёк", ("кошелек", "бумажник", "портмоне")),
    ("Им поливают цветы в огороде.",
     "лейка", ("лейка", "лейка для цветов")),
    ("На нём катаются зимой с горки.",
     "санки", ("санки", "саночки")),
    ("Им открывают замок ключом.",
     "дверь", ("дверь", "дверца")),
    ("Её носят на голове, когда идёт дождь.",
     "шляпа", ("шляпа", "панама", "кепка")),
    ("В ней хранят продукты, чтобы они не испортились.",
     "холодильник", ("холодильник", "холодос")),
    ("Им рисуют картины на бумаге.",
     "кисть", ("кисть", "кисточка", "краски")),
    ("Через неё смотрят в зеркало.",
     "окно", ("окно", "окошко")),
    ("Им сверяют время на руке.",
     "часы", ("часы", "наручные часы", "будильник")),
    ("На ней пишут заметки в школе.",
     "тетрадь", ("тетрадь", "блокнот", "записная книжка")),
    ("Им едят суп и вторые блюда.",
     "ложка", ("ложка", "вилка", "приборы")),
    ("Её надевают на ноги, чтобы не мёрзнуть.",
     "носки", ("носки", "носки шерстяные", "колготки")),
    ("Им заряжают телефон.",
     "зарядка", ("зарядка", "провод", "кабель")),
    ("Им называют место, где живут рыбы.",
     "аквариум", ("аквариум", "аква")),
    ("Её используют, чтобы узнать, который час, без наручных часов.",
     "будильник", ("будильник", "часы")),
    ("Им открывают банку с консервами.",
     "банка", ("банка", "консервы", "консервная банка")),
    ("На ней готовят еду на кухне.",
     "плита", ("плита", "духовка")),
    ("Им моют руки и посуду.",
     "мыло", ("мыло", "гель для рук", "средство для мытья")),
    ("Им защищаются от дождя на улице.",
     "зонт", ("зонт", "зонтик")),
    ("Им носят вещи в путешествии.",
     "чемодан", ("чемодан", "багаж", "сумка")),
    ("Им играют в футбол на поле.",
     "мяч", ("мяч", "футбольный мяч", "мячик")),
    ("В ней живут книги в библиотеке.",
     "полка", ("полка", "стеллаж", "книжный шкаф")),
    ("Им фотографируют на телефон.",
     "камера", ("камера", "фотоаппарат", "объектив")),
    ("Им запускают фейерверк в праздник.",
     "хлопушка", ("хлопушка", "салют", "петарда", "фейерверк")),
    ("Им пишут на асфальте мелом.",
     "мел", ("мел", "мелок")),
    ("На ней катаются по льду.",
     "коньки", ("коньки", "ролики", "лыжи")),
    ("Её ставят под ёлкой в Новый год.",
     "подарок", ("подарок", "сюрприз", "дар")),
)


def normalize_answer(text: str) -> str:
    """Нормализация ответа игрока: lower, ё→е, только буквы/цифры."""
    t = (text or "").strip().lower().replace("ё", "е")
    return "".join(ch for ch in t if ch.isalnum() or ch == " ").strip()


def answer_matches(text: str, accepted: tuple[str, ...] | list[str]
                   ) -> bool:
    """Ответ засчитан, если нормализованная форма совпадает с
    любым принятым вариантом (включая алиасы)."""
    norm = normalize_answer(text)
    if not norm:
        return False
    for a in accepted:
        if norm == normalize_answer(a):
            return True
    return False


def word_answer_matches(text: str, word: str) -> bool:
    """Строгая проверка «Перепутанного слова»: принимается
    ТОЛЬКО исходное слово. Толерантны лишь регистр, Ё/Е и
    внешние пробелы — произвольно вставленные символы,
    знаки препинания и внутренние пробелы НЕ удаляются и
    дают несовпадение."""
    def _strict(s: str) -> str:
        return unicodedata.normalize(
            "NFC", s or "").strip().lower().replace("ё", "е")

    t, w = _strict(text), _strict(word)
    return bool(t) and t == w


def make_config(game: str, prize: dict[str, Any],
                settings: dict[str, Any],
                rng: random.Random,
                content: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    """Сгенерировать скрытую конфигурацию игры. Вызывается ДО
    публикации карточки — секреты здесь, публично не видны.

    ``content`` — запись каталога (Events Content Expansion)
    для word/krokodil; без неё — legacy-встроенные списки
    (совместимость со старыми вызовами/тестами)."""
    cfg: dict[str, Any] = {}
    if game == "chests":
        n = int(GAME_DEFS[game].get("chests", 5))
        cfg["win_chest"] = rng.randrange(n)
    elif game == "quick_math":
        cfg.update(_math_problem(rng))
        cfg["accepted"] = [str(cfg["answer"])]
    elif game == "word":
        if content:
            word = str(content["word"])
            # Строгий ответ: ТОЛЬКО исходное слово — синонимы,
            # сокращения и алиасы не принимаются (поле accepted
            # word-каталога валидатором запрещено).
            accepted = [word]
            from services.event_content import scramble_word
            cfg["scrambled"] = scramble_word(word, rng)
        else:
            word, aliases = rng.choice(WORDS)
            accepted = [word, *aliases]
            cfg["scrambled"] = _scramble(word.replace(" ", ""), rng)
        cfg["word"] = word
        cfg["accepted"] = accepted
        if content and content.get("id"):
            cfg["content_id"] = str(content["id"])
            cfg["content_difficulty"] = str(
                content.get("difficulty") or "")
    elif game == "guess_number":
        cfg["number"] = rng.randint(1, 20)
        cfg["accepted"] = [str(cfg["number"])]
    elif game == "krokodil":
        if content:
            word = str(content["answer"])
            aliases = [str(a) for a in content.get("accepted") or []]
            accepted = list(dict.fromkeys([word, *aliases]))
            cfg["clue"] = str(content["clue"])
        else:
            clue, word, aliases = rng.choice(CLUES)
            accepted = [word, *aliases]
            cfg["clue"] = clue
        cfg["word"] = word
        cfg["accepted"] = accepted
        if content and content.get("id"):
            cfg["content_id"] = str(content["id"])
            cfg["content_difficulty"] = str(
                content.get("difficulty") or "")
    elif game == "growing":
        dur = float(GAME_DEFS[game]["duration"])
        bmin = float(settings.get("growing_burn_min_seconds", 15))
        bmax = float(settings.get("growing_burn_max_seconds", 40))
        bmin, bmax = min(bmin, bmax), max(bmin, bmax)
        burn = rng.uniform(min(bmin, dur - 1), min(bmax, dur - 1))
        cfg["burn_after"] = burn  # скрытый дедлайн сгорания, сек
    elif game == "last_click":
        # Скрытый дедлайн 15–45 c: public deadline = этот же момент,
        # но карточка не показывает точное время.
        cfg["duration"] = rng.uniform(15.0, 45.0)
    return cfg


def public_deadline(game: str, cfg: dict[str, Any],
                    start_ts: float) -> float:
    """Публичный дедлайн (unix). Для last_click — скрытый,
    но он же является реальным дедлайном события."""
    if game == "last_click":
        return start_ts + float(cfg.get("duration", 30.0))
    return start_ts + float(GAME_DEFS[game]["duration"])


def close_deadline(game: str, cfg: dict[str, Any],
                   start_ts: float, deadline_ts: float) -> float | None:
    """Раннее закрытие: growing сгорает на скрытом burn_after.
    Возвращает unix-дедлайн закрытия или None (= deadline)."""
    if game == "growing":
        return start_ts + float(cfg.get("burn_after", 0.0))
    return None


def growing_value(prize: dict[str, Any], elapsed: float,
                  duration: float) -> int:
    """Текущее видимое значение «Растущего куша»: линейный рост
    от prize['start'] к prize['amount'] к концу публичного
    дедлайна. Целое, ограниченное [start, amount]."""
    start = int(prize.get("start") or prize.get("amount") or 0)
    amount = int(prize.get("amount") or start)
    if duration <= 0:
        return amount
    frac = max(0.0, min(1.0, elapsed / duration))
    return int(round(start + (amount - start) * frac))


# ------------------------------------------------------------------
# Итоги игр (чистые функции — покрыты тестами)
# ------------------------------------------------------------------

def _ints(d: dict[str, Any], prefix: str) -> list[int]:
    out = []
    for k, v in d.items():
        if k.startswith(prefix):
            try:
                out.append(int(str(k)[len(prefix):]))
            except ValueError:
                continue
    return out



def _payout(prize: dict[str, Any]) -> int | None:
    """Числовая выплата приза; для item — None (выдаётся предмет)."""
    if prize.get("type") == "item":
        return None
    return int(prize.get("amount") or 0)


def compute_outcome(game: str, cfg: dict[str, Any],
                    actions: dict[str, Any],
                    prize: dict[str, Any],
                    now: float) -> dict[str, Any]:
    """Посчитать итог события по actions.

    Возвращает {"winners": [uid], "reason": str, "payout": int|None,
    "detail": str}. payout — числовая награда (для item = None,
    тип берётся из prize). Никогда не выдумывает победителя.
    """
    none = {"winners": [], "reason": "no_winner", "payout": None,
            "detail": ""}

    if game == "speed":
        w = actions.get("winner")
        if w is None:
            return none
        return {"winners": [int(w)], "reason": "first_click",
                "payout": _payout(prize),
                "detail": "первый клик"}

    if game == "raffle":
        parts = _ints(actions, "p:")
        if not parts:
            return {**none, "reason": "no_participants",
                    "detail": "никто не участвовал"}
        return {"winners": [random.choice(parts)],
                "reason": "raffle", "payout": _payout(prize),
                "detail": f"участников: {len(parts)}"}

    if game == "chests":
        win = int(cfg.get("win_chest", 0))
        correct = [int(str(k)[7:]) for k, v in actions.items()
                   if k.startswith("choice:") and int(v) == win]
        if not correct:
            return {**none, "reason": "no_correct",
                    "detail": "никто не нашёл сундук"}
        w = random.choice(correct)
        return {"winners": [w], "reason": "chest",
                "payout": _payout(prize),
                "detail": f"сундук {win + 1}, верных: {len(correct)}"}

    if game == "quick_math":
        w = actions.get("winner")
        if w is None:
            return none
        return {"winners": [int(w)], "reason": "correct_answer",
                "payout": _payout(prize),
                "detail": f"{cfg.get('a')} {cfg.get('op')} {cfg.get('b')}"}

    if game == "minority":
        red = [int(str(k)[7:]) for k, v in actions.items()
               if k.startswith("choice:") and v == "red"]
        blue = [int(str(k)[7:]) for k, v in actions.items()
                if k.startswith("choice:") and v == "blue"]
        if not red or not blue:
            return {**none, "reason": "empty_side",
                    "detail": f"red={len(red)} blue={len(blue)}"}
        if len(red) == len(blue):
            return {**none, "reason": "tie",
                    "detail": f"ничья {len(red)}:{len(blue)}"}
        side = red if len(red) < len(blue) else blue
        w = random.choice(side)
        return {"winners": [w], "reason": "minority",
                "payout": _payout(prize),
                "detail": (f"{'красных' if side is red else 'синих'} "
                           f"{min(len(red), len(blue))} "
                           f"против {max(len(red), len(blue))}")}

    if game == "dice":
        rolls = {int(str(k)[5:]): int(v) for k, v in actions.items()
                 if k.startswith("roll:")}
        if not rolls:
            return none
        best = max(rolls.values())
        leaders = [u for u, v in rolls.items() if v == best]
        w = random.choice(leaders)
        return {"winners": [w], "reason": "dice",
                "payout": _payout(prize),
                "detail": f"макс. бросок {best}, лидеров: {len(leaders)}"}

    if game == "growing":
        # Глобальный атомарный ключ ``gclaim`` = {"uid","elapsed"}:
        # первый валидный клейм фиксируется must_absent-гардом
        # на уровне PG — два одновременных клейма разных игроков
        # не могут оба пройти. ``claim:<uid>`` — legacy-формат
        # для событий, начатых до введения gclaim.
        gc = actions.get("gclaim")
        if isinstance(gc, dict) and gc.get("uid") is not None:
            uid = int(gc["uid"])
            elapsed = float(gc.get("elapsed") or 0.0)
        else:
            claims = {int(str(k)[6:]): float(v)
                      for k, v in actions.items()
                      if k.startswith("claim:")}
            if not claims:
                return {**none, "reason": "burned",
                        "detail": "куш сгорел — никто не успел"}
            uid = min(claims, key=claims.get)
            elapsed = claims[uid]
        burn = float(cfg.get("burn_after", 0.0))
        # Валидность клейма проверена при записи (now < start+burn
        # и до close_at по PG-гарду). Пересчитываем сумму на момент
        # клейма: prize.start→amount за duration.
        dur = float(GAME_DEFS["growing"]["duration"])
        payout = growing_value(prize, elapsed, dur)
        return {"winners": [uid], "reason": "claimed",
                "payout": payout,
                "detail": f"клейм на {payout}"}

    if game == "last_click":
        clicks = {int(str(k)[6:]): float(v) for k, v in actions.items()
                  if k.startswith("click:")}
        if not clicks:
            return none
        uid = max(clicks, key=clicks.get)
        return {"winners": [uid], "reason": "last_click",
                "payout": _payout(prize),
                "detail": "последний клик перед дедлайном"}

    if game in ("word", "guess_number", "krokodil"):
        w = actions.get("winner")
        if w is None:
            return none
        return {"winners": [int(w)], "reason": "correct_answer",
                "payout": _payout(prize), "detail": ""}

    return none


def scramble_display(game: str, cfg: dict[str, Any]) -> str:
    """Публичная строка загадки для карточки."""
    if game == "quick_math":
        return f"**{cfg.get('a')} {cfg.get('op')} {cfg.get('b')} = ?**"
    if game == "word":
        return f"**{cfg.get('scrambled')}**"
    if game == "guess_number":
        return "**Угадайте число от 1 до 20**"
    if game == "krokodil":
        return f"**{cfg.get('clue')}**"
    return ""
