"""P0: /tournament create — Discord 10062 Unknown interaction.

Регрессии:
- defer(ephemeral, thinking) вызывается ДО архивации и публикации;
- валидация аргументов до архивации завершённого турнира;
- защита от параллельного создания в одном канале (без дублей);
- «Турнир создан» только после channel.send + store.set;
- при сбое публикации карточка удаляется, турнир не сохраняется;
- архив завершённого турнира не теряется при ошибке создания.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import TournamentPhase, TournamentSize


# ---------------------------------------------------------------------------
# Фейки
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = None

    async def send_message(self, content=None, *, ephemeral=False, **kw):
        self.messages.append((content, ephemeral))

    async def defer(self, *, ephemeral=False, thinking=False, **kw):
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False, **kw):
        self.messages.append((content, ephemeral))
        return SimpleNamespace(delete=lambda: None)


class _FakeChannel:
    def __init__(self, fail_send=False, gate=None):
        self.sent = []
        self.deleted_ids = []
        self._fail_send = fail_send
        self._gate = gate

    async def send(self, **kw):
        if self._gate is not None:
            await self._gate.wait()
        if self._fail_send:
            raise RuntimeError("discord send failed")
        msg = SimpleNamespace(id=1000 + len(self.sent), delete=self._mk_delete)
        self.sent.append(msg)
        return msg

    def _mk_delete(self):
        async def _del():
            pass
        return _del()


class _FakeInteraction:
    def __init__(self, user_id=10, gid=1, cid=100):
        self.user = SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(id=gid, get_channel=lambda c: None)
        self.guild_id = gid
        self.channel_id = cid
        self.channel = _FakeChannel()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _FakeStore:
    def __init__(self, tournaments=None):
        self.tournaments = dict(tournaments or {})

    def get(self, gid, cid):
        return self.tournaments.get((gid, cid))

    def set(self, t):
        self.tournaments[(t.guild_id, t.channel_id)] = t


class _FakeOrgStore:
    def get(self, gid, cid):
        return None

    def set(self, gid, cid, orgs):
        pass


@pytest.fixture
def env(monkeypatch):
    """Общий стенд: store, embed, compose, notification, audit, orgs."""
    import cogs.tournament as tmod
    import services.tournament_lifecycle as lifecycle
    import services.tournament_audit_service as audit_svc
    import services.settings_service as settings_mod
    import storage.organizer_store as org_mod

    fake_store = _FakeStore()
    monkeypatch.setattr(tmod, "store", fake_store)
    async def _embed(t, g):
        return SimpleNamespace(name="embed")
    monkeypatch.setattr(tmod, "build_setup_embed", _embed)
    monkeypatch.setattr(tmod, "compose", lambda e, v: SimpleNamespace(view=v))
    monkeypatch.setattr(tmod, "send_tournament_notification",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(tmod, "_delete_message_later",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(tmod, "_delete_ephemeral_later",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(tmod, "_CREATE_IN_FLIGHT", set())

    class _Settings:
        def peek(self, gid, key):
            return True
    monkeypatch.setattr(settings_mod, "settings_service", _Settings())

    released = []
    async def _release(guild, tournament, channel=None):
        released.append(tournament)
        fake_store.tournaments.pop(
            (tournament.guild_id, tournament.channel_id), None)
        return True
    monkeypatch.setattr(lifecycle, "release_completed_tournament", _release)

    audited = []
    async def _audit(interaction, **kw):
        audited.append(kw)
    monkeypatch.setattr(audit_svc, "record_interaction", _audit)

    monkeypatch.setattr(org_mod, "organizer_store", _FakeOrgStore())

    bot = SimpleNamespace(
        build_view_for_tournament=lambda t: None,
        _register_view=lambda v: None,
    )
    cog = tmod.TournamentCog(bot)
    tmod._CREATE_IN_FLIGHT.clear()
    return SimpleNamespace(
        tmod=tmod, cog=cog, store=fake_store, released=released,
        audited=audited,
    )


def _completed_tournament(gid=1, cid=100):
    from models.tournament import Tournament
    t = Tournament(guild_id=gid, channel_id=cid,
                   size=TournamentSize.EIGHT, organizer_id=5)
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    return t


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defer_before_archive_and_publish(env):
    """Defer(ephemeral, thinking) — до архивации и channel.send."""
    env.store.tournaments[(1, 100)] = _completed_tournament()
    interaction = _FakeInteraction()

    order = []
    orig_defer = interaction.response.defer
    async def _defer(**kw):
        order.append("defer")
        await orig_defer(**kw)
    interaction.response.defer = _defer

    orig_send = interaction.channel.send
    async def _send(**kw):
        order.append("channel.send")
        return await orig_send(**kw)
    interaction.channel.send = _send

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "draft")

    assert interaction.response.deferred == {
        "ephemeral": True, "thinking": True}
    assert order[0] == "defer"
    assert order.index("defer") < order.index("channel.send")
    # Турнир создан и сохранён
    assert (1, 100) in env.store.tournaments
    t = env.store.tournaments[(1, 100)]
    assert t.phase != TournamentPhase.COMPLETE
    # Подтверждение ушло через followup, ephemeral
    assert env.released, "завершённый турнир должен быть заархивирован"
    assert any(ep for _, ep in interaction.followup.messages)


@pytest.mark.asyncio
async def test_invalid_size_no_archive_no_defer(env):
    """Невалидный размер — отказ до defer и до архивации."""
    env.store.tournaments[(1, 100)] = _completed_tournament()
    interaction = _FakeInteraction()

    await env.cog.tournament_create.callback(
        env.cog, interaction, "999", "draft")

    assert interaction.response.deferred is None
    assert env.released == []
    assert env.store.tournaments[(1, 100)].phase == TournamentPhase.COMPLETE
    assert any("Неверный размер" in str(m) for m, _ in interaction.response.messages)


@pytest.mark.asyncio
async def test_invalid_formation_no_archive(env):
    env.store.tournaments[(1, 100)] = _completed_tournament()
    interaction = _FakeInteraction()

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "elo")

    assert interaction.response.deferred is None
    assert env.released == []
    assert any("Неверный режим" in str(m) for m, _ in interaction.response.messages)


@pytest.mark.asyncio
async def test_active_tournament_blocks_create(env):
    """Активный турнир в канале — отказ, defer не нужен."""
    from models.tournament import Tournament
    active = Tournament(guild_id=1, channel_id=100,
                        size=TournamentSize.EIGHT, organizer_id=5)
    active.phase = TournamentPhase.QUALIFIERS
    env.store.tournaments[(1, 100)] = active
    interaction = _FakeInteraction()

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "draft")

    assert interaction.response.deferred is None
    assert env.released == []
    assert any("активный турнир" in str(m) for m, _ in interaction.response.messages)


@pytest.mark.asyncio
async def test_parallel_create_no_duplicate(env):
    """Два параллельных create — только один турнир и одна карточка."""
    gate = asyncio.Event()

    async def _slow_send(**kw):
        await gate.wait()
        return SimpleNamespace(id=555, delete=lambda: None)

    i1 = _FakeInteraction(user_id=10)
    i1.channel.send = _slow_send
    i2 = _FakeInteraction(user_id=11)

    async def _run():
        t1 = asyncio.create_task(env.cog.tournament_create.callback(
            env.cog, i1, "8", "draft"))
        await asyncio.sleep(0.05)  # i1 уже в _CREATE_IN_FLIGHT
        await env.cog.tournament_create.callback(
            env.cog, i2, "8", "draft")
        gate.set()
        await t1

    await _run()

    assert len(env.store.tournaments) == 1
    assert any("уже выполняется" in str(m) for m, _ in i2.followup.messages)


@pytest.mark.asyncio
async def test_publish_failure_no_store_no_success(env):
    """channel.send упал — store.set не вызван, «создан» не отправлен."""
    interaction = _FakeInteraction()
    interaction.channel = _FakeChannel(fail_send=True)

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "draft")

    assert env.store.tournaments == {}
    followups = [str(m) for m, _ in interaction.followup.messages]
    assert not any("Турнир создан" in m for m in followups)
    assert any("Не удалось создать турнир" in m for m in followups)


@pytest.mark.asyncio
async def test_store_failure_deletes_published_card(env, monkeypatch):
    """store.set упал после публикации — карточка удаляется."""
    interaction = _FakeInteraction()

    def _boom(t):
        raise RuntimeError("disk full")
    monkeypatch.setattr(env.store, "set", _boom)

    deleted = []
    orig_send = interaction.channel.send
    async def _send(**kw):
        msg = await orig_send(**kw)
        async def _del():
            deleted.append(msg.id)
        msg.delete = _del
        return msg
    interaction.channel.send = _send

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "draft")

    assert env.store.tournaments == {}
    assert deleted, "опубликованная карточка должна быть удалена"
    followups = [str(m) for m, _ in interaction.followup.messages]
    assert not any("Турнир создан" in m for m in followups)


@pytest.mark.asyncio
async def test_archive_failure_keeps_completed(env, monkeypatch):
    """Архивация не удалась — слот не освобождён, турнир не создан."""
    import services.tournament_lifecycle as lifecycle

    async def _fail(guild, tournament, channel=None):
        return False
    monkeypatch.setattr(lifecycle, "release_completed_tournament", _fail)

    env.store.tournaments[(1, 100)] = _completed_tournament()
    interaction = _FakeInteraction()

    await env.cog.tournament_create.callback(
        env.cog, interaction, "8", "draft")

    # Завершённый турнир остался в store — результаты не потеряны
    assert env.store.tournaments[(1, 100)].phase == TournamentPhase.COMPLETE
    assert len(env.store.tournaments) == 1
    assert interaction.channel.sent == []
    assert any("архив" in str(m).lower() for m, _ in interaction.followup.messages)


@pytest.mark.asyncio
async def test_success_path_order(env):
    """Успех: defer → publish → store.set → followup «Турнир создан»."""
    interaction = _FakeInteraction()
    order = []

    orig_defer = interaction.response.defer
    async def _defer(**kw):
        order.append("defer")
        await orig_defer(**kw)
    interaction.response.defer = _defer

    orig_send = interaction.channel.send
    async def _send(**kw):
        order.append("publish")
        return await orig_send(**kw)
    interaction.channel.send = _send

    orig_set = env.store.set
    def _set(t):
        order.append("store.set")
        return orig_set(t)
    env.store.set = _set

    orig_follow = interaction.followup.send
    async def _follow(content=None, **kw):
        order.append("followup")
        return await orig_follow(content, **kw)
    interaction.followup.send = _follow

    await env.cog.tournament_create.callback(
        env.cog, interaction, "16", "balanced-sr")

    assert order == ["defer", "publish", "store.set", "followup"]
    t = env.store.tournaments[(1, 100)]
    assert t.size == TournamentSize.SIXTEEN
    assert t.message_id == 1000
