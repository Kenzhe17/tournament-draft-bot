"""Панель организатора: единое CV2-сообщение с баннером,
динамические кнопки Взять/Сдать, сброс с подтверждением,
сохранение состояния и защита от гонок."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import views.org_role_view as omv


@pytest.fixture(autouse=True)
def roles(monkeypatch, tmp_path):
    monkeypatch.setattr(omv, "_ORG_ROLES", [(11, "ORG-1"), (22, "ORG-2"), (33, "ORG-3")])
    monkeypatch.setattr(omv, "ORG_SLOT_FILE", tmp_path / "org_slot.json")
    monkeypatch.setattr(omv, "MANAGER_ROLE_ID", 0)
    yield


def _state(holders=None):
    s = omv.OrgSlotState()
    s.role_holders = holders or {}
    s.guild_id = 1
    s.channel_id = 77
    s.message_id = 55
    return s


class _Resp:
    def __init__(self, ix):
        self._ix = ix

    async def edit_message(self, **kw):
        self._ix.edits.append(kw)
        self._ix.last_view = kw.get("view")

    async def send_message(self, content=None, **kw):
        self._ix.replies.append(str(content))
        self._ix.last_kwargs = kw
        view = kw.get("view")
        if view is not None:
            self._ix.last_view = view

    async def defer(self, **kw):
        pass

    def is_done(self):
        return False


class _Follow:
    async def send(self, content=None, **kw):
        pass


class _Role:
    def __init__(self, rid):
        self.id = rid


class _Member:
    def __init__(self, uid, role_ids=(), admin=False, name=None):
        self.id = uid
        self.roles = [_Role(r) for r in role_ids]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.display_name = name or f"user{uid}"
        self.name = self.display_name
        self.added = []
        self.removed = []

    async def add_roles(self, role):
        self.added.append(role.id)
        self.roles.append(_Role(role.id))

    async def remove_roles(self, role):
        self.removed.append(role.id)
        self.roles = [r for r in self.roles if r.id != role.id]


class _Channel:
    def __init__(self, gid=1, cid=77):
        self.id = cid
        self.guild = SimpleNamespace(id=gid)
        self.messages = {}

    async def fetch_message(self, mid):
        return self.messages[mid]

    async def send(self, **kw):
        msg = SimpleNamespace(id=900)
        msg.edit_calls = []

        async def edit(**ekw):
            msg.edit_calls.append(ekw)

        msg.edit = edit
        self.messages[msg.id] = msg
        return msg


class _Guild:
    def __init__(self, members, channel=None):
        self.id = 1
        self.members = members
        self._by_id = {m.id: m for m in members}
        self.channel = channel or _Channel()

    def get_role(self, rid):
        return _Role(rid) if rid else None

    def get_member(self, uid):
        return self._by_id.get(uid)

    async def fetch_member(self, uid):
        m = self._by_id.get(uid)
        if m is None:
            raise KeyError(uid)
        return m

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None


class _Ix:
    def __init__(self, member, guild):
        self.user = member
        self.guild = guild
        self.channel = guild.channel
        self.channel_id = guild.channel.id
        self.message = SimpleNamespace(id=55)
        self.response = _Resp(self)
        self.followup = _Follow()
        self.edits = []
        self.replies = []
        self.last_view = None
        self.last_kwargs = {}


def _iter_items(item):
    """Все вложенные items (View/LayoutView/Container/ActionRow)."""
    yield item
    for ch in getattr(item, "children", []) or []:
        yield from _iter_items(ch)


def _all_buttons(view):
    return [
        i for i in _iter_items(view)
        if isinstance(i, discord.ui.Button)
    ]


def _texts(view):
    return [
        i.content for i in _iter_items(view)
        if isinstance(i, discord.ui.TextDisplay)
    ]


def _RoleSlotButton_for(slot):
    """Кнопка слота для тестов (label не влияет на callback)."""
    _, name = omv._org_roles()[slot]
    return omv._RoleSlotButton(slot, label=f"Взять {name}", occupied=False)


# ---------- структура панели ----------

class TestPanelStructure:
    def test_single_container_with_banner(self):
        guild = _Guild([])
        view = omv.OrgPanelView(guild=guild, state=_state())
        assert isinstance(view, discord.ui.LayoutView)
        containers = [
            c for c in view.children if isinstance(c, discord.ui.Container)
        ]
        assert len(containers) == 1
        kids = containers[0].children
        assert isinstance(kids[0], discord.ui.MediaGallery)
        texts = "\n".join(_texts(view))
        assert "ПАНЕЛЬ ОРГАНИЗАТОРА" in texts
        assert "Управление организаторскими ролями" in texts
        assert "ОРГАНИЗАТОРСКИЕ РОЛИ" in texts
        assert "ORG-1" in texts and "ORG-2" in texts and "ORG-3" in texts
        assert "Свободна" in texts

    def test_status_shows_holder_name(self):
        guild = _Guild([_Member(9, name="Nota")])
        state = _state({22: [9]})
        view = omv.OrgPanelView(guild=guild, state=state)
        texts = "\n".join(_texts(view))
        assert "ORG-2" in texts
        assert "@Nota" in texts
        assert "1/1" in texts

    def test_discord_role_without_state_shows_occupied(self):
        """Рассинхрон: роль есть в Discord, но нет в state — занята."""
        guild = _Guild([_Member(9, role_ids=[11])])
        view = omv.OrgPanelView(guild=guild, state=_state())
        texts = "\n".join(_texts(view))
        assert "@user9" in texts

    def test_buttons_labels_and_styles(self):
        guild = _Guild([])
        state = _state({22: [9]})
        view = omv.OrgPanelView(guild=guild, state=state)
        btns = {b.custom_id: b for b in _all_buttons(view)}
        assert btns["org_panel_role_0"].label == "Взять ORG-1"
        assert btns["org_panel_role_0"].style == discord.ButtonStyle.primary
        assert btns["org_panel_role_1"].label == "Сдать ORG-2"
        assert btns["org_panel_role_1"].style == discord.ButtonStyle.secondary
        assert btns["org_panel_reset"].label == "Сбросить роли"
        assert btns["org_panel_reset"].style == discord.ButtonStyle.danger

    def test_persistent_custom_ids(self):
        view = omv.OrgPanelView(guild=_Guild([]), state=_state())
        assert view.timeout is None
        assert view.is_persistent()
        for b in _all_buttons(view):
            assert b.custom_id

    def test_state_roundtrip(self):
        s = _state({11: [5], 22: [6]})
        omv.save_state(s)
        loaded = omv.load_state()
        assert loaded.get_holders(11) == [5]
        assert loaded.get_holders(22) == [6]
        assert loaded.message_id == 55


# ---------- взять / сдать ----------

class TestTakeRelease:
    @pytest.mark.asyncio
    async def test_take_free_role(self):
        m = _Member(1)
        guild = _Guild([m])
        omv.save_state(_state())
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(0).callback(ix)
        state = omv.load_state()
        assert state.get_holders(11) == [1]
        assert m.added == [11]
        # Панель обновлена в том же сообщении
        assert ix.edits
        assert isinstance(ix.last_view, omv.OrgPanelView)
        texts = "\n".join(_texts(ix.last_view))
        assert "@user1" in texts

    @pytest.mark.asyncio
    async def test_take_occupied_rejected(self):
        m = _Member(1)
        guild = _Guild([m, _Member(9)])
        omv.save_state(_state({11: [9]}))
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(0).callback(ix)
        assert any("занята" in r for r in ix.replies)
        assert omv.load_state().get_holders(11) == [9]
        assert m.added == []

    @pytest.mark.asyncio
    async def test_concurrent_take_one_wins(self):
        """Два пользователя нажали «Взять ORG-1»: роль получает один."""
        m1, m2 = _Member(1), _Member(2)
        guild = _Guild([m1, m2])
        omv.save_state(_state())
        await _RoleSlotButton_for(0).callback(_Ix(m1, guild))
        ix2 = _Ix(m2, guild)
        await _RoleSlotButton_for(0).callback(ix2)
        assert omv.load_state().get_holders(11) == [1]
        assert any("занята" in r for r in ix2.replies)

    @pytest.mark.asyncio
    async def test_take_replaces_other_org_role(self):
        """У пользователя ORG-1 → берёт ORG-2: старая снимается."""
        m = _Member(1, role_ids=[11])
        guild = _Guild([m])
        omv.save_state(_state({11: [1]}))
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(1).callback(ix)
        state = omv.load_state()
        assert state.get_holders(11) == []
        assert state.get_holders(22) == [1]
        assert m.removed == [11]
        assert m.added == [22]

    @pytest.mark.asyncio
    async def test_release_own_role(self):
        m = _Member(1, role_ids=[11])
        guild = _Guild([m])
        omv.save_state(_state({11: [1]}))
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(0).callback(ix)
        assert omv.load_state().get_holders(11) == []
        assert m.removed == [11]
        assert ix.edits

    @pytest.mark.asyncio
    async def test_release_foreign_rejected(self):
        m = _Member(1)
        guild = _Guild([m, _Member(9, role_ids=[11])])
        omv.save_state(_state({11: [9]}))
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(0).callback(ix)
        assert any("занята" in r for r in ix.replies)
        assert omv.load_state().get_holders(11) == [9]
        assert guild.get_member(9).removed == []

    @pytest.mark.asyncio
    async def test_unconfigured_role_rejected(self, monkeypatch):
        monkeypatch.setattr(
            omv, "_ORG_ROLES", [(0, "ORG-1"), (22, "ORG-2"), (33, "ORG-3")]
        )
        m = _Member(1)
        guild = _Guild([m])
        omv.save_state(_state())
        ix = _Ix(m, guild)
        await _RoleSlotButton_for(0).callback(ix)
        assert any("не настроена" in r for r in ix.replies)


# ---------- сброс ----------

class TestReset:
    @pytest.mark.asyncio
    async def test_reset_denied_for_regular_user(self):
        m = _Member(1)
        guild = _Guild([m])
        btn = omv._ResetRolesButton()
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert any("Сброс доступен" in r for r in ix.replies)

    @pytest.mark.asyncio
    async def test_reset_admin_gets_confirmation(self):
        m = _Member(1, admin=True)
        guild = _Guild([m])
        btn = omv._ResetRolesButton()
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert ix.last_view is not None
        btns = _all_buttons(ix.last_view)
        ids = {b.custom_id for b in btns}
        assert "org_panel_reset_yes" in ids
        assert "org_panel_reset_no" in ids

    @pytest.mark.asyncio
    async def test_reset_confirm_clears_everything(self):
        m1 = _Member(1, admin=True)
        m2 = _Member(9, role_ids=[11, 22])
        guild = _Guild([m1, m2])
        omv.save_state(_state({11: [9], 22: [9], 33: [5]}))
        state = omv.load_state()
        # Сообщение панели в канале (для fetch_message после сброса)
        panel_msg = SimpleNamespace(id=55, edits=[])

        async def _pedit(**kw):
            panel_msg.edits.append(kw)

        panel_msg.edit = _pedit
        guild.channel.messages[55] = panel_msg
        view = omv.OrgPanelView(guild=guild, state=state)
        btn = next(
            b for b in _all_buttons(view) if b.custom_id == "org_panel_reset"
        )
        ix = _Ix(m1, guild)
        await btn.callback(ix)
        confirm = next(
            b for b in _all_buttons(ix.last_view)
            if b.custom_id == "org_panel_reset_yes"
        )
        ix2 = _Ix(m1, guild)
        await confirm.callback(ix2)
        state = omv.load_state()
        assert state.role_holders == {}
        assert m2.removed == [11, 22]
        # Панель отредактирована через fetch_message
        assert panel_msg.edits
        assert isinstance(panel_msg.edits[0].get("view"), omv.OrgPanelView)
        assert any("сброшены" in r for r in ix2.replies)

    @pytest.mark.asyncio
    async def test_reset_cancel_keeps_roles(self):
        m1 = _Member(1, admin=True)
        m2 = _Member(9, role_ids=[11])
        guild = _Guild([m1, m2])
        omv.save_state(_state({11: [9]}))
        view = omv.OrgPanelView(guild=guild, state=omv.load_state())
        btn = next(
            b for b in _all_buttons(view) if b.custom_id == "org_panel_reset"
        )
        ix = _Ix(m1, guild)
        await btn.callback(ix)
        cancel = next(
            b for b in _all_buttons(ix.last_view)
            if b.custom_id == "org_panel_reset_no"
        )
        ix2 = _Ix(m1, guild)
        await cancel.callback(ix2)
        assert omv.load_state().get_holders(11) == [9]
        assert m2.removed == []
        assert any("отмен" in r for r in ix2.replies)


# ---------- админская сдача роли за участника ----------


def _panel_msg(guild):
    """Сообщение публичной панели в канале (для refresh)."""
    msg = SimpleNamespace(id=55, edits=[])

    async def _edit(**kw):
        msg.edits.append(kw)

    msg.edit = _edit
    guild.channel.messages[55] = msg
    return msg


class TestAdminRelease:
    @pytest.mark.asyncio
    async def test_denied_for_regular_user(self):
        m = _Member(1)
        guild = _Guild([m])
        btn = omv._AdminReleaseRoleButton()
        ix = _Ix(m, guild)
        await btn.callback(ix)
        assert any("только владельцу" in r for r in ix.replies)

    @pytest.mark.asyncio
    async def test_admin_gets_user_select(self):
        m = _Member(1, admin=True)
        guild = _Guild([m])
        btn = omv._AdminReleaseRoleButton()
        ix = _Ix(m, guild)
        await btn.callback(ix)
        view = ix.last_view
        assert view is not None
        selects = [
            i for i in _iter_items(view)
            if isinstance(i, discord.ui.UserSelect)
        ]
        assert selects

    @pytest.mark.asyncio
    async def test_pick_user_without_role(self, monkeypatch):
        import services.tournament_audit_service as audit_svc
        monkeypatch.setattr(
            audit_svc, "record_interaction", AsyncMock())
        admin = _Member(1, admin=True)
        target = _Member(9)
        guild = _Guild([admin, target])
        omv.save_state(_state())
        btn = omv._AdminReleaseRoleButton()
        ix = _Ix(admin, guild)
        await btn.callback(ix)
        pick_view = ix.last_view
        select = next(
            i for i in _iter_items(pick_view)
            if isinstance(i, discord.ui.UserSelect)
        )
        select._values = [SimpleNamespace(id=9)]
        ix2 = _Ix(admin, guild)
        await select.callback(ix2)
        # edit_message — карточка «нет ORG-роли»
        assert ix2.edits
        assert ix2.last_view is None or not _all_buttons(
            ix2.last_view or pick_view
        )

    @pytest.mark.asyncio
    async def test_full_release_flow(self, monkeypatch):
        import services.tournament_audit_service as audit_svc
        audit_mock = AsyncMock()
        monkeypatch.setattr(
            audit_svc, "record_interaction", audit_mock)

        admin = _Member(1, admin=True)
        target = _Member(9, role_ids=[22], name="Holder")
        guild = _Guild([admin, target])
        panel_msg = _panel_msg(guild)
        omv.save_state(_state({22: [9]}))

        # 1. Кнопка → UserSelect
        btn = omv._AdminReleaseRoleButton()
        ix = _Ix(admin, guild)
        await btn.callback(ix)
        pick_view = ix.last_view
        select = next(
            i for i in _iter_items(pick_view)
            if isinstance(i, discord.ui.UserSelect)
        )
        select._values = [SimpleNamespace(id=9)]
        ix2 = _Ix(admin, guild)
        await select.callback(ix2)

        # 2. Подтверждение: показана роль ORG-2
        confirm_view = ix2.last_view
        texts = "\n".join(
            getattr(c, "content", "")
            for i in _iter_items(confirm_view)
            for c in getattr(i, "children", [])
        ) if hasattr(confirm_view, "children") else ""
        # content карточки — в edit_message kwargs, не в view;
        # проверяем через кнопку подтверждения
        yes = next(
            b for b in _all_buttons(confirm_view)
            if b.custom_id.startswith("org_admin_release_yes")
        )
        ix3 = _Ix(admin, guild)
        await yes.callback(ix3)

        # 3. Роль снята: Discord + state, панель обновлена
        assert target.removed == [22]
        assert omv.load_state().get_holders(22) == []
        assert panel_msg.edits
        assert isinstance(
            panel_msg.edits[0].get("view"), omv.OrgPanelView)
        # Аудит: revoke с target=9 и admin_forced
        assert audit_mock.await_count >= 1
        kw = audit_mock.await_args.kwargs
        assert kw.get("target_user_id") == 9
        assert kw["after"].get("admin_forced") is True
        assert "ORG-2" in kw["after"].get("role", "")

    @pytest.mark.asyncio
    async def test_release_denied_for_non_admin_at_confirm(
        self, monkeypatch
    ):
        """Права перепроверяются при подтверждении: admin→user
        между выбором и нажатием «Сдать роль» — отказ."""
        import services.tournament_audit_service as audit_svc
        monkeypatch.setattr(
            audit_svc, "record_interaction", AsyncMock())
        admin = _Member(1, admin=True)
        target = _Member(9, role_ids=[22])
        guild = _Guild([admin, target])
        omv.save_state(_state({22: [9]}))

        # Строим confirm-view напрямую (как после выбора)
        confirm = omv._AdminReleaseConfirmView(
            user_id=9, display_name="Holder", role_ids=[22])
        # Жмёт обычный пользователь — не admin
        regular = _Member(5)
        guild2 = _Guild([regular, target])
        ix = _Ix(regular, guild2)
        yes = next(
            b for b in _all_buttons(confirm)
            if b.custom_id.startswith("org_admin_release_yes")
        )
        await yes.callback(ix)
        assert target.removed == []
        assert omv.load_state().get_holders(22) == [9]
        assert any("только владельцу" in r for r in ix.replies)

    @pytest.mark.asyncio
    async def test_release_when_state_only_no_discord_role(
        self, monkeypatch
    ):
        """Роль есть в state, но нет в Discord — слот освобождается."""
        import services.tournament_audit_service as audit_svc
        monkeypatch.setattr(
            audit_svc, "record_interaction", AsyncMock())
        admin = _Member(1, admin=True)
        target = _Member(9)  # без discord-роли
        guild = _Guild([admin, target])
        _panel_msg(guild)
        omv.save_state(_state({22: [9]}))

        confirm = omv._AdminReleaseConfirmView(
            user_id=9, display_name="Holder", role_ids=[22])
        ix = _Ix(admin, guild)
        yes = next(
            b for b in _all_buttons(confirm)
            if b.custom_id.startswith("org_admin_release_yes")
        )
        await yes.callback(ix)
        assert omv.load_state().get_holders(22) == []
        assert target.removed == []  # discord-роли не было
