"""
build_schedules.py — генерує ОДИН schedules.json одразу для кількох груп
з одного .docx (замінює запуск parse_schedule.py окремо на кожну групу).

Використання:
    python build_schedules.py розклад.docx schedules.json ТДН-24 ТДН-25 ТДН-26

Бот (bot.py) читає саме schedules.json — новий формат {група: розклад}.
Старий однокгруповий schedule.json, який робить parse_schedule.py, більше
ботом не використовується, але сам parse_schedule.py не змінювався і працює
як раніше.
"""

import json
import sys

from parse_schedule import parse


def main():
    if len(sys.argv) < 4:
        print('Використання: python build_schedules.py розклад.docx schedules.json "ГРУПА1" "ГРУПА2" ...')
        sys.exit(1)

    docx_path, out_path, *groups = sys.argv[1:]
    combined = {}
    for group in groups:
        combined[group] = parse(docx_path, group)
        total = sum(len(v) for v in combined[group]["days"].values())
        print(f"{group}: {total} записів")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(f"Готово! Збережено {len(groups)} груп(и) у {out_path}")


if __name__ == "__main__":
    main()
