"""Мета-информация команд: author (/cmd), иконка бота, аватар игрока.

ContextVar заполняется обёрткой вокруг callback'ов slash-команд
(см. install_command_meta в bot.py). Все send/edit-вызовы внутри
команды автоматически получают:

- author-секцию «/command» с иконкой бота в начало V2-карточки
- аватар игрока как thumbnail, если у карточки его ещё нет
- author + thumbnail на классических Embed

Component-callback'и (кнопки/select) выполняются вне контекста
команды — мета не проставляется, турнирные сообщения не засоряются.
"""

from __future__ import annotations

import contextvars
import functools

import discord

_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "rz3_command_meta", default=None
)

_installed = False


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
    return {
        "name": f"/{name}",
        "user_avatar": _avatar_url(getattr(interaction,"user", None)),
        "bot_avatar": _avatar_url(bot_user),
    }


def _set_interaction_ctx(interaction):
    return _CTX.set(meta_from_interaction(interaction))


# ---------------------------------------------------------------------------
# Инъекция в V2 LayoutView
# ---------------------------------------------------------------------------

def _first_container(layout: discord.ui.LayoutView):
    for child in layout.children:
        if isinstance(child, discord.ui.Container):
            return child
    return None


def _author_section(meta: dict) -> discord.ui.Item:
    text = f"-# `{meta['name']}`"
    if meta.get("bot_avatar"):
        return discord.ui.Section(
            discord.ui.TextDisplay(content=text),
            accessory=discord.ui.Thumbnail(media=meta["bot_avatar"]),
        )
    return discord.ui.TextDisplay(content=text)


def _inject_layout(layout: discord.ui.LayoutView, meta: dict) -> None:
    container = _first_container(layout)
    if container is None:
        container = discord.ui.Container()
        layout.add_item(container)

    # 1) Author-секция в самое начало контейнера
    author = _author_section(meta)
    container.add_item(author)
    # add_item кладёт в конец — поднимаем в начало
    children = container._children
    children.remove(author)
    children.insert(0, author)

    # 2) Аватар игрока — на первый текстовый блок без своей картинки
    avatar = meta.get("user_avatar")
    if not avatar:
        return
    for child in list(container.children):
        if child is author:
            continue # author-секция с иконкой бота — не блокирует аватар
        if isinstance(child, discord.ui.Section):
            accessory = getattr(child,"accessory", None)
            if isinstance(accessory, discord.ui.Thumbnail):
                return # уже есть thumbnail (аватар/embed-thumb)
            continue
        if isinstance(child, discord.ui.MediaGallery):
            return # уже есть картинка
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
            return
        if isinstance(child, discord.ui.ActionRow):
            return # кнопки раньше текста — не лезем дальше


# ---------------------------------------------------------------------------
# Инъекция в классические Embed
# ---------------------------------------------------------------------------

def _inject_embed(embed: discord.Embed, meta: dict) -> None:
    embed.set_author(
        name=meta["name"],
        icon_url=meta.get("bot_avatar"),
    )
    if not embed.thumbnail and not embed.image:
        if meta.get("user_avatar"):
            embed.set_thumbnail(url=meta["user_avatar"])


# ---------------------------------------------------------------------------
# Обёртки send/edit
# ---------------------------------------------------------------------------

def _transform_kwargs(kwargs: dict) -> None:
    """Мутирует kwargs: добавляет author/avatar по ctx-мета."""
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
