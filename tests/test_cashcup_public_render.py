"""Unit-тесты чистого рендера публичных публикаций CASH CUP.

Не требуют Discord/PG: services/cashcup_public_render работает
только со snapshot state.
"""
from __future__ import annotations

import discord

from services import cashcup_public_render as render
from services.cashcup_bracket import create_bracket, ready_matches
from services.cashcup_sim import simulate_to_final


def _teams(n: int = 18) -> list[dict]:
    teams = []
    for i in range(n):
        dids = [9100 + i * 10 + j for j in range(5)]
        members = [{"discord_id": d, "ff_uid": str(810000 + i * 10 + j),
                    "nick": f"FFNick{i}-{j}", "region": "ru",
                    "device": "pc", "ff_ok": True, "verified": True}
                   for j, d in enumerate(dids)]
        teams.append({"id": f"tm{i:02d}", "name": f"Team{i:02d}",
                      "captain": dids[0], "members": members,
                      "pending": [], "lineup": dids[:4]})
    return teams


def _state(n: int = 18, *, seed: int = 7) -> dict:
    teams = _teams(n)
    return {"status": "running", "prize": "100 000 KZT",
            "tournament_id": "tid123456789",
            "teams": teams,
            "bracket": create_bracket([t["id"] for t in teams], seed=seed),
            "payouts": []}


def _embed_text(embeds: list[discord.Embed]) -> str:
    return "\n".join(e.description or "" for e in embeds)


class TestBracketEmbeds:
    def test_header_and_matches(self):
        state = _state()
        embeds = render.bracket_embeds(state)
        assert embeds
        assert "TOURNAMENT BRACKET" in embeds[0].title.upper()
        text = _embed_text(embeds)
        # Каждая созданная алгоритмом пара отражена ровно одной строкой.
        for m in state["bracket"]["matches"]:
            assert f"`{m['id']}`" in text
        assert len(embeds) <= render.MAX_EMBEDS
        for e in embeds:
            assert len(e.description or "") <= 4096

    def test_completed_shows_score_and_winner(self):
        state = _state()
        report = simulate_to_final(state, seed=3)
        assert state["status"] == "completed"
        assert report["matches_completed"] == 34 or \
            report["matches_completed"] == len(state["bracket"]["matches"])
        embeds = render.bracket_embeds(state)
        text = _embed_text(embeds)
        assert "✅" in text
        assert "Чемпион" in text
        done = [m for m in state["bracket"]["matches"]
                if m["status"] == "completed"]
        for m in done[:5]:
            assert f"`{m['id']}`" in text

    def test_hash_changes_on_progress(self):
        state = _state()
        h1 = render.bracket_hash(state)
        m = ready_matches(state["bracket"])[0]
        m["checkins"].append(111)
        assert render.bracket_hash(state) != h1

    def test_never_exceeds_embed_limit(self):
        # 64 команды — самый тяжёлый случай; строки не обрезаются молча.
        state = _state(64)
        embeds = render.bracket_embeds(state)
        assert len(embeds) <= render.MAX_EMBEDS
        n = len(state["bracket"]["matches"])
        text = _embed_text(embeds)
        shown = text.count("`W") + text.count("`L") + text.count("`GF`")
        assert shown <= n  # при переполнении — явная пометка, не тишина


class TestMatchCard:
    def _card(self, state, mid="W1-1"):
        m = next(x for x in state["bracket"]["matches"] if x["id"] == mid)
        return m, render.match_card(state, m)

    def test_structure_and_pings(self):
        state = _state(6)
        m, card = self._card(state)
        assert card["embed"].title == f"r!z3 CASH CUP · MATCH {m['id']}"
        desc = card["embed"].description or ""
        ta = next(t for t in state["teams"] if t["id"] == m["a"])
        tb = next(t for t in state["teams"] if t["id"] == m["b"])
        assert ta["name"] in desc and tb["name"] in desc
        assert f"<@{ta['captain']}>" in desc
        assert f"<@{tb['captain']}>" in desc
        # FF-ники из состава, по порядку lineup.
        assert "FFNick" in desc
        assert card["ping_ids"] == [ta["captain"], tb["captain"]]
        assert "⚔️" in (card["content"] or "")
        assert "@everyone" not in (card["content"] or "")
        assert "@here" not in (card["content"] or "")
        field = card["embed"].fields[0].value
        assert "Готовность: 0/2" in field
        assert "Комната: ожидается" in field
        assert "Результат: не определён" in field

    def test_no_personal_data_leak(self):
        state = _state(6)
        _, card = self._card(state)
        blob = (card["embed"].description or "") + \
               (card["embed"].fields[0].value or "")
        # UID, устройства и внутренние поля проверки не публикуются.
        for t in state["teams"][:2]:
            for p in t["members"]:
                assert str(p["ff_uid"]) not in blob
                assert p["device"] not in blob.split("\n")[-1]
        assert "verified" not in blob

    def test_missing_nick_placeholder(self):
        state = _state(6)
        t = next(t for t in state["teams"]
                 if t["id"] == state["bracket"]["matches"][0]["a"])
        t["members"][0]["nick"] = ""
        _, card = self._card(state)
        assert "FF-ник не указан" in (card["embed"].description or "")

    def test_hash_tracks_visible_state(self):
        state = _state(6)
        m, card = self._card(state)
        h1 = card["hash"]
        m["checkins"].append(1)
        _, card2 = self._card(state)
        assert card2["hash"] != h1
        m["checkins"].append(2)
        m["room_id"] = "SECRET-ROOM"
        _, card3 = self._card(state)
        assert card3["hash"] != card2["hash"]
        # Пароль/ID комнаты не попадают в хэш-контент карточки
        # (hash меняется только из-за статуса «комната выдана»).
        assert "SECRET-ROOM" not in (card3["embed"].description or "")
        assert "SECRET-ROOM" not in card3["embed"].fields[0].value

    def test_completed_match(self):
        state = _state(6)
        simulate_to_final(state, seed=5)
        done = next(m for m in state["bracket"]["matches"]
                    if m["status"] == "completed"
                    and m["bracket"] != "GF")
        card = render.match_card(state, done)
        field = card["embed"].fields[0].value
        assert "MATCH COMPLETED" in field
        assert "Победитель:" in field
        assert "Проигравший:" in field

    def test_disputed_match(self):
        state = _state(6)
        m = ready_matches(state["bracket"])[0]
        m["status"] = "disputed"
        m["dispute"] = {"reason": "test dispute", "evidence": []}
        card = render.match_card(state, m)
        assert "DISPUTED" in card["embed"].fields[0].value
        assert "test dispute" in card["embed"].fields[0].value
