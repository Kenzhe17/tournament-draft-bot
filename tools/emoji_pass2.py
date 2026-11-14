import re
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KEEP = {
    "money", "coin", "gold_medal", "silver_medal", "bronze_medal",
    "trophy", "winner", "crown", "medal", "rating", "guild_tag",
}


def keep_key(key: str) -> bool:
    return key in KEEP or key.startswith(
        ("num_", "icon_", "case_", "rare_", "emoji_")
    )


EXPR = re.compile(r"\{(?:replace_emojis|get_emoji|em)\(\s*['\"]([^'\"]+)['\"]\s*\)\}")


def strip_run(text: str):
    """Remove leading run of non-meaningful emoji exprs + spaces."""
    out = []
    i = 0
    changed = False
    while i < len(text):
        m = EXPR.match(text, i)
        if m and not keep_key(m.group(1)):
            j = m.end()
            while j < len(text) and text[j] in " \t":
                j += 1
            i = j
            changed = True
            continue
        out.append(text[i])
        i += 1
    return ("".join(out), changed)


# literal backslash-n inside string literals, or real newline
NL_PATTERNS = [
    re.compile(r"(\\n[ \t]*)((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"),
    re.compile(r"(\n[ \t]*)((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"),
]
START_PATTERN = re.compile(
    r"([\"'`])((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"
)

files = []
for d in ["views", "cogs", "services", "utils", "games", "models", "storage"]:
    files += list(pathlib.Path(d).rglob("*.py"))
files.append(pathlib.Path("bot.py"))

changed_files = []
for path in files:
    if "__pycache__" in str(path):
        continue
    p = pathlib.Path(path)
    try:
        src = p.read_text(encoding="utf-8")
    except Exception:
        continue
    orig = src

    def repl(m):
        new, ch = strip_run(m.group(2))
        return m.group(1) + new if ch else m.group(0)

    src = START_PATTERN.sub(repl, src)
    for pat in NL_PATTERNS:
        src = pat.sub(repl, src)

    if src != orig:
        p.write_text(src, encoding="utf-8")
        changed_files.append(str(p))

print("\n".join(changed_files))
print(len(changed_files))
