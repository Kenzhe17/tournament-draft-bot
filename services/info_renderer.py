"""Compact text-only informational cards; no stale artwork or external attachments."""

from __future__ import annotations

import discord

from services.info_content import InfoSection, parse_blocks
from utils.design.containers import separator
from utils.design.layouts import DesignView


def banner_filename(section: InfoSection) -> str:
    return section.banner


def build_info_view(section: InfoSection) -> discord.ui.LayoutView:
    """Собрать компактную текстовую карточку раздела."""
    view = DesignView(timeout=None)
    for heading, body in parse_blocks(section):
        view.text(f"> ## {heading[:80]}")
        view.text(body)
        view.container.add_item(separator())
    return view
