"""Smoke-тесты запуска: каждый extension из bot.load_extension
должен иметь entry point setup(), иначе bot падает на старте
с NoEntryPointError (инцидент: cogs.br после PR #43)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _module_file(dotted: str) -> Path:
    parts = dotted.split(".")
    py = REPO.joinpath(*parts).with_suffix(".py")
    if py.exists():
        return py
    init = REPO.joinpath(*parts) / "__init__.py"
    assert init.exists(), f"module file not found for {dotted}"
    return init


def _defines_setup(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "setup"
        for node in tree.body
    )


def _load_extension_modules() -> list[str]:
    src = (REPO / "bot.py").read_text(encoding="utf-8")
    return re.findall(r'load_extension\("([^"]+)"\)', src)


def test_all_load_extension_modules_define_setup():
    modules = _load_extension_modules()
    assert modules, "bot.py: вызовы load_extension не найдены"
    missing = [m for m in modules if not _defines_setup(_module_file(m))]
    assert not missing, f"extensions без setup(): {missing}"


@pytest.mark.asyncio
async def test_cogs_br_load_extension_and_binds_subcommands():
    """Реальный путь prod: commands.Bot.load_extension('cogs.br')
    без подключения к Discord. Проверяет entry point, BRCog,
    вложение /tournament br и binding всех пяти подкоманд."""
    import asyncio

    import discord
    from discord import app_commands
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    try:
        # /tournament регистрируется класс-атрибутом TournamentCog;
        # здесь — пустая группа с тем же именем.
        bot.tree.add_command(app_commands.Group(
            name="tournament", description="Управление турнирами"))
        await bot.load_extension("cogs.br")  # именно этот вызов падал в prod

        cog = bot.get_cog("BRCog")
        assert cog is not None

        tourney = bot.tree.get_command("tournament")
        assert isinstance(tourney, app_commands.Group)
        br = next((c for c in tourney.commands
                   if getattr(c, "name", None) == "br"), None)
        assert isinstance(br, app_commands.Group), "/tournament br не найден"

        names = {c.name for c in br.commands}
        assert names == {"create", "delete", "test_fill",
                         "test_teams", "test_stats"}
        for cmd in br.commands:
            assert getattr(cmd, "binding", None) is cog, (
                f"{cmd.name}: binding не указывает на BRCog")
    finally:
        await asyncio.sleep(0)
