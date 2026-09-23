"""
cleanup_choir_overrides.py — одноразовий скрипт: знаходить і (лише за
явним підтвердженням) видаляє вручну додані "доп." пари з хором для групи
ТДН-24, які дублюють хор, що й так вже є в базовому розкладі
(schedules.json, Хоровий клас — Вівторок 2/3/4, П'ятниця 3/4).

Торкається ЛИШЕ записів у таблиці overrides з kind="add" (тобто саме
вручну доданих "зверху" пар — не базового розкладу і не change/cancel
правок), у назві яких є "хор". Нічого іншого не чіпає.

Запуск — з тими самими env-змінними, що й на Render (TURSO_DATABASE_URL,
TURSO_AUTH_TOKEN), або локально з DB_PATH:

    python cleanup_choir_overrides.py

Спершу ЛИШЕ показує знайдені записи. Видаляє тільки після того, як введеш
"так" на запит підтвердження.
"""

import db

GROUP = "ТДН-24"
SUBJECT_NEEDLE = "хор"  # без урахування регістру — знайде "Хоровий клас", "Хор" тощо


def main():
    db.init_db()
    rows = db.overrides_for_group(GROUP, kind="add")
    matches = [r for r in rows if SUBJECT_NEEDLE in (r["subject"] or "").lower()]

    if not matches:
        print(f"Не знайшов жодного вручну доданого запису з «{SUBJECT_NEEDLE}» для групи {GROUP}.")
        return

    print(f"Знайдено {len(matches)} запис(ів) групи {GROUP} (додані вручну, kind=add), що містять «{SUBJECT_NEEDLE}»:\n")
    for r in matches:
        print(
            f"  id={r['id']:>4}  дата={r['date']:>5}  пара={str(r['pair'] or ''):<6} "
            f"час={r['time'] or '-':<15} предмет={r['subject']!r:<30} викладач={r['teacher'] or '-'}"
        )

    print(
        "\nЦе видалить ЛИШЕ перелічені вище override-записи (вручну додані пари). "
        "Базовий розклад (schedules.json), де хор уже є на Вт/Пт, не чіпається."
    )
    answer = input("Видалити всі перелічені вище записи? (так/ні): ").strip().lower()
    if answer != "так":
        print("Скасовано, нічого не видалено.")
        return

    for r in matches:
        db.delete_override(r["id"])
    print(f"Видалено {len(matches)} запис(ів).")


if __name__ == "__main__":
    main()
