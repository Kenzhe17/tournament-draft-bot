import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

E = r"(❌|✅|⚠️|❗|ℹ️|⏳|🔔)"
# emoji followed by whitespace => decorative prefix; '✅' as whole string untouched
LEAD = re.compile(r"([\"'`])[ \t]*" + E + r"[ \t]+")
NL_LEAD = re.compile(r"(\\n[ \t]*)" + E + r"[ \t]+")

changed = []
for d in ["views", "cogs", "services", "utils"]:
    for p in sorted(pathlib.Path(d).rglob("*.py")):
        sp = str(p)
        if "__pycache__" in sp or "design" in sp:
            continue
        if sp.startswith(("cogs\\games", "cogs/games")):
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        orig = src
        src = NL_LEAD.sub(lambda m: m.group(1), src)
        src = LEAD.sub(lambda m: m.group(1), src)
        if src != orig:
            p.write_text(src, encoding="utf-8")
            changed.append(sp)

print("\n".join(changed))
print(len(changed))
