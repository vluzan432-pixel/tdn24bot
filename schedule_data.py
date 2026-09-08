"""
schedule_data.py — завантаження розкладу з schedules.json (кілька груп в
одному файлі) та спільні функції, якими користуються і бот, і перевірка
нагадувань.
"""

import json
import os
import re
from datetime import date, time as time_cls

DAY_NAMES = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

TYPE_LABELS = {
    "лек": ("Лекція", "📖"),
    "пр": ("Практичне заняття", "📝"),
    "сем": ("Семінар", "💬"),
    "лаб": ("Лабораторна робота", "🧪"),
    "контр": ("Контрольний захід", "🧾"),
    "мк": ("Модульний контроль", "📦"),
    "пк": ("Проміжний контроль", "🎯"),
}

# Пошук за префіксом, а не точним співпадінням — щоб "Семінар." чи
# "Практ." розпізнавались так само, як звичні короткі "Сем."/"Пр.".
# Порядок важливий лише якби один префікс був початком іншого — тут такого
# немає, але "пк" перевіряємо окремо від "пр", бо обидва починаються на "п".
_TYPE_PREFIXES = [
    ("лек", "лек"),
    ("сем", "сем"),
    ("лаб", "лаб"),
    ("пк", "пк"),
    ("контр", "контр"),
    ("мк", "мк"),
    ("пр", "пр"),
]

SCHEDULE_PATH = os.environ.get("SCHEDULE_PATH", "schedules.json")
ZOOM_PATH = os.environ.get("ZOOM_PATH", "zoom_links.json")

with open(SCHEDULE_PATH, encoding="utf-8") as f:
    SCHEDULES = json.load(f)  # {"ТДН-24": {"days": {...}}, "ТДН-25": {...}}

try:
    with open(ZOOM_PATH, encoding="utf-8") as f:
        ZOOM_LINKS = json.load(f)
except FileNotFoundError:
    ZOOM_LINKS = {}

GROUPS = sorted(SCHEDULES.keys())

TIME_START_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def classify_type(abbrev: str) -> str:
    key = (abbrev or "").strip().lower().rstrip(".")
    for prefix, canon in _TYPE_PREFIXES:
        if key.startswith(prefix):
            return canon
    return key


def type_label(abbrev: str):
    key = classify_type(abbrev)
    return TYPE_LABELS.get(key, (abbrev.strip(), "🔖"))


def _normalize_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", (subject or "").strip().lower())


_lecture_subjects_cache: dict[str, set] = {}


def _lecture_subjects(group: str) -> set:
    """Множина предметів групи, у яких десь у розкладі є хоча б одна лекція.
    Рахується один раз на групу і кешується (SCHEDULES не змінюється без
    рестарту сервісу, тож кеш безпечний)."""
    if group not in _lecture_subjects_cache:
        subjects = set()
        schedule = SCHEDULES.get(group) or {}
        for day_lessons in schedule.get("days", {}).values():
            for lesson in day_lessons:
                for s in lesson.get("sessions", []):
                    if classify_type(s.get("type")) == "лек":
                        subjects.add(_normalize_subject(lesson.get("subject")))
                        break
        _lecture_subjects_cache[group] = subjects
    return _lecture_subjects_cache[group]


def subject_has_lectures(group: str, subject: str) -> bool:
    """True, якщо в предмета десь у розкладі групи є лекції — тобто це
    "лекційний" предмет, і випадкова практична серед лекцій — рідкісна,
    вартісна для показу подія. Якщо предмет виключно практичний (лекцій
    немає взагалі), його регулярні практичні — це рутина, не "важливе"."""
    return _normalize_subject(subject) in _lecture_subjects(group)


def find_zoom(teacher):
    if not teacher:
        return None
    for surname, info in ZOOM_LINKS.items():
        if re.search(rf"\b{re.escape(surname)}\b", teacher):
            return surname, info
    return None


def lesson_key(group: str, day_name: str, lesson: dict) -> str:
    return f"{group}|{day_name}|{lesson.get('pair')}|{lesson.get('time')}"


def lessons_for_date(group: str, d: date):
    schedule = SCHEDULES.get(group)
    if not schedule:
        return []
    day_name = DAY_NAMES[d.weekday()]
    day_lessons = schedule["days"].get(day_name, [])
    date_str = d.strftime("%d.%m")

    result = []
    for lesson in day_lessons:
        matches = [s for s in lesson.get("sessions", []) if s["date"] == date_str]
        if matches:
            result.append((lesson, matches))
        elif not lesson.get("dates") and not lesson.get("parsed_ok", True):
            # резервний варіант для нерозпізнаних клітинок — показуємо, тільки
            # якщо в них взагалі немає жодної дати (щотижнева пара)
            result.append((lesson, []))

    result.sort(key=lambda item: item[0]["pair"])
    return result


def lesson_start_time(lesson: dict):
    """Парсить '09:00-10:20' -> time(9, 0). None, якщо формат незрозумілий."""
    m = TIME_START_RE.search(lesson.get("time", ""))
    if not m:
        return None
    return time_cls(int(m.group(1)), int(m.group(2)))
