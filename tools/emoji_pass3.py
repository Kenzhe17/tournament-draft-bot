import pathlib
import re
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


# quote + optional spaces + run of non-meaningful emoji exprs
PAT = re.compile(
    r"([\"'`])[ \t]*((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"
)
NL_PAT = re.compile(
    r"(\\n[ \t]*)((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"
)
REAL_NL = re.compile(
    r"(\n[ \t]*)((?:\{(?:replace_emojis|get_emoji|em)\([^}]*\)\}[ \t]*)+)"
)

changed_files = []
for d in ["views", "cogs", "services", "utils", "games", "models", "storage"]:
    for p in sorted(pathlib.Path(d).rglob("*.py")):
        sp = str(p)
        if "__pycache__" in sp or "design" in sp:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        orig = src

        def repl(m):
            new, ch = strip_run(m.group(2))
            return (m.group(1) if ch else m.group(0) + m.group(2)[:0]) if False else (
                (m.group(1) if ch else m.group(0))
            )

        # For quote pattern: replacement is quote + stripped run; but if run
        # becomes empty we still consumed leading spaces -> fine.
        def repl_q(m):
            new, ch = strip_run(m.group(2))
            if not ch:
                return m.group(0)
            return m.group(1) + new

        def repl_nl(m):
            new, ch = strip_run(m.group(2))
            if not ch:
                return m.group(0)
            return m.group(1) + new

        src = PAT.sub(repl_q, src)
        src = NL_PAT.sub(repl_nl, src)
        src = REAL_NL.sub(repl_nl, src)

        if src != orig:
            p.write_text(src, encoding="utf-8")
            changed_files.append(sp)

print("\n".join(changed_files))
print(len(changed_files))
