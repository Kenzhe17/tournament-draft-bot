"""API спорных раздач BLACKJACK 2.0 (Control Center).

Диагностика и разрешение delivery_state='disputed' — закрытый
админ-раздел: просмотр по audit.view, все изменяющие действия
(recheck/resolve/resync/compensate) — только economy.finance
(по умолчанию только owner).

Панель работает в ОТДЕЛЬНОМ процессе от Discord-бота: «восстановить
отображение» не редактирует сообщение напрямую, а сбрасывает
render-счётчики — бот подхватывает карточку ближайшим проходом
sweeper (list_needing_sync).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import (
    PanelUser,
    require_perm,
    verify_csrf,
)
from storage.blackjack_store import (
    RoundNotFound,
    blackjack_store,
)

router = APIRouter(prefix="/api/blackjack", tags=["blackjack"])

_MAX_COMPENSATION = 10**9


class CompensateBody(BaseModel):
    amount: int = Field(gt=0, le=_MAX_COMPENSATION)
    reason: str = Field(min_length=3, max_length=500)
    # Клиентский ключ идемпотентности: повторный запрос с тем же
    # op_id вернёт duplicate без повторного начисления.
    op_id: str = Field(min_length=8, max_length=64)


class ResolveBody(BaseModel):
    note: str = Field(min_length=1, max_length=500)


def _ledger_summary(ops: list[dict]) -> dict[str, int]:
    """Суммы по типам ledger-операций раздачи."""
    out = {"stake": 0, "payout": 0, "refund": 0, "admin_comp": 0}
    for op in ops:
        kind = op["op"].split(":", 1)[0]
        if kind in out:
            out[kind] += int(op["amount"])
    return out


def _serialize(stored, ledger: list[dict]) -> dict[str, Any]:
    """Безопасное представление: скрытые карты НЕ раскрываются —
    hands/dealer не включаются в ответ вообще."""
    sums = _ledger_summary(ledger)
    paid = sums["payout"] or sums["refund"] or sums["admin_comp"]
    return {
        "round_id": stored.round_id,
        "guild_id": stored.guild_id,
        "user_id": stored.user_id,
        "channel_id": stored.channel_id,
        "message_id": stored.message_id,
        "stake": stored.stake,
        "status": stored.status,
        "phase": stored.game.phase,
        "outcome": stored.game.outcome,
        "reason": stored.reason,
        "delivery_state": stored.delivery_state,
        "delivery_attempt": stored.delivery_attempt,
        "reconcile_attempts": stored.reconcile_attempts,
        "last_reconcile_at": (
            stored.last_reconcile_at.isoformat()
            if stored.last_reconcile_at else None),
        "version": stored.version,
        "card_closed": stored.card_closed,
        "created_at": (
            stored.created_at.isoformat() if stored.created_at else None),
        "settled_at": (
            stored.settled_at.isoformat() if stored.settled_at else None),
        "ledger": ledger,
        "ledger_summary": sums,
        # чистое изменение баланса игрока по раздаче (до компенсаций)
        "net_coins": paid - stored.stake,
        "dispute_resolved_at": (
            stored.dispute_resolved_at.isoformat()
            if stored.dispute_resolved_at else None),
        "dispute_resolved_by": stored.dispute_resolved_by,
        "dispute_note": stored.dispute_note,
    }


async def _disputed_or_404(round_id: str):
    stored = await blackjack_store.get_round(round_id)
    if stored is None or stored.delivery_state != "disputed":
        raise HTTPException(404, "disputed round not found")
    return stored


@router.get("/disputed")
async def list_disputed(
    user: PanelUser = Depends(require_perm(rbac.PERM_AUDIT_VIEW)),
) -> dict[str, Any]:
    rounds = await blackjack_store.list_disputed()
    out = []
    for stored in rounds:
        ledger = await blackjack_store.get_dispute_ledger(
            stored.round_id)
        out.append(_serialize(stored, ledger))
    return {"disputed": out, "count": len(out)}


@router.get("/disputed/{round_id}")
async def get_disputed(
    round_id: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_AUDIT_VIEW)),
) -> dict[str, Any]:
    stored = await _disputed_or_404(round_id)
    ledger = await blackjack_store.get_dispute_ledger(round_id)
    return _serialize(stored, ledger)


@router.post("/disputed/{round_id}/recheck")
async def recheck_disputed(
    round_id: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_ECONOMY_FINANCE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Повторная проверка доставки: сброс в 'unknown' + attempts=0 —
    sweeper повторит поиск маркера в истории канала."""
    await _disputed_or_404(round_id)
    ok = await blackjack_store.recheck_delivery(round_id)
    if not ok:
        raise HTTPException(409, "round is not in disputed state")
    return {"status": "requeued", "round_id": round_id}


@router.post("/disputed/{round_id}/resync")
async def resync_disputed(
    round_id: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_ECONOMY_FINANCE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Восстановить отображение результата: render-счётчики → -1,
    бот перерисует карточку ближайшим проходом sweeper."""
    stored = await _disputed_or_404(round_id)
    ok = await blackjack_store.request_card_resync(round_id)
    if not ok:
        raise HTTPException(
            409, "no delivered card to resync (message_id missing)")
    return {"status": "resync_requested", "round_id": round_id,
            "message_id": stored.message_id}


@router.post("/disputed/{round_id}/resolve")
async def resolve_disputed(
    round_id: str,
    body: ResolveBody,
    user: PanelUser = Depends(require_perm(rbac.PERM_ECONOMY_FINANCE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Закрыть спор без финансовых изменений (итог раздачи не трогаем)."""
    await _disputed_or_404(round_id)
    ok = await blackjack_store.resolve_dispute(
        round_id, user.id, body.note)
    if not ok:
        raise HTTPException(409, "round is not in disputed state")
    return {"status": "resolved", "round_id": round_id}


@router.post("/disputed/{round_id}/compensate")
async def compensate_disputed(
    round_id: str,
    body: CompensateBody,
    user: PanelUser = Depends(require_perm(rbac.PERM_ECONOMY_FINANCE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Обоснованная компенсация: идемпотентная ledger-операция
    admin_comp:<op_id>. Актёр, время, причина и сумма фиксируются
    в blackjack_ledger + dispute_note. Повтор с тем же op_id —
    duplicate без повторного начисления. Карты/исход/выплата
    раздачи не изменяются."""
    stored = await _disputed_or_404(round_id)
    # op_id привязываем к round_id — один клиентский ключ не может
    # компенсировать разные раздачи.
    op_id = f"{stored.round_id}:{body.op_id}"
    try:
        result = await blackjack_store.compensate_dispute(
            round_id, user.id, op_id, body.amount, body.reason)
    except RoundNotFound:
        raise HTTPException(404, "round not found") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    status = result.get("status")
    if status == "not_compensable":
        raise HTTPException(
            409, "dispute already resolved or compensated")
    if status == "cap_exceeded":
        raise HTTPException(
            422, "compensation would exceed safe balance cap")
    return {"round_id": round_id, **result}
