"""Мета-информация команд: author (/cmd + эмодзи), аватар игрока, footer.

ContextVar заполняется обёрткой вокруг callback'ов slash-команд
(см. install_command_meta в bot.py). Все send/edit-вызовы внутри
команды автоматически получают:

- author-строку «/command» с эмодзи команды в начало V2-карточки
- аватар игрока как thumbnail, если у карточки его ещё нет
- footer «powered by <бот> • <время>» с аватаром бота
- author + thumbnail + footer на классических Embed
- убирает хвост «| /command» из title embed'а

Component-callback'и (кнопки/select) выполняются вне контекста
команды — мета не проставляется, турнирные сообщения не засоряются.
"""

from __future__ import annotations

import contextvars
import functools
import re

import discord

from utils.design.emojis import em

_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "rz3_command_meta", default=None
)

_installed = False


# Команда -> ключ эмодзи дизайн-системы (em()).
# Ищется по полному имени, затем по последнему и первому слову.
_COMMAND_EMOJI: dict[str, str] = {
    # Экономика
    "balance": "money",
    "bank": "bank",
    "pay": "money",
    "bet": "bet",
    "daily": "daily",
    "gift": "gift",
    "shop": "shop",
    "sell": "shop",
    "inventory": "gift",
    "item": "gift",
    "rob": "sword",
    "robgroup": "sword",
    # Профиль / соц.
    "profile": "profile",
    "player": "profile",
    "rank": "rating",
    "top": "rating",
    "leaderboard": "rating",
    "history": "stats",
    "elo": "rating",
    "elo_battle": "sword",
    "betting": "bet",
    "setavatar": "profile",
    "setbio": "profile",
    # Турниры / матчмейкинг
    "tournament": "tournament",
    "create": "tournament",
    "delete": "delete",
    "br": "tournament",
    "booyah": "winner",
    "mm": "team",
    "matchmaking": "team",
    "teams": "team",
    "draft": "draft",
    "koth": "winner",
    "lottery": "gift",
    "forcedrop": "gift",
    # Игры
    "games": "game",
    "game": "game",
    "dice": "dice",
    "roulette": "roulette",
    "slots": "slots",
    "rps": "rps",
    "coin_flip": "dice",
    "craps": "dice",
    "snap": "game",
    "blackjack": "game",
    "poker": "game",
    "baccarat": "game",
    "chess": "game",
    "checkers": "game",
    "reversi": "game",
    "game_2048": "game",
    "minigame_tournament": "game",
    "anagrams": "brain",
    "hangman": "brain",
    "memory": "brain",
    "math_quiz": "brain",
    "mathquiz": "brain",
    "movie_quiz": "brain",
    "flag_quiz": "brain",
    "guess_number": "brain",
    "guess_emoji": "eye",
    "high_low": "brain",
    "highlow": "brain",
    "logic_puzzle": "brain",
    "millionaire": "money",
    "riddles": "brain",
    "wordle": "brain",
    "sudoku": "brain",
    # Free Fire / статистика
    "ff": "freefire",
    "freefire": "freefire",
    "statistic": "stats",
    "ocr": "screenshot",
    "catalog": "freefire",
    "info": "info",
    # Питомцы
    "pet": "pet",
    # Прочее
    "help": "question",
    "settings": "settings",
    "admin": "settings",
    "coowner": "settings",
    "role": "tag",
    "announce": "announce",
    "limit": "clock",
    "check2fa": "check",
    "debug_emoji": "sparkle",
    "fix": "settings",
    "isolate": "block",
    "release": "block",
    "replace": "refresh",
    "reset": "refresh",
    "delete_thread": "delete",
    "deleteall": "delete",
    "delete_player": "delete",
    "edit": "settings",
}


def _command_emoji(qualified_name: str) -> str:
    """Подобрать эмодзи под команду."""
    parts = qualified_name.split()
    for candidate in (qualified_name, parts[-1], parts[0]):
        key = _COMMAND_EMOJI.get(candidate)
        if key:
            return em(key)
    return em("game")


def _is_author_text(content: str, meta: dict) -> bool:
    """Уже ли в карточке author-строка этой команды."""
    text = content.strip()
    return text.startswith("-#") and meta["name"] in text


def _has_footer(items) -> bool:
    return any(
        "powered by " in t
        for item in items
        for t in _iter_texts(item)
    )


def _avatar_url(user) -> str | None:
    try:
        return user.display_avatar.url
    except Exception:
        return None


def meta_from_interaction(interaction: discord.Interaction) -> dict | None:
    """Собрать мета-данные из Interaction (slash-команда)."""
    command = getattr(interaction,"command", None)
    name = None
    if command is not None:
        name = getattr(command,"qualified_name", None) or getattr(
            command,"name", None
        )
    if not name:
        return None
    client = getattr(interaction,"client", None)
    bot_user = getattr(client,"user", None)
    try:
        timestamp = int(interaction.created_at.timestamp())
    except Exception:
        timestamp = 0
    return {
        "name": f"/{name}",
        "emoji": _command_emoji(name),
        "user_avatar": _avatar_url(getattr(interaction,"user", None)),
        "bot_avatar": _avatar_url(bot_user),
        "bot_name": getattr(bot_user,"display_name", None)
        or getattr(bot_user,"name", None)
        or "bot",
        "timestamp": timestamp,
    }


def _set_interaction_ctx(interaction):
    return _CTX.set(meta_from_interaction(interaction))


# ---------------------------------------------------------------------------
# Инъекция в V2 LayoutView
# ---------------------------------------------------------------------------

def _containers(layout: discord.ui.LayoutView):
    return [
        child
        for child in layout.children
        if isinstance(child, discord.ui.Container)
    ]


def _first_container(layout: discord.ui.LayoutView):
    containers = _containers(layout)
    return containers[0] if containers else None


def _last_container(layout: discord.ui.LayoutView):
    containers = _containers(layout)
    if containers:
        return containers[-1]
    container = discord.ui.Container()
    layout.add_item(container)
    return container


def _iter_texts(item) -> list[str]:
    """Тексты TextDisplay внутри item (включая Section)."""
    if isinstance(item, discord.ui.TextDisplay):
        return [item.content]
    if isinstance(item, discord.ui.Section):
        children = getattr(item,"children", None) or getattr(
            item,"_children", []
        )
        return [
            c.content
            for c in children
            if isinstance(c, discord.ui.TextDisplay)
        ]
    return []


def _author_item(meta: dict) -> discord.ui.Item:
    return discord.ui.TextDisplay(
        content=f"-# {meta.get('emoji') or ''} {meta['name']}".replace("  "," ").strip()
    )


def _footer_item(meta: dict) -> discord.ui.Item:
    text = f"-# powered by {meta['bot_name']}"
    if meta.get("timestamp"):
        text += f" • <t:{meta['timestamp']}:f>"
    if meta.get("bot_avatar"):
        return discord.ui.Section(
            discord.ui.TextDisplay(content=text),
            accessory=discord.ui.Thumbnail(media=meta["bot_avatar"]),
        )
    return discord.ui.TextDisplay(content=text)


def _inject_layout(layout: discord.ui.LayoutView, meta: dict) -> None:
    containers = _containers(layout)
    if not containers:
        container = discord.ui.Container()
        layout.add_item(container)
        containers = [container]
    container = containers[0]

    # 1) Author-строка «/command» в самое начало контейнера
    children = container._children
    already_author = any(
        _is_author_text(t, meta)
        for child in list(children)
        for t in _iter_texts(child)
    )
    if not already_author:
        author = _author_item(meta)
        container.add_item(author)
        children = container._children
        children.remove(author)
        children.insert(0, author)

    # 2) Аватар игрока — на первый текстовый блок без своей картинки
    avatar = meta.get("user_avatar")
    if avatar:
        for child in list(container.children):
            if any(_is_author_text(t, meta) for t in _iter_texts(child)):
                continue
            if isinstance(child, discord.ui.Section):
                accessory = getattr(child,"accessory", None)
                if isinstance(accessory, discord.ui.Thumbnail):
                    break # уже есть thumbnail (аватар/embed-thumb)
                continue
            if isinstance(child, discord.ui.MediaGallery):
                break # уже есть картинка
            if isinstance(child, discord.ui.TextDisplay):
                section = discord.ui.Section(
                    discord.ui.TextDisplay(content=child.content),
                    accessory=discord.ui.Thumbnail(media=avatar),
                )
                children = container._children
                idx = children.index(child)
                children[idx] = section
                child._detach_view()
                section._update_view(layout)
                section._parent = container
                break
            if isinstance(child, discord.ui.ActionRow):
                break # кнопки раньше текста — не лезем дальше

    # 3) Footer «powered by <бот> • <время>» — в конец последнего контейнера
    last = containers[-1]
    last_children = list(last._children)
    if not _has_footer(last_children):
        last.add_item(discord.ui.Separator())
        last.add_item(_footer_item(meta))


# ---------------------------------------------------------------------------
# Инъекция в классические Embed
# ---------------------------------------------------------------------------

_TITLE_CMD_RE = re.compile(r"\s*\|\s*/\S[^\n]*")


def _inject_embed(embed: discord.Embed, meta: dict) -> None:
    # Убрать «| /command» из заголовка
    if embed.title:
        stripped = _TITLE_CMD_RE.sub("", embed.title).rstrip()
        embed.title = stripped or None

    # Author: команда + её эмодзи (без иконки — аватар только у игрока)
    author_name = f"{meta.get('emoji') or ''} {meta['name']}".replace("  "," ").strip()
    embed.set_author(name=author_name)

    # Footer: powered by <бот> • <время>, иконка — аватар бота
    footer_text = f"powered by {meta['bot_name']}"
    if meta.get("timestamp"):
        footer_text += f" • <t:{meta['timestamp']}:f>"
    existing = embed.footer.text if embed.footer else ""
    if existing and "powered by " not in existing:
        footer_text = f"{existing} • {footer_text}"
    elif existing and "powered by " in existing:
        footer_text = existing
    embed.set_footer(text=footer_text, icon_url=meta.get("bot_avatar"))

    if not embed.thumbnail and not embed.image:
        if meta.get("user_avatar"):
            embed.set_thumbnail(url=meta["user_avatar"])


# ---------------------------------------------------------------------------
# Обёртки send/edit
# ---------------------------------------------------------------------------

def _transform_kwargs(kwargs: dict) -> None:
    """Мутирует kwargs: добавляет author/avatar/footer по ctx-мета."""
    meta = _CTX.get()
    if not meta:
        return

    view = kwargs.get("view")
    if isinstance(view, discord.ui.LayoutView):
        _inject_layout(view, meta)
    else:
        embed = kwargs.get("embed")
        if embed is not None:
            _inject_embed(embed, meta)
        embeds = kwargs.get("embeds")
        if embeds:
            for e in embeds:
                if e is not None:
                    _inject_embed(e, meta)


def _wrap_coro(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        _transform_kwargs(kwargs)
        return await fn(*args, **kwargs)

    return wrapper


def install() -> None:
    """Установить обёртки на send/edit (идемпотентно)."""
    global _installed
    if _installed:
        return
    _installed = True

    discord.InteractionResponse.send_message = _wrap_coro(
        discord.InteractionResponse.send_message
    )
    discord.InteractionResponse.edit_message = _wrap_coro(
        discord.InteractionResponse.edit_message
    )
    discord.Webhook.send = _wrap_coro(discord.Webhook.send)
    discord.Webhook.edit_message = _wrap_coro(discord.Webhook.edit_message)
    discord.Message.edit = _wrap_coro(discord.Message.edit)


def wrap_command_callback(command) -> None:
    """Обёрнуть _callback slash-команды контекстом мета."""
    original = getattr(command,"_callback", None)
    if original is None or getattr(original,"_rz3_wrapped", False):
        return

    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        interaction = next(
            (a for a in args if isinstance(a, discord.Interaction)), None
        )
        token = (
            _CTX.set(meta_from_interaction(interaction))
            if interaction is not None
            else _CTX.set(None)
        )
        try:
            return await original(*args, **kwargs)
        finally:
            _CTX.reset(token)

    wrapped._rz3_wrapped = True
    command._callback = wrapped


def wrap_all_commands(tree) -> None:
    """Рекурсивно обернуть все команды дерева (включая группы)."""

    def _walk(commands):
        for cmd in commands:
            wrap_command_callback(cmd)
            children = getattr(cmd,"children", None)
            if children:
                _walk(list(children.values()))
            nested = getattr(cmd,"commands", None)
            if isinstance(nested, dict):
                _walk(list(nested.values()))

    try:
        _walk(tree.get_commands())
    except Exception:
        pass
