"""Единый PNG-баннер assets/queue_banner.png: панель поиска
игроков и панель организатора используют один и тот же
attachment://queue_banner.png; вложение не дублируется при edit."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utils.banner as banner
from models.mm_search import MMSearch


def _iter_items(item):
    yield item
    for ch in getattr(item, "children", []) or []:
        yield from _iter_items(ch)


def _galleries(view):
    return [
        i for i in _iter_items(view)
        if isinstance(i, discord.ui.MediaGallery)
    ]


def _queue_panel():
    from views.mm_search_view import build_queue_panel
    return build_queue_panel(MMSearch(guild_id=1, channel_id=10))


def _org_panel():
    import views.org_role_view as omv
    monkey_state = omv.OrgSlotState()
    return omv.OrgPanelView(guild=None, state=monkey_state)


class TestBannerAsset:
    def test_original_png_restored(self):
        assert banner._PATH.exists()
        # Оригинал из main: 2169x551
        data = banner._PATH.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_queue_panel_uses_shared_banner(self):
        galleries = _galleries(_queue_panel())
        assert galleries, "MediaGallery отсутствует в панели поиска"
        assert galleries[0].items[0].media.url == "attachment://queue_banner.png"

    def test_org_panel_uses_same_banner(self):
        galleries = _galleries(_org_panel())
        assert galleries, "MediaGallery отсутствует в панели организатора"
        assert galleries[0].items[0].media.url == "attachment://queue_banner.png"


class TestAttachmentReuse:
    def test_edit_skips_when_banner_attached(self):
        msg = SimpleNamespace(
            attachments=[SimpleNamespace(filename="queue_banner.png")]
        )
        assert banner.edit_attachments(msg) is None

    def test_edit_attaches_when_missing(self):
        msg = SimpleNamespace(attachments=[])
        att = banner.edit_attachments(msg)
        assert att is not None and len(att) == 1
        assert att[0].filename == "queue_banner.png"

    def test_edit_replaces_stale_banner(self):
        """Старая панель с ranked.png → заменяется на queue_banner.png."""
        msg = SimpleNamespace(
            attachments=[SimpleNamespace(filename="ranked.png")]
        )
        att = banner.edit_attachments(msg)
        assert att is not None
        assert att[0].filename == "queue_banner.png"

    def test_no_attachments_attr(self):
        msg = SimpleNamespace()
        att = banner.edit_attachments(msg)
        assert att is not None
