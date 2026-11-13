"""Динамический таймер и умные уведомления Ranked MM.

Покрывает: лимиты очереди, max-эффективный лимит, условия
уведомлений (1-я минута; 2-я — >=5мин И >=5 игроков), claim-
атомарность, suppress-cooldown после requeue, expiry сессии.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.mm_search import (
    QUEUE_OPEN,
    MMFormat,
    MMSearch,
    queue_limit_seconds,
)


def _search(n: int = 0) -> MMSearch:
    s = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    for i in range(n):
        s.add_player(f"P{i}", 1000 + i)
    return s


class _Store:
    def __init__(self, s):
        self.s = s

    def get(self, guild_id, channel_id):
        return self.s

    def set(self, s):
        pass

    def get_by_guild(self, guild_id):
        return [self.s] if self.s else []

    def all(self):
        return [self.s] if self.s else []


def _cog(monkeypatch, search):
    import cogs.mm as mm_mod

    monkeypatch.setattr(mm_mod, "mm_search_store", _Store(search))
    bot = SimpleNamespace(mm_cog=None, get_guild=lambda gid: None)
    return mm_mod.MMCog(bot)


# ------------------------------------------------------------ лимиты

class TestQueueLimits:
    @pytest.mark.parametrize("count,expected", [
        (1, 600), (3, 600),
        (4, 720), (5, 720),
        (6, 900),
        (7, 1200),
    ])
    def test_limit_table(self, count, expected):
        assert queue_limit_seconds(count, 8) == expected

    def test_duo_keeps_600(self):
        assert queue_limit_seconds(3, 4) == 600

    def test_effective_limit_never_decreases(self):
        s = _search(7)
        s.start_new_session(1000.0)
        assert s.effective_limit_seconds == 1200
        # Два игрока вышли — лимит остаётся 20 минут
        s.players = s.players[:-2]
        s.update_effective_limit()
        assert s.effective_limit_seconds == 1200
        assert s.deadline_at == 1000.0 + 1200

    def test_limit_grows_with_fill(self):
        s = _search(3)
        s.start_new_session(0.0)
        assert s.effective_limit_seconds == 600
        s.players.append("P3")
        s.players.append("P4")  # -> 5 игроков? нет, players уже 3+2
        s.update_effective_limit()
        # 5 игроков → 720
        assert s.effective_limit_seconds == 720

    def test_start_new_session_resets_notifications(self):
        s = _search(2)
        s.start_new_session(0.0)
        s.first_notification_status = "sent"
        s.second_notification_status = "sent"
        s.notify_suppressed_until = 9999.0
        s.start_new_session(500.0)
        assert s.session_started_at == 500.0
        assert s.first_notification_status == "pending"
        assert s.second_notification_status == "pending"
        assert s.notify_suppressed_until == 0.0


# ------------------------------------------------------- уведомления

class TestNotificationConditions:
    def _due(self, monkeypatch, n, elapsed, kind, locked=False,
             first_sent_ago=None):
        s = _search(n)
        s.timer_started = True
        s.state = "locked" if locked else QUEUE_OPEN
        now = time.time()
        s.session_started_at = now - elapsed
        s.effective_limit_seconds = 1200
        if first_sent_ago is not None:
            s.first_notification_status = "sent"
            s.first_notification_sent_at = now - first_sent_ago
        cog = _cog(monkeypatch, s)
        return cog._notification_due(s, kind, now)

    def test_first_needs_120s_and_2_players(self, monkeypatch):
        # 2+ минуты и 2+ игрока → да
        assert self._due(monkeypatch, 2, 121, "first")
        assert self._due(monkeypatch, 3, 121, "first")
        # 119с — ещё нет
        assert not self._due(monkeypatch, 3, 119, "first")
        # Один игрок на второй минуте — уведомления нет, ждём второго
        assert not self._due(monkeypatch, 1, 300, "first")
        # Пустая/полная очередь — нет
        assert not self._due(monkeypatch, 0, 300, "first")
        assert not self._due(monkeypatch, 8, 300, "first")

    def test_second_needs_7min_5players_and_first_sent(self, monkeypatch):
        # Без подтверждённого первого — нет при любых условиях
        assert not self._due(monkeypatch, 6, 500, "second")
        # 5 игроков, 7+ мин, первое отправлено >120с назад → да
        assert self._due(
            monkeypatch, 5, 421, "second", first_sent_ago=121
        )
        assert self._due(
            monkeypatch, 7, 600, "second", first_sent_ago=300
        )
        # 4 игрока → нет (05:00 — 4/8 не триггерит)
        assert not self._due(
            monkeypatch, 4, 500, "second", first_sent_ago=300
        )
        # <7 минут от старта → нет, даже если первое давно отправлено
        assert not self._due(
            monkeypatch, 6, 419, "second", first_sent_ago=300
        )
        # Минимум 120с между ФАКТИЧЕСКИМИ отправками:
        # первое ушло 60с назад → второго нет
        assert not self._due(
            monkeypatch, 6, 500, "second", first_sent_ago=60
        )
        # locked → нет
        assert not self._due(
            monkeypatch, 6, 600, "second", locked=True,
            first_sent_ago=300,
        )

    def test_claim_atomic_single_sender(self, monkeypatch):
        s = _search(5)
        s.timer_started = True
        s.session_started_at = time.time() - 400
        s.effective_limit_seconds = 1200
        cog = _cog(monkeypatch, s)
        assert cog._claim_notification(s, "second") is True
        # Повторный claim — уже sending
        assert cog._claim_notification(s, "second") is False
        assert s.second_notification_status == "sending"

    def test_claim_respects_suppress_cooldown(self, monkeypatch):
        s = _search(5)
        s.timer_started = True
        s.session_started_at = time.time() - 400
        s.effective_limit_seconds = 1200
        s.notify_suppressed_until = time.time() + 1800
        cog = _cog(monkeypatch, s)
        assert cog._claim_notification(s, "second") is False
        assert s.second_notification_status == "suppressed"

    def test_finish_sent_and_failed_terminal(self, monkeypatch):
        s = _search(3)
        s.timer_started = True
        cog = _cog(monkeypatch, s)
        cog._claim_notification(s, "first")
        cog._finish_notification(s, "first", 12345)
        assert s.first_notification_status == "sent"
        assert s.first_notification_message_id == 12345
        assert s.first_notification_sent_at > 0
        # failed — терминален, повторной отправки нет
        cog._claim_notification(s, "second")
        cog._finish_notification(s, "second", 0)
        assert s.second_notification_status == "failed"
        assert cog._claim_notification(s, "second") is False

    @pytest.mark.asyncio
    async def test_maybe_send_both_max_two(self, monkeypatch):
        """За сессию максимум два уведомления; повторные проверки
        не создают пингов. Второе — не раньше 120с после первого."""
        s = _search(6)
        s.timer_started = True
        s.session_started_at = time.time() - 500
        s.effective_limit_seconds = 1200
        cog = _cog(monkeypatch, s)
        sent: list[str] = []

        async def _fake_send(search, kind):
            sent.append(kind)
            cog._finish_notification(search, kind, 1)

        monkeypatch.setattr(cog, "_send_queue_notification", _fake_send)
        await cog._maybe_send_notifications(s)
        await cog._maybe_send_notifications(s)
        # Только первое: второе требует >=120с после факта отправки
        assert sent == ["first"]
        # «Прошло» 121с с момента отправки первого
        s.first_notification_sent_at = time.time() - 121
        await cog._maybe_send_notifications(s)
        await cog._maybe_send_notifications(s)
        assert sent == ["first", "second"]

    @pytest.mark.asyncio
    async def test_both_conditions_simultaneous_sends_first_only(
        self, monkeypatch
    ):
        """ТЗ §3: 1 игрок 8 минут → резко 5/8. Оба условия впервые
        выполняются одновременно → только первое уведомление,
        второе — не раньше чем через 120с."""
        s = _search(1)
        s.timer_started = True
        s.session_started_at = time.time() - 480
        s.effective_limit_seconds = 1200
        cog = _cog(monkeypatch, s)
        sent: list[str] = []

        async def _fake_send(search, kind):
            sent.append(kind)
            cog._finish_notification(search, kind, 1)

        monkeypatch.setattr(cog, "_send_queue_notification", _fake_send)
        # Один игрок — вообще ничего
        await cog._maybe_send_notifications(s)
        assert sent == []
        # Резкий приток до 5/8 — только первое
        for i in range(4):
            s.add_player(f"New{i}", 2000 + i)
        await cog._maybe_send_notifications(s)
        assert sent == ["first"]
        # +121с и 5–7 игроков — второе
        s.first_notification_sent_at = time.time() - 121
        await cog._maybe_send_notifications(s)
        assert sent == ["first", "second"]

    @pytest.mark.asyncio
    async def test_second_cancelled_when_queue_drops(self, monkeypatch):
        """ТЗ §4: игроки вышли до 5/8 — второго нет; при возврате
        к 5/8 и живом интервале — отправляется сразу."""
        s = _search(6)
        s.timer_started = True
        s.session_started_at = time.time() - 500
        s.effective_limit_seconds = 1200
        s.first_notification_status = "sent"
        s.first_notification_sent_at = time.time() - 300
        cog = _cog(monkeypatch, s)
        sent: list[str] = []

        async def _fake_send(search, kind):
            sent.append(kind)
            cog._finish_notification(search, kind, 1)

        monkeypatch.setattr(cog, "_send_queue_notification", _fake_send)
        # Ушли 2 игрока → 4/8 — второго нет
        s.players = s.players[:-2]
        await cog._maybe_send_notifications(s)
        assert sent == []
        # Вернулся пятый игрок — второе сразу (интервал соблюдён)
        s.add_player("Back", 3000)
        await cog._maybe_send_notifications(s)
        assert sent == ["second"]


# ------------------------------------------------------------- expiry

class TestExpireQueue:
    @pytest.mark.asyncio
    async def test_expire_clears_session(self, monkeypatch):
        s = _search(5)
        s.timer_started = True
        s.session_started_at = time.time() - 700
        s.effective_limit_seconds = 600  # дедлайн прошёл
        s.first_notification_status = "sent"
        s.second_notification_status = "pending"
        cog = _cog(monkeypatch, s)
        monkeypatch.setattr(cog, "_update_queue_panel", AsyncMock())

        await cog._expire_queue(1, 10)
        assert s.players == []
        assert s.timer_started is False
        assert s.session_started_at == 0.0
        assert s.first_notification_status == "pending"

    @pytest.mark.asyncio
    async def test_expire_skipped_when_deadline_extended(self, monkeypatch):
        """Дедлайн продлён (7/8 → 20 мин) — expiry не срабатывает."""
        s = _search(5)
        s.timer_started = True
        s.session_started_at = time.time() - 700
        s.effective_limit_seconds = 1200  # продлённый лимит
        cog = _cog(monkeypatch, s)
        monkeypatch.setattr(cog, "_update_queue_panel", AsyncMock())

        await cog._expire_queue(1, 10)
        assert len(s.players) == 5
        assert s.timer_started is True

    @pytest.mark.asyncio
    async def test_expire_skipped_when_locked(self, monkeypatch):
        s = _search(5)
        s.timer_started = True
        s.state = "locked"
        s.session_started_at = time.time() - 700
        s.effective_limit_seconds = 600
        cog = _cog(monkeypatch, s)

        await cog._expire_queue(1, 10)
        assert len(s.players) == 5


# --------------------------------------------------- requeue suppression

class TestRequeueSuppression:
    def test_suppress_marks_pending_terminal(self):
        s = _search(4)
        s.start_new_session(time.time())
        s.suppress_notifications(time.time() + 1800)
        assert s.first_notification_status == "suppressed"
        assert s.second_notification_status == "suppressed"
        assert s.notify_suppressed_until > time.time()

    def test_new_session_after_full_completion_not_suppressed(self):
        """Ручной запуск новой сессии — suppress не наследуется."""
        s = _search(4)
        s.start_new_session(time.time())
        s.suppress_notifications(time.time() + 1800)
        s.start_new_session(time.time())  # новая сессия
        assert s.first_notification_status == "pending"
        assert s.notify_suppressed_until == 0.0
