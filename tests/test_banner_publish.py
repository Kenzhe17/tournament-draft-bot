"""Тесты публикации дневного баннера: идемпотентность, рестарт,
пустой день, права Discord. Без сети и без БД — фейки."""

from __future__ import annotations

from datetime import date

import pytest

import services.banner.publisher as publisher
from services.banner import activity as act
from services.banner.publisher import (
    publish_day_banner, yesterday_almaty,
)

DAY = date(2026, 11, 13)


# ---------------------------------------------------------------------------
# Фейки
# ---------------------------------------------------------------------------

class FakePerms:
    def __init__(self, manage_guild: bool = True):
        self.manage_guild = manage_guild


class FakeMe:
    def __init__(self, manage_guild: bool = True):
        self.guild_permissions = FakePerms(manage_guild)


class FakeAvatar:
    async def read(self) -> bytes:
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (120, 60, 200, 255)).save(buf, "PNG")
        return buf.getvalue()


class FakeMember:
    def __init__(self, uid: int, name: str = "User"):
        self.id = uid
        self.display_name = name
        self.bot = False
        self.display_avatar = FakeAvatar()


class FakeGuild:
    def __init__(self, gid: int = 1, manage_guild: bool = True):
        self.id = gid
        self.me = FakeMe(manage_guild)
        self.banner = None
        self.banner_edits = 0
        self.voice_channels = []
        self._members: dict[int, FakeMember] = {}

    def get_member(self, uid: int):
        return self._members.get(uid)

    async def fetch_member(self, uid: int):
        return self._members.get(uid)

    async def edit(self, **kwargs):
        if "banner" in kwargs:
            self.banner = kwargs["banner"]
            self.banner_edits += 1


class FakeBannerStore:
    """In-memory реализация контракта storage.banner_store."""

    def __init__(self):
        self.voice: dict[tuple[int, int, date], int] = {}
        self.updates: dict[tuple[int, date], dict] = {}
        self.auto: dict[int, bool] = {}
        self.day_stats: list = []
        self.batch_calls = 0

    # voice
    async def get_voice_day_map(self, guild_id, day):
        return {
            uid: min(sec, act.MAX_VOICE_SECONDS)
            for (g, uid, d), sec in self.voice.items()
            if g == guild_id and d == day
        }

    async def add_voice_seconds_batch(self, guild_id, day, rows):
        self.batch_calls += 1
        for uid, sec in rows:
            key = (guild_id, uid, day)
            self.voice[key] = self.voice.get(key, 0) + sec

    # matches / stats
    async def get_match_counts(self, guild_id, start, end):
        return {}

    async def save_day_stats(self, guild_id, day, rows):
        self.day_stats.append((guild_id, day, list(rows)))

    # updates
    async def get_update(self, guild_id, day):
        row = self.updates.get((guild_id, day))
        return dict(row) if row else None

    async def record_update(self, guild_id, day, status,
                            winner_id=None, detail=None, published=False):
        self.updates[(guild_id, day)] = {
            "winner_id": winner_id, "status": status, "detail": detail,
        }

    # settings
    async def is_auto_enabled(self, guild_id):
        return self.auto.get(guild_id, True)

    async def set_auto_enabled(self, guild_id, enabled):
        self.auto[guild_id] = enabled


@pytest.fixture
def store(monkeypatch):
    fake = FakeBannerStore()
    monkeypatch.setattr(publisher, "banner_store", fake)
    # cogs.banner импортирует banner_store отдельно — патчим обе точки
    import cogs.banner as banner_cog
    monkeypatch.setattr(banner_cog, "banner_store", fake)
    monkeypatch.setattr(banner_cog, "DB_ENABLED", True)
    return fake


@pytest.fixture
def fast_render(monkeypatch):
    """Не гонять Pillow в каждом тесте — подменяем рендер на PNG-заглушку."""
    async def _fake(**kwargs):
        return b"\x89PNG\r\n\x1a\nFAKE"
    monkeypatch.setattr(publisher, "render_banner_png", _fake)


# ---------------------------------------------------------------------------
# publish_day_banner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_writes_banner_once(store, fast_render):
    guild = FakeGuild()
    guild._members[42] = FakeMember(42, "Winner")
    store.voice[(1, 42, DAY)] = 3 * 3600  # 36 очков

    r1 = await publish_day_banner(guild, DAY)
    assert r1["status"] == "published"
    assert r1["winner_id"] == 42
    assert guild.banner == b"\x89PNG\r\n\x1a\nFAKE"
    assert guild.banner_edits == 1

    # повторный запуск (рестарт) — не публикуем второй раз
    r2 = await publish_day_banner(guild, DAY)
    assert r2["status"] == "already_published"
    assert guild.banner_edits == 1


@pytest.mark.asyncio
async def test_publish_skips_when_banner_deleted(store, fast_render):
    """Если баннер удалили вручную — статус published, но баннера нет
    на сервере → публикуем повторно."""
    guild = FakeGuild()
    store.updates[(1, DAY)] = {"status": "published", "winner_id": 1}
    guild.banner = None

    r = await publish_day_banner(guild, DAY)
    assert r["status"] == "published"
    assert guild.banner_edits == 1


@pytest.mark.asyncio
async def test_publish_no_activity_uses_empty_variant(store, fast_render):
    guild = FakeGuild()
    r = await publish_day_banner(guild, DAY)
    assert r["status"] == "published"
    assert r["has_activity"] is False
    assert r["winner_id"] is None
    assert guild.banner is not None


@pytest.mark.asyncio
async def test_publish_no_manage_permission(store, fast_render):
    guild = FakeGuild(manage_guild=False)
    store.voice[(1, 42, DAY)] = 3600
    r = await publish_day_banner(guild, DAY)
    assert r["status"] == "failed"
    assert r["reason"] == "no_permission"
    assert guild.banner is None
    assert guild.banner_edits == 0
    # ошибка зафиксирована в журнале
    assert store.updates[(1, DAY)]["status"] == "failed"


@pytest.mark.asyncio
async def test_publish_render_failure_keeps_banner(store, monkeypatch):
    guild = FakeGuild()
    guild.banner = b"OLD_BANNER"
    store.voice[(1, 42, DAY)] = 3600

    async def _boom(**kwargs):
        raise RuntimeError("render exploded")
    monkeypatch.setattr(publisher, "render_banner_png", _boom)

    r = await publish_day_banner(guild, DAY)
    assert r["status"] == "failed"
    assert "render" in r["reason"]
    assert guild.banner == b"OLD_BANNER"  # текущий баннер не тронут


@pytest.mark.asyncio
async def test_force_republishes(store, fast_render):
    guild = FakeGuild()
    await publish_day_banner(guild, DAY)
    assert guild.banner_edits == 1
    r = await publish_day_banner(guild, DAY, force=True)
    assert r["status"] == "published"
    assert guild.banner_edits == 2


# ---------------------------------------------------------------------------
# maybe_publish / автодогон
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_publish_skips_published(store, fast_render):
    from cogs.banner import BannerCog

    class FakeBot:
        guilds = []

    cog = BannerCog(FakeBot())
    guild = FakeGuild()
    await publish_day_banner(guild, DAY)

    # уже опубликовано за вчера → None, без повторной публикации
    store.updates[(guild.id, yesterday_almaty())] = {
        "status": "published"}
    # подменяем день: maybe_publish работает с yesterday_almaty(),
    # поэтому кладём запись именно на вчера
    res = await cog.maybe_publish(guild)
    assert res is None
    assert guild.banner_edits == 1


@pytest.mark.asyncio
async def test_maybe_publish_auto_disabled(store, fast_render):
    from cogs.banner import BannerCog

    class FakeBot:
        guilds = []

    cog = BannerCog(FakeBot())
    guild = FakeGuild()
    store.auto[guild.id] = False
    res = await cog.maybe_publish(guild)
    assert res == {"status": "auto_disabled"}
    assert guild.banner_edits == 0


# ---------------------------------------------------------------------------
# Сканер голоса
# ---------------------------------------------------------------------------

class _Voice:
    def __init__(self, self_deaf=False, deaf=False):
        self.self_deaf = self_deaf
        self.deaf = deaf


class _Member:
    def __init__(self, uid, bot=False, self_deaf=False, deaf=False):
        self.id = uid
        self.bot = bot
        self.voice = _Voice(self_deaf, deaf)


class _Channel:
    def __init__(self, afk=False, members=()):
        self.afk = afk
        self.members = list(members)


class _Guild:
    def __init__(self, gid, channels):
        self.id = gid
        self.voice_channels = channels


class _Bot:
    def __init__(self, guilds):
        self.guilds = guilds


@pytest.mark.asyncio
async def test_voice_scan_credits_two_plus_humans(store, monkeypatch):
    import cogs.banner as banner_mod
    from cogs.banner import BannerCog

    monkeypatch.setattr(banner_mod, "banner_store", store)
    monkeypatch.setattr(banner_mod.act, "today_almaty",
                        lambda now=None: DAY)

    ch = _Channel(members=[_Member(1), _Member(2), _Member(3, bot=True)])
    bot = _Bot([_Guild(7, [ch])])
    cog = BannerCog(bot)

    await cog.scan_voice_once()
    # бот не считается → канал 'живой' (2 человека), все получают +60с
    assert store.voice[(7, 1, DAY)] == 60
    assert store.voice[(7, 2, DAY)] == 60
    assert (7, 3, DAY) not in store.voice
    assert store.batch_calls == 1  # один batch-запрос на гильдию

    # повторный проход — ещё +60, не двойное начисление
    await cog.scan_voice_once()
    assert store.voice[(7, 1, DAY)] == 120


@pytest.mark.asyncio
async def test_voice_scan_skips_lone_afk_deaf(store, monkeypatch):
    import cogs.banner as banner_mod
    from cogs.banner import BannerCog

    monkeypatch.setattr(banner_mod, "banner_store", store)
    monkeypatch.setattr(banner_mod.act, "today_almaty",
                        lambda now=None: DAY)

    channels = [
        _Channel(members=[_Member(1)]),                    # один — мимо
        _Channel(afk=True, members=[_Member(2), _Member(3)]),  # AFK-канал
        _Channel(members=[_Member(4, self_deaf=True),
                          _Member(5, deaf=True)]),          # все заглушены
        _Channel(members=[_Member(6), _Member(7, self_deaf=True)]),
    ]
    # в последнем канале живой только один (6) → тоже мимо
    bot = _Bot([_Guild(9, channels)])
    cog = BannerCog(bot)
    await cog.scan_voice_once()
    assert not store.voice
    assert store.batch_calls == 0


@pytest.mark.asyncio
async def test_voice_scan_no_double_after_restart(store, monkeypatch):
    """Рестарт бота: новый Cog сидируется из БД — уже зачтённые
    секунды не начисляются повторно."""
    import cogs.banner as banner_mod
    from cogs.banner import BannerCog

    monkeypatch.setattr(banner_mod, "banner_store", store)
    monkeypatch.setattr(banner_mod.act, "today_almaty",
                        lambda now=None: DAY)

    ch = _Channel(members=[_Member(1), _Member(2)])
    guild = _Guild(5, [ch])

    cog1 = BannerCog(_Bot([guild]))
    await cog1.scan_voice_once()   # +60 в БД
    assert store.voice[(5, 1, DAY)] == 60

    # «рестарт»: новый cog с пустым кэшем, тот же store
    cog2 = BannerCog(_Bot([guild]))
    await cog2.scan_voice_once()   # читает 60 из БД, добавляет ещё 60
    assert store.voice[(5, 1, DAY)] == 120  # а не 180


@pytest.mark.asyncio
async def test_voice_scan_caps_at_six_hours(store, monkeypatch):
    import cogs.banner as banner_mod
    from cogs.banner import BannerCog
    from services.banner import activity as act

    monkeypatch.setattr(banner_mod, "banner_store", store)
    monkeypatch.setattr(banner_mod.act, "today_almaty",
                        lambda now=None: DAY)

    ch = _Channel(members=[_Member(1), _Member(2)])
    guild = _Guild(5, [ch])
    # уже почти всё выбрали за день
    store.voice[(5, 1, DAY)] = act.MAX_VOICE_SECONDS - 30
    store.voice[(5, 2, DAY)] = act.MAX_VOICE_SECONDS

    cog = BannerCog(_Bot([guild]))
    await cog.scan_voice_once()
    # у 1: +30 до капа; у 2: 0 (delta не пишется)
    assert store.voice[(5, 1, DAY)] == act.MAX_VOICE_SECONDS
    assert store.voice[(5, 2, DAY)] == act.MAX_VOICE_SECONDS
