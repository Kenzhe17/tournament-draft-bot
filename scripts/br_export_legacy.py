"""Экспорт br_tournaments.json в формат, совместимый со СТАРЫМ кодом.

Использовать ТОЛЬКО при откате кода на версию до feat/br-cup-formats
(старый BRStore ждал плоский JSON {"guild:channel": {...}}).

    python scripts/br_export_legacy.py [input.json] [output.json]

По умолчанию читает $DATA_DIR/br_tournaments.json и пишет
br_tournaments.legacy.json рядом.

Ограничения (осознанные, НЕ тихие):
- В файл попадают только АКТИВНЫЕ турниры (не архивные/опубликованные —
  старый код не имеет history и воскресил бы их как активные).
- Фазы маппятся на ближайшие старые: registration→registration,
  draft/playing→bermuda, collect→kalahari. Результаты/очередь карт
  старый UI не покажет — статистика map_N нечитаема для старого кода.
- Отменённые и завершённые турниры НЕ экспортируются.
- После отката кода новый файл кладётся в DATA_DIR/br_tournaments.json
  вместо нового формата — на свой страх и риск, см. отчёт PR #43.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# новые фазы -> старые (старый enum: registration/bermuda/purgatory/
# kalahari/results/complete). draft/playing в старой модели = bermuda.
PHASE_MAP = {
    "registration": "registration",
    "draft": "bermuda",
    "playing": "bermuda",
    "collect": "kalahari",
}
SKIP_PHASES = {"results", "complete", "cancelled"}


def main() -> int:
    from config import DATA_DIR

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        DATA_DIR / "br_tournaments.json")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        src.with_name("br_tournaments.legacy.json"))

    if not src.exists():
        print(f"Файл {src} не найден — экспортировать нечего.")
        return 0
    raw = json.loads(src.read_text(encoding="utf-8"))

    # поддержать оба формата: новый {"active","history"} и старый плоский
    if isinstance(raw, dict) and ("active" in raw or "history" in raw):
        active_raw = raw.get("active", {}) or {}
        history_count = len(raw.get("history", []) or [])
    else:
        active_raw = raw if isinstance(raw, dict) else {}
        history_count = 0

    out: dict[str, dict] = {}
    skipped = 0
    for key, data in active_raw.items():
        if not isinstance(data, dict):
            skipped += 1
            continue
        phase = data.get("phase", "registration")
        if phase in SKIP_PHASES:
            skipped += 1
            continue
        entry = dict(data)
        entry["phase"] = PHASE_MAP.get(phase, "bermuda")
        # старый код не знает новые поля — оставляем, from_dict их игнорирует
        out[str(key)] = entry

    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"Активных турниров экспортировано: {len(out)}")
    print(f"Пропущено (завершённые/отменённые/битые): {skipped}")
    if history_count:
        print(f"Архивных записей в новом формате: {history_count} "
              "— в legacy-экспорт НЕ включены (старый код их воскресит).")
    print(f"Записано: {dst}")
    print("ВНИМАНИЕ: статистика map_N и итоги, введённые в новом коде, "
          "старым UI не отображаются. Это аварийный экспорт, не миграция.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
