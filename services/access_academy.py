"""Academy организатора: руководство, кнопки панели доступа, ЛС.

Единый источник контента обучения — только реально доступные
организатору функции: ORG-1/2/3 + /tournament create. Никаких
BR/Ultimatum/Cash Cup — у ORG на них нет полномочий.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from config import ACCESS_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

ORG_ROLE_IDS = (ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID)


def org_panel_url(guild_id: int) -> str | None:
    """Ссылка на сообщение ORG-панели из org_role_view state."""
    try:
        from views.org_role_view import load_state
        state = load_state()
        if state and state.guild_id == guild_id and state.channel_id \
                and state.message_id:
            return (
                f"https://discord.com/channels/{guild_id}/"
                f"{state.channel_id}/{state.message_id}"
            )
    except Exception as exc:
        logger.warning("academy: org panel url failed: %s", exc)
    return None


def guide_lines() -> list[str]:
    """Руководство организатора — только фактические функции."""
    return [
        "## ОРГАНИЗАТОР — ПУТЬ К ТУРНИРУ",
        "",
        "### 1. Роль «Доступ»",
        "- «Доступ» позволяет занимать свободные ORG-слоты — "
        "сама по себе турниры создавать не даёт.",
        "- ORG-1/2/3 — роли организаторов, каждая привязана к "
        "своему турнирному каналу.",
        "- Занять можно только свободный слот; после работы роль "
        "нужно сдать.",
        "",
        "### 2. ORG-слот",
        "- Откройте **Панель организатора** (кнопка ниже или "
        "ORG-панель на сервере) и выберите свободную роль.",
        "- Перейдите в турнирный канал вашей ORG-роли.",
        "",
        "### 3. Создание турнира",
        "- Команда: `/tournament create` — только в вашем "
        "турнирном канале.",
        "- `size` — 8, 16 или 32 игрока.",
        "- `formation` — `draft` (драфт капитанами), "
        "`balanced-sr` (баланс по SR) или `random`.",
        "",
        "### 4. Регистрация",
        "- Откройте регистрацию кнопкой в карточке турнира.",
        "- Следите за лимитом участников и списком заявок.",
        "- Закройте регистрацию и перейдите к формированию команд.",
        "",
        "### 5. Проведение",
        "- Драфт или балансировка — по выбранному режиму.",
        "- Открывайте комнаты, публикуйте данные, загружайте "
        "скриншоты результатов.",
        "- Проверяйте и подтверждайте результаты в карточке "
        "матча.",
        "",
        "### 6. Завершение",
        "- Подтвердите финальный результат — итоги появятся в "
        "карточке и журнале турнира.",
        "- После завершения **сдайте ORG-роль** в панели "
        "организатора.",
        "",
        "-# ORG-роль не даёт доступа к Battle Royale, "
        "Ultimatum и Cash Cup — для них нужны отдельные права.",
    ]


def build_guide_view() -> discord.ui.LayoutView:
    """CV2-руководство (для ephemeral-ответа и ЛС)."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        accent_colour=discord.Colour.gold())
    container.add_item(discord.ui.TextDisplay(
        content="\n".join(guide_lines())[:4000]))
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# Личное сообщение после получения «Доступа»
# ---------------------------------------------------------------------------


class AccessDmView(discord.ui.View):
    """Persistent View ЛС: «Пройти обучение» + ORG-панель."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Пройти обучение",
        style=discord.ButtonStyle.primary,
        custom_id="access_dm:guide",
    )
    async def guide(
        self, interaction: discord.Interaction, _button
    ) -> None:
        await interaction.response.send_message(
            view=build_guide_view(), allowed_mentions=_NO_MENTIONS)

    @discord.ui.button(
        label="Открыть ORG-панель",
        style=discord.ButtonStyle.secondary,
        custom_id="access_dm:orgpanel",
    )
    async def orgpanel(
        self, interaction: discord.Interaction, _button
    ) -> None:
        guild_id = interaction.guild_id or 0
        url = org_panel_url(guild_id) if guild_id else None
        if url:
            await interaction.response.send_message(url,
                                                    allowed_mentions=_NO_MENTIONS)
        else:
            await interaction.response.send_message(
                "ORG-панель пока не настроена — обратитесь к "
                "администратору.", allowed_mentions=_NO_MENTIONS)


async def send_welcome_dm(
    member: discord.Member, *, guild_id: int,
) -> bool:
    """Отправить инструкцию новому владельцу «Доступа» один раз.

    False — уже отправляли или ЛС закрыты (выдача роли не
    блокируется). Повторные сообщения после рестарта исключены
    флагом access_welcomed.
    """
    from storage import access_store
    try:
        if await access_store.was_welcomed(guild_id, member.id):
            return False
    except Exception as exc:
        logger.warning("academy: welcomed check failed: %s", exc)
        return False
    try:
        await member.send(
            "# r!z3 CLASSIC · ДОСТУП ОРГАНИЗАТОРА\n\n"
            "Вы получили доступ к организации турниров.\n\n"
            "Чтобы начать:\n"
            "1. Откройте панель организаторов.\n"
            "2. Займите свободный ORG-слот.\n"
            "3. Перейдите в свой турнирный канал.\n"
            "4. Используйте `/tournament create`.\n"
            "5. После окончания турнира сдайте ORG-роль.",
            view=AccessDmView(),
            allowed_mentions=_NO_MENTIONS,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info("academy: welcome DM closed guild=%s user=%s: %s",
                    guild_id, member.id, exc)
        # ЛС закрыты — не считаем «отправленным», но и не ретраим
        # каждый запуск: помечаем, чтобы не спамить.
        try:
            await access_store.mark_welcomed(guild_id, member.id)
        except Exception:
            pass
        return False
    except Exception as exc:
        logger.warning("academy: welcome DM failed: %s", exc)
        return False
    try:
        await access_store.mark_welcomed(guild_id, member.id)
    except Exception as exc:
        logger.warning("academy: mark_welcomed failed: %s", exc)
    return True
