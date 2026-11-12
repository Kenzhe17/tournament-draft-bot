"""Панель организатора: компактный embed без баннера,
кнопки Взять/Сдать/Занята, сохранение состояния."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import views.org_role_view as omv


@pytest.fixture(autouse=True)
def roles(monkeypatch, tmp_path):
    monkeypatch.setattr(omv, "_ORG_ROLES", [(11, "ORG-1"), (22, "ORG-2"), (33, "ORG-3")])
    monkeypatch.setattr(omv, "ORG_SLOT_FILE", tmp_path / "org_slot.json")
    yield


def _state(holders=None):
    s = omv.OrgSlotState()
    s.role_holders = holders or {}
    return s


class _Resp:
    def __init__(self, ix):
        self._ix = ix

    async def edit_message(self, **kw):
        self._ix.edits.append(kw)
        self._ix.last_view = kw.get("view")

    async def send_message(self, content=None, **kw):
        self._ix.replies.append(str(content))

    async def defer(self, **kw):
        pass

    def is_done(self):
        return False


class _Follow:
    async def send(self, content=None, **kw):
        pass


class _Member:
    def __init__(self, uid, role_ids=(), admin=False):
        self.id = uid
        self.roles = [SimpleNamespace(id=r) for r in role_ids]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.display_name = f"user{uid}"
        self.added = []
        self.removed = []

    async def add_roles(self, role):
        self.added.append(role.id)
        self.roles.append(SimpleNamespace(id=role.id))

    async def remove_roles(self, role):
        self.removed.append(role.id)
        self.roles = [r for r in self.roles if r.id != role.id]


class _Guild:
    def __init__(self, members):
        self.id = 1
        self.members = members
        self._by_id = {m.id: m for m in members}

    def get_role(self, rid):
        return SimpleNamespace(id=rid) if rid else None

    def get_member(self, uid):
        return self._by_id.get(uid)


class _Ix:
    def __init__(self, member, guild):
        self.user = member
        self.guild = guild
        self.channel = SimpleNamespace(id=77)
        self.message = SimpleNamespace(id=55)
        self.response = _Resp(self)
        self.followup = _Follow()
        self.edits = []
        self.replies = []
        self.last_view = None


def _view_children(view):
    return list(getattr(view, "children", view._children))


# ---------- embed ----------

class TestEmbed:
    def test_compact_no_image(self):
        guild = _Guild([])
        embed = omv.build_org_role_embed(guild, _state())
        assert embed.title == "ПАНЕЛЬ ОРГАНИЗАТОРА"
        assert getattr(embed.image, "url", None) is None
        assert getattr(embed.thumbnail, "url", None) is None
        assert "r!z3" in (embed.footer.text or "")
        assert "ORG-1" in embed.description
        assert "Свободна" in embed.description
        assert "queue_banner" not in embed.description

    def test_occupied_shows_holder(self):
        guild = _Guild([])
        state = _state({22: [555]})
        embed = omv.build_org_role_embed(guild, state)
        assert "ORG-2" in embed.description
        assert "Занята" in embed.description
        assert "<@555>" in embed.description


# ---------- кнопки ----------

class TestRoleButtons:
    def _guild_with(self, uid, role_ids=()):
        m = _Member(uid, role_ids)
        return _Guild([m]), m

    def test_free_roles_take_buttons(self):
        guild, m = self._guild_with(1)
        view = omv.RoleSelectionView(_state(), guild, 1)
        btns = {b.custom_id.split("_")[-2]: b for b in _view_children(view)}
        for rid in ("11", "22", "33"):
            b = btns[rid]
            assert b.label.startswith("Взять ORG")
            assert b.style == __import__("discord").ButtonStyle.primary
            assert not b.disabled

    def test_occupied_role_disabled_for_stranger(self):
        guild, m = self._guild_with(1)
        state = _state({22: [999]})
        view = omv.RoleSelectionView(state, guild, 1)
        occ = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_occupied_")
        ]
        assert len(occ) == 1
        assert occ[0].disabled
        assert "Занята" in occ[0].label
        # Остальные роли — кнопки взятия
        takes = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_take_role_")
        ]
        assert len(takes) == 2

    def test_own_role_shows_release(self):
        guild, m = self._guild_with(1, role_ids=[11])
        state = _state({11: [1]})
        view = omv.RoleSelectionView(state, guild, 1)
        rel = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_release_role_")
        ]
        assert len(rel) == 1
        assert rel[0].label == "Сдать ORG-1"

    def test_all_occupied_no_take_for_stranger(self):
        guild, m = self._guild_with(1)
        state = _state({11: [9], 22: [8], 33: [7]})
        view = omv.RoleSelectionView(state, guild, 1)
        takes = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_take_role_")
        ]
        occ = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_occupied_")
        ]
        assert takes == []
        assert len(occ) == 3

    def test_back_button_secondary(self):
        import discord
        b = omv.BackButton()
        assert b.style == discord.ButtonStyle.secondary
        assert b.label == "Назад"


# ---------- take / release ----------

class TestTakeRelease:
    @pytest.mark.asyncio
    async def test_take_role_updates_state_and_panel(self):
        guild, m = _Guild([_Member(1)]), None
        m = guild.get_member(1)
        btn = omv.TakeRoleButton(_state(), 11, "ORG-1", 0)
        ix = _Ix(m, guild)
        await btn.callback(ix)
        state = omv.load_state()
        assert state.get_holders(11) == [1]
        assert m.added == [11]
        # Панель обновлена, без вложений-баннера
        assert ix.edits
        assert ix.edits[0].get("attachments") == []
        assert ix.last_view is not None

    @pytest.mark.asyncio
    async def test_take_occupied_rejected(self):
        guild = _Guild([_Member(1)])
        m = guild.get_member(1)
        omv.save_state(_state({11: [9]}))
        btn = omv.TakeRoleButton(_state({11: [9]}), 11, "ORG-1", 0)
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert any("лимит" in r for r in ix.replies)
        assert omv.load_state().get_holders(11) == [9]

    @pytest.mark.asyncio
    async def test_double_take_same_user_rejected(self):
        guild = _Guild([_Member(1)])
        m = guild.get_member(1)
        # Первый take
        await omv.TakeRoleButton(_state(), 11, "ORG-1", 0).callback(
            _Ix(m, guild)
        )
        # Повторный take тем же пользователем
        ix2 = _Ix(m, guild)
        await omv.TakeRoleButton(_state(), 11, "ORG-1", 0).callback(ix2)
        assert any("уже есть" in r for r in ix2.replies)
        assert omv.load_state().get_holders(11) == [1]

    @pytest.mark.asyncio
    async def test_release_role(self):
        guild = _Guild([_Member(1, role_ids=[11])])
        m = guild.get_member(1)
        omv.save_state(_state({11: [1]}))
        btn = omv.ReleaseRoleButton(_state({11: [1]}), 11, "ORG-1", 0)
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert omv.load_state().get_holders(11) == []
        assert m.removed == [11]
        assert ix.edits

    @pytest.mark.asyncio
    async def test_release_foreign_rejected(self):
        guild = _Guild([_Member(1)])
        m = guild.get_member(1)
        omv.save_state(_state({11: [9]}))
        btn = omv.ReleaseRoleButton(_state({11: [9]}), 11, "ORG-1", 0)
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert any("нет роли" in r for r in ix.replies)
        assert omv.load_state().get_holders(11) == [9]

    @pytest.mark.asyncio
    async def test_concurrent_take_one_wins(self):
        """Два пользователя одновременно: лимит 1 — занимает один."""
        guild = _Guild([_Member(1), _Member(2)])
        b1 = omv.TakeRoleButton(_state(), 11, "ORG-1", 0)
        await b1.callback(_Ix(guild.get_member(1), guild))
        ix2 = _Ix(guild.get_member(2), guild)
        await omv.TakeRoleButton(_state(), 11, "ORG-1", 0).callback(ix2)
        assert omv.load_state().get_holders(11) == [1]
        assert any("лимит" in r for r in ix2.replies)


# ---------- persistence ----------

class TestPersistence:
    def test_save_load_roundtrip(self):
        s = _state({11: [5], 22: [6]})
        s.guild_id = 1
        s.message_id = 2
        s.channel_id = 3
        omv.save_state(s)
        loaded = omv.load_state()
        assert loaded.get_holders(11) == [5]
        assert loaded.get_holders(22) == [6]
        assert loaded.message_id == 2

    def test_restart_restores(self, monkeypatch):
        """После 'рестарта' (load_state) владельцы и кнопки
        восстанавливаются корректно."""
        s = _state({11: [5]})
        omv.save_state(s)
        loaded = omv.load_state()
        guild = _Guild([_Member(7)])
        view = omv.RoleSelectionView(loaded, guild, 7)
        occ = [
            b for b in _view_children(view)
            if getattr(b, "custom_id", "").startswith("org_occupied_")
        ]
        assert len(occ) == 1  # ORG-1 занята, не показана свободной
        assert not any(
            getattr(b, "custom_id", "") == "org_take_role_11_0"
            for b in _view_children(view)
        )
