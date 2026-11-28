"""BLACKJACK 2.0 — PNG-рендер стола (Pillow, локальные ассеты).

Визуал r!z3 CLASSIC: монохром + тёмный приглушённый зелёный стол,
белые карты, красные ♥♦, чёрные ♣♠, рубашка с «r!z3».
Без внешних URL. При любой ошибке возвращает None — вызывающий код
обязан иметь текстовый fallback (вью всегда дублирует карты текстом).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from games.blackjack_engine import RED_SUITS, Card, hand_total, is_natural

logger = logging.getLogger(__name__)

FELT = (27, 67, 50)          # тёмный приглушённый зелёный
FELT_DARK = (20, 50, 38)
LINE = (232, 230, 225)       # монохром r!z3 — off-white
MUTED = (154, 165, 177)
CARD_BG = (247, 245, 240)
CARD_EDGE = (210, 205, 196)
INK = (26, 26, 26)
RED = (179, 38, 30)
BACK_BG = (35, 39, 46)
BACK_EDGE = (58, 64, 74)

CARD_W, CARD_H = 74, 102
CARD_GAP = 10
ROW_GAP = 26
PAD_X = 36
TOP = 92
BOTTOM = 40
MAX_CARDS_FIT = 11

_FONTS_DIR = Path(__file__).resolve().parents[1] / "assets_ds_server" / "fonts"


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (
        ["RussoOne-Regular.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
        if bold
        else ["DejaVuSans.ttf", "Inter-Variable.ttf"]
    )
    for name in names:
        path = _FONTS_DIR / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


_F_TITLE = _font(30, bold=True)
_F_LABEL = _font(20, bold=True)
_F_RANK = _font(24, bold=True)
_F_SUIT = _font(26)
_F_SMALL = _font(16)
_F_TOTAL = _font(22, bold=True)


def _card_size(count: int) -> tuple[int, int, int]:
    """(width, height, gap) карточки с учётом числа в ряду."""
    if count <= MAX_CARDS_FIT:
        return CARD_W, CARD_H, CARD_GAP
    # сжатие с перекрытием при >11 картах
    w = max(40, int(CARD_W * MAX_CARDS_FIT / count))
    return w, int(CARD_H * w / CARD_W), 6


def _draw_card(draw, x: int, y: int, w: int, h: int, card: Card | None) -> None:
    """card=None → рубашка."""
    if card is None:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=8,
                               fill=BACK_BG, outline=BACK_EDGE, width=2)
        # «r!z3» на рубашке
        text = "r!z3"
        bbox = draw.textbbox((0, 0), text, font=_F_TOTAL)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x + (w - tw) / 2, y + (h - th) / 2 - th / 4),
                  text, font=_F_TOTAL, fill=LINE)
        # диагональный паттерн-полоса
        for i in range(-h, w + h, 18):
            draw.line([(x + i, y + h), (x + i + h, y)],
                      fill=(45, 50, 60), width=1)
        return
    rank, suit = card
    color = RED if suit in RED_SUITS else INK
    draw.rounded_rectangle([x, y, x + w, y + h], radius=8,
                           fill=CARD_BG, outline=CARD_EDGE, width=2)
    # ранг + масть слева сверху
    draw.text((x + 7, y + 6), rank, font=_F_RANK, fill=color)
    draw.text((x + 7, y + 32), suit, font=_F_SUIT, fill=color)
    # масть крупно по центру
    bbox = draw.textbbox((0, 0), suit, font=_F_SUIT)
    sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x + (w - sw) / 2, y + (h - sh) / 2 + 6), suit,
              font=_F_SUIT, fill=color)
    # индекс в правом нижнем углу
    draw.text((x + w - 26, y + h - 34), rank, font=_F_SMALL, fill=color)
    draw.text((x + w - 22, y + h - 12), suit, font=_F_SUIT, fill=color)


def _hand_extent(cards: list[Card | None]) -> int:
    if not cards:
        return 0
    w, _, gap = _card_size(len(cards))
    return len(cards) * w + (len(cards) - 1) * gap


def render_round(
    player: list[Card],
    dealer: list[Card],
    *,
    reveal_dealer: bool,
    stake: int = 0,
    outcome: str | None = None,
) -> bytes | None:
    """PNG-байты стола. None при ошибке рендера (текстовый fallback)."""
    try:
        return _render(player, dealer, reveal_dealer=reveal_dealer,
                       stake=stake, outcome=outcome)
    except Exception:
        logger.warning("blackjack render failed", exc_info=True)
        return None


def _render(player, dealer, *, reveal_dealer, stake, outcome) -> bytes:
    from PIL import Image, ImageDraw

    # Отображаемые карты дилера: первая открыта, остальные — рубашки
    dealer_cards: list[Card | None] = list(dealer[:1]) + [None] * (len(dealer) - 1)
    if reveal_dealer:
        dealer_cards = list(dealer)

    max_count = max(len(player), len(dealer_cards), 2)
    cw, ch, gap = _card_size(max_count)

    p_w = _hand_extent([None] * len(player)) if player else 0
    d_w = _hand_extent(dealer_cards) if dealer_cards else 0
    width = max(PAD_X * 2 + p_w, PAD_X * 2 + d_w, 560)
    height = TOP + ch + ROW_GAP + 30 + ch + BOTTOM
    if stake or outcome:
        height += 44

    img = Image.new("RGB", (width, height), FELT)
    draw = ImageDraw.Draw(img)

    # виньетка-виньетка: тёмные углы через градиент-полосы
    for i in range(26):
        a = int(60 * (1 - i / 26))
        draw.rectangle([0, height - 1 - i * 2, width, height - i * 2],
                       fill=(max(0, FELT[0] - a // 3),
                             max(0, FELT[1] - a // 3),
                             max(0, FELT[2] - a // 3)))

    # заголовок
    draw.text((width / 2, 18), "BLACKJACK", font=_F_TITLE,
              fill=LINE, anchor="ma")
    draw.text((width / 2, 56), "r!z3 CLASSIC", font=_F_SMALL,
              fill=MUTED, anchor="ma")

    def row(y: int, label: str, cards: list[Card | None], total: int | None,
            natural: bool) -> None:
        row_w = _hand_extent(cards) if cards else 0
        x0 = (width - row_w) / 2 if row_w else PAD_X
        # лейбл слева от карт
        draw.text((PAD_X, y + ch / 2 - 12), label, font=_F_LABEL,
                  fill=LINE)
        total_text = "?" if total is None else str(total)
        if natural and total is not None:
            total_text += "  BJ"
        tw = draw.textlength(total_text, font=_F_TOTAL)
        draw.text((width - PAD_X - tw, y + ch / 2 - 14), total_text,
                  font=_F_TOTAL, fill=(240, 190, 90) if natural else LINE)
        x = x0
        for c in cards:
            _draw_card(draw, int(x), y, cw, ch, c)
            x += cw + gap

    dealer_total = hand_total(dealer) if reveal_dealer else None
    p_total = hand_total(player) if player else None
    row(TOP, "ДИЛЕР", dealer_cards, dealer_total,
        reveal_dealer and is_natural(dealer))
    row(TOP + ch + ROW_GAP + 30, "ИГРОК", list(player), p_total,
        is_natural(player))

    if stake or outcome:
        y = TOP + ch + ROW_GAP + 30 + ch + 14
        parts = []
        if stake:
            parts.append(f"СТАВКА {stake:,}")
        if outcome:
            parts.append(str(outcome).upper())
        draw.text((width / 2, y), "  ·  ".join(parts), font=_F_SMALL,
                  fill=MUTED, anchor="ma")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
