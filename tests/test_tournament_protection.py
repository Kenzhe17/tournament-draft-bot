"""P0: защита завершённых турниров, архивирование, единые права.

Покрывает:
- can_manage_tournament: владелец/админы/ADMIN_USER_IDS/org/ORG-1..3,
  создатель, основной+доп. организаторы канала (с tournament_id),
  ORG_ROLE_MAP по каналу, обычный участник/капитан, отзыв прав,
  чужой канал;
- TournamentArchive: идемпотентность, персистентность между
  «перезапусками»;
- /tournament delete: отказ для COMPLETE (8/16/32), подтверждение
  для активного, deleteall пропускает завершённые;
- confirm_final_winner идемпотентен.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament, TournamentPhase, TournamentSize
from utils import permissions
from storage.organizer_store import ChannelOrganizers


# ---------------------------------------------------------------------------
# Фейки Discord-объектов
# ---------------------------------------------------------------------------


def _member(user_id: int, *, admin: bool = False, roles=()):
    return SimpleNamespace(
        id=user_id,
        roles=list(roles),
        guild_permissions=SimpleNamespace(administrator=admin),
        display_name=f"u{user_id}",
        name=f"u{user_id}",
    )


def _guild(guild_id: int = 1, roles=None):
    roles = roles or []
    return SimpleNamespace(
        id=guild_id,
        roles=roles,
        get_role=lambda rid: next((r for r in roles if r.id == rid), None),
        get_member=lambda uid: None,
        fetch_member=None,
    )


def _tournament(gid: int = 1, cid: int = 100, organizer_id: int = 0,
                size=TournamentSize.EIGHT) -> Tournament:
    return Tournament(
        guild_id=gid, channel_id=cid, size=size,
        organizer_id=organizer_id,
    )


@pytest.fixture
def orgs(monkeypatch):
    """Подмена organizer_store.get контролируемой мапой."""
    table: dict[tuple[int, int], ChannelOrganizers] = {}

    class _Store:
        def get(self, gid, cid):
            return table.get((gid, cid))

        def set(self, gid, cid, orgs_):
            table[(gid, cid)] = orgs_

    store_obj = _Store()
    import storage.organizer_store as os_mod
    monkeypatch.setattr(os_mod, "organizer_store", store_obj)
    # permissions.py импортирует объект по имени — патчим и там
    monkeypatch.setattr(permissions, "organizer_store", store_obj)
    return table, store_obj


# ---------------------------------------------------------------------------
# can_manage_tournament
# ---------------------------------------------------------------------------


def test_can_manage_bot_owner(orgs):
    table, _ = orgs
    t = _tournament(organizer_id=999)
    guild = _guild()
    owner = _member(permissions.BOT_OWNER_ID)
    assert permissions.can_manage_tournament(owner, guild, t)


def test_can_manage_admin_and_admin_ids(orgs):
    t = _tournament(organizer_id=999)
    guild = _guild()
    admin = _member(555, admin=True)
    assert permissions.can_manage_tournament(admin, guild, t)
    if permissions.ADMIN_USER_IDS:
        aid = next(iter(permissions.ADMIN_USER_IDS))
        assert permissions.can_manage_tournament(_member(aid), guild, t)


def test_can_manage_global_org_role(orgs):
    org_role = SimpleNamespace(id=777, name="org")
    guild = _guild(roles=[org_role])
    t = _tournament(organizer_id=999)
    user = _member(42, roles=[org_role])
    assert permissions.can_manage_tournament(user, guild, t)


@pytest.mark.parametrize("role_name", ["ORG-1", "ORG-2", "ORG-3"])
def test_can_manage_org_numbered_roles(orgs, monkeypatch, role_name):
    role_id = {"ORG-1": 101, "ORG-2": 102, "ORG-3": 103}[role_name]
    cfg_name = {"ORG-1": "ORG_1_ROLE_ID", "ORG-2": "ORG_2_ROLE_ID",
                "ORG-3": "ORG_3_ROLE_ID"}[role_name]
    monkeypatch.setattr(permissions, cfg_name, role_id)
    role = SimpleNamespace(id=role_id, name=role_name)
    guild = _guild(roles=[role])
    t = _tournament(organizer_id=999)
    user = _member(42, roles=[role])
    assert permissions.can_manage_tournament(user, guild, t)


def test_can_manage_tournament_creator(orgs):
    t = _tournament(organizer_id=42)
    guild = _guild()
    assert permissions.can_manage_tournament(_member(42), guild, t)


def test_can_manage_channel_organizers(orgs):
    table, store_obj = orgs
    t = _tournament(cid=100)
    tid = str(t.id)
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.additional_organizers = [11, 12]
    orgs_obj.tournament_id = tid
    store_obj.set(1, 100, orgs_obj)

    guild = _guild()
    for uid in (10, 11, 12):
        assert permissions.can_manage_tournament(_member(uid), guild, t), uid


def test_stale_tournament_binding_denied(orgs):
    """Организатор прошлого турнира канала не получает доступ к новому."""
    table, store_obj = orgs
    old = _tournament(cid=100)
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = str(old.id)  # привязка к СТАРОМУ турниру
    store_obj.set(1, 100, orgs_obj)

    new_t = _tournament(cid=100)
    new_t.id = f"{new_t.guild_id}_{new_t.channel_id}_new"
    assert new_t.id != old.id
    guild = _guild()
    assert not permissions.can_manage_tournament(_member(10), guild, new_t)


def test_unbound_channel_org_allowed(orgs):
    """Legacy-запись без tournament_id действует на текущий турнир."""
    table, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 100, orgs_obj)
    t = _tournament(cid=100)
    assert permissions.can_manage_tournament(_member(10), _guild(), t)


def test_org_role_map_channel_scoped(orgs, monkeypatch):
    """ORG_ROLE_MAP даёт права только в привязанном канале."""
    role = SimpleNamespace(id=555, name="channel-org")
    guild = _guild(roles=[role])
    import config
    monkeypatch.setattr(config, "ORG_ROLE_MAP", {100: 555})

    t_here = _tournament(cid=100)
    t_other = _tournament(cid=200)
    user = _member(42, roles=[role])
    assert permissions.can_manage_tournament(user, guild, t_here)
    assert not permissions.can_manage_tournament(user, guild, t_other)


def test_regular_user_and_captain_denied(orgs):
    table, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 100, orgs_obj)

    t = _tournament(cid=100, organizer_id=10)
    t.teams = [{"captain": "Cap", "players": ["Cap", "Mate"]}]
    t.player_user_ids = {"Cap": 77, "Mate": 78}
    guild = _guild()
    # Обычный участник и капитан без орг-прав
    assert not permissions.can_manage_tournament(_member(77), guild, t)
    assert not permissions.can_manage_tournament(_member(78), guild, t)
    assert not permissions.can_manage_tournament(_member(999), guild, t)


def test_revoked_access_immediate(orgs):
    """Отзыв прав прекращает доступ без перезапуска."""
    table, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 100, orgs_obj)
    t = _tournament(cid=100)
    guild = _guild()
    user = _member(10)
    assert permissions.can_manage_tournament(user, guild, t)
    # Отзываем
    orgs_obj.remove_organizer(10)
    store_obj.set(1, 100, orgs_obj)
    assert not permissions.can_manage_tournament(user, guild, t)


def test_other_channel_org_denied(orgs):
    """Организатор канала 200 не управляет турниром канала 100."""
    table, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 200, orgs_obj)  # только канал 200

    t = _tournament(cid=100)
    assert not permissions.can_manage_tournament(_member(10), _guild(), t)


# ---------------------------------------------------------------------------
# Архив завершённых турниров
# ---------------------------------------------------------------------------


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from storage import tournament_archive as ta

    monkeypatch.setattr(ta, "ARCHIVE_FILE", tmp_path / "tournament_archive.json")
    inst = ta.TournamentArchive()
    return inst


def test_archive_idempotent_and_persistent(archive):
    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    assert archive.archive(t) is True
    assert archive.is_archived(str(t.id))

    # Повторное архивирование — без дублей
    assert archive.archive(t) is True
    assert len(archive.list(guild_id=1)) == 1

    # «Перезапуск»: новый экземпляр читает тот же файл
    from storage import tournament_archive as ta
    inst2 = ta.TournamentArchive()
    assert inst2.is_archived(str(t.id))
    assert len(inst2.list()) == 1


def test_archive_preserves_winner_and_matches(archive):
    t = _tournament()
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 1
    t.teams = [{"captain": "A"}, {"captain": "B"}]
    t.team_names = {0: "Alpha", 1: "Beta"}
    t.qualifier_matches = [(0, 1)]
    t.qualifier_winners = [1]
    archive.archive(t)

    entry = archive.get(str(t.id))
    data = entry["data"]
    assert data["winner_team_index"] == 1
    assert data["phase"] == "complete"
    assert data["team_names"] == {"0": "Alpha", "1": "Beta"} or \
        data["team_names"] == {0: "Alpha", 1: "Beta"}


# ---------------------------------------------------------------------------
# /tournament delete — защита COMPLETE + подтверждение
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.views = []
        self.done = False

    async def send_message(self, content=None, *, view=None, ephemeral=False, **kw):
        self.done = True
        self.messages.append(content)
        self.views.append(view)

    async def edit_message(self, *, view=None, content=None, **kw):
        self.views.append(view)

    async def defer(self, **kw):
        self.done = True

    @property
    def is_done(self):
        return self.done


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False, **kw):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, user, guild, gid=1, cid=100):
        self.user = user
        self.guild = guild
        self.guild_id = gid
        self.channel_id = cid
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.client = SimpleNamespace()


class _FakeStore:
    def __init__(self, tournaments):
        self.tournaments = dict(tournaments)
        self.deleted = []

    def get(self, gid, cid):
        return self.tournaments.get((gid, cid))

    def get_by_guild(self, gid):
        return [t for (g, _), t in self.tournaments.items() if g == gid]

    def set(self, t):
        self.tournaments[(t.guild_id, t.channel_id)] = t

    def delete(self, gid, cid):
        self.deleted.append((gid, cid))
        self.tournaments.pop((gid, cid), None)


@pytest.mark.parametrize("size", [
    TournamentSize.EIGHT, TournamentSize.SIXTEEN, TournamentSize.THIRTY_TWO])
@pytest.mark.asyncio
async def test_delete_completed_refused(orgs, monkeypatch, size):
    """Завершённый турнир любого размера нельзя удалить."""
    import cogs.tournament as tmod

    t = _tournament(size=size)
    t.phase = TournamentPhase.COMPLETE
    fake_store = _FakeStore({(1, 100): t})
    monkeypatch.setattr(tmod, "store", fake_store)

    guild = _guild()
    user = _member(permissions.BOT_OWNER_ID)
    interaction = _FakeInteraction(user, guild)

    cog = tmod.TournamentCog(SimpleNamespace())
    await cog.tournament_delete.callback(cog, interaction)

    assert fake_store.deleted == []
    assert any("защищён" in str(m) for m in interaction.response.messages)


@pytest.mark.asyncio
async def test_delete_active_requires_confirm(orgs, monkeypatch):
    """Активный турнир удаляется только после подтверждения."""
    import cogs.tournament as tmod

    t = _tournament()
    t.phase = TournamentPhase.SEMIFINALS
    fake_store = _FakeStore({(1, 100): t})
    monkeypatch.setattr(tmod, "store", fake_store)

    guild = _guild()
    # Основной организатор канала
    _, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 100, orgs_obj)

    user = _member(10)
    interaction = _FakeInteraction(user, guild)

    cog = tmod.TournamentCog(SimpleNamespace())
    await cog.tournament_delete.callback(cog, interaction)

    # Только подтверждение показано — удаления ещё нет
    assert fake_store.deleted == []
    assert interaction.response.views, "ожидалась карточка подтверждения"


@pytest.mark.asyncio
async def test_delete_confirm_executes_and_rechecks(orgs, monkeypatch):
    """Кнопка подтверждения удаляет активный турнир; COMPLETE — нет."""
    import cogs.tournament as tmod

    t = _tournament()
    t.phase = TournamentPhase.SEMIFINALS
    fake_store = _FakeStore({(1, 100): t})
    monkeypatch.setattr(tmod, "store", fake_store)

    guild = _guild()
    _, store_obj = orgs
    orgs_obj = ChannelOrganizers()
    orgs_obj.organizer_id = 10
    orgs_obj.tournament_id = ""
    store_obj.set(1, 100, orgs_obj)

    view = tmod._DeleteTournamentConfirmView(1, 100)
    interaction = _FakeInteraction(_member(10), guild)
    interaction.client = SimpleNamespace(
        fetch_message=None,
    )
    # _perform_tournament_delete требует bot без mm_cog — подойдёт любой
    interaction.client = SimpleNamespace()

    await view._confirm(interaction)
    assert fake_store.deleted == [(1, 100)]

    # Повторное нажатие после завершения — отказ
    t2 = _tournament()
    t2.phase = TournamentPhase.COMPLETE
    fake_store2 = _FakeStore({(1, 100): t2})
    monkeypatch.setattr(tmod, "store", fake_store2)
    interaction2 = _FakeInteraction(_member(10), guild)
    await view._confirm(interaction2)
    assert fake_store2.deleted == []


@pytest.mark.asyncio
async def test_deleteall_skips_completed(orgs, monkeypatch):
    """deleteall удаляет только активные турниры."""
    import cogs.tournament as tmod

    sizes = [TournamentSize.EIGHT, TournamentSize.SIXTEEN,
             TournamentSize.THIRTY_TWO]
    tournaments = {}
    for i, size in enumerate(sizes):
        done = _tournament(cid=100 + i, size=size)
        done.phase = TournamentPhase.COMPLETE
        tournaments[(1, 100 + i)] = done
        active = _tournament(cid=200 + i, size=size)
        active.phase = TournamentPhase.SEMIFINALS
        tournaments[(1, 200 + i)] = active

    fake_store = _FakeStore(tournaments)
    monkeypatch.setattr(tmod, "store", fake_store)

    async def _fetch_message(mid):
        return SimpleNamespace(delete=lambda: None)

    bot = SimpleNamespace(fetch_message=_fetch_message)
    cog = tmod.TournamentCog(bot)

    guild = _guild()
    interaction = _FakeInteraction(_member(permissions.BOT_OWNER_ID), guild)
    await cog.tournament_deleteall.callback(cog, interaction)

    deleted_channels = {cid for _, cid in fake_store.deleted}
    # Все активные удалены, все завершённые — на месте
    assert deleted_channels == {200, 201, 202}
    for i in range(3):
        assert (1, 100 + i) in fake_store.tournaments


# ---------------------------------------------------------------------------
# Идемпотентность финала
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_final_winner_idempotent():
    t = _tournament()
    t.phase = TournamentPhase.FINAL
    t.final_teams = [0, 1]
    t.teams = [{"captain": "A"}, {"captain": "B"}]

    await t.confirm_final_winner(0)
    assert t.phase == TournamentPhase.COMPLETE
    assert t.winner_team_index == 0

    # Повторный вызов — no-op, результат не меняется
    await t.confirm_final_winner(1)
    assert t.phase == TournamentPhase.COMPLETE
    assert t.winner_team_index == 0


# ---------------------------------------------------------------------------
# create → архивирование (логика ветки COMPLETE)
# ---------------------------------------------------------------------------


def test_completed_slot_released_via_archive(archive, orgs):
    """Сценарий create: COMPLETE → archive → delete слота."""
    old = _tournament()
    old.phase = TournamentPhase.COMPLETE
    old.winner_team_index = 0

    # Имитируем ветку tournament_create для существующего COMPLETE
    assert old.phase == TournamentPhase.COMPLETE
    assert archive.archive(old) is True
    # Слот освобождается store.delete — архив уже содержит данные
    assert archive.is_archived(str(old.id))
