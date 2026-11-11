"""P1: дедупликация update_tournament_message.

Проверяет:
- повторный апдейт без изменений НЕ делает message.edit;
- изменение контента → edit выполняется;
- кэшированный Message.edit без fetch_message;
- NotFound инвалидирует кэш;
- announce_phase_matches вызывается на каждый апдейт.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

# Корневой bot.py — точка входа (Dockerfile: python bot.py);
# пакет bot/ — legacy, поэтому грузим файл напрямую.
_spec = importlib.util.spec_from_file_location(
    "root_bot", Path(__file__).resolve().parent.parent / "bot.py"
)
bot_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot_module)


def _guild():
    return SimpleNamespace(id=1)


def _tournament():
    return SimpleNamespace(
        id="t1", guild_id=1, channel_id=2, message_id=42,
        phase="SETUP",
    )


class _FakeMessage:
    def __init__(self, channel):
        self.channel = channel
        self.edits = []
        self._n = 0

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self._n += 1
        return self


class _FakeChannel:
    def __init__(self, mid=42):
        self.id = 2
        self.fetches = 0
        self.message = _FakeMessage(self)
        self._mid = mid
        self.fail_next_edit = False

    async def fetch_message(self, mid):
        self.fetches += 1
        if mid != self._mid:
            raise bot_module.discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "msg"
            )
        return self.message


def _make_bot(monkeypatch, embed_desc="v1"):
    bot = object.__new__(bot_module.TournamentBot)
    bot._registered_view_keys = set()
    bot._register_view = lambda view: None
    bot.build_view_for_tournament = lambda t: SimpleNamespace(
        children=[SimpleNamespace(
            custom_id="btn", label="OK", disabled=False,
            style=None, emoji=None, content="", placeholder="",
            value="", title="", description="",
        )]
    )

    async def _embed(t, guild):
        return SimpleNamespace(
            to_dict=lambda: {"description": embed_desc, "title": "T"}
        )

    def _compose(embed, view):
        return SimpleNamespace(children=[])

    monkeypatch.setattr(bot_module, "build_embed_for_phase", _embed)
    monkeypatch.setattr(bot_module, "compose", _compose)
    return bot


@pytest.fixture(autouse=True)
def _clean_caches():
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    yield
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()


class TestUpdateDedup:
    @pytest.mark.asyncio
    async def test_second_identical_update_skips_edit(
        self, monkeypatch
    ):
        announces = []

        async def _announce(t, g, b):
            announces.append(1)

        import services.match_announcer as ma
        monkeypatch.setattr(ma, "announce_phase_matches", _announce)

        bot = _make_bot(monkeypatch)
        guild = _guild()
        channel = _FakeChannel()
        guild.get_channel = lambda cid: channel
        t = _tournament()

        await bot.update_tournament_message(guild, t)
        await bot.update_tournament_message(guild, t)
        await bot.update_tournament_message(guild, t)

        # 3 апдейта → 1 fetch + 1 edit (остальные — sig совпал).
        assert channel.fetches == 1
        assert len(channel.message.edits) == 1
        # Анонс — на каждый апдейт (идемпотентность внутри него).
        assert len(announces) == 3

    @pytest.mark.asyncio
    async def test_changed_content_triggers_edit(self, monkeypatch):
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)

        state = {"desc": "v1"}
        bot = object.__new__(bot_module.TournamentBot)
        bot._registered_view_keys = set()
        bot._register_view = lambda view: None
        bot.build_view_for_tournament = lambda t: SimpleNamespace(
            children=[]
        )

        async def _embed(t, guild):
            return SimpleNamespace(
                to_dict=lambda: {"description": state["desc"]}
            )

        monkeypatch.setattr(bot_module, "build_embed_for_phase", _embed)
        monkeypatch.setattr(
            bot_module, "compose", lambda e, v: SimpleNamespace(children=[])
        )

        guild = _guild()
        channel = _FakeChannel()
        guild.get_channel = lambda cid: channel
        t = _tournament()

        await bot.update_tournament_message(guild, t)
        assert len(channel.message.edits) == 1

        # Без изменений — пропуск.
        await bot.update_tournament_message(guild, t)
        assert len(channel.message.edits) == 1

        # Изменился контент (новый pick/фаза/статус) — edit снова.
        state["desc"] = "v2"
        await bot.update_tournament_message(guild, t)
        assert len(channel.message.edits) == 2
        # fetch_message не вызывался повторно — Message кэширован.
        assert channel.fetches == 1

    @pytest.mark.asyncio
    async def test_notfound_invalidates_cache(self, monkeypatch):
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)
        bot = _make_bot(monkeypatch)

        guild = _guild()
        channel = _FakeChannel()
        guild.get_channel = lambda cid: channel
        t = _tournament()

        await bot.update_tournament_message(guild, t)
        assert len(channel.message.edits) == 1

        # Сообщение «удалено» — edit бросает NotFound.
        async def _nf(**kw):
            raise bot_module.discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "msg"
            )

        channel.message.edit = _nf
        # sig изменился → попытка edit → NotFound → кэш очищен.
        # следующий вызов: fetch_message → NotFound → message=None.
        bot2_embed = "v2"
        # форсируем изменение sig через новый embed
        async def _embed2(t, guild):
            return SimpleNamespace(
                to_dict=lambda: {"description": bot2_embed}
            )

        monkeypatch.setattr(bot_module, "build_embed_for_phase", _embed2)
        await bot.update_tournament_message(guild, t)
        assert "1:2" not in bot_module._EDIT_CACHE
