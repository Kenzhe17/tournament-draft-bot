"""Восстановление OCR-сессий, застрявших в статусе ``finalizing``.

Сессия переходит в ``finalizing`` до начисления ELO. Если процесс
упал (перезапуск Railway, сбой Discord API, таймаут PostgreSQL)
между фиксацией статуса и ``completed``, при следующем запуске
бот безопасно доводит финализацию до конца:

- ``match_finalizations`` в PostgreSQL — authoritative-статус:
  ``stats_done``/``done`` пропускают повторное начисление ELO,
  ``processing``/отсутствие — финализация выполняется целиком,
  но ровно один раз (PK match_id + processed_elo_matches).
- Повторная публикация итоговой карточки защищена
  ``result_message_id`` и ``_RESULT_PUBLISH_LOCKS``.
- Если победитель не разрешим (админ-комплект без явного выбора) —
  сессия пропускается, остаётся для ручного подтверждения.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class _NullResponse:
    def is_done(self) -> bool:
        return True

    async def defer(self, **kwargs) -> None:
        return None

    async def send_message(self, *args, **kwargs):
        return None


class _NullFollowup:
    async def send(self, *args, **kwargs):
        return None


class _RecoveryInteraction:
    """Минимальный stub discord.Interaction для фоновой финализации.

    finalize_ocr_match использует interaction только для передачи
    в process_match_result (уровень-ап уведомления по interaction.guild)
    и опциональных followup — все каналы здесь no-op.
    """

    def __init__(self, guild, user) -> None:
        self.guild = guild
        self.user = user
        self.channel = None
        self.response = _NullResponse()
        self.followup = _NullFollowup()


async def recover_stuck_finalizing(bot, tournament) -> int:
    """Довести до конца сессии ``finalizing`` турнира.

    Возвращает число восстановленных сессий. Безопасно вызывать
    многократно и при отсутствии зависших сессий.
    """
    from views.ocr_match_view import (
        _match_tuple,
        _ocr_winning_team_index,
        _rooms_confirmed_winner,
        _session_mm_winner,
        finalize_ocr_match,
    )

    recovered = 0
    sessions = dict(getattr(tournament, "ocr_sessions", None) or {})
    for key, session in sessions.items():
        if not isinstance(session, dict):
            continue
        if session.get("status") != "finalizing":
            continue
        match_type = session.get("match_type")
        match_index = session.get("match_index")
        if match_type is None or match_index is None:
            # fallback: ключ сессии "<match_type>_<match_index>"
            try:
                match_type, idx_s = str(key).rsplit("_", 1)
                match_index = int(idx_s)
            except (ValueError, AttributeError):
                logger.warning(
                    "finalizing session %s: cannot resolve match ids",
                    key,
                )
                continue
        try:
            guild = bot.get_guild(tournament.guild_id)
            if guild is None:
                guild = await bot.fetch_guild(tournament.guild_id)
        except Exception as e:
            logger.warning(
                "finalizing session %s: guild %s unavailable: %s",
                key, tournament.guild_id, e,
            )
            continue
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception as e:
            logger.warning(
                "finalizing session %s: match tuple failed: %s",
                key, e,
            )
            continue

        mode = session.get("mode")
        is_mm = bool(getattr(tournament, "mm_search_channel_id", None))
        if mode == "rooms":
            winner = _rooms_confirmed_winner(tournament, session, t1, t2)
        elif is_mm:
            winner = _session_mm_winner(tournament, session, t1, t2)
        else:
            winner = _ocr_winning_team_index(session)
            if winner is None:
                wti = session.get("winning_team_index")
                try:
                    winner = int(wti) if wti is not None else None
                except (TypeError, ValueError):
                    winner = None
        if winner not in (t1, t2):
            # Победитель не разрешим (админ-комплект без явного выбора
            # или повреждённые данные) — не гадаем: оставляем сессию
            # для ручного подтверждения/отмены администратором.
            logger.warning(
                "finalizing session %s: winner unresolved, skipped",
                key,
            )
            continue

        try:
            await finalize_ocr_match(
                _RecoveryInteraction(guild, getattr(bot, "user", None)),
                tournament, match_type, match_index, winner,
            )
            recovered += 1
            logger.info(
                "Recovered finalizing OCR session %s (match %s_%s)",
                key, match_type, match_index,
            )
        except Exception:
            logger.exception(
                "Recovery finalize failed for session %s", key
            )
    return recovered
