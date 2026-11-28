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
    team = await team_store.create(_team("India", 1500))
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
    team = await team_store.create(_team("Juliett", 1600))
    await team_store.join_team(team.team_id, 1601, G)
    # Запрос от капитана
    await team_store.transfer_captain(team.team_id, 1601, actor_id=1600)
    # Стоп — transfer_captain сразу меняет; confirm-путь отдельно
    t = team_store.get(team.team_id)
    assert t.captain_id == 1601


@pytest.mark.asyncio
async def test_confirm_captain_transfer(pg_pool):
    team = await team_store.create(_team("Kilo", 1700))
    await team_store.join_team(team.team_id, 1701, G)
    await team_store.confirm_captain_transfer(team.team_id, 1701)
    assert team_store.get(team.team_id).captain_id == 1701
    # Не-участник не может
    with pytest.raises(TeamStoreError):
        await team_store.confirm_captain_transfer(team.team_id, 1799)


@pytest.mark.asyncio
async def test_disband(pg_pool):
    team = await team_store.create(_team("Lima", 1800))
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
    team = await team_store.create(_team("Mike", 1900))
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
    # Принятие на уровне store: вступление + resolve
    await team_store.join_team(team.team_id, 2201, G)
    ok = await squad_store.resolve_invite(inv.invite_id, "accepted")
    assert ok
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
    # Принятие заявки капитаном: join + resolve
    await team_store.join_team(team.team_id, 2401, G)
    assert await squad_store.resolve_application(app2.application_id,
                                                 "accepted")
    # После вступления остальные заявки закрыты вручную
    n = await squad_store.cancel_applications_for_user(
        G, 2401, "joined_team")
    assert n >= 1
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
    team = await team_store.create(_team("Victor", 2900))
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
