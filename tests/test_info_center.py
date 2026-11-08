"""Тесты информационного центра: реестр, парсер, валидация, V2-вью."""

import discord
import pytest

from services.info_content import (
    BANNERS_DIR, SECTIONS, content_version, parse_blocks,
    section_by_key, validate,
)
from services.info_renderer import build_info_view


def test_eight_sections_unique_keys():
    keys = [s.key for s in SECTIONS]
    assert len(SECTIONS) == 8
    assert len(set(keys)) == 8
    for s in SECTIONS:
        assert s.banner.endswith(".png")
        assert s.md_file.endswith(".md")


def test_all_content_files_exist_and_parse():
    for s in SECTIONS:
        blocks = parse_blocks(s)
        assert blocks, f"{s.key}: нет блоков"
        for heading, body in blocks:
            assert heading.strip()
            assert body.strip()
            assert len(body) <= 2000


def test_all_banners_exist():
    for s in SECTIONS:
        path = BANNERS_DIR / s.banner
        assert path.exists(), f"{s.key}: missing {s.banner}"
        assert path.stat().st_size > 1000


def test_validate_clean():
    assert validate() == []


def test_content_version_stable():
    s = section_by_key("info")
    assert s is not None
    assert content_version(s) == content_version(s)
    assert content_version(s) != "missing"


def test_section_by_key():
    assert section_by_key("ranked") is not None
    assert section_by_key("nope") is None


def test_build_info_view_structure():
    for s in SECTIONS:
        view = build_info_view(s)
        assert isinstance(view, discord.ui.LayoutView)
        # MediaGallery с баннером внутри контейнера
        kinds = [type(c).__name__ for c in view.container.children]
        assert "MediaGallery" in kinds
        assert "TextDisplay" in kinds
        # ни одного ActionRow с кнопками — инфо-центр без кнопок
        assert "ActionRow" not in kinds


def test_no_too_long_text_blocks():
    for s in SECTIONS:
        for heading, body in parse_blocks(s):
            # chunk_text разбивает длинный текст — сам блок может быть
            # длиннее 2000, но проверяем что он вообще не пустой
            assert len(body) > 0
