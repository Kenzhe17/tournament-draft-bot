"""Генерирует tools/report_ambiguous_names.md — отчёт для ручной
проверки 37 ambiguous записей из ocr_nicknames_reference.json.

Для каждой ambiguous-записи: файл, сторона, строка, текущий OCR
nickname и secondary_text (из tools/_fast_names.json) и
эталонное значение из reference. После ручной сверки по
изображениям обновить reference JSON и пересчитать
tools/eval_nicknames.py.

Запуск: .venv/Scripts/python tools/report_ambiguous_names.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "tests" / "fixtures" / "ocr_nicknames_reference.json"
FAST = ROOT / "tools" / "_fast_names.json"
OUT = ROOT / "tools" / "report_ambiguous_names.md"

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    ref = json.loads(REF.read_text(encoding="utf-8"))
    fast = json.loads(FAST.read_text(encoding="utf-8"))

    lines = [
        "# Ручная проверка ambiguous ников (OCR-assisted reference)",
        "",
        "Эталон `ocr_nicknames_reference.json` получен через OCR/bbox,",
        "а не вручную по изображениям. Записи ниже помечены ambiguous —",
        "проверьте их по скриншотам в `C:\\Users\\2006k\\Downloads\\скрины тест`",
        "и исправьте `nickname`/`secondary_text`/`ambiguous` в JSON при",
        "расхождении. После правки пересчитайте `tools/eval_nicknames.py`.",
        "",
        "| # | Файл | Сторона | Строка | OCR nickname | OCR secondary | Эталон nickname | Эталон secondary |",
        "|---|------|---------|--------|--------------|---------------|-----------------|-----------------|",
    ]
    n = 0
    missing_fast = 0
    for fname, sides in ref.items():
        fdata = fast.get(fname, {})
        for side in ("left", "right"):
            ref_rows = sides.get(side) or []
            fast_rows = fdata.get(side) or []
            for i, g in enumerate(ref_rows, start=1):
                if not g.get("ambiguous"):
                    continue
                n += 1
                fr = fast_rows[i - 1] if i - 1 < len(fast_rows) else {}
                if not fr:
                    missing_fast += 1
                lines.append(
                    f"| {n} | `{fname}` | {side} | {g.get('row', i)} "
                    f"| {fr.get('name') or '—'} "
                    f"| {fr.get('secondary_text') or '—'} "
                    f"| {g.get('nickname') or '—'} "
                    f"| {g.get('secondary_text') or '—'} |"
                )
    lines += [
        "",
        f"Всего ambiguous: {n} (missing fast data: {missing_fast}).",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written {OUT} ({n} rows, missing_fast={missing_fast})")


if __name__ == "__main__":
    main()
