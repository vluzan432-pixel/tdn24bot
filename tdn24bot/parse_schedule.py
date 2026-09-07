"""
parse_schedule.py

Перетворює .docx з розкладом (формат ФММ: 5 таблиць Пн-Пт, колонка на групу)
у schedule.json, який потім читає бот.

Використання:
    python parse_schedule.py розклад.docx "ТДН-24" schedule.json
"""

import html
import json
import re
import sys

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

DAY_NAMES = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
DATE_RE = re.compile(r"\d{2}\.\d{2}")

# Рядок-початок сесії: "Лек.", "Пр.", "Сем.", "Лаб." і т.п. — слово з крапкою,
# і десь далі в рядку є дата. Це відрізняє його від рядка викладача типу
# "ст. викл. Іванов І.І." (там теж є крапка після слова, але немає дати).
SESSION_LINE_RE = re.compile(r"^([А-ЯІЇЄҐа-яіїєґA-Za-z]+)\.\s*(.+)$")
TEACHER_HINT_RE = re.compile(r"[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.")  # ініціали типу "І.І."

TYPE_LABELS = {
    "лек": ("Лекція", "📖"),
    "пр": ("Практичне заняття", "📝"),
    "сем": ("Семінар", "💬"),
    "лаб": ("Лабораторна робота", "🧪"),
    "контр": ("Контрольний захід", "🧾"),
    "мк": ("Модульний контроль", "🧾"),
}


def normalize_apostrophes(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u02bc", "'").replace("`", "'")


def get_day_labels_and_tables(doc: Document):
    """Повертає список (назва_дня, table_object).

    У цьому шаблоні підпис дня ("Понеділок ІІІ") стоїть ПІД таблицею, якій він
    належить (як підпис до малюнка), а не над нею. Тому просто збираємо таблиці
    та підписи-дні в порядку появи й зіставляємо їх по індексу (i-та таблиця =
    i-й підпис дня)."""
    body = doc.element.body
    tables = list(doc.tables)
    day_labels = []
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            text = normalize_apostrophes(p.text.strip())
            if any(text.startswith(d) for d in DAY_NAMES):
                day_labels.append(text)

    return list(zip(day_labels, tables))


def type_label(abbrev: str):
    key = abbrev.strip().lower().rstrip(".")
    return TYPE_LABELS.get(key, (abbrev.strip(), "🔖"))


def parse_content(content: str):
    """Розбирає текст клітинки на структуру: предмет, примітка, сесії
    (тип+дата+ознака ПК), викладач, аудиторія. Якщо структура незвична —
    залишає 'raw' як резервний варіант показу."""
    lines = [l.strip() for l in content.split("\n") if l.strip()]

    subject_lines = []
    note = None
    sessions = []  # [{"type": "Лек", "date": "21.09", "pk": False}, ...]
    teacher = None
    room_lines = []
    seen_teacher = False

    for line in lines:
        m = SESSION_LINE_RE.match(line)
        has_date = bool(DATE_RE.search(line))

        if m and has_date and not TEACHER_HINT_RE.search(line):
            abbrev, rest = m.groups()
            for token in rest.split(","):
                token = token.strip()
                date_m = DATE_RE.search(token)
                if not date_m:
                    continue
                sessions.append(
                    {
                        "type": abbrev,
                        "date": date_m.group(),
                        "pk": "ПК" in token.upper(),
                    }
                )
        elif line.startswith("(") and not seen_teacher:
            note = (note + " " if note else "") + line
        elif TEACHER_HINT_RE.search(line) and not seen_teacher:
            # інколи викладач і аудиторія написані в одному рядку через кому
            room_split = re.split(r",?\s*(?=ауд\.?)", line, maxsplit=1, flags=re.IGNORECASE)
            teacher = room_split[0].strip().rstrip(",")
            if len(room_split) > 1:
                room_lines.append(room_split[1].strip())
            seen_teacher = True
        elif "ауд" in line.lower():
            room_lines.append(line)
        elif seen_teacher:
            # рядок після викладача, що не містить "ауд" — швидше за все,
            # продовження адреси аудиторії
            room_lines.append(line)
        else:
            subject_lines.append(line)

    all_dates = sorted(set(DATE_RE.findall(content)))

    parsed_ok = bool(subject_lines) and bool(sessions) and "Підгрупа" not in content

    return {
        "subject": " ".join(subject_lines) if subject_lines else None,
        "note": note,
        "sessions": sessions,
        "teacher": teacher,
        "room": ", ".join(room_lines) if room_lines else None,
        "dates": all_dates,
        "raw": content,
        "parsed_ok": parsed_ok,
    }


def find_group_column(header_cells, group_name):
    for i, cell_text in enumerate(header_cells):
        if group_name in cell_text:
            return i
    return None


def parse(docx_path: str, group_name: str):
    doc = Document(docx_path)
    day_tables = get_day_labels_and_tables(doc)

    schedule = {"group": group_name, "days": {}}

    for day_label, table in day_tables:
        if not day_label:
            continue
        day_key = day_label.split()[0]  # напр. "Понеділок ІІІ" -> "Понеділок"

        header = [c.text.strip() for c in table.rows[0].cells]
        col_idx = find_group_column(header, group_name)
        if col_idx is None:
            continue

        seen = set()
        lessons = []
        for row in table.rows[1:]:
            pair_num = row.cells[0].text.strip()
            time_range = row.cells[1].text.strip().replace("\n", " ")
            content = row.cells[col_idx].text.strip()
            if not content:
                continue
            key = (pair_num, time_range, content)
            if key in seen:
                continue
            seen.add(key)

            parsed = parse_content(content)
            lessons.append({"pair": pair_num, "time": time_range, **parsed})

        schedule["days"].setdefault(day_key, [])
        schedule["days"][day_key].extend(lessons)

    return schedule


def main():
    if len(sys.argv) != 4:
        print('Використання: python parse_schedule.py розклад.docx "ТДН-24" schedule.json')
        sys.exit(1)

    docx_path, group_name, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    schedule = parse(docx_path, group_name)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(schedule, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in schedule["days"].values())
    print(f"Готово! Знайдено {total} записів для групи {group_name} у {out_path}")


if __name__ == "__main__":
    main()
