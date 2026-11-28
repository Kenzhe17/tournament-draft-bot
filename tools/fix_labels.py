import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# file -> ordered list of labels for each label="" occurrence
PLAN = {
    "views/br_view.py": ["Назад", "Вперёд"],
    "views/freefire_profile_view.py": ["Закрыть"],
    "views/freefire_stats_view.py": ["Закрыть"],
    "views/games_view.py": ["Назад", "Вперёд"],
    "views/history_view.py": ["Назад", "Вперёд", "Назад", "Вперёд"],
    "views/leaderboard_view.py": ["Назад", "Вперёд", "Назад", "Вперёд"],
    "views/setup_view.py": ["Назад", "Вперёд"],
    "cogs/pet.py": ["Назад", "Вперёд", "Назад", "Вперёд", "Назад", "Вперёд"],
}

for rel, labels in PLAN.items():
    p = pathlib.Path(rel)
    src = p.read_text(encoding="utf-8")
    it = iter(labels)

    def repl(m):
        try:
            return f'label="{next(it)}"'
        except StopIteration:
            return m.group(0)

    new = re.sub(r'label=""', repl, src)
    if new != src:
        p.write_text(new, encoding="utf-8")
        print(rel, "updated")
    else:
        print(rel, "NO CHANGE")
