"""Интеграционные тесты Squad Hub 2.0 на отдельной тестовой PostgreSQL.

pgserver поднимает временный кластер — production-БД не используется.
Покрывают: схему, создание команд (уникальность, валидация),
атомарное вступление (вместимость, одна команда на пользователя,
конкурентность), приглашения, заявки, LFG, каталог, хеши паролей,
защиту активных турниров, расформирование.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from models.team import (
    JOIN_APPLICATIONS, JOIN_CLOSED, JOIN_OPEN, JOIN_PASSWORD,
    PURPOSE_CASHCUP, Team, hash_password, verify_password,
)
from storage import squad_store
from storage.squad_schema import ensure_squad_schema
from storage.team_store import TeamStoreError, team_store

G = 888000


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_squad")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    # Схема: базовая teams + squad-расширения
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id TEXT PRIMARY KEY,
                team_name TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                captain_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                members BIGINT[] NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await ensure_squad_schema(conn)
        for t in ("team_invites", "team_applications", "team_lfg",
                  "team_audit", "squad_hub_state", "teams"):
            await conn.execute(f"DELETE FROM {t}")
    team_store._teams.clear()
    try:
        yield pool
    finally:
        team_store._teams.clear()
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


def _team(name: str, captain: int, gid: int = G, **kw) -> Team:
    return Team(team_id="", team_name=name, captain_id=captain,
                guild_id=gid, **kw)


# ----------------------------------------------------------------------
# Схема и модель
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_tables_exist(pg_pool):
    async with pg_pool.acquire() as conn:
        for t in ("team_invites", "team_applications", "team_lfg",
                  "team_audit", "squad_hub_state"):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_name = $1)", t)
            assert exists, f"table {t} missing"
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'teams'")
        names = {r["column_name"] for r in cols}
        for c in ("join_mode", "recruiting", "purpose", "description",
                  "requirements", "logo_url", "password_hash",
                  "updated_at", "recruitment_channel_id",
                  "recruitment_message_id"):
            assert c in names, f"teams.{c} missing"


def test_password_hash_roundtrip():
    h = hash_password("s3cret!")
    assert h.startswith("pbkdf2$")
    assert verify_password("s3cret!", h)
    assert not verify_password("wrong", h)
    # legacy plaintext принимается на переходный период
    assert verify_password("plain", "plain")
    assert not verify_password("other", "plain")


def test_team_name_validation():
    from models.team import validate_team_name, validate_description
    assert validate_team_name("ab") is None
    assert validate_team_name("a") is not None
    assert validate_team_name("x" * 25) is not None
    assert validate_description("y" * 151) is not None
    assert validate_description("y" * 150) is None


# ----------------------------------------------------------------------
# Создание команд
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_team_and_unique_name(pg_pool):
    t1 = await team_store.create(_team("Alpha", 1001))
    assert t1.team_id
    assert t1.is_captain(1001) and t1.is_member(1001)
    # Регистронезависимая уникальность
    with pytest.raises(TeamStoreError) as ei:
        await team_store.create(_team("alpha", 1002))
    assert ei.value.code == "name_taken"
    with pytest.raises(TeamStoreError) as ei2:
        await team_store.create(_team("ALPHA", 1003))
    assert ei2.value.code == "name_taken"
    # Другой guild — можно
    t2 = await team_store.create(_team("alpha", 2001, gid=G + 1))
    assert t2.guild_id == G + 1


@pytest.mark.asyncio
async def test_create_team_user_already_in_team(pg_pool):
    await team_store.create(_team("Bravo", 1010))
    with pytest.raises(TeamStoreError) as ei:
        await team_store.create(_team("Charlie", 1010))
    assert ei.value.code == "already_in_team"


@pytest.mark.asyncio
async def test_create_invalid_name(pg_pool):
    with pytest.raises(TeamStoreError) as ei:
        await team_store.create(_team("x", 1020))
    assert ei.value.code == "name_invalid"


# ----------------------------------------------------------------------
# Вступление / выход / исключение
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_open_team(pg_pool):
    team = await team_store.create(
        _team("Delta", 1100, join_mode=JOIN_OPEN))
    joined = await team_store.join_team(team.team_id, 1101, G)
    assert 1101 in joined.members
    # Повторное вступление — ошибка
    with pytest.raises(TeamStoreError):
        await team_store.join_team(team.team_id, 1101, G)


@pytest.mark.asyncio
async def test_join_respects_capacity(pg_pool):
    team = await team_store.create(
        _team("Echo", 1200, join_mode=JOIN_OPEN))
    for uid in (1201, 1202, 1203):
        await team_store.join_team(team.team_id, uid, G)
    assert team_store.get(team.team_id).is_full()
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 1204, G)
    assert ei.value.code == "team_full"


@pytest.mark.asyncio
async def test_join_one_team_per_user_concurrent(pg_pool):
    t1 = await team_store.create(_team("Foxtrot", 1300,
                                       join_mode=JOIN_OPEN))
    t2 = await team_store.create(_team("Golf", 1301,
                                       join_mode=JOIN_OPEN))
    results = await asyncio.gather(
        team_store.join_team(t1.team_id, 1310, G),
        team_store.join_team(t2.team_id, 1310, G),
        return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, TeamStoreError)]
    assert len(ok) == 1
    assert len(errs) == 1
    assert errs[0].code == "user_in_other_team"


@pytest.mark.asyncio
async def test_join_last_slot_concurrent(pg_pool):
    team = await team_store.create(
        _team("Hotel", 1400, join_mode=JOIN_OPEN))
    for uid in (1401, 1402):
        await team_store.join_team(team.team_id, uid, G)
    # Осталось 1 место — два претендента одновременно
    results = await asyncio.gather(
        team_store.join_team(team.team_id, 1410, G),
        team_store.join_team(team.team_id, 1411, G),
        return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, TeamStoreError)]
    assert len(ok) == 1
    assert len(errs) == 1
    assert errs[0].code == "team_full"


@pytest.mark.asyncio
async def test_leave_and_kick(pg_pool):
    team = await team_store.create(
        _team("India", 1500, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 1501, G)
    # Капитан не может выйти
    with pytest.raises(TeamStoreError) as ei:
        await team_store.leave_team(team.team_id, 1500)
    assert ei.value.code == "captain_must_transfer"
    # Участник выходит
    await team_store.leave_team(team.team_id, 1501)
    assert 1501 not in team_store.get(team.team_id).members
    # Кик
    await team_store.join_team(team.team_id, 1502, G)
    await team_store.remove_member(team.team_id, 1502,
                                   actor_id=1500, guild_id=G)
    assert 1502 not in team_store.get(team.team_id).members
    # Капитана кикнуть нельзя
    with pytest.raises(TeamStoreError):
        await team_store.remove_member(team.team_id, 1500,
                                       actor_id=1500, guild_id=G)


@pytest.mark.asyncio
async def test_transfer_and_confirm_captain(pg_pool):
    team = await team_store.create(
        _team("Juliett", 1600, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 1601, G)
    # Запрос от капитана
    await team_store.transfer_captain(team.team_id, 1601, actor_id=1600)
    # Стоп — transfer_captain сразу меняет; confirm-путь отдельно
    t = team_store.get(team.team_id)
    assert t.captain_id == 1601


@pytest.mark.asyncio
async def test_confirm_captain_transfer(pg_pool):
    team = await team_store.create(
        _team("Kilo", 1700, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 1701, G)
    await team_store.confirm_captain_transfer(team.team_id, 1701)
    assert team_store.get(team.team_id).captain_id == 1701
    # Не-участник не может
    with pytest.raises(TeamStoreError):
        await team_store.confirm_captain_transfer(team.team_id, 1799)


@pytest.mark.asyncio
async def test_disband(pg_pool):
    team = await team_store.create(
        _team("Lima", 1800, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 1801, G)
    inv = await squad_store.create_invite(G, team.team_id, 1800, 1802)
    assert inv is not None
    name = await team_store.disband(team.team_id, 1800)
    assert name == "Lima"
    assert team_store.get(team.team_id) is None
    # Приглашения/заявки удаляются вместе с командой
    # (история — в team_audit)
    assert await squad_store.get_invite(inv.invite_id) is None


@pytest.mark.asyncio
async def test_disband_not_captain(pg_pool):
    team = await team_store.create(
        _team("Mike", 1900, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 1901, G)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.disband(team.team_id, 1901)
    assert ei.value.code == "not_captain"


# ----------------------------------------------------------------------
# Пароли
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_team(pg_pool):
    team = _team("November", 2000, join_mode=JOIN_PASSWORD)
    team.set_password("hunter2")
    await team_store.create(team)
    stored = team_store.get(team.team_id)
    assert stored.password == ""  # plaintext не хранится
    assert verify_password("hunter2", stored.password_hash)
    assert not verify_password("bad", stored.password_hash)


@pytest.mark.asyncio
async def test_legacy_password_migration(pg_pool):
    """Старая команда с plaintext-паролем: from_dict хеширует,
    load_from_postgres персистит hash и затирает plaintext."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO teams (team_id, team_name, password, "
            "captain_id, guild_id, members) "
            "VALUES ('legacy1','Oscar', 'legacypass', 2100, $1, $2)",
            G, [2100])
    await team_store.load_from_postgres()
    team = team_store.get("legacy1")
    assert team is not None
    assert team.check_password("legacypass")
    assert team.password == ""
    assert team.password_hash.startswith("pbkdf2$")
    # plaintext затёрт в БД
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password, password_hash FROM teams "
            "WHERE team_id='legacy1'")
        assert row["password"] == ""
        assert row["password_hash"].startswith("pbkdf2$")


# ----------------------------------------------------------------------
# Приглашения
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_lifecycle(pg_pool):
    team = await team_store.create(_team("Papa", 2200))
    inv = await squad_store.create_invite(G, team.team_id, 2200, 2201)
    assert inv is not None and inv.is_pending
    # Дубликат pending — нельзя
    dup = await squad_store.create_invite(G, team.team_id, 2200, 2201)
    assert dup is None
    pending = await squad_store.list_pending_invites_for_user(G, 2201)
    assert len(pending) == 1
    # Принятие на уровне store: атомарное вступление по приглашению
    # + resolve + закрытие остальных приглашений — внутри tx.
    await team_store.join_team(team.team_id, 2201, G, via="invite",
                               invite_id=inv.invite_id)
    inv_after = await squad_store.get_invite(inv.invite_id)
    assert inv_after.status == "accepted"
    assert await squad_store.list_pending_invites_for_user(G, 2201) == []


@pytest.mark.asyncio
async def test_invite_decline_and_cancel(pg_pool):
    team = await team_store.create(_team("Quebec", 2300))
    inv = await squad_store.create_invite(G, team.team_id, 2300, 2301)
    assert await squad_store.resolve_invite(inv.invite_id, "declined")
    # После declined можно новое приглашение
    inv2 = await squad_store.create_invite(G, team.team_id, 2300, 2301)
    assert inv2 is not None
    n = await squad_store.cancel_invites_for_user(G, 2301,
                                                  "joined_team")
    assert n == 1
    assert await squad_store.list_pending_invites_for_user(G, 2301) == []


# ----------------------------------------------------------------------
# Заявки
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_application_lifecycle(pg_pool):
    team = await team_store.create(
        _team("Romeo", 2400, join_mode=JOIN_APPLICATIONS))
    app = await squad_store.create_application(
        G, team.team_id, 2401, "hello")
    assert app is not None and app.is_pending
    # Дубликат — нельзя
    assert await squad_store.create_application(
        G, team.team_id, 2401) is None
    apps = await squad_store.list_pending_applications_for_team(
        team.team_id)
    assert len(apps) == 1
    assert apps[0].message == "hello"
    # Игрок может иметь заявки в несколько команд
    team2 = await team_store.create(
        _team("Sierra", 2402, join_mode=JOIN_APPLICATIONS))
    app2 = await squad_store.create_application(G, team2.team_id, 2401)
    assert app2 is not None
    mine = await squad_store.list_pending_applications_for_user(G, 2401)
    assert len(mine) == 2
    # Отзыв своей заявки
    assert await squad_store.withdraw_application(app.application_id,
                                                  2401)
    assert not await squad_store.withdraw_application(
        app.application_id, 9999)  # чужая — нельзя
    # Ещё одна pending-заявка до вступления
    app3 = await squad_store.create_application(G, team.team_id, 2401)
    assert app3 is not None
    # Принятие заявки: единая транзакция — вступление + фиксация
    # app3 + автоматическое закрытие остальных заявок/приглашений.
    await team_store.join_team(team.team_id, 2401, G,
                               via="application",
                               application_id=app3.application_id,
                               actor_id=team.captain_id)
    assert (await squad_store.get_application(
        app3.application_id)).status == "accepted"
    # Остальные pending-заявки игрока закрыты внутри tx
    assert (await squad_store.get_application(
        app2.application_id)).status == "cancelled"
    assert await squad_store.list_pending_applications_for_user(
        G, 2401) == []


# ----------------------------------------------------------------------
# LFG
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lfg_upsert_deactivate(pg_pool):
    prof = await squad_store.upsert_lfg(
        G, 2500, device="mobile", play_role="Рашер",
        purpose="cashcup", description="ищу состав")
    assert prof is not None and prof.is_active
    got = await squad_store.get_lfg(G, 2500)
    assert got.device == "mobile" and got.play_role == "Рашер"
    # Обновление
    await squad_store.upsert_lfg(G, 2500, device="pc")
    got = await squad_store.get_lfg(G, 2500)
    assert got.device == "pc"
    # Список с исключением
    lst = await squad_store.list_lfg(G, exclude_user_ids={2500})
    assert all(p.user_id != 2500 for p in lst)
    lst2 = await squad_store.list_lfg(G)
    assert any(p.user_id == 2500 for p in lst2)
    # Деактивация
    assert await squad_store.deactivate_lfg(G, 2500)
    got = await squad_store.get_lfg(G, 2500)
    assert not got.is_active


@pytest.mark.asyncio
async def test_lfg_filters(pg_pool):
    await squad_store.upsert_lfg(G, 2600, device="mobile",
                                 play_role="Снайпер")
    await squad_store.upsert_lfg(G, 2601, device="pc",
                                 play_role="Рашер")
    mob = await squad_store.list_lfg(G, device="mobile")
    assert all(p.device == "mobile" for p in mob)
    snipers = await squad_store.list_lfg(G, play_role="Снайпер")
    assert all(p.play_role == "Снайпер" for p in snipers)


# ----------------------------------------------------------------------
# Каталог и статистика хаба
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_teams(pg_pool):
    # Команда с закрытым набором и закрытым режимом — не в каталоге
    await team_store.create(_team("Zulu", 2700, recruiting=False))
    await team_store.create(_team("Yankee", 2701, join_mode=JOIN_CLOSED))
    open_t = await team_store.create(
        _team("Xray", 2702, join_mode=JOIN_OPEN))
    await team_store.join_team(open_t.team_id, 2703, G)
    cat = await squad_store.catalog_teams(G)
    names = {t["team_name"] for t in cat}
    assert "Xray" in names
    assert "Zulu" not in names  # recruiting=False
    assert "Yankee" not in names  # closed


@pytest.mark.asyncio
async def test_hub_stats(pg_pool):
    await team_store.create(_team("StatsOne", 2750))
    await squad_store.upsert_lfg(G, 2751)
    stats = await squad_store.squad_hub_stats(G)
    assert stats["teams"] >= 1
    assert stats["lfg"] >= 1


@pytest.mark.asyncio
async def test_hub_state_roundtrip(pg_pool):
    assert await squad_store.get_hub_state(G) is None
    await squad_store.set_hub_state(G, 555, 666)
    st = await squad_store.get_hub_state(G)
    assert st == {"channel_id": 555, "message_id": 666}
    await squad_store.clear_hub_state(G)
    assert await squad_store.get_hub_state(G) is None


# ----------------------------------------------------------------------
# Аудит
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write(pg_pool):
    team = await team_store.create(_team("Whiskey", 2800))
    await squad_store.audit(G, team.team_id, 2800, "test_action",
                            target_id=2801, metadata={"k": "v"})
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action, target_id, metadata FROM team_audit "
            "WHERE team_id = $1 ORDER BY audit_id DESC LIMIT 1",
            team.team_id)
        assert row["action"] == "test_action"
        assert int(row["target_id"]) == 2801


# ----------------------------------------------------------------------
# Защита активных турниров (BR)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_br_active_tournament_blocks_roster(pg_pool):
    from models.br_tournament import BRTournament
    from storage.br_store import br_store
    team = await team_store.create(
        _team("Victor", 2900, join_mode=JOIN_OPEN))
    # Регистрируем команду в активном BR напрямую в кэше
    brt = BRTournament(id="brt1", guild_id=G, channel_id=1,
                       is_test=False)
    brt.registered_teams.append(team.team_id)
    br_store._tournaments[(G, 1)] = brt
    try:
        with pytest.raises(TeamStoreError) as ei:
            await team_store.join_team(team.team_id, 2901, G)
        assert ei.value.code == "roster_locked"
        with pytest.raises(TeamStoreError):
            await team_store.disband(team.team_id, 2900)
        reason = await svc_reason(G, team.team_id)
        assert reason is not None
    finally:
        br_store._tournaments.pop((G, 1), None)
    # После завершения — управление доступно
    brt.phase = __import__("models.br_tournament",
                           fromlist=["BRTournamentPhase"]).BRTournamentPhase.COMPLETE
    br_store._tournaments[(G, 1)] = brt
    try:
        await team_store.join_team(team.team_id, 2901, G)
        assert 2901 in team_store.get(team.team_id).members
    finally:
        br_store._tournaments.pop((G, 1), None)


async def svc_reason(gid, tid):
    from services.squad_service import active_tournament_reason
    return await active_tournament_reason(gid, tid)


# ----------------------------------------------------------------------
# Обновление метаданных
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_meta_and_name_uniqueness(pg_pool):
    team = await team_store.create(_team("Uniform", 3000))
    team.description = "новое описание"
    team.purpose = PURPOSE_CASHCUP
    team.join_mode = JOIN_APPLICATIONS
    assert await team_store.update_team(team)
    fresh = team_store.get(team.team_id)
    assert fresh.description == "новое описание"
    assert fresh.purpose == PURPOSE_CASHCUP
    # Дубликат имени при переименовании
    await team_store.create(_team("Tango", 3001))
    team.team_name = "tango"
    with pytest.raises(TeamStoreError) as ei:
        await team_store.update_team(team)
    assert ei.value.code == "name_taken"


@pytest.mark.asyncio
async def test_update_team_rejects_roster_change(pg_pool):
    """update_team не должен изменять состав/капитана — даже если
    вызывающий код мутировал кэш-объект перед вызовом."""
    team = await team_store.create(_team("Uniform2", 3010))
    team.members = [3010, 3011]  # попытка изменить состав напрямую
    team.description = "ok"
    with pytest.raises(TeamStoreError) as ei:
        await team_store.update_team(team)
    assert ei.value.code == "roster_locked"
    # Состав в БД не пострадал
    fresh = team_store.get(team.team_id)
    assert sorted(fresh.members) == [3010]


# ----------------------------------------------------------------------
# P0.2 — миграция legacy-команд
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_team_gets_password_mode(pg_pool):
    """Команда, созданная до Squad Hub (plaintext-пароль, без
    squad-колонок в данных) → join_mode='password', recruiting
    выключен, публичный набор недоступен."""
    async with pg_pool.acquire() as conn:
        # Имитируем строку до миграции: schema уже применена
        # фикстурой, но legacy-флаг ловится по password_hash=''.
        await conn.execute(
            "INSERT INTO teams (team_id, team_name, password, "
            "captain_id, guild_id, members, join_mode, recruiting) "
            "VALUES ('legacy_pw','LegacyPW','secretpw', 3100, $1, "
            "$2, 'applications', TRUE)", G, [3100])
        # Прогоняем legacy-миграцию ещё раз — она идемпотентна и
        # переведёт строку в password-mode.
        from storage.squad_schema import _LEGACY_MIGRATION
        for stmt in _LEGACY_MIGRATION:
            await conn.execute(stmt)
        row = await conn.fetchrow(
            "SELECT join_mode, recruiting FROM teams "
            "WHERE team_id='legacy_pw'")
        assert row["join_mode"] == "password"
        assert row["recruiting"] is False
    await team_store.load_from_postgres()
    team = team_store.get("legacy_pw")
    assert team is not None
    assert team.join_mode == "password"
    assert not team.recruiting
    # Пароль работает, открытое вступление — нет
    joined = await team_store.join_team(
        "legacy_pw", 3101, G, via="password", password="secretpw")
    assert 3101 in joined.members
    team2 = await team_store.create(
        _team("OpenX", 3110, join_mode=JOIN_OPEN))
    team2.join_mode = "closed"
    team2.recruiting = True
    await team_store.update_team(team2)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team2.team_id, 3111, G,
                                   via="open")
    assert ei.value.code == "recruiting_closed"


@pytest.mark.asyncio
async def test_unique_index_conflict_report(pg_pool):
    """Дубликаты названий без учёта регистра → уникальный индекс
    НЕ создаётся, отчёт пишется, команды не трогаются."""
    from storage.squad_schema import _ensure_unique_name_index
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS "
                           "ux_teams_guild_lower_name")
        # Прямые INSERT — create() сам не даст дубликат.
        for tid, name in (("dupA", "Same"), ("dupB", "same")):
            await conn.execute(
                "INSERT INTO teams (team_id, team_name, password, "
                "captain_id, guild_id, members) VALUES ($1,$2,'',"
                " 3200, $3, $4)", tid, name, G, [3200])
        await _ensure_unique_name_index(conn)
        idx = await conn.fetchval(
            "SELECT indexname FROM pg_indexes WHERE tablename='teams' "
            "AND indexname='ux_teams_guild_lower_name'")
        assert idx is None  # уникальный индекс не создан
        # Отчёт сохранён
        from config import DATA_DIR
        report = DATA_DIR / "squad_migration_report.json"
        assert report.exists()
        report.unlink(missing_ok=True)
        # Команды не изменены
        n = await conn.fetchval(
            "SELECT count(*) FROM teams WHERE team_id IN "
            "('dupA','dupB')")
        assert n == 2


# ----------------------------------------------------------------------
# P0.3/P0.9 — гонки и обходы единого join
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_cancelled_before_accept(pg_pool):
    """Отмена приглашения одновременно с принятием → join
    отклоняется, игрок не добавлен."""
    team = await team_store.create(
        _team("Yankee2", 3300, join_mode=JOIN_OPEN))
    inv = await squad_store.create_invite(G, team.team_id, 3300, 3301)
    assert inv is not None
    await squad_store.resolve_invite(inv.invite_id, "cancelled")
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3301, G,
                                   via="invite", invite_id=inv.invite_id)
    assert ei.value.code == "invite_invalid"
    assert 3301 not in team_store.get(team.team_id).members


@pytest.mark.asyncio
async def test_application_expired_before_accept(pg_pool):
    """Заявка, истёкшая к моменту принятия → join отклоняется."""
    team = await team_store.create(
        _team("Zulu2", 3400, join_mode=JOIN_APPLICATIONS))
    app = await squad_store.create_application(G, team.team_id, 3401)
    assert app is not None
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE team_applications SET expires_at = now() - "
            "interval '1 hour' WHERE application_id = $1",
            app.application_id)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3401, G,
                                   via="application",
                                   application_id=app.application_id,
                                   actor_id=team.captain_id)
    assert ei.value.code == "application_invalid"
    assert 3401 not in team_store.get(team.team_id).members


@pytest.mark.asyncio
async def test_mode_change_after_card_opened(pg_pool):
    """Режим вступления сменился после открытия карточки →
    устаревший via отклоняется."""
    team = await team_store.create(
        _team("Alpha2", 3500, join_mode=JOIN_APPLICATIONS))
    app = await squad_store.create_application(G, team.team_id, 3501)
    assert app is not None
    # Капитан закрыл набор и сменил режим на closed
    team.join_mode = JOIN_CLOSED
    team.recruiting = False
    await team_store.update_team(team)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3501, G,
                                   via="application",
                                   application_id=app.application_id,
                                   actor_id=team.captain_id)
    assert ei.value.code == "mode_changed"


@pytest.mark.asyncio
async def test_password_change_after_modal_opened(pg_pool):
    """Пароль сменился после открытия модалки → старый пароль
    не подходит."""
    team = _team("Bravo2", 3600, join_mode=JOIN_PASSWORD,
                 recruiting=True)
    team.set_password("old-pass")
    await team_store.create(team)
    team = team_store.get(team.team_id)
    team.set_password("new-pass")
    await team_store.update_team(team)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3601, G,
                                   via="password",
                                   password="old-pass")
    assert ei.value.code == "bad_password"
    joined = await team_store.join_team(
        team.team_id, 3601, G, via="password", password="new-pass")
    assert 3601 in joined.members


@pytest.mark.asyncio
async def test_failed_join_leaves_state_untouched(pg_pool):
    """Ошибка между вступлением и подтверждением (здесь —
    переполненная команда) → кэш и БД не меняются."""
    team = await team_store.create(
        _team("Charlie2", 3700, join_mode=JOIN_OPEN))
    for uid in (3701, 3702, 3703):
        await team_store.join_team(team.team_id, uid, G)
    before = list(team_store.get(team.team_id).members)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3704, G,
                                   via="open")
    assert ei.value.code == "team_full"
    assert team_store.get(team.team_id).members == before
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT members FROM teams WHERE team_id=$1",
            team.team_id)
        assert sorted(int(m) for m in row["members"]) == sorted(before)


@pytest.mark.asyncio
async def test_tournament_check_error_fails_closed(pg_pool,
                                                    monkeypatch):
    """Ошибка проверки активного турнира → операция запрещена
    (fail-closed), а не разрешена."""
    team = await team_store.create(
        _team("Delta2", 3800, join_mode=JOIN_OPEN))
    from storage.br_store import br_store

    def _boom():
        raise RuntimeError("br store down")
    monkeypatch.setattr(br_store, "all", _boom)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 3801, G, via="open")
    assert ei.value.code == "check_failed"
    assert 3801 not in team_store.get(team.team_id).members


@pytest.mark.asyncio
async def test_cashcup_registration_blocks_join(pg_pool):
    """Команда, заявленная в активный Cash Cup, не принимает
    новых игроков (проверка на том же соединении, без вложенных
    acquire)."""
    from services.cashcup_store import cashcup_store
    # _ready мог быть выставлен другим тестом против другого
    # pgserver-кластера — сбрасываем, чтобы схема создалась
    # в нашей тестовой БД.
    cashcup_store._ready = False
    await cashcup_store.ensure_schema()
    team = await team_store.create(
        _team("Echo2", 3900, join_mode=JOIN_OPEN))
    tid = "cctest1"
    import json as _json
    state = {"status": "running", "teams": [
        {"id": "ct1", "br_team_id": team.team_id,
         "members": [{"discord_id": 3900}]}]}
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cashcup_tournaments (id, guild_id, "
            "channel_id, status, creator_id, state) VALUES "
            "($1,$2,1,'running',$3,$4::jsonb)",
            tid, G, 3900, _json.dumps(state))
    try:
        with pytest.raises(TeamStoreError) as ei:
            await team_store.join_team(team.team_id, 3901, G,
                                       via="open")
        assert ei.value.code == "roster_locked"
        # Выход обычного участника тоже заблокирован
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE teams SET members = $2 WHERE team_id = $1",
                team.team_id, [3900, 3901])
        with pytest.raises(TeamStoreError) as ei2:
            await team_store.leave_team(team.team_id, 3901)
        assert ei2.value.code == "roster_locked"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cashcup_tournaments WHERE id=$1", tid)


@pytest.mark.asyncio
async def test_corrupt_cashcup_state_fails_closed(pg_pool):
    """Повреждённое state турнира → check_failed, а не пропуск
    проверки (fail-closed, не fail-open)."""
    from services.cashcup_store import cashcup_store
    cashcup_store._ready = False
    await cashcup_store.ensure_schema()
    team = await team_store.create(
        _team("Corrupt", 4000, join_mode=JOIN_OPEN))
    tid = "cctest-corrupt"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cashcup_tournaments (id, guild_id, "
            "channel_id, status, creator_id, state) VALUES "
            "($1,$2,1,'running',$3,'\"oops\"'::jsonb)",
            tid, G, 4000)
        # '"oops"' — валидный JSON, но не dict → fail-closed.
    try:
        with pytest.raises(TeamStoreError) as ei:
            await team_store.join_team(team.team_id, 4001, G,
                                       via="open")
        assert ei.value.code == "check_failed"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cashcup_tournaments WHERE id=$1", tid)


# ----------------------------------------------------------------------
# P0.6 — массовый сброс
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disband_many_atomic_and_fk(pg_pool):
    """Массовый сброс: одна транзакция, FK invites/applications
    обработаны, аудит записан, кэш очищен после COMMIT."""
    teams = []
    for i, name in enumerate(("MassA", "MassB")):
        t = await team_store.create(
            _team(name, 4000 + i, join_mode=JOIN_OPEN))
        teams.append(t)
        await squad_store.create_invite(G, t.team_id, 4000 + i,
                                        4100 + i)
        await squad_store.create_application(G, t.team_id, 4200 + i)
    snaps = [{"team_id": t.team_id, "updated_at": t.updated_at,
              "members": sorted(t.members)} for t in teams]
    result = await team_store.disband_many(G, snaps, 4999)
    removed = result["removed"]
    assert len(removed) == 2
    assert result["backup_path"]
    for t in teams:
        assert team_store.get(t.team_id) is None
        async with pg_pool.acquire() as conn:
            n_inv = await conn.fetchval(
                "SELECT count(*) FROM team_invites WHERE team_id=$1",
                t.team_id)
            n_app = await conn.fetchval(
                "SELECT count(*) FROM team_applications "
                "WHERE team_id=$1", t.team_id)
            assert n_inv == 0 and n_app == 0
            n_audit = await conn.fetchval(
                "SELECT count(*) FROM team_audit WHERE team_id=$1 "
                "AND action='admin_mass_disband'", t.team_id)
            assert n_audit == 1


@pytest.mark.asyncio
async def test_disband_many_rolls_back_on_change(pg_pool):
    """Состав изменился после предпросмотра → вся транзакция
    откатывается, НИ одна команда не удалена."""
    t1 = await team_store.create(
        _team("KeepA", 4300, join_mode=JOIN_OPEN))
    t2 = await team_store.create(
        _team("KeepB", 4301, join_mode=JOIN_OPEN))
    snaps = [{"team_id": t1.team_id, "updated_at": t1.updated_at,
              "members": sorted(t1.members)},
             {"team_id": t2.team_id, "updated_at": "1999-01-01",
              "members": sorted(t2.members)}]
    with pytest.raises(TeamStoreError) as ei:
        await team_store.disband_many(G, snaps, 4999)
    assert ei.value.code == "list_changed"
    # Обе команды на месте — откат полный
    assert team_store.get(t1.team_id) is not None
    assert team_store.get(t2.team_id) is not None
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM teams WHERE team_id IN ($1,$2)",
            t1.team_id, t2.team_id)
        assert n == 2


@pytest.mark.asyncio
async def test_disband_many_rejects_unknown_team(pg_pool):
    """Снимок с несуществующей командой → отказ без удалений."""
    t = await team_store.create(_team("Solo", 4400))
    snaps = [{"team_id": t.team_id, "updated_at": t.updated_at,
              "members": sorted(t.members)},
             {"team_id": "ghost-team", "updated_at": "",
              "members": []}]
    with pytest.raises(TeamStoreError) as ei:
        await team_store.disband_many(G, snaps, 4999)
    assert ei.value.code == "list_changed"
    assert team_store.get(t.team_id) is not None


# ----------------------------------------------------------------------
# P0.4 — guild-изоляция join
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_wrong_guild_rejected(pg_pool):
    team = await team_store.create(
        _team("Iso", 4500, join_mode=JOIN_OPEN))
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(team.team_id, 4501, G + 999,
                                   via="open")
    assert ei.value.code == "team_not_found"
    assert team_store.get(team.team_id).members == [4500]


# ----------------------------------------------------------------------
# Модель: сериализация
# ----------------------------------------------------------------------


def test_team_serialization_roundtrip():
    t = Team(team_id="t1", team_name="X", captain_id=1, guild_id=2,
             members=[1], join_mode=JOIN_OPEN, purpose=PURPOSE_CASHCUP,
             description="d", requirements={"min_sr": 7.0})
    d = t.to_dict()
    assert d["password"] == ""  # plaintext не сериализуем
    t2 = Team.from_dict(d)
    assert t2.team_name == "X"
    assert t2.join_mode == JOIN_OPEN
    assert t2.requirements == {"min_sr": 7.0}
    assert t2.members == [1]


# ----------------------------------------------------------------------
# Final blockers — in-transaction authorization (блокер №2)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_application_accept_requires_current_captain(pg_pool):
    """via='application': actor_id проверяется под FOR UPDATE —
    не-капитан и бывший капитан не могут принять заявку."""
    team = await team_store.create(
        _team("AuthA", 4600, join_mode=JOIN_APPLICATIONS))
    app = await squad_store.create_application(G, team.team_id, 4601)
    assert app is not None
    # Не-капитан → not_captain
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(
            team.team_id, 4601, G, via="application",
            application_id=app.application_id, actor_id=9999)
    assert ei.value.code == "not_captain"
    # Без actor_id → тоже отказ (fail-closed)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(
            team.team_id, 4601, G, via="application",
            application_id=app.application_id)
    assert ei.value.code == "not_captain"
    # Капитан передаёт капитанство участнику (вступление — по
    # приглашению: режим команды applications)
    inv = await squad_store.create_invite(G, team.team_id, 4600, 4602)
    await team_store.join_team(team.team_id, 4602, G, via="invite",
                               invite_id=inv.invite_id)
    await team_store.transfer_captain(team.team_id, 4602, 4600)
    # Бывший капитан (4600) больше не может принять заявку
    app2 = await squad_store.create_application(G, team.team_id, 4603)
    with pytest.raises(TeamStoreError) as ei:
        await team_store.join_team(
            team.team_id, 4603, G, via="application",
            application_id=app2.application_id, actor_id=4600)
    assert ei.value.code == "not_captain"
    # Новый капитан — может
    joined = await team_store.join_team(
        team.team_id, 4603, G, via="application",
        application_id=app2.application_id, actor_id=4602)
    assert 4603 in joined.members


@pytest.mark.asyncio
async def test_resolve_application_as_captain(pg_pool):
    """Отклонение заявки: капитанство под FOR UPDATE в той же tx."""
    team = await team_store.create(
        _team("AuthB", 4700, join_mode=JOIN_APPLICATIONS))
    app = await squad_store.create_application(G, team.team_id, 4701)
    assert app is not None
    # Посторонний не может отклонить
    assert not await squad_store.resolve_application_as_captain(
        app.application_id, 9999, "declined")
    assert (await squad_store.get_application(
        app.application_id)).status == "pending"
    # Капитан отклоняет
    assert await squad_store.resolve_application_as_captain(
        app.application_id, 4700, "declined")
    assert (await squad_store.get_application(
        app.application_id)).status == "declined"


@pytest.mark.asyncio
async def test_cancel_invite_as_captain(pg_pool):
    """Отзыв приглашения: только актуальный капитан команды."""
    team = await team_store.create(
        _team("AuthC", 4800, join_mode=JOIN_APPLICATIONS))
    inv = await squad_store.create_invite(
        G, team.team_id, 4800, 4801)
    assert inv is not None
    assert not await squad_store.cancel_invite_as_captain(
        inv.invite_id, 9999)
    assert (await squad_store.get_invite(inv.invite_id)).is_pending
    assert await squad_store.cancel_invite_as_captain(
        inv.invite_id, 4800)
    assert (await squad_store.get_invite(inv.invite_id)).status \
        == "cancelled"


@pytest.mark.asyncio
async def test_update_team_actor_gate(pg_pool):
    """update_team(actor_id): бывший капитан не меняет настройки."""
    team = await team_store.create(
        _team("AuthD", 4900, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 4901, G, via="open")
    await team_store.transfer_captain(team.team_id, 4901, 4900)
    team = team_store.get(team.team_id)
    team.description = "new desc"
    # Бывший капитан → not_captain
    with pytest.raises(TeamStoreError) as ei:
        await team_store.update_team(team, actor_id=4900)
    assert ei.value.code == "not_captain"
    # Новый капитан → ok
    assert await team_store.update_team(team, actor_id=4901)


@pytest.mark.asyncio
async def test_remove_member_actor_gate(pg_pool):
    """remove_member(actor_id): не-капитан не исключает игроков."""
    team = await team_store.create(
        _team("AuthE", 4950, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 4951, G, via="open")
    await team_store.join_team(team.team_id, 4952, G, via="open")
    with pytest.raises(TeamStoreError) as ei:
        await team_store.remove_member(
            team.team_id, 4951, actor_id=4952)
    assert ei.value.code == "not_captain"
    t = await team_store.remove_member(
        team.team_id, 4951, actor_id=4950)
    assert 4951 not in t.members


# ----------------------------------------------------------------------
# Final blockers — ff_uid_required (блокер №3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ff_uid_required_not_blocking(pg_pool):
    """ff_uid_required не проверяется: LfgProfile.ff_uid не
    существует, верифицированного источника нет — настройка
    скрыта и не должна блокировать вступление."""
    team = await team_store.create(_team(
        "FFReq", 5000, join_mode=JOIN_OPEN,
        requirements={"ff_uid_required": True}))
    joined = await team_store.join_team(team.team_id, 5001, G,
                                        via="open")
    assert 5001 in joined.members


# ----------------------------------------------------------------------
# Final blockers — согласованная блокировка (блокер №1)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_registration_lock_fresh_and_serialized(pg_pool):
    """team_registration_lock: актуальный снимок под FOR UPDATE;
    параллельный join ждёт блокировку и применяется после."""
    team = await team_store.create(
        _team("RegL", 5100, join_mode=JOIN_OPEN))
    async with team_store.team_registration_lock(
            team.team_id, G, 5100) as fresh:
        assert fresh.team_id == team.team_id
        assert fresh.captain_id == 5100
        # Параллельный join блокируется row-lock'ом — не должен
        # завершиться, пока мы держим регистрацию.
        join_done = asyncio.Event()

        async def _join():
            await team_store.join_team(team.team_id, 5101, G,
                                       via="open")
            join_done.set()

        task = asyncio.create_task(_join())
        await asyncio.sleep(0.3)
        assert not join_done.is_set()  # ждёт нашей блокировки
    await task
    assert join_done.is_set()
    assert 5101 in team_store.get(team.team_id).members


@pytest.mark.asyncio
async def test_registration_lock_rejects_ex_captain(pg_pool):
    """Регистрация под lock: бывший капитан отсекается."""
    team = await team_store.create(
        _team("RegX", 5200, join_mode=JOIN_OPEN))
    await team_store.join_team(team.team_id, 5201, G, via="open")
    await team_store.transfer_captain(team.team_id, 5201, 5200)
    with pytest.raises(TeamStoreError) as ei:
        async with team_store.team_registration_lock(
                team.team_id, G, 5200):
            pass
    assert ei.value.code == "not_captain"
    async with team_store.team_registration_lock(
            team.team_id, G, 5201) as fresh:
        assert fresh.captain_id == 5201


@pytest.mark.asyncio
async def test_locked_team_on_missing_and_wrong_guild(pg_pool):
    """locked_team_on: несуществующая команда / чужой guild →
    team_not_found (регистрация удалённой команды невозможна)."""
    team = await team_store.create(
        _team("RegY", 5300, join_mode=JOIN_OPEN))
    pool = await team_store._pg()
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(TeamStoreError) as ei:
                await team_store.locked_team_on(
                    conn, "no-such-team", guild_id=G,
                    actor_id=5300)
            assert ei.value.code == "team_not_found"
            with pytest.raises(TeamStoreError) as ei:
                await team_store.locked_team_on(
                    conn, team.team_id, guild_id=G + 1,
                    actor_id=5300)
            assert ei.value.code == "team_not_found"
            fresh = await team_store.locked_team_on(
                conn, team.team_id, guild_id=G, actor_id=5300)
            assert fresh.team_name == "RegY"


# ----------------------------------------------------------------------
# Final blockers — проверенный бэкап (блокер №5)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disband_many_backup_contents(pg_pool):
    """Бэкап формируется из заблокированных строк: teams +
    invites + applications, файл проверен."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path

    t = await team_store.create(
        _team("BakA", 5400, join_mode=JOIN_OPEN))
    await squad_store.create_invite(G, t.team_id, 5400, 5401)
    await squad_store.create_application(G, t.team_id, 5402)
    snaps = [{"team_id": t.team_id, "updated_at": t.updated_at,
              "members": sorted(t.members)}]
    result = await team_store.disband_many(G, snaps, 4999)
    bp = _Path(result["backup_path"])
    assert bp.exists()
    data = _json.loads(bp.read_text(encoding="utf-8"))
    assert data["guild_id"] == G
    assert data["team_ids"] == [t.team_id]
    rec = data["teams"][0]
    assert rec["team"]["team_id"] == t.team_id
    assert rec["team"]["team_name"] == "BakA"
    assert len(rec["invites"]) == 1
    assert len(rec["applications"]) == 1
    bp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_disband_many_backup_failure_aborts(pg_pool,
                                                 monkeypatch):
    """Невозможность прочитать/проверить бэкап → удаление
    отменяется, команды остаются."""
    t = await team_store.create(
        _team("BakB", 5500, join_mode=JOIN_OPEN))
    snaps = [{"team_id": t.team_id, "updated_at": t.updated_at,
              "members": sorted(t.members)}]
    from pathlib import Path as _P
    monkeypatch.setattr(
        _P, "read_bytes",
        lambda self: (_ for _ in ()).throw(OSError("read fail")))
    with pytest.raises(TeamStoreError) as ei:
        await team_store.disband_many(G, snaps, 4999)
    assert ei.value.code == "backup_failed"
    assert team_store.get(t.team_id) is not None
