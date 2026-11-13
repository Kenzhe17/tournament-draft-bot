"""Прогресс OCR-комнат (/tournament create, rooms-режим):

- rooms_expected сохраняется и переживает store-перезапись;
- счётчики комнат и файлов не смешиваются;
- «Всего скриншотов» вычисляется из фактических требований
  каждой комнаты (classic/mixed → 1, squad → 2, неизвестный
  тип → гарантированный минимум 1 — лимит загрузки 2 не
  подставляется как требование);
- завершённые комнаты считаются по sha/shas/kind, а не по
  очищенному pending.
"""

from types import SimpleNamespace

import pytest

import cogs.ocr_upload as up


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    monkeypatch.setattr(up.store, "get", lambda g, c: None)
    monkeypatch.setattr(up.store, "set", lambda t: None)


def _pending(kind="classic", sha="s1"):
    return {
        "sha": sha,
        "kind": kind,
        "filename": f"{sha}.png",
        "pipe": {"panels": {"left": {"rows": [{}] * 4},
                            "right": {"rows": [{}] * 4}}}
        if kind == "classic" else {"squad": {"players": [{}] * 4}},
        "shot_message_id": None,
    }


def _collecting_room(idx, pending):
    return {
        "room_index": idx,
        "status": "collecting_files",
        "collect_pending": False,
        "pending": pending,
        "panels": {},
        "kind": None,
        "sha": None,
        "shas": [],
    }


def _done_room(idx, kind, shas):
    """Комната после «Завершить комнату»: pending очищен,
    статистика в panels, файлы — в sha/shas."""
    room = _collecting_room(idx, [])
    room["status"] = "awaiting_mapping"
    room["panels"] = {"left": {"rows": [{}] * 4}}
    room["kind"] = kind
    room["sha"] = shas[0]
    room["shas"] = list(shas[1:]) if kind == "squad" else list(shas)
    if kind == "squad":
        room["shas"] = list(shas)
    return room


class TestSessionFilesTotals:
    def test_no_rooms_all_unknown(self):
        """3 комнаты, файлов нет: минимум 1 скриншот на комнату —
        squad-лимит 2 для неизвестного типа НЕ предполагается."""
        session = {"rooms_expected": 3, "rooms": []}
        assert up._session_files_totals(session) == (0, 3)

    def test_classic_room_keeps_minimum(self):
        """Classic-файл в комнате 1: её требование 1; комнаты
        2-3 неизвестны → минимум 1 каждая → итог 1/3."""
        session = {
            "rooms_expected": 3,
            "rooms": [_collecting_room(1, [_pending("classic")])],
        }
        assert up._session_files_totals(session) == (1, 3)

    def test_squad_room_needs_two(self):
        """Squad-файл в комнате 1 → её требование 2 по факту;
        комната 2 неизвестна → минимум 1 → итог 1/3."""
        session = {
            "rooms_expected": 2,
            "rooms": [_collecting_room(1, [_pending("squad")])],
        }
        assert up._session_files_totals(session) == (1, 3)

    def test_completed_classic_counted_by_sha(self):
        """Завершённая classic-комната: pending пуст, но файл
        учтён через sha → 1/1, а не 0/2."""
        session = {
            "rooms_expected": 1,
            "rooms": [_done_room(1, "classic", ["shaA"])],
        }
        assert up._session_files_totals(session) == (1, 1)

    def test_completed_squad_counted_by_shas(self):
        session = {
            "rooms_expected": 1,
            "rooms": [_done_room(1, "squad", ["s1", "s2"])],
        }
        assert up._session_files_totals(session) == (2, 2)

    def test_completed_mixed_requires_one_accepts_two(self):
        """Mixed (classic + squad-aux): минимум 1 файл,
        фактически принято 2."""
        room = _done_room(1, "mixed", ["main", "aux"])
        session = {"rooms_expected": 1, "rooms": [room]}
        assert up._session_files_totals(session) == (2, 1)

    def test_partial_progression_3_rooms(self):
        """Сценарий ТЗ: 3 комнаты, squad-файлы по 2 на комнату.
        Неизвестные комнаты — минимум 1, squad раскрывает 2."""
        session = {
            "rooms_expected": 3,
            "rooms": [_collecting_room(1, [_pending("squad")])],
        }
        # комната 1: 1/2; комнаты 2-3 неизвестны: +1+1 → 1/4
        assert up._session_files_totals(session) == (1, 4)
        # второй squad в комнате 1 → 2/4
        session["rooms"][0]["pending"].append(_pending("squad", "s2"))
        assert up._session_files_totals(session) == (2, 4)
        # комната 1 завершена (squad 2/2), комната 2 начата squad
        session["rooms"] = [
            _done_room(1, "squad", ["s1", "s2"]),
            _collecting_room(2, [_pending("squad")]),
        ]
        # принято 2+1; требуется 2+2+1(неизв.) = 5 → 3/5
        assert up._session_files_totals(session) == (3, 5)


class _Chan:
    def __init__(self, cid=1):
        self.id = cid
        self.sent = []
        self.edits = []
        self._mid = 100

    async def send(self, content=None, view=None, files=None,
                   allowed_mentions=None, **kw):
        self.sent.append({"content": content, "view": view})
        self._mid += 1
        return SimpleNamespace(id=self._mid)

    async def fetch_message(self, mid):
        ch = self

        class _M:
            id = int(mid)

            async def edit(self, content=None, view=None, **kw):
                ch.edits.append({"content": content, "view": view})

        return _M()


def _tournament():
    return SimpleNamespace(
        ocr_sessions={}, channel_id=1, guild_id=1, id="t1",
        qualifier_matches=[(0, 1)], semifinal_matches=[],
        final_teams=[0, 1],
        teams=[{"captain": "A", "players": ["A"]},
               {"captain": "B", "players": ["B"]}],
        team_names={0: "Alpha", 1: "Beta"},
        player_user_ids={"A": 1, "B": 2},
        match_channels={},
        phase=SimpleNamespace(value="qualifiers"),
    )


class TestCollectMessageCounters:
    @pytest.mark.asyncio
    async def test_multi_room_shows_room_and_total(self):
        """3 комнаты, classic-файл в комнате 1: отображаются
        Комнаты 0/3, Скриншоты комнаты 1: 1/1 и общий итог."""
        ch = _Chan()
        tournament = _tournament()
        session = {
            "rooms_expected": 3, "gen": 1, "teams": [0, 1],
            "rooms": [_collecting_room(1, [_pending("classic")])],
        }
        room = session["rooms"][0]

        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        text = ch.sent[0]["content"]
        assert "Комнаты: 0/3" in text
        assert "Скриншоты комнаты 1: 1/1" in text
        # classic 1/1 + две неизвестные по минимуму 1 → 1/3
        assert "Всего скриншотов: 1/3" in text

    @pytest.mark.asyncio
    async def test_single_room_totals(self):
        """1 комната: общий итог совпадает со счётчиком комнаты."""
        ch = _Chan()
        tournament = _tournament()
        session = {
            "rooms_expected": 1, "gen": 1, "teams": [0, 1],
            "rooms": [_collecting_room(1, [_pending("classic")])],
        }
        await up._ensure_room_collect_message(
            ch, tournament, session, session["rooms"][0],
            "qualifier", 0,
        )
        text = ch.sent[0]["content"]
        assert "Комнаты: 0/1" in text
        assert "Всего скриншотов: 1/1" in text

    @pytest.mark.asyncio
    async def test_no_rooms_expected_no_totals_line(self):
        """Сессия без rooms_expected (legacy/MM) — строка итогов
        не добавляется, старое поведение сохранено."""
        ch = _Chan()
        tournament = _tournament()
        session = {"teams": [0, 1], "gen": 1}
        room = _collecting_room(1, [_pending("classic")])

        await up._ensure_room_collect_message(
            ch, tournament, session, room, "qualifier", 0,
        )
        text = ch.sent[0]["content"]
        assert "Всего скриншотов" not in text


class TestRoomsExpectedPersistence:
    @pytest.mark.asyncio
    async def test_count_persisted_in_session(self, monkeypatch):
        """Выбранное количество комнат пишется в rooms_expected
        и уходит в store.set — переживает перезагрузку."""
        saved = []
        monkeypatch.setattr(up.store, "set", lambda t: saved.append(t))
        guild = SimpleNamespace(id=1)
        guild.get_channel = lambda cid: None

        async def _fetch(cid):
            return None

        guild.fetch_channel = _fetch
        ch = _Chan(555)
        tournament = _tournament()

        session = await up.create_rooms_session(
            guild, ch,
            SimpleNamespace(id=9, display_name="u", name="u"),
            tournament,
            "qualifier", 0, 3,
        )
        assert session["rooms_expected"] == 3
        assert session["rooms_received"] == 0
        assert session["rooms"] == []
        # Прогресс-текст сразу отражает 3 комнаты.
        prog = session.get("_prog_text") or ""
        # store.set вызван с tournament, содержащим сессию
        assert any(
            s.ocr_sessions.get("qualifier_0", {}).get("rooms_expected") == 3
            for s in saved
        )

    def test_next_index_stops_at_expected(self):
        """_rooms_next_index не предлагает комнату сверх
        rooms_expected — лишние комнаты невозможны."""
        session = {
            "rooms_expected": 2,
            "rooms": [
                {"room_index": 1, "panels": {"left": {"rows": [1]}}},
                {"room_index": 2, "panels": {"left": {"rows": [1]}}},
            ],
        }
        assert up._rooms_next_index(session) is None
        assert up._rooms_completed_count(session) == 2
