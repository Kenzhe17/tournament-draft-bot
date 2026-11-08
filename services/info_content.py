"""Реестр контента информационного центра r!z3.

8 разделов, контент — в content/info/*.md (формат: блоки с заголовком
«## Заголовок» и телом в markdown). Баннеры — assets/info/*.png.
Версия раздела = короткий хэш файла — используется для идемпотентного
/info update (правим сообщение только если версия изменилась).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content" / "info"
BANNERS_DIR = ROOT / "assets" / "info"

MAX_TEXT_LENGTH = 2000  # лимит TextDisplay в Components V2


@dataclass(frozen=True)
class InfoSection:
    key: str
    title: str
    banner: str          # имя файла в assets/info/
    md_file: str         # имя файла в content/info/


SECTIONS: tuple[InfoSection, ...] = (
    InfoSection("info", "Информация", "info.png", "info.md"),
    InfoSection("osnovnoe", "Основное", "osnovnoe.png", "osnovnoe.md"),
    InfoSection("ranked", "Ranked Matchmaking", "ranked.png", "ranked.md"),
    InfoSection("turniry", "Турниры", "turniry.png", "turniry.md"),
    InfoSection("mekhaniki", "Механики", "mekhaniki.png", "mekhaniki.md"),
    InfoSection("reputatsiya", "Репутация", "reputatsiya.png", "reputatsiya.md"),
    InfoSection("nagrady", "Награды", "nagrady.png", "nagrady.md"),
    InfoSection("pravila", "Правила", "pravila.png", "pravila.md"),
)


def section_by_key(key: str) -> InfoSection | None:
    for s in SECTIONS:
        if s.key == key:
            return s
    return None


def _md_path(section: InfoSection) -> Path:
    return CONTENT_DIR / section.md_file


def _banner_path(section: InfoSection) -> Path:
    return BANNERS_DIR / section.banner


def content_version(section: InfoSection) -> str:
    """Короткий хэш содержимого md-файла (или 'missing')."""
    try:
        raw = _md_path(section).read_bytes()
    except OSError:
        return "missing"
    return hashlib.sha1(raw).hexdigest()[:10]


def parse_blocks(section: InfoSection) -> list[tuple[str, str]]:
    """Разобрать md-файл на блоки [(заголовок, тело)].

    «## Заголовок» начинает блок, остальные строки — тело блока.
    """
    try:
        text = _md_path(section).read_text(encoding="utf-8")
    except OSError:
        return []
    blocks: list[tuple[str, str]] = []
    heading: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                blocks.append((heading, "\n".join(body).strip()))
            heading = line[3:].strip()
            body = []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        blocks.append((heading, "\n".join(body).strip()))
    return [(h, b) for h, b in blocks if h and b]


def estimate_text_length(section: InfoSection) -> int:
    """Суммарная длина текста блоков (для прикидки лимитов)."""
    return sum(len(h) + len(b) + 16 for h, b in parse_blocks(section))


def validate() -> list[str]:
    """Проверить реестр и файлы. Возвращает список проблем (пусто = ок)."""
    issues: list[str] = []
    keys = [s.key for s in SECTIONS]
    if len(keys) != len(set(keys)):
        issues.append("дубликаты ключей разделов")
    if len(SECTIONS) != 8:
        issues.append(f"ожидается 8 разделов, найдено {len(SECTIONS)}")
    for s in SECTIONS:
        md = _md_path(s)
        if not md.exists():
            issues.append(f"{s.key}: нет файла {md.name}")
            continue
        blocks = parse_blocks(s)
        if not blocks:
            issues.append(f"{s.key}: нет блоков '## ' в {md.name}")
        for h, b in blocks:
            if len(b) > MAX_TEXT_LENGTH:
                issues.append(
                    f"{s.key}: блок «{h}» длиннее {MAX_TEXT_LENGTH} символов"
                )
        bp = _banner_path(s)
        if not bp.exists() or bp.stat().st_size < 1000:
            issues.append(f"{s.key}: нет баннера {s.banner}")
    return issues
