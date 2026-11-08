"""View и embed для списка дней рождения (/birthdays)."""

import math
from datetime import date, datetime, timedelta, timezone

import discord
from utils.design import compose

from storage.player_stats_store import player_stats_store

PAGE_SIZE = 5


def astana_today() -> date:
    """Текущая дата по Астане (UTC+5, без перехода на летнее время)."""
    return (datetime.now(timezone.utc) + timedelta(hours=5)).date()


def days_until_birthday(birthday: str, today: date | None = None) -> int | None:
    """Дней до ближайшего дня рождения. birthday — 'ДД.ММ.ГГГГ'."""
    if not birthday:
        return None
    try:
        day_s, month_s = birthday.split(".")[:2]
        day, month = int(day_s), int(month_s)
    except (ValueError, AttributeError):
        return None
    today = today or astana_today()
    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return None
    nxt = this_year if this_year >= today else date(today.year + 1, month, day)
    return (nxt - today).days


async def collect_entries(guild: discord.Guild) -> list[dict]:
    """Собрать дни рождения сервера, отсортированные по ближайшим."""
    stats_list = await player_stats_store.get_all(guild.id)
    today = astana_today()
    entries = []
    for s in stats_list:
        birthday = getattr(s, "birthday", "") or ""
        days = days_until_birthday(birthday, today)
        if days is None:
            continue
        member = guild.get_member(s.user_id)
        entries.append({
            "user_id": s.user_id,
            "name": getattr(s, "name", "") or "Unknown",
            "birthday": birthday,
            "days": days,
            "mention": member.mention if member else None,
        })
    entries.sort(key=lambda e: (e["days"], e["name"].lower()))
    return entries


def _when_text(days: int) -> str:
    if days == 0:
        return "**сегодня!**"
    if days == 1:
        return "завтра"
    return f"через {days} дн."


def build_birthday_embed(member: discord.Member) -> discord.Embed:
    """Карточка поздравления (общая для /birthdaytest и авто-поздравлений)."""
    return discord.Embed(
        title=" ДЕНЬ РОЖДЕНИЯ",
        description=(
            f" {member.mention}\n\n"
            " С днём рождения! Желаем побед во всех турнирах и "
            "побольше монет на балансе "
        ),
        color=discord.Color.from_rgb(69, 69, 69),
    )


def create_birthdays_embed(guild: discord.Guild, entries: list[dict], page: int = 0) -> discord.Embed:
    """Embed страницы списка дней рождения."""
    total_pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    if chunk:
        lines = [
            f" {e['mention'] or e['name']} — `{e['birthday']}` • {_when_text(e['days'])}"
            for e in chunk
        ]
        desc = "**Ближайшие дни рождения**\n" + "\n".join(lines)
    else:
        desc = " Пока никто не указал день рождения.\nУстановить можно в настройках профиля."

    embed = discord.Embed(
        title=" ДНИ РОЖДЕНИЯ | /birthdays",
        description=desc,
        color=discord.Color.from_rgb(69, 69, 69),
    )
    embed.set_footer(text=f"Страница {page + 1}/{total_pages} • {len(entries)} всего")
    return embed


class BirthdaysView(discord.ui.View):
    """Пагинация списка дней рождения."""

    def __init__(self, guild: discord.Guild, entries: list[dict], page: int = 0):
        super().__init__(timeout=None)
        self.guild = guild
        self.entries = entries
        self.page = page
        self.total_pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        for item in self.children[:]:
            if isinstance(item, discord.ui.Button):
                self.remove_item(item)

        self.add_item(BirthdaysPrevButton())
        self.add_item(BirthdaysNextButton())
        self.children[0].disabled = self.page <= 0
        self.children[1].disabled = self.page >= self.total_pages - 1


class _PageButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.defer()
            return
        if isinstance(self, BirthdaysPrevButton):
            view.page = max(0, view.page - 1)
        else:
            view.page = min(view.total_pages - 1, view.page + 1)
        view.refresh_buttons()
        embed = create_birthdays_embed(view.guild, view.entries, view.page)
        await interaction.response.edit_message(view=compose(embed, view))


class BirthdaysPrevButton(_PageButton):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="◀", disabled=True)


class BirthdaysNextButton(_PageButton):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="▶", disabled=True)
