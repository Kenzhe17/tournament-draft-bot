"""Голосование за формат матча (🇧🇷 Brazil / 🇰🇿 Kazakhstan).

Покрытие: prepare только для 8v8, права участников, один голос,
досрочное завершение при 8/8, ничья → случайный выбор, дедлайн,
format label в карточке, persistent custom_id.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.format_vote as fv


def _tournament(**kw):
    names = [f"P{i}" for i in range(1, 9)]
    t = SimpleNamespace(
        id="t1",
        guild_id=1,
        channel_id=2,
        mm_search_channel_id=555,
        teams=[
            {"players": names[0:4], "captain": "P1"},
            {"players": names[4:8], "captain": "P5"},
        ],
        player_user_ids={n: i + 1 for i, n in enumerate(names)},
        match_channels={
            "2": {
                "match_type": "final",
                "match_index": 0,
                "card_message_id": 100,
                "card_state": "active",
            }
        },
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


@pytest.fixture
def env(monkeypatch):
    t = _tournament()
    monkeypatch.setattr(fv.store, "get", lambda g, c: t)
    monkeypatch.setattr(fv.store, "set", lambda x: None)
    fv._LOCKS.clear()
    fv._TIMERS.clear()
    fv._UI_DIRTY.clear()
    fv._UI_TASKS.clear()
    fv._UI_CTX.clear()
    yield SimpleNamespace(tournament=t)
    for task in list(fv._UI_TASKS.values()):
        task.cancel()
    for task in list(fv._TIMERS.values()):
        task.cancel()
    fv._UI_DIRTY.clear()
    fv._UI_TASKS.clear()
    fv._UI_CTX.clear()
    fv._TIMERS.clear()


class _Resp:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kw):
        self.messages.append(str(content))

    async def defer(self, **kw):
        pass


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kw):
        self.messages.append(str(content))


class _Ix:
    def __init__(self, env, user_id, custom_id=None, message_id=None):
        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(id=user_id)
        self.data = {"custom_id": custom_id or ""}
        self.response = _Resp()
        self.followup = _Followup()
        self.channel = SimpleNamespace(id=2)
        self.message = (
            SimpleNamespace(id=message_id) if message_id else None
        )


def _cid(env, fmt="brazil", gid=1, cid=2):
    s = fv.get_vote_state(env.tournament, "final", 0) or {}
    return f"fv:{gid}:{cid}:{s.get('vote_id', 'vX')}:{fmt}"


def _cid_raw(gid, cid, vote_id, fmt="brazil"):
    return f"fv:{gid}:{cid}:{vote_id}:{fmt}"


def _state(env):
    return fv.get_vote_state(env.tournament, "final", 0)


class TestPrepare:
    def test_creates_state_for_8v8(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        assert s is not None
        assert s["vote_status"] == "open"
        assert s["vote_deadline"] > time.time()
        assert s["votes"] == {}
        assert s["selected_format"] is None

    def test_idempotent(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        assert len(env.tournament.match_channels) == 1

    def test_skips_non_8v8(self, env):
        env.tournament.teams[0]["players"] = env.tournament.teams[0]["players"][:2]
        env.tournament.teams[1]["players"] = env.tournament.teams[1]["players"][:2]
        assert fv.prepare_format_vote(
            env.tournament, "final", 0, 2
        ) is None
        assert _state(env) is None

    def test_format_label_open(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        assert fv.match_format_label(env.tournament, "final", 0) == "Голосование"


class TestVoteClick:
    @pytest.mark.asyncio
    async def test_participant_vote_recorded(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        _state(env)["vote_message_id"] = 5
        edits = []

        class _Msg:
            async def edit(self, view=None, **kw):
                edits.append(view)

        class _Ch:
            id = 2

            async def fetch_message(self, mid):
                return _Msg()

        class _G:
            def get_channel(self, cid):
                return _Ch()

            async def fetch_channel(self, cid):
                return _Ch()

        # UI-worker берёт guild через store_bot_guild (bot instance),
        # а не через interaction — подменяем его.
        monkeypatch.setattr(fv, "store_bot_guild", lambda gid: _G())
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, _cid(env, "kazakhstan"))
        await fv.handle_vote_click(ix, "kazakhstan")
        s = _state(env)
        assert s["votes"] == {"1": "kazakhstan"}
        assert ix.response.messages == ["✅ Голос учтён."]
        await asyncio.sleep(0.5)  # debounce UI-worker
        assert edits  # сообщение обновлено

    @pytest.mark.asyncio
    async def test_non_participant_denied(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        ix = _Ix(env, 999, _cid(env))
        await fv.handle_vote_click(ix, "brazil")
        assert "не являетесь участником" in ix.response.messages[0]
        assert _state(env)["votes"] == {}

    @pytest.mark.asyncio
    async def test_double_vote_denied(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        s = _state(env)
        s["votes"]["1"] = "brazil"
        s["vote_message_id"] = 0  # edit пропускается
        ix = _Ix(env, 1, _cid(env, "kazakhstan"))
        await fv.handle_vote_click(ix, "kazakhstan")
        assert "уже проголосовали" in ix.response.messages[0]
        assert s["votes"]["1"] == "brazil"  # голос не изменён

    @pytest.mark.asyncio
    async def test_expired_vote_finalizes(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_deadline"] = time.time() - 1
        finalized = []

        async def fake_fin(guild, t, mt, mi, vote_id=None):
            finalized.append((mt, mi, vote_id))
            s["vote_status"] = "done"
            s["selected_format"] = "brazil"
            return True

        monkeypatch.setattr(fv, "finalize_format_vote", fake_fin)
        ix = _Ix(env, 1, _cid(env))
        await fv.handle_vote_click(ix, "brazil")
        assert finalized == [("final", 0, s["vote_id"])]
        assert "завершено" in ix.response.messages[0]


class TestFinalize:
    @pytest.mark.asyncio
    async def test_winner_and_card_update(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 6)}
        s["votes"].update({str(i): "kazakhstan" for i in range(6, 9)})
        s["vote_message_id"] = 0  # без Discord edit
        card_updates = []

        async def fake_card(guild, t, mt, mi, state="active", **kw):
            card_updates.append(state)

        monkeypatch.setattr(
            "services.match_announcer.update_match_card", fake_card
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ok = await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert ok is True
        assert s["selected_format"] == "brazil"
        assert s["vote_status"] == "done"
        assert s.get("tie") is False
        assert fv.match_format_label(env.tournament, "final", 0) == "🇧🇷 Brazil"
        assert card_updates == ["active"]

    @pytest.mark.asyncio
    async def test_tie_random_with_reason(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 5)}
        s["votes"].update({str(i): "kazakhstan" for i in range(5, 9)})
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert s["tie"] is True

    @pytest.mark.asyncio
    async def test_zero_votes_random(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert s.get("tie") is False  # 0 голосов — без «равенства»

    @pytest.mark.asyncio
    async def test_idempotent_second_finalize(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        assert await fv.finalize_format_vote(
            None, env.tournament, "final", 0
        ) is True
        assert await fv.finalize_format_vote(
            None, env.tournament, "final", 0
        ) is False


class TestViewAndRestore:
    def test_open_view_has_two_enabled_buttons(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        view = fv.build_vote_view(env.tournament, "final", 0, s)
        btns = [
            c for c in view.walk_children()
            if isinstance(c, __import__("discord").ui.Button)
        ]
        assert len(btns) == 2
        assert all(not b.disabled for b in btns)
        assert all(b.custom_id.startswith("fv:1:2:") and ":final:" not in b.custom_id for b in btns)

    def test_done_view_buttons_disabled(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_status"] = "done"
        s["selected_format"] = "kazakhstan"
        view = fv.build_vote_view(env.tournament, "final", 0, s)
        btns = [
            c for c in view.walk_children()
            if isinstance(c, __import__("discord").ui.Button)
        ]
        assert all(b.disabled for b in btns)

    def test_cancel_marks_votes(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        fv.cancel_open_format_votes(env.tournament)
        assert _state(env)["vote_status"] == "cancelled"
        assert fv.match_format_label(env.tournament, "final", 0) == ""


async def _noop(*a, **k):
    return None


# ---------------------------------------------------------------------------
# Поколения голосований (vote_id), конкурентность, таймеры, restore/cancel
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


def _prep(env, monkeypatch, votes=None):
    fv.prepare_format_vote(env.tournament, "final", 0, 2)
    s = _state(env)
    s["vote_message_id"] = 0  # без Discord edit
    if votes:
        s["votes"] = dict(votes)
    monkeypatch.setattr(
        "services.format_vote._mirror_to_pg", lambda *a, **k: _noop()
    )
    monkeypatch.setattr(
        "services.match_announcer.update_match_card",
        lambda *a, **k: _noop(),
    )
    return s


class TestVoteGenerations:
    @pytest.mark.asyncio
    async def test_eight_concurrent_votes_finalize_no_deadlock(
        self, env, monkeypatch
    ):
        s = _prep(env, monkeypatch)
        fmts = ["brazil", "kazakhstan"]
        ixs = [
            _Ix(env, i, _cid(env, fmts[i % 2])) for i in range(1, 9)
        ]
        await asyncio.wait_for(
            asyncio.gather(*[
                fv.handle_vote_click(ix, fmts[i % 2])
                for i, ix in enumerate(ixs)
            ]),
            timeout=10,
        )
        assert s["vote_status"] == "done"
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert len(s["votes"]) == 8  # 9-й голос не принимается

    @pytest.mark.asyncio
    async def test_stale_custom_id_rejected_after_channel_reuse(
        self, env, monkeypatch
    ):
        """Старое голосование не влияет на новый матч в том же канале."""
        s1 = _prep(env, monkeypatch)
        old_vote_id = s1["vote_id"]
        old_match_id = s1["match_id"]

        # Новый матч в ТОМ ЖЕ канале (pool reuse): новый турнир,
        # store.get отдаёт его — старый fv недоступен.
        t2 = _tournament(id="t2")
        monkeypatch.setattr(fv.store, "get", lambda g, c: t2)
        env2 = SimpleNamespace(tournament=t2)
        fv.prepare_format_vote(t2, "final", 0, 2)
        s2 = fv.get_vote_state(t2, "final", 0)
        assert s2["vote_id"] != old_vote_id

        ix = _Ix(env2, 1, _cid_raw(1, 2, old_vote_id, "brazil"))
        await fv.handle_vote_click(ix, "brazil")
        assert s2["votes"] == {}
        assert "не найдено" in ix.response.messages[0].lower()

        # Актуальная кнопка нового поколения работает.
        ix2 = _Ix(env2, 2, _cid_raw(1, 2, s2["vote_id"], "kazakhstan"))
        await fv.handle_vote_click(ix2, "kazakhstan")
        assert s2["votes"] == {"2": "kazakhstan"}

    @pytest.mark.asyncio
    async def test_legacy_custom_id_still_works(self, env, monkeypatch):
        """Кнопки, опубликованные до vote_id, продолжают работать:
        resolve по (match_type, match_index) + миграция поколения."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 0  # message-id check пропускается
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, "fv:1:2:final:0:brazil")
        await fv.handle_vote_click(ix, "brazil")
        assert s["votes"] == {"1": "brazil"}
        assert s.get("vote_id")  # поколение присвоено миграцией

    @pytest.mark.asyncio
    async def test_legacy_custom_id_stale_message_rejected(
        self, env
    ):
        """Legacy-кнопка на СТАРОМ сообщении (vote_message_id не
        совпадает) отклоняется — защита от канала, переиспользо-
        ванного новым матчем."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 555  # актуальное сообщение
        ix = _Ix(env, 1, "fv:1:2:final:0:brazil", message_id=999)
        await fv.handle_vote_click(ix, "brazil")
        assert s["votes"] == {}
        assert "устарело" in ix.response.messages[0]

    @pytest.mark.asyncio
    async def test_new_format_stale_message_rejected(self, env):
        """Кнопка нового формата с чужим message_id тоже
        отклоняется (защита от пересозданных view)."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 555
        ix = _Ix(env, 1, _cid(env), message_id=999)
        await fv.handle_vote_click(ix, "brazil")
        assert s["votes"] == {}
        assert "устарело" in ix.response.messages[0]

    @pytest.mark.asyncio
    async def test_two_matches_different_channels_independent(
        self, env, monkeypatch
    ):
        t1 = env.tournament                       # channel_id=2
        t2 = _tournament(id="t2", channel_id=3)
        t2.match_channels = {
            "3": {
                "match_type": "final", "match_index": 0,
                "card_message_id": 200, "card_state": "active",
            }
        }
        t2.teams = [
            {"players": [f"Q{i}" for i in range(1, 5)]},
            {"players": [f"Q{i}" for i in range(5, 9)]},
        ]
        t2.player_user_ids = {f"Q{i}": 100 + i for i in range(1, 9)}
        monkeypatch.setattr(
            fv.store, "get",
            lambda g, c: t1 if c == 2 else (t2 if c == 3 else None),
        )
        fv.prepare_format_vote(t1, "final", 0, 2)
        fv.prepare_format_vote(t2, "final", 0, 3)
        s1 = fv.get_vote_state(t1, "final", 0)
        s2 = fv.get_vote_state(t2, "final", 0)
        assert s1["vote_id"] != s2["vote_id"]

        # Клик по голосованию канала 3 пишет только в t2.
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg", lambda *a, **k: _noop()
        )
        s1["vote_message_id"] = 0
        s2["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env2 := SimpleNamespace(tournament=t2), 101,
                 _cid_raw(1, 3, s2["vote_id"], "brazil"))
        ix.guild = SimpleNamespace(id=1)
        await fv.handle_vote_click(ix, "brazil")
        assert s2["votes"] == {"101": "brazil"}
        assert s1["votes"] == {}

    @pytest.mark.asyncio
    async def test_five_votes_finalize_by_timer(self, env, monkeypatch):
        s = _prep(
            env, monkeypatch,
            votes={str(i): "brazil" for i in range(1, 6)},
        )
        s["vote_deadline"] = time.time() + 0.3
        fv._schedule_timer(1, 2, "final", 0, fv=s)
        key = f"vote:{s['vote_id']}"
        assert key in fv._TIMERS
        await asyncio.sleep(1.6)
        assert s["vote_status"] == "done"
        assert s["selected_format"] == "brazil"
        assert key not in fv._TIMERS  # таймер/lock почищены

    @pytest.mark.asyncio
    async def test_timers_of_two_votes_independent(
        self, env, monkeypatch
    ):
        s = _prep(env, monkeypatch)
        t2 = _tournament(id="t2", channel_id=3)
        t2.match_channels = {
            "3": {"match_type": "final", "match_index": 0}
        }
        monkeypatch.setattr(fv.store, "get", lambda g, c: env.tournament)
        fv.prepare_format_vote(t2, "final", 0, 3)
        s2 = fv.get_vote_state(t2, "final", 0)
        fv._schedule_timer(1, 2, "final", 0, fv=s)
        fv._schedule_timer(1, 3, "final", 0, fv=s2)
        assert f"vote:{s['vote_id']}" in fv._TIMERS
        assert f"vote:{s2['vote_id']}" in fv._TIMERS
        for task in list(fv._TIMERS.values()):
            task.cancel()
        fv._TIMERS.clear()

    @pytest.mark.asyncio
    async def test_restore_registers_per_vote_id(self, env, monkeypatch):
        s = _prep(env, monkeypatch)
        s["vote_deadline"] = time.time() + 60
        added = []

        class _Bot:
            def add_view(self, v):
                added.append(v)

            def get_guild(self, gid):
                return None

        monkeypatch.setattr(fv.store, "all", lambda: [env.tournament])
        await fv.restore_format_votes(_Bot())
        assert len(added) == 1
        cids = [b.custom_id for b in added[0].children]
        assert all(s["vote_id"] in c for c in cids)
        assert f"vote:{s['vote_id']}" in fv._TIMERS
        for task in list(fv._TIMERS.values()):
            task.cancel()
        fv._TIMERS.clear()

    @pytest.mark.asyncio
    async def test_restore_rerenders_done_vote_ui(
        self, env, monkeypatch
    ):
        """После restart done-голосование с устаревшей карточкой
        получает UI-обновление до финального вида."""
        s = _prep(env, monkeypatch)
        s["vote_status"] = "done"
        s["selected_format"] = "brazil"
        s["vote_message_id"] = 5
        s["vote_deadline"] = time.time() - 10

        class _Bot:
            def add_view(self, v):
                pass

            def get_guild(self, gid):
                return None

        monkeypatch.setattr(fv.store, "all", lambda: [env.tournament])
        await fv.restore_format_votes(_Bot())
        assert f"vote:{s['vote_id']}" in fv._UI_DIRTY

    @pytest.mark.asyncio
    async def test_cancel_cleans_timer_and_lock(self, env, monkeypatch):
        s = _prep(env, monkeypatch)
        key = f"vote:{s['vote_id']}"
        fv._TIMERS[key] = asyncio.create_task(asyncio.sleep(60))
        fv._LOCKS[key] = asyncio.Lock()
        fv.cancel_open_format_votes(env.tournament)
        assert s["vote_status"] == "cancelled"
        assert key not in fv._TIMERS
        # Lock-объект намеренно сохраняется: удаление при
        # ожидающих захвата создало бы второй lock для ключа.
        # UI-обновление (отключение кнопок) поставлено в очередь.
        assert key in fv._UI_DIRTY


# ---------------------------------------------------------------------------
# UI-worker: сериализация PATCH, актуальное состояние на flush
# ---------------------------------------------------------------------------

class TestUiWorker:
    @pytest.mark.asyncio
    async def test_delayed_patch_renders_latest_state(
        self, env, monkeypatch
    ):
        """PATCH, поставленный до финализации, не перезаписывает
        финальный вид: worker перечитывает состояние на flush —
        после done рендерится только финальная карточка."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 5
        s["vote_deadline"] = time.time() + 60
        edits = []

        class _Msg:
            async def edit(self, view=None, **kw):
                edits.append(view)

        class _Ch:
            id = 2

            async def fetch_message(self, mid):
                return _Msg()

        class _G:
            def get_channel(self, cid):
                return _Ch()

            async def fetch_channel(self, cid):
                return _Ch()

        monkeypatch.setattr(fv, "store_bot_guild", lambda gid: _G())
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, _cid(env, "brazil"))
        await fv.handle_vote_click(ix, "brazil")
        # Финализация ДО flush UI-worker: отложенный PATCH
        # открытого вида не должен победить.
        await fv.finalize_format_vote(
            None, env.tournament, "final", 0,
            vote_id=s["vote_id"],
        )
        await asyncio.sleep(0.6)
        assert edits, "UI-worker не отредактировал сообщение"
        import discord as _d

        def _btns(view):
            return [
                c for c in view.walk_children()
                if isinstance(c, _d.ui.Button)
            ]

        last_btns = _btns(edits[-1])
        assert all(b.disabled for b in last_btns)
        # Настоящая проверка порядка: после первого edit с
        # финальным (disabled) видом ни один PATCH не должен
        # показать снова открытые кнопки.
        seen_final = False
        for v in edits:
            btns = _btns(v)
            if all(b.disabled for b in btns):
                seen_final = True
            else:
                assert not seen_final, (
                    "открытый вид отрендерен после финального"
                )
        assert seen_final

    @pytest.mark.asyncio
    async def test_ui_updates_coalesced(self, env, monkeypatch):
        """Серия быстрых голосов → одно обновление с актуальным
        состоянием, а не PATCH на каждый клик."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 5
        edits = []

        class _Msg:
            async def edit(self, view=None, **kw):
                edits.append(view)

        class _Ch:
            id = 2

            async def fetch_message(self, mid):
                return _Msg()

        class _G:
            def get_channel(self, cid):
                return _Ch()

            async def fetch_channel(self, cid):
                return _Ch()

        monkeypatch.setattr(fv, "store_bot_guild", lambda gid: _G())
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        for uid in (1, 2, 3):
            ix = _Ix(env, uid, _cid(env, "brazil" if uid % 2 else "kazakhstan"))
            await fv.handle_vote_click(ix, "brazil" if uid % 2 else "kazakhstan")
        await asyncio.sleep(0.6)
        assert len(edits) <= 2  # debounce схлопывает серию кликов
        assert len(s["votes"]) == 3

    @pytest.mark.asyncio
    async def test_ui_worker_retries_transient_errors(
        self, env, monkeypatch
    ):
        """Временные ошибки Discord (429/5xx) → retry с backoff,
        сообщение в итоге обновлено."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 5
        attempts = {"n": 0}
        edits = []

        class _Msg:
            async def edit(self, view=None, **kw):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise RuntimeError("HTTP 500")
                edits.append(view)

        class _Ch:
            id = 2

            async def fetch_message(self, mid):
                return _Msg()

        class _G:
            def get_channel(self, cid):
                return _Ch()

            async def fetch_channel(self, cid):
                return _Ch()

        monkeypatch.setattr(fv, "store_bot_guild", lambda gid: _G())
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, _cid(env, "brazil"))
        await fv.handle_vote_click(ix, "brazil")
        await asyncio.sleep(2.5)  # debounce + retry backoff
        assert attempts["n"] >= 3
        assert edits


# ---------------------------------------------------------------------------
# Таймер: привязка к поколению, finalize vote_id guard
# ---------------------------------------------------------------------------

class TestVoteGenerationIsolation:
    @pytest.mark.asyncio
    async def test_old_timer_does_not_finalize_new_vote(
        self, env, monkeypatch
    ):
        """Таймер старого поколения в переиспользованном канале
        не должен финализировать новое голосование."""
        s_old = _prep(env, monkeypatch)
        s_old["vote_deadline"] = time.time() + 0.2
        fv._schedule_timer(1, 2, "final", 0, fv=s_old)
        old_key = f"vote:{s_old['vote_id']}"
        assert old_key in fv._TIMERS

        # Канал переиспользован: store.get отдаёт НОВЫЙ турнир
        # с новым fv (другой vote_id, далёкий deadline).
        t2 = _tournament(id="t2")
        fv.prepare_format_vote(t2, "final", 0, 2)
        s_new = fv.get_vote_state(t2, "final", 0)
        s_new["vote_deadline"] = time.time() + 60
        s_new["vote_message_id"] = 0
        monkeypatch.setattr(fv.store, "get", lambda g, c: t2)

        await asyncio.sleep(0.8)
        # Старый таймер вышел по orphan, новый fv не тронут.
        assert s_new["vote_status"] == "open"
        assert s_new["votes"] == {}
        assert old_key not in fv._TIMERS

    @pytest.mark.asyncio
    async def test_finalize_rejects_wrong_vote_id(
        self, env, monkeypatch
    ):
        """finalize с чужим vote_id не трогает fv."""
        s = _prep(env, monkeypatch)
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        ok = await fv.finalize_format_vote(
            None, env.tournament, "final", 0, vote_id="wrong_id"
        )
        assert ok is False
        assert s["vote_status"] == "open"

    @pytest.mark.asyncio
    async def test_finalize_with_correct_vote_id(
        self, env, monkeypatch
    ):
        s = _prep(env, monkeypatch)
        ok = await fv.finalize_format_vote(
            None, env.tournament, "final", 0,
            vote_id=s["vote_id"],
        )
        assert ok is True
        assert s["vote_status"] == "done"


# ---------------------------------------------------------------------------
# PostgreSQL CAS: монотонные версии format_votes
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    pgdata = tmp_path_factory.mktemp("pgdata_fvote")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri, monkeypatch):
    import asyncpg
    import storage.db as dbmod
    import config as cfg

    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    monkeypatch.setattr(cfg, "DATABASE_URL", pg_uri)
    dbmod._pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TABLE IF EXISTS format_votes"
            )
        from storage import format_vote_store as _fvs
        _fvs._TABLE_READY = False
        yield pool
    finally:
        dbmod._pool = None
        await pool.close()


def _rec(status="open", version=1, votes=None, **kw):
    r = {
        "match_id": "t1_final_0",
        "match_type": "final",
        "match_index": 0,
        "vote_message_id": 5,
        "vote_channel_id": 2,
        "vote_deadline": time.time() + 60,
        "votes": votes or {},
        "selected_format": None,
        "vote_status": status,
        "tie": False,
        "version": version,
    }
    r.update(kw)
    return r


class TestFormatVoteStoreCas:
    @pytest.mark.asyncio
    async def test_stale_open_cannot_overwrite_done(self, pg_pool):
        """Запоздалая запись 'open' (меньшая version) не должна
        перезаписать 'done'."""
        from storage import format_vote_store as fvs

        await fvs.save_vote(1, 2, _rec(status="done", version=5,
                                      selected_format="brazil"))
        # Запоздалая open-запись с version=3 приходит позже.
        await fvs.save_vote(1, 2, _rec(status="open", version=3,
                                      votes={"1": "kazakhstan"}))
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT vote_status, selected_format, version"
                " FROM format_votes WHERE match_id='t1_final_0'"
            )
        assert row["vote_status"] == "done"
        assert row["selected_format"] == "brazil"
        assert row["version"] == 5

    @pytest.mark.asyncio
    async def test_newer_version_wins(self, pg_pool):
        from storage import format_vote_store as fvs

        await fvs.save_vote(1, 2, _rec(status="open", version=2,
                                      votes={"1": "brazil"}))
        await fvs.save_vote(1, 2, _rec(status="done", version=3,
                                      selected_format="kazakhstan"))
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT vote_status, version FROM format_votes"
                " WHERE match_id='t1_final_0'"
            )
        assert row["vote_status"] == "done"
        assert row["version"] == 3

    @pytest.mark.asyncio
    async def test_out_of_order_open_votes_latest_wins(
        self, pg_pool
    ):
        """Два open-снимка: более новый (version выше) побеждает
        даже при обратном порядке прибытия."""
        from storage import format_vote_store as fvs

        await fvs.save_vote(1, 2, _rec(version=4,
                                      votes={"1": "brazil",
                                             "2": "kazakhstan"}))
        await fvs.save_vote(1, 2, _rec(version=3, votes={"1": "brazil"}))
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT votes, version FROM format_votes"
                " WHERE match_id='t1_final_0'"
            )
        assert row["version"] == 4
        assert len(json.loads(row["votes"])) == 2
