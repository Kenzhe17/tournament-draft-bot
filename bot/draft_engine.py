"""Логика драфта: порядок выборов и распределение игроков."""

from __future__ import annotations

import random
from typing import Sequence

from bot.models import DraftState, Tournament

# Порядок ручных выборов по кругам (индексы позиций капитанов 0..3)
PICK_ORDERS: dict[int, tuple[int, ...]] = {
    2: (0, 1, 2),
    3: (3, 2, 1),
    4: (0, 1, 2),
}

AUTO_CAPTAIN_INDEX: dict[int, int] = {
    2: 3,
    3: 0,
    4: 3,
}


def shuffle_captains(captain_ids: Sequence[int]) -> list[int]:
    order = list(captain_ids)
    random.shuffle(order)
    return order


def init_draft(captain_ids: Sequence[int]) -> DraftState:
    order = shuffle_captains(captain_ids)
    picks: dict[int, dict[int, str]] = {cid: {} for cid in order}
    return DraftState(
        captain_order=order,
        current_circle=2,
        pick_index=0,
        picks=picks,
        teams={},
        auto_message=None,
    )


def get_available_players(tournament: Tournament, circle: int) -> list[str]:
    circle_key = str(circle)
    all_in_circle = list(tournament.circles.get(circle_key, []))
    picked: set[str] = set()
    if tournament.draft and tournament.draft.picks:
        for captain_picks in tournament.draft.picks.values():
            if circle in captain_picks:
                picked.add(captain_picks[circle])
    return [p for p in all_in_circle if p not in picked]


def get_pick_order(circle: int) -> tuple[int, ...]:
    return PICK_ORDERS[circle]


def get_auto_captain_index(circle: int) -> int:
    return AUTO_CAPTAIN_INDEX[circle]


def get_current_picker(draft: DraftState) -> int | None:
    circle = draft.current_circle
    order = get_pick_order(circle)
    if draft.pick_index >= len(order):
        return None
    captain_index = order[draft.pick_index]
    return draft.captain_order[captain_index]


def apply_pick(draft: DraftState, captain_id: int, player: str) -> None:
    draft.picks.setdefault(captain_id, {})[draft.current_circle] = player


def finish_circle(draft: DraftState, tournament: Tournament, auto_captain_name: str) -> str | None:
    """
    Авто-назначение последнего игрока кругa и переход к следующему.
    Возвращает сообщение об авто-назначении или None.
    """
    circle = draft.current_circle
    remaining = get_available_players(tournament, circle)
    auto_msg: str | None = None

    if remaining:
        auto_idx = get_auto_captain_index(circle)
        auto_captain = draft.captain_order[auto_idx]
        apply_pick(draft, auto_captain, remaining[0])
        auto_msg = f"✅ {auto_captain_name} автоматически получает {remaining[0]}"
        draft.auto_message = auto_msg

    draft.pick_index = 0

    if circle >= 4:
        _build_teams(draft)
        return auto_msg

    draft.current_circle += 1
    return auto_msg


def step_after_manual_pick(
    draft: DraftState,
    tournament: Tournament,
    auto_captain_name: str,
) -> bool:
    """
    Продвижение после ручного выбора.
    Возвращает True, если драфт завершён.
    """
    draft.pick_index += 1
    circle = draft.current_circle
    order = get_pick_order(circle)

    if draft.pick_index >= len(order):
        finish_circle(draft, tournament, auto_captain_name)
        return draft.current_circle > 4 or bool(draft.teams)

    return False


def _build_teams(draft: DraftState) -> None:
    teams: dict[int, list[str]] = {}
    for idx, captain_id in enumerate(draft.captain_order, start=1):
        picks = draft.picks.get(captain_id, {})
        teams[idx] = [
            str(captain_id),
            picks.get(2, "?"),
            picks.get(3, "?"),
            picks.get(4, "?"),
        ]
    draft.teams = teams


def generate_semifinal_pairs() -> list[tuple[int, int]]:
    patterns = [
        [(1, 2), (3, 4)],
        [(1, 3), (2, 4)],
        [(1, 4), (2, 3)],
    ]
    return random.choice(patterns)
