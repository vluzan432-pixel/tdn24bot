"""
bot.py — Telegram-бот "розклад на день" для ВСІЄЇ групи (мультикористувацький),
з нагадуваннями перед парами та нотатками/дедлайнами до пар.

Працює через WEBHOOK (не polling) — це принципово важливо для безкоштовного
хостингу на Render: якщо тримати відкрите polling-з'єднання, Render все одно
"засинає" сервіс, коли немає вхідних HTTP-запитів, і polling-цикл обривається
разом з ним. У webhook-режимі Telegram сам стукає в наш HTTP-ендпоінт при
новому повідомленні — і саме вхідний HTTP-запит "будить" застиглий Render.

Другий HTTP-ендпоінт, /cron, стукається зовнішнім розкладником (GitHub Actions,
раз на 5 хв) — він одночасно і будить бота, і каже йому перевірити, кому з
користувачів час нагадати про пару, що ось-ось почнеться.

Змінні середовища (усі задаються в Render → Environment):
    BOT_TOKEN          — токен від @BotFather
    WEBHOOK_BASE_URL    — публічна адреса сервісу, напр. https://mybot.onrender.com
    WEBHOOK_SECRET      — будь-який довгий випадковий рядок (сам придумай)
    CRON_SECRET         — інший довгий випадковий рядок, для захисту /cron
    SCHEDULE_PATH       — шлях до schedules.json (за замовч. "schedules.json")
    ZOOM_PATH           — шлях до zoom_links.json (за замовч. "zoom_links.json")
    TURSO_DATABASE_URL, TURSO_AUTH_TOKEN — див. db.py
    PORT                — підставляє сам Render
"""

import asyncio
import calendar as calendar_module
import html
import io
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import db
from schedule_data import (
    DAY_NAMES,
    GROUPS,
    TIME_START_RE,
    classify_type,
    find_zoom,
    group_subjects,
    lesson_key,
    lesson_start_time,
    lessons_for_date,
    subject_has_lectures,
    type_label,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("schedule-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
CRON_SECRET = os.environ.get("CRON_SECRET", "change-me-too")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
PORT = int(os.environ.get("PORT", 10000))

# Telegram user id(и) через кому — єдині, хто може редагувати ЗАГАЛЬНИЙ розклад
# групи. Свій id можна дізнатись командою /whoami після першого /start.
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}

PERMISSIONS = {"schedule", "announce", "materials", "polls", "attendance"}
PERMISSION_LABELS = {
    "schedule": "розклад", "announce": "оголошення", "materials": "матеріали", "polls": "опитування",
    "attendance": "відвідування",
}


def is_owner(chat_id: int) -> bool:
    """Головний адмін із змінної Render. Його права неможливо відібрати з бота."""
    return chat_id in ADMIN_IDS


def has_permission(chat_id: int, permission: str) -> bool:
    return is_owner(chat_id) or permission in db.staff_permissions(chat_id)


def is_admin(chat_id: int) -> bool:
    """Сумісність з існуючим редагуванням: адмін розкладу."""
    return has_permission(chat_id, "schedule")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class NoteState(StatesGroup):
    waiting_text = State()


class ReminderState(StatesGroup):
    waiting_custom_minutes = State()


class GroupEditState(StatesGroup):
    waiting_value = State()


class IndividualState(StatesGroup):
    waiting_value = State()


class ImportScheduleState(StatesGroup):
    waiting_file = State()
    waiting_surname = State()
    waiting_confirmation = State()


class MaterialState(StatesGroup):
    waiting_subject = State()
    waiting_title = State()
    waiting_content = State()


class AttendanceState(StatesGroup):
    waiting_roster_file = State()
    waiting_roster_group_name = State()


CHANGE_FIELDS = [
    ("subject", "нову назву предмета"),
    ("time", "новий час (напр. «14:10 - 15:30»)"),
    ("teacher", "викладача"),
    ("room", "аудиторію"),
    ("note", "примітку"),
]

ADD_FIELDS = [
    ("subject", "назву нової пари"),
    ("time", "час (напр. «14:10 - 15:30»)"),
    ("teacher", "викладача"),
    ("room", "аудиторію"),
]

IND_FIELDS = ["subject", "when", "time", "teacher", "room"]
IND_PROMPTS = {
    "subject": "Введи назву заняття (напр. «Фортепіано»):",
    "when": (
        "Коли? Напиши день тижня (Понеділок..Неділя) для щотижневого заняття, "
        "або дату у форматі ДД.ММ для одноразового (напр. «15.09»):"
    ),
    "time": "У який час? (напр. «16:00 - 16:45»):",
    "teacher": "Викладач? (або «-», якщо не важливо):",
    "room": "Аудиторія чи посилання? (або «-»):",
}

WEEKDAY_ALIASES = {
    "понед": "Понеділок", "вівтор": "Вівторок", "серед": "Середа", "четвер": "Четвер",
    "п'ят": "П'ятниця", "пят": "П'ятниця", "субот": "Субота", "неділ": "Неділя",
    "monday": "Понеділок", "tuesday": "Вівторок", "wednesday": "Середа", "thursday": "Четвер",
    "friday": "П'ятниця", "saturday": "Субота", "sunday": "Неділя",
}
IMPORT_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
IMPORT_DATE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b")


IMPORTANT_TYPES = {"пр", "сем", "пк"}  # практичне, семінар, проміжний контроль — інших типів немає в цьому розкладі

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def today_kyiv() -> date:
    """Render-сервер працює за UTC, а розклад/пари — за київським часом.
    Використовуй цю функцію замість date.today() всюди, де йдеться про
    'сьогодні' для студента."""
    return datetime.now(KYIV_TZ).date()


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)


def _normalized(value: object) -> str:
    return re.sub(r"[^a-zа-яіїєґ0-9]+", "", str(value or "").lower())


def _weekday_in(value: object) -> str | None:
    text = str(value or "").lower().replace("’", "'")
    return next((weekday for alias, weekday in WEEKDAY_ALIASES.items() if alias in text), None)


def _date_in(value: object) -> str | None:
    text = str(value or "")
    iso = re.search(r"\b\d{4}-(\d{1,2})-(\d{1,2})\b", text)
    match = iso or IMPORT_DATE_RE.search(text)
    if not match:
        return None
    if iso:
        month, day = iso.groups()
    else:
        day, month = match.group(0).replace("/", ".").replace("-", ".").split(".")[:2]
    return f"{int(day):02d}.{int(month):02d}"


def _import_schedule_from_excel(data: bytes, surname: str) -> list[dict]:
    """Витягує рядки поруч із прізвищем із довільної таблиці Excel.

    Розклади закладів різняться, тому результат завжди показується студенту
    перед збереженням. Це запобігає тихому додаванню помилкових пар.
    """
    needle = _normalized(surname)
    if len(needle) < 2:
        return []
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    found: list[dict] = []
    seen = set()

    # Основний формат розкладу кафедри: один рядок = одне заняття, із
    # заголовками «Прізвище здобувача», «День тижня», «Тривалість» тощо.
    # Це надійніше за пошук сусідніх комірок і не плутає викладача з предметом.
    header_aliases = {
        "student": ("прізвище", "здобувача"),
        "weekday": ("день", "тижня"),
        "date": ("дата",),
        "time": ("тривалість",),
        "room": ("аудиторія",),
        "teacher": ("прізвище", "викладача"),
        "subject": ("назва", "компоненти"),
    }
    for sheet in book.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        for header_row, header in enumerate(rows):
            columns = {}
            for name, parts in header_aliases.items():
                columns[name] = next(
                    (index for index, value in enumerate(header) if all(part in _normalized(value) for part in parts)),
                    None,
                )
            if any(columns[name] is None for name in ("student", "weekday", "date", "time", "subject")):
                continue
            for row in rows[header_row + 1:]:
                if columns["student"] >= len(row) or needle not in _normalized(row[columns["student"]]):
                    continue
                time = str(row[columns["time"]] or "").strip()
                weekday = _weekday_in(row[columns["weekday"]])
                date_value = _date_in(row[columns["date"]])
                subject = str(row[columns["subject"]] or "Індивідуальне заняття").strip()
                if not time or not (weekday or date_value):
                    continue
                key = (weekday, date_value, time, subject)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        # Якщо в Excel є точна дата, це разове заняття в
                        # календарі, а не правило «щотижня в цей день».
                        "weekday": None if date_value else weekday,
                        "date": date_value,
                        "time": time,
                        "subject": subject,
                        "teacher": str(row[columns["teacher"]] or "").strip() if columns["teacher"] is not None else None,
                        "room": str(row[columns["room"]] or "").strip() if columns["room"] is not None else None,
                    }
                )
    if found:
        book.close()
        return found

    # Запасний режим для простіших або нестандартних файлів. Його результат
    # завжди показується перед збереженням.
    for sheet in book.worksheets:
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        for row_index, row in enumerate(rows):
            name_columns = [column for column, cell in enumerate(row) if needle in _normalized(cell)]
            if not name_columns:
                continue
            row_text = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            time = next((match.group(0).replace(".", ":") for cell in row_text for match in [IMPORT_TIME_RE.search(cell)] if match), None)
            weekday = next((value for cell in row_text if (value := _weekday_in(cell))), None)
            date_value = next((date_value for cell in row_text if (date_value := _date_in(cell))), None)
            # Заголовки дня/часу часто знаходяться над рядком студента.
            for above in range(max(0, row_index - 8), row_index):
                header_cells = [str(cell).strip() for cell in rows[above] if cell is not None]
                if not weekday:
                    weekday = next((value for cell in header_cells if (value := _weekday_in(cell))), None)
                if not date_value:
                    date_value = next((date_value for cell in header_cells if (date_value := _date_in(cell))), None)
                if not time:
                    time = next((match.group(0).replace(".", ":") for cell in header_cells for match in [IMPORT_TIME_RE.search(cell)] if match), None)
            if not time:
                continue
            ignored = {needle, _normalized(time), _normalized(weekday), _normalized(date_value)}
            candidates = [cell for cell in row_text if len(_normalized(cell)) > 2 and _normalized(cell) not in ignored and needle not in _normalized(cell) and not IMPORT_TIME_RE.search(cell)]
            subject = candidates[0] if candidates else "Індивідуальне заняття"
            key = (weekday, date_value, time, subject)
            if key in seen:
                continue
            seen.add(key)
            found.append({"weekday": weekday, "date": date_value, "time": time, "subject": subject})

            # Інший поширений формат: прізвище є заголовком колонки, а його
            # заняття записані нижче. Перевіряємо цю колонку окремо.
            for column in name_columns:
                empty_rows = 0
                for below_index in range(row_index + 1, min(len(rows), row_index + 41)):
                    cell = rows[below_index][column] if column < len(rows[below_index]) else None
                    subject_below = str(cell or "").strip()
                    if not subject_below:
                        empty_rows += 1
                        if empty_rows >= 5:
                            break
                        continue
                    empty_rows = 0
                    if needle in _normalized(subject_below) or IMPORT_TIME_RE.search(subject_below):
                        continue
                    below_text = [str(value).strip() for value in rows[below_index] if value is not None]
                    time_below = next((match.group(0).replace(".", ":") for value in below_text for match in [IMPORT_TIME_RE.search(value)] if match), None)
                    weekday_below = next((weekday for cell_text in below_text if (weekday := _weekday_in(cell_text))), None)
                    date_below = next((date_value for cell_text in below_text if (date_value := _date_in(cell_text))), None)
                    for header_index in range(max(0, below_index - 8), below_index):
                        header = [str(value).strip() for value in rows[header_index] if value is not None]
                        if not weekday_below:
                            weekday_below = next((weekday for cell_text in header if (weekday := _weekday_in(cell_text))), None)
                        if not date_below:
                            date_below = next((date_value for cell_text in header if (date_value := _date_in(cell_text))), None)
                        if not time_below:
                            time_below = next((match.group(0).replace(".", ":") for value in header for match in [IMPORT_TIME_RE.search(value)] if match), None)
                    if not time_below:
                        continue
                    key = (weekday_below, date_below, time_below, subject_below)
                    if key not in seen:
                        seen.add(key)
                        found.append({"weekday": weekday_below, "date": date_below, "time": time_below, "subject": subject_below})
    book.close()
    return found


# ---------------------------------------------------------------- рендеринг

def type_line_for(matches: list):
    if not matches:
        return None
    parts = []
    for m in matches:
        label, emoji = type_label(m["type"])
        pk_suffix = " ⚠️ ПК" if m["pk"] else ""
        parts.append(f"{emoji} {label}{pk_suffix}")
    return " / ".join(parts)


def build_day_entries(chat_id: int, group: str, d: date):
    """Повертає єдиний список 'пар' на день для конкретного chat_id: базовий
    розклад групи з накладеними адмінськими правками (overrides) + особисті
    індивідуальні заняття цього користувача. Кожен елемент — уніфікований dict
    (не сирий lesson із schedules.json), щоб решта коду не мала розрізняти
    джерело даних."""
    day_name = DAY_NAMES[d.weekday()]
    date_str = d.strftime("%d.%m")
    entries = []

    overrides = db.overrides_for(group, date_str)
    cancel_map = {(o["base_pair"], o["base_time"]): o for o in overrides if o["kind"] == "cancel"}
    change_map = {(o["base_pair"], o["base_time"]): o for o in overrides if o["kind"] == "change"}
    add_list = [o for o in overrides if o["kind"] == "add"]

    # Лічильник повторів пари на цей день — у розкладі трапляються паралельні
    # вибіркові ОК (напр. "Диригування" і "Спів" обидва як пара №4, різні
    # підгрупи/викладачі/зум): без цього лічильника вони отримували ОДНАКОВИЙ
    # id "b:4" і ділили один запис відміток у базі, затираючи одна одну.
    pair_seen: dict = {}

    for lesson, matches in lessons_for_date(group, d):
        pk = (lesson["pair"], lesson["time"])
        pair_seen[lesson["pair"]] = pair_seen.get(lesson["pair"], 0) + 1
        dup_suffix = "" if pair_seen[lesson["pair"]] == 1 else f":{pair_seen[lesson['pair']]}"
        entry_id = f"b:{lesson['pair']}{dup_suffix}"
        # КРИТИЧНО: ключ нотатки включає саму дату (d.isoformat()), а не лише
        # день тижня. Розклад чергує предмети по тижнях (парний/непарний
        # тиждень) — той самий номер пари/часу в п'ятницю може бути зовсім
        # іншим предметом наступного тижня. Без дати в ключі нотатка,
        # додана до пари в одну п'ятницю, "перетікала" на той самий слот
        # у будь-яку іншу п'ятницю — навіть якщо там інший предмет.
        note_key = f"{lesson_key(group, day_name, lesson)}|{d.isoformat()}"

        if pk in cancel_map:
            entries.append(
                {
                    "id": entry_id,
                    "pair": lesson["pair"],
                    "time": lesson["time"],
                    "subject": lesson.get("subject"),
                    "note": lesson.get("note"),
                    "teacher": lesson.get("teacher"),
                    "room": lesson.get("room"),
                    "type_line": type_line_for(matches),
                    "cancelled": True,
                    "changed": False,
                    "source": "base",
                    "override_id": cancel_map[pk]["id"],
                    "note_key": note_key,
                    "orig_pair": lesson["pair"],
                    "orig_time": lesson["time"],
                }
            )
            continue

        ov = change_map.get(pk)
        entry = {
            "id": entry_id,
            "pair": lesson["pair"],
            "time": lesson["time"],
            "subject": lesson.get("subject"),
            "note": lesson.get("note"),
            "teacher": lesson.get("teacher"),
            "room": lesson.get("room"),
            "type_line": type_line_for(matches),
            "cancelled": False,
            "changed": False,
            "source": "base",
            "override_id": None,
            "note_key": note_key,
            "orig_pair": lesson["pair"],
            "orig_time": lesson["time"],
        }
        if ov:
            entry["changed"] = True
            entry["override_id"] = ov["id"]
            if ov.get("subject"):
                entry["subject"] = ov["subject"]
            if ov.get("time"):
                entry["time"] = ov["time"]
            if ov.get("teacher"):
                entry["teacher"] = ov["teacher"]
            if ov.get("room"):
                entry["room"] = ov["room"]
            if ov.get("note"):
                entry["note"] = ov["note"]
        entries.append(entry)

    for o in add_list:
        entries.append(
            {
                "id": f"o:{o['id']}",
                "pair": o.get("pair") or "•",
                "time": o.get("time"),
                "subject": o.get("subject"),
                "note": o.get("note"),
                "teacher": o.get("teacher"),
                "room": o.get("room"),
                "type_line": None,
                "cancelled": False,
                "changed": False,
                "source": "override_add",
                "override_id": o["id"],
                "note_key": f"ovr:{o['id']}",
            }
        )

    for il in db.individual_lessons_for(chat_id, day_name, date_str):
        entries.append(
            {
                "id": f"i:{il['id']}",
                "pair": "🎓",
                "time": il["time"],
                "subject": il.get("subject"),
                "note": il.get("note"),
                "teacher": il.get("teacher"),
                "room": il.get("room"),
                "type_line": "🎓 Індивідуальне",
                "cancelled": False,
                "changed": False,
                "source": "individual",
                "individual_id": il["id"],
                "note_key": f"ind:{il['id']}",
            }
        )

    entries.sort(key=lambda e: (e.get("time") or ""))
    return entries


def group_entries_for_edit(group: str, d: date):
    """Те саме, але без індивідуальних занять — для адмінського редагування
    загального розкладу (chat_id=0, бо індивідуальні тут не потрібні)."""
    return [e for e in build_day_entries(0, group, d) if e["source"] != "individual"]


def _entry_or_none(chat_id: int, group: str, d: date, idx: int):
    entries = build_day_entries(chat_id, group, d)
    if idx >= len(entries):
        return None
    return entries[idx]


def format_entry(entry: dict) -> str:
    label = "Індивідуальне" if entry["source"] == "individual" else f"Пара {html.escape(str(entry.get('pair') or ''))}"
    header = f"🕐 <b>{label}</b> · {html.escape(entry.get('time') or '')}"

    if entry.get("cancelled"):
        subject = html.escape(entry.get("subject") or "")
        return header + f"\n⛔ <s>{subject}</s> — скасовано"

    lines = [header]
    subject = html.escape(entry.get("subject") or "")
    if entry.get("note"):
        subject += f" <i>{html.escape(entry['note'])}</i>"
    if entry.get("changed"):
        prefix = "✏️ "
    elif entry["source"] == "individual":
        prefix = "🎓 "
    elif entry["source"] == "override_add":
        prefix = "➕ "
    else:
        prefix = "📘 "
    lines.append(f"{prefix}{subject}")

    if entry.get("type_line"):
        lines.append(entry["type_line"])
    if entry.get("teacher"):
        lines.append(f"👤 {html.escape(entry['teacher'])}")
    if entry.get("room"):
        lines.append(f"📍 {html.escape(entry['room'])}")
    return "\n".join(lines)


def format_day_for_chat(chat_id: int, group: str, d: date, entries: list | None = None) -> str:
    day_name = DAY_NAMES[d.weekday()]
    header = f"📅 <b>{day_name}, {d.strftime('%d.%m.%Y')}</b>\n👥 Група: {html.escape(group)}"

    if entries is None:
        entries = build_day_entries(chat_id, group, d)
    if not entries:
        return header + "\n\nПар немає 🎉"

    notes_by_lesson = db.notes_for_lessons(chat_id, [entry["note_key"] for entry in entries])
    blocks = []
    for entry in entries:
        text = format_entry(entry)
        notes = notes_by_lesson[entry["note_key"]]
        if notes:
            note_lines = "\n".join(f"  📝 {html.escape(n['text'])}" for n in notes)
            text += f"\n{note_lines}"
        blocks.append(text)

    divider = "\n➖➖➖➖➖➖➖➖\n"
    return header + "\n" + divider + divider.join(blocks)


async def broadcast_group(group: str, text: str):
    await _broadcast_to(db.users_in_group(group), text)


async def broadcast_announcement(group: str, text: str):
    await _broadcast_to(db.announcement_users_in_group(group), text)


async def _broadcast_to(chat_ids: list[int], text: str):
    async def send(chat_id: int):
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не вдалось розіслати повідомлення %s", chat_id)
    await asyncio.gather(*(send(chat_id) for chat_id in chat_ids))


MAX_UPCOMING_WEEKS = 8  # не даємо гортати роками наперед — розклад так далеко не сягає

DAY_SHORT = {
    "Понеділок": "Пн", "Вівторок": "Вт", "Середа": "Ср", "Четвер": "Чт",
    "П'ятниця": "Пт", "Субота": "Сб", "Неділя": "Нд",
}


def upcoming_important_text(group: str, week_offset: int = 0) -> str:
    today = today_kyiv()
    start = today + timedelta(days=7 * week_offset)
    end = start + timedelta(days=6)
    period = f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}"

    if week_offset == 0:
        header = f"🗓 <b>Найближчий тиждень</b> · {period}"
    else:
        header = f"🗓 <b>Тиждень +{week_offset}</b> · {period}"

    day_blocks = []
    for offset in range(7):
        d = start + timedelta(days=offset)
        day_name = DAY_NAMES[d.weekday()]
        items = []
        for lesson, matches in lessons_for_date(group, d):
            for m in matches:
                type_key = classify_type(m["type"])
                if type_key not in IMPORTANT_TYPES:
                    continue
                if type_key == "пр" and not subject_has_lectures(group, lesson.get("subject")):
                    # Суто практичний предмет (лекцій за ним немає взагалі) —
                    # його щотижневі практичні це рутина, а не "важлива подія".
                    continue
                label, emoji = type_label(m["type"])
                pk_suffix = " ⚠️ <b>ПК</b>" if m["pk"] else ""
                subject = html.escape(lesson.get("subject") or "")
                time_ = html.escape(lesson.get("time", ""))
                items.append(f"    {emoji} <code>{time_}</code>  {subject} · {label}{pk_suffix}")
        if items:
            today_mark = " 👈" if d == today else ""
            day_blocks.append(
                f"<b>{DAY_SHORT.get(day_name, day_name)}, {d.strftime('%d.%m')}</b>{today_mark}\n" + "\n".join(items)
            )

    if not day_blocks:
        body = "На цей тиждень нічого важливого не заплановано 🎉"
    else:
        body = "\n\n".join(day_blocks)

    return f"{header}\n{'─' * 18}\n\n{body}"


def upcoming_keyboard(week_offset: int) -> InlineKeyboardMarkup:
    nav_row = []
    if week_offset > 0:
        nav_row.append(InlineKeyboardButton(text="◀ Тиждень назад", callback_data=f"upcoming:{week_offset - 1}"))
    if week_offset < MAX_UPCOMING_WEEKS:
        nav_row.append(InlineKeyboardButton(text="Тиждень вперед ▶", callback_data=f"upcoming:{week_offset + 1}"))
    rows = [nav_row] if nav_row else []
    if week_offset != 0:
        rows.append([InlineKeyboardButton(text="📍 На цей тиждень", callback_data="upcoming:0")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MONTH_NAMES_UA = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]
MIN_CALENDAR_MONTH_OFFSET = -1  # можна глянути один місяць назад
MAX_CALENDAR_MONTH_OFFSET = 4   # і на 4 місяці наперед — цього вистачає на семестр


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    today = today_kyiv()
    first_of_target = date(year, month, 1)
    first_of_current = date(today.year, today.month, 1)
    offset_months = (first_of_target.year - first_of_current.year) * 12 + (first_of_target.month - first_of_current.month)

    weeks = calendar_module.Calendar(firstweekday=0).monthdatescalendar(year, month)

    rows = [[InlineKeyboardButton(text=d, callback_data="noop") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]]]
    for week in weeks:
        row = []
        for day_date in week:
            if day_date.month != month:
                row.append(InlineKeyboardButton(text=" ", callback_data="noop"))
            else:
                label = f"·{day_date.day}·" if day_date == today else str(day_date.day)
                row.append(InlineKeyboardButton(text=label, callback_data=f"day:{day_date.isoformat()}"))
        rows.append(row)

    nav_row = []
    if offset_months > MIN_CALENDAR_MONTH_OFFSET:
        prev_m = _add_months(first_of_target, -1)
        nav_row.append(InlineKeyboardButton(text="◀", callback_data=f"calendar:{prev_m.year}-{prev_m.month:02d}"))
    nav_row.append(
        InlineKeyboardButton(text=f"{MONTH_NAMES_UA[month]} {year}", callback_data="noop")
    )
    if offset_months < MAX_CALENDAR_MONTH_OFFSET:
        next_m = _add_months(first_of_target, 1)
        nav_row.append(InlineKeyboardButton(text="▶", callback_data=f"calendar:{next_m.year}-{next_m.month:02d}"))
    rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="📅 На сьогодні", callback_data="today")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def keyboard_for_day(chat_id: int, group: str, d: date, entries: list | None = None) -> InlineKeyboardMarkup:
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()
    rows = [
        [
            InlineKeyboardButton(text="◀ Назад", callback_data=f"day:{prev_day}"),
            InlineKeyboardButton(text="Вперед ▶", callback_data=f"day:{next_day}"),
        ],
        [
            InlineKeyboardButton(text="📅 На сьогодні", callback_data="today"),
            InlineKeyboardButton(text="📆 Календар", callback_data=f"calendar:{d.year}-{d.month:02d}"),
        ],
    ]

    if entries is None:
        entries = build_day_entries(chat_id, group, d)
    if any(find_zoom(e.get("teacher")) for e in entries if not e.get("cancelled")):
        rows.append([InlineKeyboardButton(text="🎥 Посилання в Zoom", callback_data=f"zoom_menu:{d.isoformat()}")])

    if entries:
        rows.append([InlineKeyboardButton(text="📝 Нотатки", callback_data=f"notes_menu:{d.isoformat()}")])

    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def zoom_menu_keyboard(chat_id: int, group: str, d: date) -> InlineKeyboardMarkup:
    rows = []
    for idx, entry in enumerate(build_day_entries(chat_id, group, d)):
        if entry.get("cancelled"):
            continue
        found = find_zoom(entry.get("teacher"))
        if not found:
            continue
        subject = entry.get("subject") or "Пара"
        label = subject if len(subject) <= 40 else subject[:37] + "..."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"zoom:{d.isoformat()}:{idx}")])
    rows.append([InlineKeyboardButton(text="🔙 До розкладу", callback_data=f"day:{d.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def zoom_info_keyboard(d: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 До списку предметів", callback_data=f"zoom_menu:{d.isoformat()}")],
            [InlineKeyboardButton(text="📅 До розкладу", callback_data=f"day:{d.isoformat()}")],
        ]
    )


def notes_menu_keyboard(chat_id: int, group: str, d: date, entries: list | None = None) -> InlineKeyboardMarkup:
    rows = []
    if entries is None:
        entries = build_day_entries(chat_id, group, d)
    notes_by_lesson = db.notes_for_lessons(chat_id, [entry["note_key"] for entry in entries])
    for idx, entry in enumerate(entries):
        count = len(notes_by_lesson[entry["note_key"]])
        subject = entry.get("subject") or f"Пара {entry.get('pair')}"
        label = subject if len(subject) <= 30 else subject[:27] + "..."
        mark = f"📝×{count}" if count else "➕"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"notes_lesson:{d.isoformat()}:{idx}")])
    rows.append([InlineKeyboardButton(text="🔙 До розкладу", callback_data=f"day:{d.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notes_lesson_keyboard(d: date, idx: int, notes: list) -> InlineKeyboardMarkup:
    rows = []
    for i, n in enumerate(notes, start=1):
        rows.append(
            [
                InlineKeyboardButton(text=f"✏️ Редагувати #{i}", callback_data=f"noteedit:{d.isoformat()}:{idx}:{n['id']}"),
                InlineKeyboardButton(text=f"🗑 Видалити #{i}", callback_data=f"notedel:{d.isoformat()}:{idx}:{n['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Додати нотатку", callback_data=f"noteadd:{d.isoformat()}:{idx}")])
    rows.append([InlineKeyboardButton(text="🔙 До списку предметів", callback_data=f"notes_menu:{d.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_choice_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=g, callback_data=f"setgroup:{g}")] for g in GROUPS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_text(group: str) -> str:
    return (
        f"🎓 <b>{html.escape(group)}</b>\n"
        "Обери розділ:\n\n"
        "📅 <b>Розклад пар</b> — заняття на день, гортання по датах або одразу через 📆 календар\n"
        "🗓 <b>Найближчі семінари / практичні / ПК</b> — важливе на кілька тижнів наперед, без рутинних практичних\n"
        "🎓 <b>Індивідуальні заняття</b> — твій особистий розклад, вручну або імпортом з Excel\n"
        "📚 <b>Матеріали</b> — посилання від старости та викладачів\n"
        "⚙️ <b>Налаштування</b> — нагадування перед парою, розклад на завтра, оголошення\n"
        "✏️ <b>Редагувати розклад</b> — виправити пару, якщо її перенесли\n"
        "👥 <b>Вибір групи</b> — змінити групу"
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Розклад пар", callback_data="today")],
        [InlineKeyboardButton(text="🗓 Найближчі семінари / практичні / ПК", callback_data="upcoming")],
        [InlineKeyboardButton(text="🎓 Індивідуальні заняття", callback_data="individual_menu")],
        [InlineKeyboardButton(text="📚 Матеріали", callback_data="materials_menu")],
        [InlineKeyboardButton(text="📋 Відвідування 🔒", callback_data="att_menu")],
        [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings_menu")],
        [InlineKeyboardButton(text="✏️ Редагувати розклад", callback_data="edit_menu")],
        [InlineKeyboardButton(text="👥 Вибір групи", callback_data="choose_group")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]])


def individual_menu_text(chat_id: int) -> str:
    base = (
        "🎓 <b>Індивідуальні заняття</b>\n\n"
        "Тут можна додати власні заняття (інструмент, вокал тощо), які бачиш "
        "тільки ти — вони з'являться в твоєму розкладі дня поруч із парами групи."
    )
    if db.has_imported_individual_lessons(chat_id):
        base += (
            "\n\n📥 Excel-розклад уже завантажено. Щоб залити оновлений файл — "
            "спершу видали поточні заняття кнопкою нижче, потім тисни "
            "«Додати файл Excel» знову."
        )
    else:
        base += "\n\nМожна також завантажити Excel-розклад і знайти себе за прізвищем."
    return base


def individual_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    lessons = db.all_individual_lessons(chat_id)
    has_import = any(l.get("source") == "import" for l in lessons)

    rows = [[InlineKeyboardButton(text="➕ Додати заняття", callback_data="ind_add")]]
    if not has_import:
        rows.append([InlineKeyboardButton(text="📥 Додати файл Excel", callback_data="ind_import")])
    if lessons:
        rows.append([InlineKeyboardButton(text="👀 Мої заняття", callback_data="ind_view")])
        rows.append([InlineKeyboardButton(text="🗑 Видалити всі індивідуальні заняття", callback_data="ind_delete_all_ask")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def individual_list_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for il in db.all_individual_lessons(chat_id):
        when = il.get("weekday") or il.get("date") or ""
        label = f"{il['subject']} ({when}, {il['time']})"
        label = label if len(label) <= 40 else label[:37] + "..."
        rows.append([InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"ind_del:{il['id']}")])
    rows.append([InlineKeyboardButton(text="🔙 Меню", callback_data="individual_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📋 Загальний (уся група) 🔒", callback_data="edit_scope:group")],
        [InlineKeyboardButton(text="👤 Індивідуальний (тільки я)", callback_data="edit_scope:personal")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_group_date_keyboard(group: str) -> InlineKeyboardMarkup:
    rows = []
    today = today_kyiv()
    for i in range(7):
        d = today + timedelta(days=i)
        if i == 0:
            label = "Сьогодні"
        elif i == 1:
            label = "Завтра"
        else:
            label = f"{DAY_NAMES[d.weekday()][:2]} {d.strftime('%d.%m')}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"editday:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_lesson_list_keyboard(group: str, d: date) -> InlineKeyboardMarkup:
    rows = []
    for idx, e in enumerate(group_entries_for_edit(group, d)):
        subject = e.get("subject") or f"Пара {e.get('pair')}"
        label = subject if len(subject) <= 30 else subject[:27] + "..."
        mark = "⛔ " if e.get("cancelled") else ("✏️ " if e.get("changed") else "")
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"editlesson:{d.isoformat()}:{idx}")])
    rows.append([InlineKeyboardButton(text="➕ Додати позачергову пару", callback_data=f"editadd:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="🔙 До вибору дати", callback_data="edit_scope:group")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_lesson_action_keyboard(d: date, idx: int, entry: dict) -> InlineKeyboardMarkup:
    rows = []
    if entry.get("cancelled") or entry.get("changed") or entry["source"] == "override_add":
        rows.append(
            [InlineKeyboardButton(text="↩️ Скасувати редагування", callback_data=f"editrevert:{d.isoformat()}:{idx}")]
        )
    else:
        rows.append([InlineKeyboardButton(text="⛔ Скасувати пару", callback_data=f"editcancel:{d.isoformat()}:{idx}")])
        rows.append(
            [InlineKeyboardButton(text="✏️ Змінити (час/ауд./викл.)", callback_data=f"editchange:{d.isoformat()}:{idx}")]
        )
    rows.append([InlineKeyboardButton(text="🔙 До списку пар", callback_data=f"editday:{d.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


REMINDER_PRESETS = [5, 10, 15, 20, 30, 45, 60]


def reminders_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    user = db.get_user(chat_id)
    on = user["reminders_on"] if user else True
    offsets = set(db.get_reminder_offsets(chat_id))
    tomorrow_on = user["tomorrow_on"] if user else False
    announcements_on = user["announcements_on"] if user else True

    rows = []
    preset_row = []
    for m in REMINDER_PRESETS:
        text = f"✅ {m} хв" if m in offsets else f"{m} хв"
        preset_row.append(InlineKeyboardButton(text=text, callback_data=f"togglemin:{m}"))
        if len(preset_row) == 4:
            rows.append(preset_row)
            preset_row = []
    if preset_row:
        rows.append(preset_row)

    custom_offsets = sorted((o for o in offsets if o not in REMINDER_PRESETS), reverse=True)
    if custom_offsets:
        rows.append([InlineKeyboardButton(text=f"✅ {m} хв ✕", callback_data=f"togglemin:{m}") for m in custom_offsets])

    rows.append([InlineKeyboardButton(text="✏️ Свій варіант (хв)", callback_data="custom_min")])
    rows.append([InlineKeyboardButton(text=("🔕 Вимкнути нагадування" if on else "🔔 Увімкнути нагадування"),
                                       callback_data="toggle_reminders")])
    rows.append([InlineKeyboardButton(
        text=("✅ Розклад на завтра" if tomorrow_on else "📅 Розклад на завтра: вимкнено"),
        callback_data="toggle_tomorrow",
    )])
    rows.append([InlineKeyboardButton(
        text=("📣 Оголошення: увімкнено" if announcements_on else "🔕 Оголошення: вимкнено"),
        callback_data="toggle_announcements",
    )])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def require_group(message_or_cb) -> str | None:
    chat_id = message_or_cb.from_user.id
    user = db.get_user(chat_id)
    if user:
        return user["group"]
    target = message_or_cb.message if isinstance(message_or_cb, CallbackQuery) else message_or_cb
    await target.answer("Спершу обери свою групу 👇", reply_markup=group_choice_keyboard())
    return None


# ------------------------------------------------------------------ хендлери

@dp.message(CommandStart())
async def cmd_start(message: Message):
    group = await require_group(message)
    if not group:
        return
    await message.answer(main_menu_text(group), reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(main_menu_text(group), reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "settings_menu")
async def cb_settings_menu(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спершу обери групу", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ <b>Налаштування</b>\n\n"
        "Нагадування — можна обрати кілька рубежів одразу (напр. 15, 10 і 5 хв), "
        "додати свій варіант хвилин, або вимкнути зовсім:",
        reply_markup=reminders_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "choose_group")
async def cb_choose_group(callback: CallbackQuery):
    await callback.message.edit_text("Обери свою групу:", reply_markup=group_choice_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "edit_menu")
async def cb_edit_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text("✏️ Який розклад редагувати?", reply_markup=edit_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "edit_scope:group")
async def cb_edit_scope_group(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Загальний розклад може редагувати тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(
        "✏️ Редагування загального розкладу групи.\nОбери дату:",
        reply_markup=edit_group_date_keyboard(group),
    )
    await callback.answer()


@dp.callback_query(F.data == "edit_scope:personal")
async def cb_edit_scope_personal(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(individual_menu_text(callback.from_user.id), reply_markup=individual_menu_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("editday:"))
async def cb_editday(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    await callback.message.edit_text(
        f"✏️ {d.strftime('%d.%m.%Y')} ({DAY_NAMES[d.weekday()]}) — обери пару:",
        reply_markup=edit_lesson_list_keyboard(group, d),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("editlesson:"))
async def cb_editlesson(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    idx = int(idx_str)
    entries = group_entries_for_edit(group, d)
    if idx >= len(entries):
        await callback.answer("Не знайдено, спробуй ще раз", show_alert=True)
        return
    entry = entries[idx]
    text = "✏️ Керування парою:\n\n" + format_entry(entry)
    await callback.message.edit_text(text, reply_markup=edit_lesson_action_keyboard(d, idx, entry))
    await callback.answer()


@dp.callback_query(F.data.startswith("editcancel:"))
async def cb_editcancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    idx = int(idx_str)
    entries = group_entries_for_edit(group, d)
    if idx >= len(entries):
        await callback.answer("Не знайдено, спробуй ще раз", show_alert=True)
        return
    entry = entries[idx]
    db.add_override(
        group,
        d.strftime("%d.%m"),
        "cancel",
        callback.from_user.id,
        base_pair=entry.get("orig_pair") or entry.get("pair"),
        base_time=entry.get("orig_time") or entry.get("time"),
    )
    subject = entry.get("subject") or "пару"
    await broadcast_group(
        group,
        f"⛔ <b>Скасовано:</b> {html.escape(subject)} — {d.strftime('%d.%m')} ({html.escape(entry.get('time') or '')})",
    )
    await callback.message.edit_text(
        f"Скасовано ✅\n\n✏️ {d.strftime('%d.%m.%Y')} — обери пару:", reply_markup=edit_lesson_list_keyboard(group, d)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("editrevert:"))
async def cb_editrevert(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    idx = int(idx_str)
    entries = group_entries_for_edit(group, d)
    if idx >= len(entries):
        await callback.answer("Не знайдено, спробуй ще раз", show_alert=True)
        return
    entry = entries[idx]
    if entry.get("override_id"):
        db.delete_override(entry["override_id"])
        subject = entry.get("subject") or "пару"
        await broadcast_group(group, f"↩️ <b>Повернуто без змін:</b> {html.escape(subject)} — {d.strftime('%d.%m')}")
    await callback.message.edit_text(
        f"Повернуто ✅\n\n✏️ {d.strftime('%d.%m.%Y')} — обери пару:", reply_markup=edit_lesson_list_keyboard(group, d)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("editchange:"))
async def cb_editchange(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    idx = int(idx_str)
    entries = group_entries_for_edit(group, d)
    if idx >= len(entries):
        await callback.answer("Не знайдено, спробуй ще раз", show_alert=True)
        return
    entry = entries[idx]
    await state.update_data(
        mode="change",
        group=group,
        iso_date=iso_date,
        base_pair=entry.get("orig_pair") or entry.get("pair"),
        base_time=entry.get("orig_time") or entry.get("time"),
        field_idx=0,
        values={},
    )
    await state.set_state(GroupEditState.waiting_value)
    await callback.message.answer(f"Введи {CHANGE_FIELDS[0][1]} (або «-», щоб лишити без змін):")
    await callback.answer()


@dp.callback_query(F.data.startswith("editadd:"))
async def cb_editadd(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Тільки адміністратор 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    iso_date = callback.data.split(":", 1)[1]
    await state.update_data(mode="add", group=group, iso_date=iso_date, field_idx=0, values={})
    await state.set_state(GroupEditState.waiting_value)
    await callback.message.answer(f"Введи {ADD_FIELDS[0][1]}:")
    await callback.answer()


@dp.message(StateFilter(GroupEditState.waiting_value))
async def group_edit_value_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    mode = data["mode"]
    fields = CHANGE_FIELDS if mode == "change" else ADD_FIELDS
    field_idx = data["field_idx"]
    field_key, _ = fields[field_idx]
    values = data["values"]

    text = message.text.strip()
    if mode == "change" and text == "-":
        pass  # лишаємо без змін
    elif field_key == "time" and not TIME_START_RE.search(text):
        # Без розпізнаваного часу (напр. "14:10") lesson_start_time() пізніше
        # мовчки поверне None, і check_reminders() тихо пропустить цю пару —
        # ловимо це одразу тут, а не після того, як нагадування вже не прийшло.
        await message.answer(
            f"Не бачу часу у форматі ГГ:ХХ у «{html.escape(text)}». "
            f"Введи {fields[field_idx][1]} ще раз (напр. «14:10 - 15:30»):"
        )
        return
    else:
        values[field_key] = text

    field_idx += 1
    if field_idx < len(fields):
        await state.update_data(field_idx=field_idx, values=values)
        skip_hint = " (або «-», щоб лишити без змін)" if mode == "change" else ""
        await message.answer(f"Введи {fields[field_idx][1]}{skip_hint}:")
        return

    await state.clear()
    group = data["group"]
    d = date.fromisoformat(data["iso_date"])

    if mode == "change":
        db.add_override(
            group,
            d.strftime("%d.%m"),
            "change",
            message.from_user.id,
            base_pair=data["base_pair"],
            base_time=data["base_time"],
            subject=values.get("subject"),
            time=values.get("time"),
            teacher=values.get("teacher"),
            room=values.get("room"),
            note=values.get("note"),
        )
        subject = values.get("subject") or "пару"
        broadcast_text = f"✏️ <b>Зміна в розкладі:</b> {html.escape(subject)} — {d.strftime('%d.%m')}."
    else:
        db.add_override(
            group,
            d.strftime("%d.%m"),
            "add",
            message.from_user.id,
            pair="доп.",
            time=values.get("time"),
            subject=values.get("subject") or "Нова пара",
            teacher=values.get("teacher"),
            room=values.get("room"),
        )
        subject = values.get("subject") or "Нова пара"
        broadcast_text = f"➕ <b>Додано пару:</b> {html.escape(subject)} — {d.strftime('%d.%m')} {html.escape(values.get('time') or '')}."

    await broadcast_group(group, broadcast_text + " Перевір деталі в боті 👇")
    await message.answer("Збережено ✅", reply_markup=edit_lesson_list_keyboard(group, d))


@dp.callback_query(F.data == "upcoming")
async def cb_upcoming(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(upcoming_important_text(group, 0), reply_markup=upcoming_keyboard(0))
    await callback.answer()


@dp.callback_query(F.data.startswith("upcoming:"))
async def cb_upcoming_week(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    week_offset = max(0, min(MAX_UPCOMING_WEEKS, int(callback.data.split(":", 1)[1])))
    await callback.message.edit_text(
        upcoming_important_text(group, week_offset), reply_markup=upcoming_keyboard(week_offset)
    )
    await callback.answer()


@dp.callback_query(F.data == "individual_menu")
async def cb_individual_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(
        individual_menu_text(callback.from_user.id), reply_markup=individual_menu_keyboard(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data == "ind_add")
async def cb_ind_add(callback: CallbackQuery, state: FSMContext):
    await state.update_data(field_idx=0, values={})
    await state.set_state(IndividualState.waiting_value)
    await callback.message.answer(IND_PROMPTS["subject"])
    await callback.answer()


@dp.callback_query(F.data == "ind_import")
async def cb_ind_import(callback: CallbackQuery, state: FSMContext):
    if db.has_imported_individual_lessons(callback.from_user.id):
        await callback.answer(
            "Excel уже завантажено. Спершу видали поточні заняття кнопкою "
            "«🗑 Видалити всі індивідуальні заняття», потім тисни цю кнопку знову.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(ImportScheduleState.waiting_file)
    await callback.message.answer("Надішли Excel-файл розкладу у форматі .xlsx (до 5 МБ).")
    await callback.answer()


@dp.callback_query(F.data == "ind_delete_all_ask")
async def cb_ind_delete_all_ask(callback: CallbackQuery):
    lessons = db.all_individual_lessons(callback.from_user.id)
    if not lessons:
        await callback.answer("У тебе й так немає індивідуальних занять.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, видалити все", callback_data="ind_delete_all_yes")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="individual_menu")],
    ])
    await callback.message.edit_text(
        f"Видалити всі індивідуальні заняття ({len(lessons)} шт.), і ручні, і імпортовані з Excel? "
        "Це не можна скасувати.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data == "ind_delete_all_yes")
async def cb_ind_delete_all_yes(callback: CallbackQuery):
    db.delete_all_individual_lessons(callback.from_user.id)
    await callback.message.edit_text(
        "Видалено всі індивідуальні заняття ✅\nТепер можна залити оновлений Excel-файл.",
        reply_markup=individual_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.message(StateFilter(ImportScheduleState.waiting_file), F.document)
async def individual_import_file(message: Message, state: FSMContext):
    document = message.document
    filename = (document.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await message.answer("Поки підтримується лише файл .xlsx. В Excel: «Зберегти як» → Excel Workbook (.xlsx).")
        return
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await message.answer("Файл завеликий. Надішли Excel до 5 МБ.")
        return
    try:
        downloaded = await bot.download(document)
        data = downloaded.read()
    except Exception:
        log.exception("Не вдалось завантажити Excel-файл")
        await message.answer("Не зміг завантажити файл. Спробуй ще раз.")
        return
    await state.update_data(excel_data=data, excel_name=document.file_name)
    await state.set_state(ImportScheduleState.waiting_surname)
    await message.answer("Тепер напиши своє прізвище так, як воно записане в таблиці.")


@dp.message(StateFilter(ImportScheduleState.waiting_file))
async def individual_import_waiting_file(message: Message):
    await message.answer("Надішли саме Excel-файл .xlsx або натисни /start, щоб скасувати.")


@dp.message(StateFilter(ImportScheduleState.waiting_surname))
async def individual_import_surname(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        lessons = _import_schedule_from_excel(data["excel_data"], message.text.strip())
    except Exception:
        log.exception("Не вдалось прочитати Excel-файл індивідуального розкладу")
        await state.clear()
        await message.answer("Не зміг прочитати цей файл. Переконайся, що це справжній .xlsx, і спробуй ще раз.")
        return
    if not lessons:
        await message.answer(
            "Не знайшов занять із цим прізвищем або не зміг розпізнати час. "
            "Спробуй інше написання прізвища."
        )
        return
    await state.update_data(imported_lessons=lessons)
    await state.set_state(ImportScheduleState.waiting_confirmation)
    lines = ["📥 <b>Знайдено такі заняття:</b>"]
    for lesson in lessons[:30]:
        when = lesson.get("weekday") or lesson.get("date") or "день не визначено"
        lines.append(f"• {html.escape(when)}, {html.escape(lesson['time'])} — {html.escape(lesson['subject'])}")
    if len(lessons) > 30:
        lines.append(f"… і ще {len(lessons) - 30}")
    lines.append("\nПідтвердити імпорт?")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зберегти в мій розклад", callback_data="ind_import_confirm")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="ind_import_cancel")],
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@dp.callback_query(F.data == "ind_import_confirm")
async def cb_ind_import_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lessons = data.get("imported_lessons")
    if not lessons:
        await callback.answer("Імпорт уже завершився або скасований. Спробуй ще раз.", show_alert=True)
        return
    db.replace_imported_individual_lessons(callback.from_user.id, lessons)
    await state.clear()
    await callback.message.edit_text(
        f"Збережено {len(lessons)} занять ✅\nВони показуватимуться лише у твоєму розкладі.",
        reply_markup=individual_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "ind_import_cancel")
async def cb_ind_import_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Імпорт скасовано.", reply_markup=individual_menu_keyboard(callback.from_user.id))
    await callback.answer()


@dp.message(StateFilter(IndividualState.waiting_value))
async def ind_value_received(message: Message, state: FSMContext):
    data = await state.get_data()
    field_idx = data["field_idx"]
    field = IND_FIELDS[field_idx]
    values = data["values"]
    text = message.text.strip()

    if field == "when":
        norm = text.replace("\u2019", "'")
        day_match = next((dn for dn in DAY_NAMES if norm.startswith(dn)), None)
        date_match = re.match(r"^\d{2}\.\d{2}$", norm)
        if day_match:
            values["weekday"] = day_match
        elif date_match:
            values["date"] = norm
        else:
            await message.answer(
                "Не розпізнав 🤔 Напиши день тижня (напр. «Вівторок») або дату ДД.ММ (напр. «15.09»):"
            )
            return
    elif field in ("teacher", "room"):
        if text != "-":
            values[field] = text
    else:
        values[field] = text

    field_idx += 1
    if field_idx < len(IND_FIELDS):
        await state.update_data(field_idx=field_idx, values=values)
        await message.answer(IND_PROMPTS[IND_FIELDS[field_idx]])
        return

    await state.clear()
    db.add_individual_lesson(
        message.from_user.id,
        subject=values["subject"],
        time=values["time"],
        weekday=values.get("weekday"),
        date=values.get("date"),
        teacher=values.get("teacher"),
        room=values.get("room"),
    )
    await message.answer("Заняття додано ✅", reply_markup=individual_menu_keyboard(message.from_user.id))


@dp.callback_query(F.data == "ind_view")
async def cb_ind_view(callback: CallbackQuery):
    lessons = db.all_individual_lessons(callback.from_user.id)
    text = "🎓 <b>Мої індивідуальні заняття:</b>\nНатисни, щоб видалити." if lessons else "У тебе поки немає індивідуальних занять."
    await callback.message.edit_text(text, reply_markup=individual_list_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("ind_del:"))
async def cb_ind_del(callback: CallbackQuery):
    lesson_id = int(callback.data.split(":", 1)[1])
    db.delete_individual_lesson(callback.from_user.id, lesson_id)
    await callback.message.edit_text("Видалено ✅", reply_markup=individual_list_keyboard(callback.from_user.id))
    await callback.answer()


# ================================================================== ВІДВІДУВАННЯ
#
# Права: "attendance" (див. PERMISSIONS/PERMISSION_LABELS вище). Два кроки:
#   1. Адмін одноразово (і далі за потреби) заливає excel зі списком
#      студентів групи — той самий журнал, що зазвичай ведеться вручну
#      (колонка "Прізвище та ініціали студентів", група в клітинці A2).
#      Можна кілька аркушів/груп в одному файлі.
#   2. Далі "📝 Відмітити відсутніх" → група → дата → пара → тап на студента
#      циклічно міняє відмітку (порожньо → н → хв → нб → сп → вп → нп → порожньо),
#      і "📄 Згенерувати Excel" одразу шле готовий файл журналу за цей день.
#
# Навмисно НЕ використовує FSMContext для самого проставляння відміток —
# усе (група/дата/пара/студент) кодується прямо в callback_data через "~",
# щоб довга сесія розмітки не залежала від пам'яті процесу (FSM тут
# MemoryStorage і губиться, якщо безкоштовний Render засне й перезапуститься
# між натисканнями).

DAY_SHORT = {
    "Понеділок": "пн", "Вівторок": "вт", "Середа": "ср",
    "Четвер": "чт", "П'ятниця": "пт", "Субота": "сб", "Неділя": "нд",
}

# Скільки предметних слотів (Zoom-підколонок) відводити на день у семестровому
# журналі — стільки, скільки в оригінальному ТДН-24.xls. Якщо в конкретний
# день пар більше — блок дня просто розширюється під фактичну кількість, щоб
# нічого не загубити (плата за це — трохи ширші колонки того дня).
ATT_SEMESTER_DAY_SLOTS = 4

ATT_ROOM_NUM_RE = re.compile(r"ауд\.?\s*([^\s,]+)", re.IGNORECASE)


def _att_room_short(room: str) -> str:
    """'ауд. 227, Є. Коновальця, 36' -> 'Ауд. 227' — повна адреса в комірку
    шапки шириною ~8 символів не влізе, а короткий номер аудиторії — саме
    те, що читають у журналі на льоту."""
    room = (room or "").strip()
    if not room:
        return ""
    m = ATT_ROOM_NUM_RE.search(room)
    if m:
        return f"Ауд. {m.group(1)}"
    return room[:15]


ATT_MARK_CYCLE = ["", "н", "хв", "нб", "сп", "вп", "нп"]
ATT_MARK_NAMES = {"н": "Н", "хв": "ХВ", "нб": "НБ", "сп": "СП", "вп": "ВП", "нп": "НП"}
ATT_MARK_EMOJI = {"": "⬜", "н": "❌", "хв": "🤒", "нб": "❌", "сп": "⏰", "вп": "🏖", "нп": "📋"}
ATT_LEGEND_LINES = [
    "Позначення:",
    "нп - поважна причина",
    "н, нб - відсутній",
    "хв - відсутній по хворобі",
    "сп - запізнення",
    "вп - відпустка",
]


def att_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📥 Завантажити список студентів", callback_data="att_roster")],
        [InlineKeyboardButton(text="📝 Відмітити відсутніх", callback_data="att_mark_groups")],
        [InlineKeyboardButton(text="📚 Журнал за семестр (Excel)", callback_data="att_semester_groups")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("semester"))
async def cmd_semester(message: Message):
    """Задає межі семестру для групи — потрібно один раз (і за потреби,
    коли починається новий семестр). schedules.json прив'язує пари лише до
    "дд.мм" без року, тому саме рік і сама наявність меж має задати людина."""
    if not has_permission(message.from_user.id, "attendance"):
        await message.answer("Для цього потрібне право <code>attendance</code> 🔒")
        return
    parts = message.text.split(maxsplit=3)
    if len(parts) != 4:
        bounds_lines = []
        for group in GROUPS:
            b = db.get_semester_bounds(group)
            if b:
                start_d = date.fromisoformat(b["start"])
                end_d = date.fromisoformat(b["end"])
                bounds_lines.append(f"• {group}: {start_d.strftime('%d.%m.%Y')} — {end_d.strftime('%d.%m.%Y')}")
        current = ("\n\nЗараз задано:\n" + "\n".join(bounds_lines)) if bounds_lines else ""
        await message.answer(
            "Приклад: <code>/semester ТДН-24 01.09.2026 20.12.2026</code>\n"
            "(перший і останній день семестру групи — потрібні для журналу за весь семестр)" + current
        )
        return
    _, group, start_s, end_s = parts
    try:
        start_d = datetime.strptime(start_s, "%d.%m.%Y").date()
        end_d = datetime.strptime(end_s, "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Дати мають бути у форматі дд.мм.рррр, напр. 01.09.2026.")
        return
    if end_d <= start_d:
        await message.answer("Останній день має бути пізніше першого.")
        return
    db.set_semester_bounds(group, start_d.isoformat(), end_d.isoformat())
    await message.answer(
        f"Межі семестру для групи {html.escape(group)} збережено: "
        f"{start_d.strftime('%d.%m.%Y')} — {end_d.strftime('%d.%m.%Y')} ✅\n"
        f"Тепер «📚 Журнал за семестр (Excel)» у меню відвідування згенерує повний журнал за цей період."
    )


ATT_ELECTIVE_RE = re.compile(r"\(\s*вибіркова\s+ок\s*\)", re.IGNORECASE)


def _att_strip_elective_suffix(subject: str) -> str:
    return ATT_ELECTIVE_RE.sub("", subject or "").strip(" ,")


def _att_short_teacher(teacher: str) -> str:
    """'доц. з/н Ус О.С.' -> 'Ус О.С.' — без звання/посади, щоб кілька
    викладачів разом влізли в один компактний підпис об'єднаної пари."""
    teacher = (teacher or "").strip()
    if not teacher:
        return ""
    m = re.search(r"([А-ЯІЇЄҐ][а-яіїєґ'\-]+\s+[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.)", teacher)
    return m.group(1) if m else teacher


def _att_join_and(items: list) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " і " + items[-1]


def _att_merge_elective_lessons(lessons: list) -> list:
    """Кілька вибіркових ОК в один і той самий час (студенти групи обирають
    одну з них — напр. "Диригування" і "Спів (вибіркова ОК)" паралельно) — у
    журналі відвідувань це один часовий слот, не два: об'єднуємо їх в один
    запис "Диригування/Спів (Вибіркова ОК)" з усіма викладачами разом, замість
    двох окремих колонок (тим паче що обидві зараз і так ділять один
    lesson_id "b:{пара}", тож без об'єднання відмітки однієї електи́вки
    затирали б відмітки іншої)."""
    by_pair: dict = {}
    order = []
    for entry in lessons:
        pair = entry.get("pair")
        if pair not in by_pair:
            by_pair[pair] = []
            order.append(pair)
        by_pair[pair].append(entry)

    merged = []
    for pair in order:
        group_entries = by_pair[pair]
        if len(group_entries) <= 1:
            merged.extend(group_entries)
            continue
        # Два+ записи на одну й ту саму пару того самого дня — на практиці це
        # завжди паралельні вибіркові ОК (в реальних даних розкладу маркер
        # "(вибіркова ОК)" буває вказаний лише в одному з двох записів, не в
        # обох — тому орієнтуємось на сам факт збігу пари, а не вимагаємо
        # маркер в усіх одразу).
        has_elective_marker = any(ATT_ELECTIVE_RE.search(e.get("subject") or "") for e in group_entries)
        subjects = [_att_strip_elective_suffix(e.get("subject")) for e in group_entries]
        teachers = [_att_short_teacher(e.get("teacher")) for e in group_entries]
        merged_ids = sorted({str(e.get("id")) for e in group_entries})
        combined = dict(group_entries[0])
        combined["id"] = "+".join(merged_ids)
        combined["_source_ids"] = merged_ids
        subject_joined = "/".join(s for s in subjects if s)
        combined["subject"] = f"{subject_joined} (Вибіркова ОК)" if has_elective_marker else subject_joined
        combined["teacher"] = "Викладачі: " + _att_join_and(teachers)
        combined["room"] = next((e.get("room") for e in group_entries if e.get("room")), None)
        merged.append(combined)
    return merged


def _att_entry_mark(entry: dict, marks_for_day: dict, student_id) -> str:
    """Позначка студента для запису журналу — якщо запис об'єднаний з
    кількох вибіркових ОК (є _source_ids), береться з того з вихідних id, де
    вона фактично проставлена (студент належить лише до однієї підгрупи,
    тож позначка знайдеться щонайбільше в одному з них)."""
    for source_id in entry.get("_source_ids") or [entry["id"]]:
        mark = marks_for_day.get(source_id, {}).get(student_id)
        if mark:
            return mark
    return ""


def att_lessons_for_day(group: str, d: date) -> list:
    """Пари групи на день (з урахуванням правок/скасувань), без скасованих —
    відмічати відвідування на скасованій парі немає сенсу. Тут пари НЕ
    об'єднуються (навіть паралельні вибіркові ОК лишаються окремими записами
    зі своїм id/викладачем/зумом) — бо відмічати відвідування треба саме по
    факту, хто на якій підгрупі був, а не одним спільним записом."""
    return [e for e in group_entries_for_edit(group, d) if not e.get("cancelled")]


def att_lessons_for_journal(group: str, d: date) -> list:
    """Те саме, але для друку в журнал (Excel, поденний чи семестровий):
    - вручну додані пари (кнопка "➕ Додати пару", source == "override_add",
      напр. додатковий хор) у журнал не йдуть — лише в розклад бота;
    - паралельні вибіркові ОК на одній парі об'єднуються в один запис
      "Предмет1/Предмет2 (Вибіркова ОК)" з усіма викладачами (в реальному
      журналі деканату це один часовий слот, а не два) — але лише для
      відображення: base-записи (і їхні id, використані при відмічанні)
      лишаються різними, тому при об'єднанні беремо позначку з того з двох
      id, де вона фактично проставлена (_att_merge_elective_lessons)."""
    lessons = [
        e for e in group_entries_for_edit(group, d)
        if not e.get("cancelled") and e.get("source") != "override_add"
    ]
    return _att_merge_elective_lessons(lessons)


def _att_find_entry(group: str, d: date, entry_id: str):
    for e in att_lessons_for_day(group, d):
        if e["id"] == entry_id:
            return e
    return None


def _att_entry_label(entry: dict) -> str:
    subj = (entry.get("subject") or "").strip()
    teacher = (entry.get("teacher") or "").strip()
    label = f"Пара {entry.get('pair', '')} · {subj}"
    if teacher:
        label += f" ({teacher})"
    return label


def att_date_keyboard(group: str) -> InlineKeyboardMarkup:
    today = today_kyiv()
    yesterday = today - timedelta(days=1)
    rows = [
        [InlineKeyboardButton(
            text=f"📅 Сьогодні ({today.strftime('%d.%m')})",
            callback_data=f"attday~{group}~{today.isoformat()}",
        )],
        [InlineKeyboardButton(
            text=f"📅 Вчора ({yesterday.strftime('%d.%m')})",
            callback_data=f"attday~{group}~{yesterday.isoformat()}",
        )],
        [InlineKeyboardButton(text="📆 Інша дата", callback_data=f"attcal~{group}~{today.year}-{today.month:02d}")],
        [InlineKeyboardButton(text="🔙 Групи", callback_data="att_mark_groups")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_calendar_keyboard(group: str, year: int, month: int) -> InlineKeyboardMarkup:
    today = today_kyiv()
    weeks = calendar_module.Calendar(firstweekday=0).monthdatescalendar(year, month)
    rows = [[InlineKeyboardButton(text=d, callback_data="noop") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]]]
    for week in weeks:
        row = []
        for day_date in week:
            if day_date.month != month:
                row.append(InlineKeyboardButton(text=" ", callback_data="noop"))
            else:
                label = f"·{day_date.day}·" if day_date == today else str(day_date.day)
                row.append(InlineKeyboardButton(text=label, callback_data=f"attday~{group}~{day_date.isoformat()}"))
        rows.append(row)
    prev_m = _add_months(date(year, month, 1), -1)
    next_m = _add_months(date(year, month, 1), 1)
    rows.append([
        InlineKeyboardButton(text="◀", callback_data=f"attcal~{group}~{prev_m.year}-{prev_m.month:02d}"),
        InlineKeyboardButton(text=f"{MONTH_NAMES_UA[month]} {year}", callback_data="noop"),
        InlineKeyboardButton(text="▶", callback_data=f"attcal~{group}~{next_m.year}-{next_m.month:02d}"),
    ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"attg~{group}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_day_keyboard(group: str, d: date, lessons: list) -> InlineKeyboardMarkup:
    marks = db.attendance_marks_for_date(group, d.isoformat())
    rows = []
    for entry in lessons:
        marked = marks.get(entry["id"], {})
        subj = (entry.get("subject") or "").strip()
        label = f"{entry.get('pair', '')} · {subj[:28]}"
        if marked:
            label += f" — {len(marked)} відс."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"attless~{group}~{d.isoformat()}~{entry['id']}")])
    rows.append([InlineKeyboardButton(text="📄 Згенерувати Excel", callback_data=f"attexport~{group}~{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="🔙 Інша дата", callback_data=f"attg~{group}")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="att_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def att_lesson_students_keyboard(group: str, iso: str, entry_id: str) -> InlineKeyboardMarkup:
    students = db.students_for_group(group)
    current = db.attendance_marks_for_lesson(group, iso, entry_id)
    rows = []
    for s in students:
        mark = current.get(s["id"], "")
        prefix = ATT_MARK_EMOJI.get(mark, "⬜")
        suffix = f" [{ATT_MARK_NAMES[mark]}]" if mark else ""
        rows.append([InlineKeyboardButton(
            text=f"{prefix} {s['full_name']}{suffix}",
            callback_data=f"attstud~{group}~{iso}~{entry_id}~{s['id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔙 До пар", callback_data=f"attday~{group}~{iso}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_roster_sheet(ws) -> list:
    """Шукає рядок-заголовок з "Прізвище" й колонку з ПІБ; далі читає рядки,
    доки колонка не спорожніє або не почнеться "Усього.../Позначення:"
    (службові рядки внизу оригінального журналу)."""
    header_row = None
    name_col = None
    for row in ws.iter_rows(min_row=1, max_row=min(10, ws.max_row or 1)):
        for cell in row:
            if cell.value and "прізвище" in str(cell.value).strip().lower():
                header_row, name_col = cell.row, cell.column
                break
        if header_row:
            break
    if not name_col:
        header_row, name_col = 4, 2  # запасний варіант — типовий шаблон деканату

    # Між заголовком і першим студентом часто є ще один службовий рядок
    # (напр. "Дата" з датами занять) — тому спершу пропускаємо порожні
    # клітинки в колонці ПІБ, і лише після першого непорожнього значення
    # трактуємо наступну порожню клітинку як кінець списку.
    names = []
    started = False
    for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row or header_row + 1):
        cell = row[name_col - 1]
        text = str(cell.value).strip() if cell.value is not None else ""
        if not text:
            if started:
                break
            continue
        low = text.lower()
        if low.startswith("усього") or low.startswith("позначен"):
            break
        started = True
        names.append(text)
    return names


def parse_roster_workbook(data: bytes) -> dict:
    """{група: [ПІБ, ...]} з можливо кількох аркушів. Назву групи бере з
    клітинки A2 (як в оригінальному журналі), інакше — назву аркуша."""
    wb = load_workbook(io.BytesIO(data), data_only=True)
    result: dict = {}
    for ws in wb.worksheets:
        names = _parse_roster_sheet(ws)
        if not names:
            continue
        group_val = ws.cell(row=2, column=1).value
        group = str(group_val).strip() if group_val and str(group_val).strip() else ws.title
        bucket = result.setdefault(group, [])
        for n in names:
            if n not in bucket:
                bucket.append(n)
    return result


def build_attendance_excel(group: str, d: date, lessons: list, students: list, marks: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Відвідування"

    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_font = Font(name="Arial", size=10, bold=True)
    normal_font = Font(name="Arial", size=10)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    last_col = max(3, 2 + len(lessons))
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    title = ws.cell(
        row=1, column=1,
        value=f"Журнал відвідувань — група {group} — {d.strftime('%d.%m.%Y')} ({DAY_NAMES[d.weekday()]})",
    )
    title.font = Font(name="Arial", size=12, bold=True)

    header_row = 3
    ws.merge_cells(start_row=header_row, start_column=1, end_row=header_row + 1, end_column=1)
    ws.merge_cells(start_row=header_row, start_column=2, end_row=header_row + 1, end_column=2)
    ws.cell(row=header_row, column=1, value="№")
    ws.cell(row=header_row, column=2, value="Прізвище та ініціали студентів")

    for i, entry in enumerate(lessons):
        col = 3 + i
        subj = (entry.get("subject") or "").strip()
        teacher = (entry.get("teacher") or "").strip()
        room = (entry.get("room") or "").strip()
        header_lines = [f"Пара {entry.get('pair', '')} · {entry.get('time', '')}", subj]
        if teacher:
            header_lines.append(teacher)
        ws.cell(row=header_row, column=col, value="\n".join(header_lines))
        ws.cell(row=header_row + 1, column=col, value=f"Ауд. {room}" if room else "")

    for col in range(1, last_col + 1):
        for r in (header_row, header_row + 1):
            c = ws.cell(row=r, column=col)
            c.font = header_font
            c.alignment = center
            c.fill = header_fill
            c.border = border

    first_data_row = header_row + 2
    for row_i, student in enumerate(students):
        row = first_data_row + row_i
        num_cell = ws.cell(row=row, column=1, value=row_i + 1)
        name_cell = ws.cell(row=row, column=2, value=student["full_name"])
        num_cell.font = normal_font
        num_cell.alignment = center
        num_cell.border = border
        name_cell.font = normal_font
        name_cell.alignment = left_align
        name_cell.border = border
        for i, entry in enumerate(lessons):
            col = 3 + i
            mark = _att_entry_mark(entry, marks, student["id"])
            cell = ws.cell(row=row, column=col, value=mark or None)
            cell.font = normal_font
            cell.alignment = center
            cell.border = border

    total_row = first_data_row + len(students)
    ws.cell(row=total_row, column=2, value="Усього відсутніх (запізнилося)").font = header_font
    if students:
        first_r, last_r = first_data_row, first_data_row + len(students) - 1
        for i in range(len(lessons)):
            col = 3 + i
            col_letter = get_column_letter(col)
            cell = ws.cell(row=total_row, column=col, value=f'=COUNTIF({col_letter}{first_r}:{col_letter}{last_r},"<>")')
            cell.font = header_font
            cell.alignment = center
            cell.border = border

    legend_row = total_row + 2
    for i, line in enumerate(ATT_LEGEND_LINES):
        c = ws.cell(row=legend_row + i, column=2, value=line)
        c.font = header_font if i == 0 else normal_font

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 32
    for i in range(len(lessons)):
        ws.column_dimensions[get_column_letter(3 + i)].width = 20
    ws.row_dimensions[header_row].height = 48
    ws.freeze_panes = ws.cell(row=first_data_row, column=3).coordinate

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def semester_weeks(start_d: date, end_d: date) -> list:
    """Список тижнів (пн-пт) між start_d і end_d включно, кожен — список
    date. Перший/останній тиждень може бути неповним, якщо семестр
    починається/закінчується не з понеділка/п'ятниці — зайві дні поза
    діапазоном просто не додаються."""
    weeks = []
    cursor = start_d - timedelta(days=start_d.weekday())  # понеділок тижня start_d
    while cursor <= end_d:
        week_days = [
            d for d in (cursor + timedelta(days=i) for i in range(5))
            if start_d <= d <= end_d
        ]
        if week_days:
            weeks.append(week_days)
        cursor += timedelta(days=7)
    return weeks


def build_attendance_excel_semester(group: str, weeks: list, students: list, marks_by_date: dict) -> bytes:
    """Журнал відвідувань за весь семестр в один аркуш (.xlsx, без ліміту
    256 колонок старого .xls) — макет 1:1 як у ТДН-24.xls: тижні як блоки
    колонок, у блоці тижня — по 4 колонки (слоти пар) на кожен робочий день,
    у шапці дня — назва предмета+викладач (текст повернутий на 90°) і
    статичний підпис "Zoom", під ними — коротка назва дня і дата (мерж на
    решту колонок дня). Позначки студентів пишуться прямо в комірку
    відповідного предмета/дня; підсумковий рядок — формулою COUNTIF, як і
    в поденному журналі.

    Ширина дня — ATT_SEMESTER_DAY_SLOTS (як в оригіналі), але якщо в
    конкретний день пар фактично більше — блок цього дня розширюється, щоб
    жодна пара не загубилась."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Відвідування"

    thin = Side(style="thin", color="000000")
    medium = Side(style="medium", color="000000")
    thick = Side(style="thick", color="000000")

    def border(left=thin, right=thin, top=thin, bottom=thin):
        return Border(left=left, right=right, top=top, bottom=bottom)

    font_group = Font(name="Arial Narrow", size=28, bold=True)
    font_header = Font(name="Arial Narrow", size=12, bold=True)
    font_header_small = Font(name="Arial Narrow", size=14, bold=True)
    font_normal = Font(name="Arial Narrow", size=12)
    font_total_label = Font(name="Arial Narrow", size=16, bold=True)

    rotated = Alignment(horizontal="general", vertical="bottom", wrap_text=True, textRotation=90)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    center_no_wrap = Alignment(horizontal="center", vertical="bottom")
    left_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    ROW_WEEK, ROW_SUBJECT, ROW_ZOOM, ROW_DATE = 1, 2, 3, 4
    first_data_row = 5

    # Спершу прораховуємо ширину блоку кожного дня (>= ATT_SEMESTER_DAY_SLOTS,
    # більше — якщо реально пар більше) і призначаємо колонки наперед, щоб
    # писати заголовки й дані за один прохід.
    day_plan = []  # [(d, lessons, start_col, width), ...]
    col_cursor = 4  # A,B,C зайняті під №/ПІБ/Аудит.
    week_col_ranges = []  # [(start_col, end_col), ...] по одному на тиждень
    for week in weeks:
        week_start_col = col_cursor
        for d in week:
            lessons = att_lessons_for_journal(group, d)
            width = max(ATT_SEMESTER_DAY_SLOTS, len(lessons))
            day_plan.append((d, lessons, col_cursor, width))
            col_cursor += width
        week_col_ranges.append((week_start_col, col_cursor - 1))

    last_col = max(3, col_cursor - 1)

    # --- Фіксована ліва частина (№ / ПІБ / Аудит. / Дата) ---
    ws.merge_cells(start_row=ROW_SUBJECT, start_column=1, end_row=ROW_SUBJECT, end_column=2)
    g = ws.cell(row=ROW_SUBJECT, column=1, value=group)
    g.font = font_group
    g.alignment = center_no_wrap

    ws.merge_cells(start_row=ROW_ZOOM, start_column=1, end_row=ROW_DATE, end_column=1)
    ws.merge_cells(start_row=ROW_ZOOM, start_column=2, end_row=ROW_DATE, end_column=2)
    c1 = ws.cell(row=ROW_ZOOM, column=1, value="№ з/п")
    c1.font = font_header_small
    c1.alignment = center
    c2 = ws.cell(row=ROW_ZOOM, column=2, value="Прізвище та ініціали студентів")
    c2.font = font_header_small
    c2.alignment = center
    c3 = ws.cell(row=ROW_ZOOM, column=3, value="Аудит.")
    c3.font = font_header
    c3.alignment = center
    c4 = ws.cell(row=ROW_DATE, column=3, value="Дата")
    c4.font = font_header
    c4.alignment = center

    # --- Заголовки тижнів (рядок 1) ---
    for week_no, (week, (wcol_start, wcol_end)) in enumerate(zip(weeks, week_col_ranges), start=1):
        if wcol_end > wcol_start:
            ws.merge_cells(start_row=ROW_WEEK, start_column=wcol_start, end_row=ROW_WEEK, end_column=wcol_end)
        wc = ws.cell(row=ROW_WEEK, column=wcol_start, value=f"Тиждень №{week_no}")
        wc.font = font_header
        wc.alignment = center_no_wrap

    # --- Заголовки днів (рядки 2-4) + дані студентів ---
    for d, lessons, start_col, width in day_plan:
        day_short = DAY_SHORT.get(DAY_NAMES[d.weekday()], "")
        for slot in range(width):
            col = start_col + slot
            if slot < len(lessons):
                entry = lessons[slot]
                subj = (entry.get("subject") or "").strip()
                teacher = (entry.get("teacher") or "").strip()
                if teacher.startswith("Викладачі:"):
                    subj_cell_value = f"{subj}\n{teacher}"
                else:
                    subj_cell_value = f"{subj}\n({teacher})" if teacher else subj
                # Формат заняття може змінюватись протягом семестру (спершу
                # Zoom, потім очно) — беремо це з даних конкретного заняття на
                # конкретну дату (entry["room"], включно з ручними правками
                # розкладу через адмінське редагування), а не пишемо "Zoom"
                # для всіх заздалегідь: якщо в парі вказана аудиторія — вона
                # й показується, інакше вважаємо заняття дистанційним (Zoom).
                room = (entry.get("room") or "").strip()
                format_label = _att_room_short(room) if room else "Zoom"
            else:
                subj_cell_value = ""
                format_label = ""
            sc = ws.cell(row=ROW_SUBJECT, column=col, value=subj_cell_value or None)
            sc.font = font_header
            sc.alignment = rotated
            zc = ws.cell(row=ROW_ZOOM, column=col, value=format_label or None)
            zc.font = font_header
            zc.alignment = Alignment(horizontal="general", vertical="bottom")

        first = ws.cell(row=ROW_DATE, column=start_col, value=day_short)
        first.font = font_header
        first.alignment = Alignment(horizontal="general", vertical="bottom")
        if width > 1:
            ws.merge_cells(start_row=ROW_DATE, start_column=start_col + 1, end_row=ROW_DATE, end_column=start_col + width - 1)
        date_cell = ws.cell(row=ROW_DATE, column=start_col + 1, value=d)
        date_cell.number_format = "DD.MM"
        date_cell.font = font_header
        date_cell.alignment = center_no_wrap

        # межі: медіум навколо блоку дня, товста зліва — якщо це початок тижня
        is_week_start = any(start_col == wstart for wstart, _ in week_col_ranges)
        for slot in range(width):
            col = start_col + slot
            left_side = thick if (slot == 0 and is_week_start) else (medium if slot == 0 else thin)
            right_side = medium if slot == width - 1 else thin
            for r in (ROW_SUBJECT, ROW_ZOOM, ROW_DATE):
                cell = ws.cell(row=r, column=col)
                cell.border = border(left=left_side, right=right_side, top=medium, bottom=medium)

        for row_i, student in enumerate(students):
            row = first_data_row + row_i
            for slot in range(width):
                col = start_col + slot
                mark = None
                if slot < len(lessons):
                    entry = lessons[slot]
                    day_marks = marks_by_date.get(d.isoformat(), {})
                    mark = _att_entry_mark(entry, day_marks, student["id"])
                cell = ws.cell(row=row, column=col, value=mark or None)
                cell.font = font_normal
                cell.alignment = Alignment(horizontal="center", vertical="center")
                left_side = thick if (slot == 0 and is_week_start) else (medium if slot == 0 else thin)
                right_side = medium if slot == width - 1 else thin
                cell.border = border(left=left_side, right=right_side, top=thin, bottom=thin)

    # --- Ліва фіксована колонка з даними студентів ---
    for row_i, student in enumerate(students):
        row = first_data_row + row_i
        num_cell = ws.cell(row=row, column=1, value=row_i + 1)
        num_cell.font = font_normal
        num_cell.alignment = Alignment(horizontal="center", vertical="center")
        num_cell.border = border(left=thick, top=thin, bottom=thin)
        name_cell = ws.cell(row=row, column=2, value=student["full_name"])
        name_cell.font = font_normal
        name_cell.alignment = left_align
        name_cell.border = border(top=thin, bottom=thin)
        room_cell = ws.cell(row=row, column=3)
        room_cell.font = font_normal
        room_cell.border = border(top=thin, bottom=thin)

    # --- Підсумковий рядок ---
    total_row = first_data_row + len(students)
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=3)
    total_label = ws.cell(row=total_row, column=1, value="Усього відсутніх (запізнилося)")
    total_label.font = font_total_label
    total_label.border = border(left=thick, top=thin, bottom=medium)
    if students:
        first_r, last_r = first_data_row, first_data_row + len(students) - 1
        for d, lessons, start_col, width in day_plan:
            for slot in range(len(lessons)):
                col = start_col + slot
                col_letter = get_column_letter(col)
                cell = ws.cell(
                    row=total_row, column=col,
                    value=f'=COUNTIF({col_letter}{first_r}:{col_letter}{last_r},"<>")',
                )
                cell.font = font_header
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = border(top=thin, bottom=medium)

    # --- Легенда ---
    legend_row = total_row + 2
    for i, line in enumerate(ATT_LEGEND_LINES):
        c = ws.cell(row=legend_row + i, column=1, value=line)
        c.font = font_header if i == 0 else font_normal

    # --- Ширини колонок / висоти рядків ---
    ws.column_dimensions["A"].width = 6.55
    ws.column_dimensions["B"].width = 48.55
    ws.column_dimensions["C"].width = 11.21
    for c in range(4, last_col + 1):
        ws.column_dimensions[get_column_letter(c)].width = 8.66
    ws.row_dimensions[ROW_SUBJECT].height = 164.25
    ws.row_dimensions[ROW_ZOOM].height = 15.75
    ws.row_dimensions[ROW_DATE].height = 16.5
    ws.freeze_panes = ws.cell(row=first_data_row, column=4).coordinate

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@dp.callback_query(F.data == "att_menu")
async def cb_att_menu(callback: CallbackQuery, state: FSMContext):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("📋 <b>Відвідування</b>\n\nОбери дію:", reply_markup=att_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "att_roster")
async def cb_att_roster(callback: CallbackQuery, state: FSMContext):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    await state.clear()
    await state.set_state(AttendanceState.waiting_roster_file)
    await callback.message.answer(
        "Надішли Excel-файл (.xlsx) зі списком студентів — журнал у звичному вигляді: "
        "колонка «Прізвище та ініціали студентів», назва групи в клітинці A2.\n"
        "Можна кілька аркушів з різними групами в одному файлі — заберу всі."
    )
    await callback.answer()


@dp.message(StateFilter(AttendanceState.waiting_roster_file), F.document)
async def att_roster_file_received(message: Message, state: FSMContext):
    if not has_permission(message.from_user.id, "attendance"):
        await state.clear()
        return
    document = message.document
    filename = (document.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await message.answer(
            "Потрібен файл .xlsx. Якщо це .xls — відкрий в Excel і збережи як «Excel Workbook (.xlsx)»."
        )
        return
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await message.answer("Файл завеликий. Надішли до 5 МБ.")
        return
    try:
        downloaded = await bot.download(document)
        groups = parse_roster_workbook(downloaded.read())
    except Exception:
        log.exception("Не вдалось прочитати файл зі списком студентів")
        await message.answer("Не зміг прочитати цей файл. Переконайся, що структура як у зразку, і спробуй ще раз.")
        return
    if not groups:
        await message.answer(
            "Не знайшов жодного студента. Перевір, що є колонка «Прізвище та ініціали студентів»."
        )
        return
    await state.update_data(roster_groups=groups)
    lines = ["📥 <b>Знайдено:</b>"]
    for group, names in groups.items():
        lines.append(f"• {html.escape(group)} — {len(names)} студент(ів)")
    lines.append("\n⚠️ Це <b>повністю замінить</b> поточний список студентів цих груп, якщо він уже був.\nЗберегти?")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зберегти", callback_data="att_roster_confirm")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="att_roster_cancel")],
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@dp.message(StateFilter(AttendanceState.waiting_roster_file))
async def att_roster_file_waiting(message: Message):
    await message.answer("Надішли саме Excel-файл (.xlsx), або /start щоб скасувати.")


@dp.callback_query(F.data == "att_roster_confirm")
async def cb_att_roster_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    groups = data.get("roster_groups")
    if not groups:
        await callback.answer("Дані застаріли — завантаж файл ще раз.", show_alert=True)
        return
    for group, names in groups.items():
        db.replace_group_students(group, names)
    await state.clear()
    total = sum(len(v) for v in groups.values())
    await callback.message.edit_text(
        f"Збережено ✅ {len(groups)} груп(и), {total} студентів.",
        reply_markup=att_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "att_roster_cancel")
async def cb_att_roster_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Скасовано.", reply_markup=att_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "att_mark_groups")
async def cb_att_mark_groups(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    groups = db.groups_with_students()
    if not groups:
        await callback.answer("Спершу завантаж список студентів хоч однієї групи.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=g, callback_data=f"attg~{g}")] for g in groups]
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="att_menu")])
    await callback.message.edit_text("Обери групу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("attg~"))
async def cb_attg(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    group = callback.data.split("~", 1)[1]
    await callback.message.edit_text(f"Група {html.escape(group)}. Обери дату:", reply_markup=att_date_keyboard(group))
    await callback.answer()


@dp.callback_query(F.data.startswith("attcal~"))
async def cb_attcal(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    _, group, ym = callback.data.split("~", 2)
    year_s, month_s = ym.split("-")
    await callback.message.edit_text(
        f"Група {html.escape(group)}. Обери дату:",
        reply_markup=att_calendar_keyboard(group, int(year_s), int(month_s)),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("attday~"))
async def cb_attday(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    _, group, iso = callback.data.split("~", 2)
    d = date.fromisoformat(iso)
    if not db.students_for_group(group):
        await callback.answer("У групи ще немає списку студентів — спершу завантаж excel.", show_alert=True)
        return
    lessons = att_lessons_for_day(group, d)
    if not lessons:
        await callback.message.edit_text(
            f"На {d.strftime('%d.%m.%Y')} ({DAY_NAMES[d.weekday()]}) у групи {html.escape(group)} пар немає.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Інша дата", callback_data=f"attg~{group}")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="att_menu")],
            ]),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        f"📅 {d.strftime('%d.%m.%Y')} ({DAY_NAMES[d.weekday()]}), група {html.escape(group)}\nОбери пару:",
        reply_markup=att_day_keyboard(group, d, lessons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("attless~"))
async def cb_attless(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    _, group, iso, entry_id = callback.data.split("~", 3)
    if not db.students_for_group(group):
        await callback.answer("У групи ще немає списку студентів.", show_alert=True)
        return
    d = date.fromisoformat(iso)
    entry = _att_find_entry(group, d, entry_id)
    label = _att_entry_label(entry) if entry else entry_id
    await callback.message.edit_text(
        f"📅 {d.strftime('%d.%m.%Y')} · {html.escape(label)}\nНатисни на студента, щоб змінити відмітку "
        "(порожньо → н → хв → нб → сп → вп → нп → порожньо):",
        reply_markup=att_lesson_students_keyboard(group, iso, entry_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("attstud~"))
async def cb_attstud(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    _, group, iso, entry_id, student_id_s = callback.data.split("~", 4)
    student_id = int(student_id_s)
    current = db.attendance_marks_for_lesson(group, iso, entry_id).get(student_id, "")
    next_mark = ATT_MARK_CYCLE[(ATT_MARK_CYCLE.index(current) + 1) % len(ATT_MARK_CYCLE)]
    d = date.fromisoformat(iso)
    entry = _att_find_entry(group, d, entry_id)
    label = _att_entry_label(entry) if entry else entry_id
    if next_mark:
        db.set_attendance_mark(group, iso, entry_id, label, student_id, next_mark, callback.from_user.id)
    else:
        db.clear_attendance_mark(group, iso, entry_id, student_id)
    await callback.message.edit_reply_markup(reply_markup=att_lesson_students_keyboard(group, iso, entry_id))
    await callback.answer(ATT_MARK_NAMES.get(next_mark, "присутній"))


@dp.callback_query(F.data.startswith("attexport~"))
async def cb_attexport(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    _, group, iso = callback.data.split("~", 2)
    d = date.fromisoformat(iso)
    students = db.students_for_group(group)
    if not students:
        await callback.answer("У групи ще немає списку студентів.", show_alert=True)
        return
    lessons = att_lessons_for_journal(group, d)
    if not lessons:
        await callback.answer("На цей день пар немає.", show_alert=True)
        return
    await callback.answer("Генерую файл…")
    marks = db.attendance_marks_for_date(group, iso)
    try:
        file_bytes = build_attendance_excel(group, d, lessons, students, marks)
    except Exception:
        log.exception("Не вдалось згенерувати excel журналу відвідувань")
        await callback.message.answer("Не вдалось згенерувати файл. Спробуй ще раз.")
        return
    filename = f"Відвідування_{group}_{d.strftime('%d.%m.%Y')}.xlsx"
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=f"📋 Журнал відвідувань — {html.escape(group)}, {d.strftime('%d.%m.%Y')} ({DAY_NAMES[d.weekday()]})",
    )


@dp.callback_query(F.data == "att_semester_groups")
async def cb_att_semester_groups(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    groups = db.groups_with_students()
    if not groups:
        await callback.answer("Спершу завантаж список студентів хоч однієї групи.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=g, callback_data=f"attsem~{g}")] for g in groups]
    rows.append([InlineKeyboardButton(text="🔙 Меню", callback_data="att_menu")])
    await callback.message.edit_text(
        "Обери групу для семестрового журналу.\n"
        "Якщо для групи ще не задано межі семестру — спершу виконай, напр.:\n"
        "<code>/semester ТДН-24 01.09.2026 20.12.2026</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("attsem~"))
async def cb_attsem(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "attendance"):
        await callback.answer("Ця функція лише для адмінів 🔒", show_alert=True)
        return
    group = callback.data.split("~", 1)[1]
    students = db.students_for_group(group)
    if not students:
        await callback.answer("У групи ще немає списку студентів.", show_alert=True)
        return
    bounds = db.get_semester_bounds(group)
    if not bounds:
        await callback.answer(
            "Спершу задай межі семестру: /semester ГРУПА дд.мм.рррр дд.мм.рррр",
            show_alert=True,
        )
        return
    start_d = date.fromisoformat(bounds["start"])
    end_d = date.fromisoformat(bounds["end"])
    weeks = semester_weeks(start_d, end_d)
    if not weeks:
        await callback.answer("Порожній діапазон семестру — перевір межі.", show_alert=True)
        return
    await callback.answer("Генерую файл, це може зайняти кілька секунд…")
    marks_by_date = db.attendance_marks_for_range(group, bounds["start"], bounds["end"])
    try:
        file_bytes = build_attendance_excel_semester(group, weeks, students, marks_by_date)
    except Exception:
        log.exception("Не вдалось згенерувати семестровий excel журналу відвідувань")
        await callback.message.answer("Не вдалось згенерувати файл. Спробуй ще раз.")
        return
    filename = f"Журнал_відвідувань_{group}_{start_d.strftime('%Y')}.xlsx"
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=filename),
        caption=(
            f"📚 Журнал відвідувань за семестр — {html.escape(group)}\n"
            f"{start_d.strftime('%d.%m.%Y')} — {end_d.strftime('%d.%m.%Y')}"
        ),
    )


@dp.message(Command("group"))
async def cmd_group(message: Message):
    await message.answer("Обери свою групу:", reply_markup=group_choice_keyboard())


@dp.message(Command("whoami"))
async def cmd_whoami(message: Message):
    if is_owner(message.from_user.id):
        role = "головний адміністратор ✅"
    else:
        rights = db.staff_permissions(message.from_user.id)
        role = "адміністратор: " + ", ".join(PERMISSION_LABELS[p] for p in sorted(rights)) if rights else "звичайний користувач"
    await message.answer(f"Твій chat_id: <code>{message.from_user.id}</code>\nСтатус: {role}")


def _owner_only(message: Message) -> bool:
    return is_owner(message.from_user.id)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    rights = PERMISSIONS if is_owner(message.from_user.id) else db.staff_permissions(message.from_user.id)
    if not rights:
        await message.answer("Адмін-функції тобі не доступні 🔒")
        return
    lines = ["🛠 <b>Адмін-команди</b>"]
    if "schedule" in rights:
        lines.append("• Редагування розкладу — через кнопку «✏️ Редагувати розклад».")
        zoom_status = "увімк ✅" if db.zoom_in_reminders_enabled() else "вимк ⛔"
        lines.append(f"• <code>/zoom_reminders on|off</code> — Zoom-кнопка в нагадуваннях перед парою (зараз: {zoom_status})")
    if "announce" in rights:
        lines.append("• <code>/announce текст</code> — звичайне оголошення")
        lines.append("• <code>/urgent текст</code> — термінове оголошення")
    if "materials" in rights:
        lines.append("• «📚 Матеріали» → «➕ Додати матеріал» — прикріпити файл (ноти, PDF, таблицю) або посилання")
        lines.append("• <code>/material Предмет | Назва | https://посилання</code> — швидко додати лише лінк")
        lines.append("• <code>/materials del назва</code> — видалити матеріал за назвою (запитає уточнення, якщо збігів кілька)")
    if "polls" in rights:
        lines.append("• <code>/poll Питання | Варіант 1 | Варіант 2</code>")
    if "attendance" in rights:
        lines.append("• «📋 Відвідування» в головному меню — список студентів і журнал відвідувань")
        lines.append("• <code>/semester ГРУПА дд.мм.рррр дд.мм.рррр</code> — задати межі семестру (для журналу за весь семестр)")
    if is_owner(message.from_user.id):
        lines.append("• <code>/staff</code> — права заступників")
    await message.answer("\n".join(lines))


@dp.message(Command("zoom_reminders"))
async def cmd_zoom_reminders(message: Message):
    if not has_permission(message.from_user.id, "schedule"):
        await message.answer("Для цього потрібне право <code>schedule</code> 🔒")
        return
    arg = message.text.partition(" ")[2].strip().lower()
    if arg in ("on", "увімк", "увімкнути", "1"):
        db.set_zoom_in_reminders(True)
        await message.answer("🎥 Посилання на Zoom у нагадуваннях перед парою: <b>увімкнено</b> ✅")
        return
    if arg in ("off", "вимк", "вимкнути", "0"):
        db.set_zoom_in_reminders(False)
        await message.answer(
            "🎥 Посилання на Zoom у нагадуваннях перед парою: <b>вимкнено</b> ✅\n"
            "(кнопка «🎥 Посилання в Zoom» під розкладом дня й далі працює як завжди — "
            "це стосується лише автоматичних нагадувань)"
        )
        return
    status = "увімкнено ✅" if db.zoom_in_reminders_enabled() else "вимкнено ⛔"
    await message.answer(
        f"🎥 Zoom у нагадуваннях зараз: <b>{status}</b>\n\n"
        "Змінити: <code>/zoom_reminders on</code> або <code>/zoom_reminders off</code>"
    )


@dp.message(Command("staff"))
async def cmd_staff(message: Message):
    if not _owner_only(message):
        await message.answer("Керувати правами може тільки головний адміністратор 🔒")
        return
    lines = ["🛠 <b>Команда бота</b>", "\nГоловний адміністратор — ти (права з Render)."]
    for staff in db.all_staff():
        rights = ", ".join(PERMISSION_LABELS[p] for p in sorted(staff["permissions"]) if p in PERMISSION_LABELS)
        lines.append(f"• <code>{staff['chat_id']}</code> — {rights or 'без прав'}")
    lines.append(
        "\nДати права: <code>/grant ID schedule,announce</code>\n"
        "Доступні: schedule, announce, materials, polls, attendance\n"
        "Забрати всі права: <code>/revoke ID</code>"
    )
    await message.answer("\n".join(lines))


@dp.message(Command("grant"))
async def cmd_grant(message: Message):
    if not _owner_only(message):
        await message.answer("Лише головний адміністратор може змінювати права 🔒")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit():
        await message.answer("Приклад: <code>/grant 123456789 schedule,announce</code>")
        return
    permissions = {p.strip().lower() for p in parts[2].split(",") if p.strip()}
    invalid = permissions - PERMISSIONS
    if not permissions or invalid:
        await message.answer("Дозволені права: <code>schedule, announce, materials, polls</code>")
        return
    chat_id = int(parts[1])
    db.set_staff_permissions(chat_id, permissions, message.from_user.id)
    labels = ", ".join(PERMISSION_LABELS[p] for p in sorted(permissions))
    await message.answer(f"Права для <code>{chat_id}</code> збережено: {labels} ✅")


@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if not _owner_only(message):
        await message.answer("Лише головний адміністратор може змінювати права 🔒")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Приклад: <code>/revoke 123456789</code>")
        return
    db.remove_staff(int(parts[1]))
    await message.answer("Усі додаткові права забрано ✅")


def _announcement_target(text: str, default_group: str) -> tuple[str, str]:
    first, sep, rest = text.strip().partition(" ")
    if first in GROUPS and sep:
        return first, rest.strip()
    return default_group, text.strip()


@dp.message(Command("announce"))
async def cmd_announce(message: Message):
    if not has_permission(message.from_user.id, "announce"):
        await message.answer("Для оголошень потрібне право <code>announce</code> 🔒")
        return
    group = await require_group(message)
    if not group:
        return
    text = message.text.partition(" ")[2]
    target_group, text = _announcement_target(text, group)
    if not text:
        await message.answer("Приклад: <code>/announce Завтра збір о 9:00</code>\nАбо: <code>/announce ТДН-24 Текст</code>")
        return
    await broadcast_announcement(target_group, f"📣 <b>Оголошення</b>\n\n{html.escape(text)}")
    await message.answer(f"Оголошення надіслано для {html.escape(target_group)} ✅")


@dp.message(Command("urgent"))
async def cmd_urgent(message: Message):
    if not has_permission(message.from_user.id, "announce"):
        await message.answer("Для термінових оголошень потрібне право <code>announce</code> 🔒")
        return
    group = await require_group(message)
    if not group:
        return
    target_group, text = _announcement_target(message.text.partition(" ")[2], group)
    if not text:
        await message.answer("Приклад: <code>/urgent Пару перенесли в 312 аудиторію</code>")
        return
    # Термінові зміни доходять усім, навіть якщо звичайні оголошення вимкнені.
    await broadcast_group(target_group, f"🚨 <b>Терміново</b>\n\n{html.escape(text)}")
    await message.answer(f"Термінове повідомлення надіслано для {html.escape(target_group)} ✅")


@dp.message(Command("material"))
async def cmd_material(message: Message):
    if not has_permission(message.from_user.id, "materials"):
        await message.answer("Додавати матеріали може лише відповідальний із правом <code>materials</code> 🔒")
        return
    group = await require_group(message)
    if not group:
        return
    parts = [p.strip() for p in message.text.partition(" ")[2].split("|")]
    if len(parts) != 3 or not all(parts) or not re.match(r"https?://", parts[2], re.I):
        await message.answer(
            "Формат: <code>/material Предмет | Назва | https://посилання</code>\n\n"
            "Хочеш прикріпити файл (ноти, PDF, таблицю) замість посилання — "
            "простіше через меню: «📚 Матеріали» → «➕ Додати матеріал», там бот "
            "прийме сам файл, а не лише лінк."
        )
        return
    db.add_material(group, parts[0], parts[1], parts[2], message.from_user.id)
    await message.answer("Матеріал додано ✅")


def material_search_results_keyboard(materials: list, chat_id: int, delete_mode: bool = False) -> InlineKeyboardMarkup:
    """delete_mode=True — кожна кнопка сама по собі видаляє матеріал (для
    уточнення, який саме видалити, коли пошук дав кілька збігів)."""
    rows = []
    for m in materials:
        label = f"{m['subject']} — {m['title']}"
        label = label if len(label) <= 40 else label[:37] + "..."
        icon = "🗑" if delete_mode else "📎"
        callback = f"mats_del:{m['id']}" if delete_mode else f"mats_get:{m['id']}"
        rows.append([InlineKeyboardButton(text=f"{icon} {label}", callback_data=callback)])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("materials"))
async def cmd_materials(message: Message):
    group = await require_group(message)
    if not group:
        return
    raw = message.text.partition(" ")[2].strip()

    if raw.lower().startswith("del "):
        if not has_permission(message.from_user.id, "materials"):
            await message.answer("Видаляти матеріали може лише відповідальний із правом <code>materials</code> 🔒")
            return
        query = raw[4:].strip()
        if not query:
            await message.answer("Приклад: <code>/materials del Лист дружини</code>")
            return
        matches = db.search_materials(group, query)
        if not matches:
            await message.answer(f"За запитом «{html.escape(query)}» нічого не знайдено.")
        elif len(matches) == 1:
            m = matches[0]
            db.delete_material(m["id"])
            await message.answer(f"Видалено: <b>{html.escape(m['subject'])}</b> — {html.escape(m['title'])} ✅")
        else:
            await message.answer(
                f"Знайдено кілька збігів за «{html.escape(query)}» — обери, що саме видалити:",
                reply_markup=material_search_results_keyboard(matches, message.from_user.id, delete_mode=True),
            )
        return

    if raw.lower().startswith("find "):
        raw = raw[5:].strip()

    if raw:
        results = db.search_materials(group, raw)
        if not results:
            await message.answer(f"🔎 За запитом «{html.escape(raw)}» нічого не знайдено (шукає і по предмету, і по назві).")
            return
        await message.answer(
            f"🔎 Знайдено за запитом «{html.escape(raw)}»:",
            reply_markup=material_search_results_keyboard(results, message.from_user.id),
        )
        return

    subjects = db.material_subjects(group)
    text = "📚 <b>Матеріали групи</b>\nОбери предмет:" if subjects else "📚 Матеріалів поки немає."
    await message.answer(text, reply_markup=materials_subject_keyboard(group, message.from_user.id))


@dp.message(Command("update"))
async def cmd_update(message: Message):
    if not _owner_only(message):
        await message.answer("Записувати оновлення бота може лише головний адміністратор 🔒")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Формат: <code>/update що змінилось у боті</code>")
        return
    db.add_changelog_entry(text, message.from_user.id)
    chat_ids = db.all_registered_chat_ids()
    await _broadcast_to(chat_ids, f"🛠 <b>Оновлення бота:</b>\n{html.escape(text)}")
    await message.answer(f"Записано й розіслано {len(chat_ids)} користувачам ✅")


@dp.message(Command("updates"))
async def cmd_updates(message: Message):
    entries = db.recent_changelog(10)
    if not entries:
        await message.answer("Оновлень поки не записано.")
        return
    lines = ["🛠 <b>Історія оновлень бота:</b>\n"]
    for e in entries:
        d = date.fromisoformat(e["created_at"])
        lines.append(f"<b>{d.strftime('%d.%m.%Y')}</b>: {html.escape(e['text'])}")
    await message.answer("\n\n".join(lines))


@dp.message(Command("poll"))
async def cmd_poll(message: Message):
    if not has_permission(message.from_user.id, "polls"):
        await message.answer("Створювати опитування може лише відповідальний із правом <code>polls</code> 🔒")
        return
    group = await require_group(message)
    if not group:
        return
    parts = [p.strip() for p in message.text.partition(" ")[2].split("|") if p.strip()]
    if not 3 <= len(parts) <= 11:
        await message.answer("Формат: <code>/poll Питання | Варіант 1 | Варіант 2 | ...</code>\nВід 2 до 10 варіантів.")
        return
    question, options = parts[0], parts[1:]
    sent = 0
    for chat_id in db.announcement_users_in_group(group):
        try:
            await bot.send_poll(chat_id, question, options, is_anonymous=False)
            sent += 1
        except Exception:
            log.exception("Не вдалось надіслати опитування %s", chat_id)
    await message.answer(f"Опитування надіслано: {sent} отримувачам ✅")


@dp.message(Command("find"))
async def cmd_find(message: Message):
    query = message.text.partition(" ")[2].strip().lower()
    if not query:
        await message.answer("Приклад: <code>/find математика</code> або <code>/find Іваненко</code>")
        return
    group = await require_group(message)
    if not group:
        return
    found = []
    for offset in range(14):
        d = today_kyiv() + timedelta(days=offset)
        for lesson, _matches in lessons_for_date(group, d):
            haystack = " ".join(str(lesson.get(k) or "") for k in ("subject", "teacher", "room")).lower()
            if query in haystack:
                found.append(f"• <b>{d.strftime('%d.%m')} ({DAY_NAMES[d.weekday()]})</b> {html.escape(lesson.get('time') or '')} — {html.escape(lesson.get('subject') or '')}")
    if found:
        await message.answer("🔎 <b>Найближчі збіги:</b>\n\n" + "\n".join(found[:20]))
    else:
        await message.answer("🔎 На найближчі 14 днів нічого не знайдено.")


@dp.message(Command("notes"))
async def cmd_notes(message: Message):
    notes = db.all_notes(message.from_user.id)
    if not notes:
        await message.answer("У тебе поки немає нотаток. Додай їх кнопкою «📝 Нотатка» під парою.")
        return
    lines = ["📝 <b>Твої нотатки:</b>\n"]
    for n in notes:
        lines.append(f"• <b>{html.escape(n['lesson_label'])}</b>: {html.escape(n['text'])} (id {n['id']})")
    lines.append("\nВидалити одну: <code>/delnote ID</code>")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Видалити всі нотатки", callback_data="notes_delete_all_ask")],
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


@dp.callback_query(F.data == "notes_delete_all_ask")
async def cb_notes_delete_all_ask(callback: CallbackQuery):
    notes = db.all_notes(callback.from_user.id)
    if not notes:
        await callback.answer("У тебе й так немає нотаток.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, видалити все", callback_data="notes_delete_all_yes")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="notes_delete_all_cancel")],
    ])
    await callback.message.edit_text(
        f"Видалити всі нотатки ({len(notes)} шт.)? Це не можна скасувати.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data == "notes_delete_all_yes")
async def cb_notes_delete_all_yes(callback: CallbackQuery):
    db.delete_all_notes(callback.from_user.id)
    await callback.message.edit_text("Видалено всі нотатки ✅")
    await callback.answer()


@dp.callback_query(F.data == "notes_delete_all_cancel")
async def cb_notes_delete_all_cancel(callback: CallbackQuery):
    await callback.message.edit_text("Скасовано, нотатки на місці.")
    await callback.answer()


@dp.message(Command("delnote"))
async def cmd_delnote(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Використання: /delnote ID (номер id береться зі списку /notes)")
        return
    db.delete_note(message.from_user.id, int(parts[1]))
    await message.answer("Видалено ✅")


@dp.callback_query(F.data.startswith("setgroup:"))
async def cb_setgroup(callback: CallbackQuery):
    group = callback.data.split(":", 1)[1]
    db.set_group(callback.from_user.id, group)
    await callback.message.edit_text(f"Група {html.escape(group)} збережена ✅\n\n" + main_menu_text(group), reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "today")
async def cb_today(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    today = today_kyiv()
    entries = build_day_entries(callback.from_user.id, group, today)
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, today, entries),
        reply_markup=keyboard_for_day(callback.from_user.id, group, today, entries),
    )
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("calendar:"))
async def cb_calendar(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    year_str, month_str = callback.data.split(":", 1)[1].split("-")
    year, month = int(year_str), int(month_str)
    await callback.message.edit_text("📆 Обери дату:", reply_markup=calendar_keyboard(year, month))
    await callback.answer()


@dp.callback_query(F.data.startswith("day:"))
async def cb_day(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    entries = build_day_entries(callback.from_user.id, group, d)
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, d, entries),
        reply_markup=keyboard_for_day(callback.from_user.id, group, d, entries),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("zoom_menu:"))
async def cb_zoom_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    entries = build_day_entries(callback.from_user.id, group, d)
    if not any(find_zoom(e.get("teacher")) for e in entries if not e.get("cancelled")):
        await callback.answer("На цей день немає збережених посилань", show_alert=True)
        return
    await callback.message.edit_text("🎥 Обери предмет:", reply_markup=zoom_menu_keyboard(callback.from_user.id, group, d))
    await callback.answer()


@dp.callback_query(F.data.startswith("zoom:"))
async def cb_zoom_info(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    idx = int(idx_str)
    entry = _entry_or_none(callback.from_user.id, group, d, idx)
    if entry is None:
        await callback.answer("Не знайдено", show_alert=True)
        return
    found = find_zoom(entry.get("teacher"))
    if not found:
        await callback.answer("Посилання не знайдено", show_alert=True)
        return
    _surname, info = found
    subject = html.escape(entry.get("subject") or "")
    text = (
        f"🎥 <b>{subject}</b>\n\n"
        f"🔗 <a href=\"{html.escape(info['link'])}\">Приєднатися до Zoom</a>\n"
        f"🆔 Ідентифікатор: <code>{html.escape(info['id'])}</code>\n"
        f"🔑 Код доступу: <code>{html.escape(info['passcode'])}</code>"
    )
    await callback.message.edit_text(text, reply_markup=zoom_info_keyboard(d), disable_web_page_preview=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("togglemin:"))
async def cb_togglemin(callback: CallbackQuery):
    minutes = int(callback.data.split(":", 1)[1])
    offsets = db.get_reminder_offsets(callback.from_user.id)
    if minutes in offsets:
        if len(offsets) == 1:
            await callback.answer(
                "Має лишитись хоча б одне нагадування. Щоб вимкнути всі — "
                "«🔕 Вимкнути нагадування» нижче.",
                show_alert=True,
            )
            return
        db.remove_reminder_offset(callback.from_user.id, minutes)
        await callback.answer(f"Нагадування за {minutes} хв прибрано")
    else:
        db.add_reminder_offset(callback.from_user.id, minutes)
        await callback.answer(f"Додано нагадування за {minutes} хв ✅")
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))


@dp.callback_query(F.data == "custom_min")
async def cb_custom_min(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReminderState.waiting_custom_minutes)
    await callback.message.answer("За скільки хвилин до пари нагадати? Напиши число (1–180).")
    await callback.answer()


@dp.message(StateFilter(ReminderState.waiting_custom_minutes))
async def custom_min_received(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 180):
        await message.answer("Введи ціле число хвилин від 1 до 180, напр. <code>7</code>.")
        return
    minutes = int(text)
    await state.clear()
    db.add_reminder_offset(message.from_user.id, minutes)
    await message.answer(
        f"Додано нагадування за {minutes} хв ✅",
        reply_markup=reminders_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data == "toggle_reminders")
async def cb_toggle_reminders(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    currently_on = user["reminders_on"] if user else True
    db.toggle_reminders(callback.from_user.id, not currently_on)
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))
    await callback.answer("Готово ✅")


@dp.callback_query(F.data == "toggle_tomorrow")
async def cb_toggle_tomorrow(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    enabled = not (user and user["tomorrow_on"])
    db.toggle_tomorrow(callback.from_user.id, enabled)
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))
    await callback.answer("Розклад на завтра увімкнено ✅" if enabled else "Розсилку на завтра вимкнено")


@dp.callback_query(F.data == "toggle_announcements")
async def cb_toggle_announcements(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    enabled = not (user and user["announcements_on"])
    db.toggle_announcements(callback.from_user.id, enabled)
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))
    await callback.answer("Оголошення увімкнено ✅" if enabled else "Звичайні оголошення вимкнено")


def materials_subject_keyboard(group: str, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for idx, (subject, count) in enumerate(db.material_subjects(group)):
        label = f"{subject} ({count})"
        label = label if len(label) <= 35 else label[:32] + "..."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mats_subj:{idx}")])
    if has_permission(chat_id, "materials"):
        rows.append([InlineKeyboardButton(text="➕ Додати матеріал", callback_data="mats_add")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def material_item_keyboard(group: str, subject: str, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for m in db.materials_for_group(group):
        if m["subject"] != subject:
            continue
        label = m["title"] if len(m["title"]) <= 30 else m["title"][:27] + "..."
        rows.append([InlineKeyboardButton(text=f"📎 {label}", callback_data=f"mats_get:{m['id']}")])
    rows.append([InlineKeyboardButton(text="🔙 До предметів", callback_data="materials_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def material_subject_choice_keyboard(group: str) -> InlineKeyboardMarkup:
    rows = []
    for idx, subject in enumerate(group_subjects(group)):
        label = subject if len(subject) <= 35 else subject[:32] + "..."
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mats_add_subj:{idx}")])
    rows.append([InlineKeyboardButton(text="✏️ Інша назва", callback_data="mats_add_subj_custom")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="materials_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "materials_menu")
async def cb_materials_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    subjects = db.material_subjects(group)
    text = "📚 <b>Матеріали групи</b>\nОбери предмет:" if subjects else "📚 Матеріалів поки немає."
    await callback.message.edit_text(text, reply_markup=materials_subject_keyboard(group, callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("mats_subj:"))
async def cb_mats_subj(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    idx = int(callback.data.split(":", 1)[1])
    subjects = db.material_subjects(group)
    if idx >= len(subjects):
        await callback.answer("Не знайдено, онови список", show_alert=True)
        return
    subject = subjects[idx][0]
    await callback.message.edit_text(
        f"📚 <b>{html.escape(subject)}</b>", reply_markup=material_item_keyboard(group, subject, callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mats_get:"))
async def cb_mats_get(callback: CallbackQuery):
    material_id = int(callback.data.split(":", 1)[1])
    m = db.get_material(material_id)
    if not m:
        await callback.answer("Матеріал не знайдено", show_alert=True)
        return
    caption = f"📚 {html.escape(m['subject'])} — {html.escape(m['title'])}"
    try:
        if m.get("file_id"):
            file_type = m.get("file_type") or "document"
            sender = {
                "photo": bot.send_photo,
                "audio": bot.send_audio,
                "video": bot.send_video,
            }.get(file_type, bot.send_document)
            await sender(callback.from_user.id, m["file_id"], caption=caption)
        elif m.get("url"):
            await bot.send_message(callback.from_user.id, f"{caption}\n🔗 {html.escape(m['url'])}")
        else:
            await callback.answer("У цього матеріалу немає ні файлу, ні посилання", show_alert=True)
            return
    except Exception:
        log.exception("Не вдалось надіслати матеріал %s", material_id)
        await callback.answer("Не вдалось надіслати, спробуй ще раз", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("mats_del:"))
async def cb_mats_del(callback: CallbackQuery):
    if not has_permission(callback.from_user.id, "materials"):
        await callback.answer("Потрібне право materials 🔒", show_alert=True)
        return
    material_id = int(callback.data.split(":", 1)[1])
    m = db.get_material(material_id)
    if not m:
        await callback.answer("Вже видалено", show_alert=True)
        return
    group, subject = m["group_name"], m["subject"]
    db.delete_material(material_id)
    remaining = [x for x in db.materials_for_group(group) if x["subject"] == subject]
    if remaining:
        await callback.message.edit_text(
            f"📚 <b>{html.escape(subject)}</b>", reply_markup=material_item_keyboard(group, subject, callback.from_user.id)
        )
    else:
        subjects = db.material_subjects(group)
        text = "📚 <b>Матеріали групи</b>\nОбери предмет:" if subjects else "📚 Матеріалів поки немає."
        await callback.message.edit_text(text, reply_markup=materials_subject_keyboard(group, callback.from_user.id))
    await callback.answer("Видалено ✅")


@dp.callback_query(F.data == "mats_add")
async def cb_mats_add(callback: CallbackQuery, state: FSMContext):
    if not has_permission(callback.from_user.id, "materials"):
        await callback.answer("Потрібне право materials 🔒", show_alert=True)
        return
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await state.update_data(group=group)
    await callback.message.edit_text("Для якого предмета матеріал?", reply_markup=material_subject_choice_keyboard(group))
    await callback.answer()


@dp.callback_query(F.data.startswith("mats_add_subj:"))
async def cb_mats_add_subj(callback: CallbackQuery, state: FSMContext):
    if not has_permission(callback.from_user.id, "materials"):
        await callback.answer("Потрібне право materials 🔒", show_alert=True)
        return
    data = await state.get_data()
    group = data.get("group") or await require_group(callback)
    if not group:
        await callback.answer()
        return
    subjects = group_subjects(group)
    idx = int(callback.data.split(":", 1)[1])
    if idx >= len(subjects):
        await callback.answer("Не знайдено, спробуй ще раз", show_alert=True)
        return
    subject = subjects[idx]
    await state.update_data(group=group, subject=subject)
    await state.set_state(MaterialState.waiting_title)
    await callback.message.answer(f"Предмет: {html.escape(subject)}\nВведи коротку назву матеріалу (напр. «Ноти — Щедрик»):")
    await callback.answer()


@dp.callback_query(F.data == "mats_add_subj_custom")
async def cb_mats_add_subj_custom(callback: CallbackQuery, state: FSMContext):
    if not has_permission(callback.from_user.id, "materials"):
        await callback.answer("Потрібне право materials 🔒", show_alert=True)
        return
    data = await state.get_data()
    group = data.get("group") or await require_group(callback)
    if not group:
        await callback.answer()
        return
    await state.update_data(group=group)
    await state.set_state(MaterialState.waiting_subject)
    await callback.message.answer("Введи назву предмета:")
    await callback.answer()


@dp.message(StateFilter(MaterialState.waiting_subject))
async def mats_subject_received(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(MaterialState.waiting_title)
    await message.answer("Введи коротку назву матеріалу (напр. «Ноти — Щедрик»):")


@dp.message(StateFilter(MaterialState.waiting_title))
async def mats_title_received(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(MaterialState.waiting_content)
    await message.answer(
        "Тепер надішли сам файл (документ, фото, аудіо, відео) АБО встав посилання одним повідомленням:"
    )


@dp.message(StateFilter(MaterialState.waiting_content))
async def mats_content_received(message: Message, state: FSMContext):
    data = await state.get_data()
    group, subject, title = data["group"], data["subject"], data["title"]

    file_id = file_name = file_type = None
    url = ""

    if message.document:
        file_id, file_name, file_type = message.document.file_id, message.document.file_name, "document"
    elif message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.audio:
        file_id, file_name, file_type = message.audio.file_id, message.audio.file_name, "audio"
    elif message.video:
        file_id, file_name, file_type = message.video.file_id, message.video.file_name, "video"
    elif message.voice:
        file_id, file_type = message.voice.file_id, "audio"
    elif message.text and re.match(r"https?://", message.text.strip(), re.I):
        url = message.text.strip()
    else:
        await message.answer(
            "Не розпізнав 🤔 Надішли документ/фото/аудіо/відео як вкладення, або встав звичайне https-посилання:"
        )
        return

    await state.clear()
    db.add_material(group, subject, title, url, message.from_user.id, file_id=file_id, file_name=file_name, file_type=file_type)
    await message.answer("Матеріал додано ✅", reply_markup=back_to_menu_keyboard())


async def _show_notes_lesson(target, chat_id: int, group: str, d: date, idx: int):
    entry = _entry_or_none(chat_id, group, d, idx)
    if not entry:
        await target.answer("Ця пара вже не актуальна, спробуй ще раз із поточного розкладу.", show_alert=True)
        return
    key = entry["note_key"]
    notes = db.get_notes(chat_id, key)
    subject = entry.get("subject") or f"Пара {entry.get('pair')}"
    if notes:
        lines = [f"📝 <b>{html.escape(subject)}</b>\n"]
        for i, n in enumerate(notes, start=1):
            lines.append(f"{i}. {html.escape(n['text'])}")
        text = "\n".join(lines)
    else:
        text = f"📝 <b>{html.escape(subject)}</b>\n\nНотаток поки немає."
    await target.message.edit_text(text, reply_markup=notes_lesson_keyboard(d, idx, notes))


@dp.callback_query(F.data.startswith("notes_menu:"))
async def cb_notes_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    entries = build_day_entries(callback.from_user.id, group, d)
    await callback.message.edit_text(
        "📝 Обери предмет:",
        reply_markup=notes_menu_keyboard(callback.from_user.id, group, d, entries),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("notes_lesson:"))
async def cb_notes_lesson(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    d = date.fromisoformat(iso_date)
    await _show_notes_lesson(callback, callback.from_user.id, group, d, int(idx_str))
    await callback.answer()


@dp.callback_query(F.data.startswith("noteadd:"))
async def cb_note_add(callback: CallbackQuery, state: FSMContext):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    await state.update_data(mode="add", group=group, iso_date=iso_date, idx=int(idx_str))
    await state.set_state(NoteState.waiting_text)
    await callback.message.answer("Напиши текст нотатки чи дедлайн для цієї пари (одним повідомленням):")
    await callback.answer()


@dp.callback_query(F.data.startswith("noteedit:"))
async def cb_note_edit(callback: CallbackQuery, state: FSMContext):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str, note_id_str = callback.data.split(":", 3)
    note = db.get_note(callback.from_user.id, int(note_id_str))
    if not note:
        await callback.answer("Нотатку не знайдено", show_alert=True)
        return
    await state.update_data(mode="edit", group=group, iso_date=iso_date, idx=int(idx_str), note_id=int(note_id_str))
    await state.set_state(NoteState.waiting_text)
    await callback.message.answer(
        f"Поточний текст:\n<i>{html.escape(note['text'])}</i>\n\nНапиши новий текст нотатки (одним повідомленням):"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("notedel:"))
async def cb_note_delete(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str, note_id_str = callback.data.split(":", 3)
    db.delete_note(callback.from_user.id, int(note_id_str))
    d = date.fromisoformat(iso_date)
    await _show_notes_lesson(callback, callback.from_user.id, group, d, int(idx_str))
    await callback.answer("Видалено ✅")


@dp.message(StateFilter(NoteState.waiting_text))
async def note_text_received(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    mode, group, iso_date, idx = data["mode"], data["group"], data["iso_date"], data["idx"]
    d = date.fromisoformat(iso_date)
    day_name = DAY_NAMES[d.weekday()]
    entry = _entry_or_none(message.from_user.id, group, d, idx)
    if not entry:
        await message.answer("Ця пара вже не актуальна, спробуй ще раз із поточного розкладу.")
        return

    key = entry["note_key"]
    if mode == "edit":
        db.update_note(message.from_user.id, data["note_id"], message.text)
        await message.answer("Оновлено ✅")
    else:
        label = f"{entry.get('subject') or 'Пара'} ({day_name}, {entry.get('time')})"
        db.add_note(message.from_user.id, key, label, message.text)
        await message.answer("Збережено ✅")

    notes = db.get_notes(message.from_user.id, key)
    subject = entry.get("subject") or f"Пара {entry.get('pair')}"
    lines = [f"📝 <b>{html.escape(subject)}</b>\n"]
    for i, n in enumerate(notes, start=1):
        lines.append(f"{i}. {html.escape(n['text'])}")
    await message.answer("\n".join(lines), reply_markup=notes_lesson_keyboard(d, idx, notes))


# --------------------------------------------------------------- нагадування

# GitHub Actions на безкоштовних/публічних репо реально запускає schedule-cron
# з інтервалами 10-13+ хв замість заданих 5 (документована особливість GH,
# не помилка налаштування). Якщо ловити лише вузьке вікно (0; lead] хвилин
# ДО пари, воно легко "провалюється" між двома запусками cron і нагадування
# не надсилається взагалі. GRACE_MINUTES дозволяє долавити пари, момент
# нагадування яких вже трохи минув, поки бот не встиг перевірити.
GRACE_MINUTES = 10

# Стан останнього запуску check_reminders() — читається через GET /cron-status,
# щоб можна було в браузері перевірити, чи зовнішній пінгер (cron-job.org,
# GitHub Actions тощо) реально стукає в /cron, не копаючись у логах Render.
_last_cron_run: dict = {
    "at": None,          # ISO-час останнього виклику /cron (Kyiv)
    "users_checked": 0,
    "reminders_sent": 0,
    "errors": 0,
}


_check_reminders_lock = asyncio.Lock()


async def check_reminders():
    # Якщо задіяно кілька зовнішніх пінгерів одночасно (cron-job.org +
    # GitHub Actions як резерв), два запити можуть прийти майже одночасно.
    # Без блокування обидва паралельно прочитали б "ще не надіслано" ДО
    # того, як перший встиг би позначити надіслане в базі — і людина
    # отримала б однакове нагадування двічі. Якщо перевірка вже триває,
    # другий виклик просто пропускається: наступний пінг (за кілька
    # хвилин) і так покриє актуальний стан.
    if _check_reminders_lock.locked():
        log.info("check_reminders() вже виконується — пропускаю паралельний виклик")
        return
    async with _check_reminders_lock:
        await _check_reminders_impl()


async def _check_reminders_impl():
    now = now_kyiv()
    today = now.date()

    users_checked = 0
    reminders_sent = 0
    errors = 0

    for user in db.all_users():
        users_checked += 1
        # КРИТИЧНО: кожен користувач обробляється в своєму try/except.
        # Раніше виняток у build_day_entries() (погані дані в overrides,
        # збій зв'язку з Turso тощо) для ОДНОГО користувача обривав увесь
        # цикл — і жоден наступний користувач у списку взагалі не
        # перевірявся на цьому запуску /cron. Тепер поганий запис одного
        # користувача/групи не заважає надіслати нагадування решті.
        try:
            group = user["group"]
            # Кілька офсетів на користувача (напр. [15, 10, 5]) — кожен
            # відстежується окремим ключем sent_reminders, тож людина
            # отримує нагадування на кожному обраному рубежі. Якщо кілька
            # рубежів дозрівають в один прохід — див. due_offsets нижче,
            # це схлопується в одне повідомлення, а не спам з трьох.
            offsets = db.get_reminder_offsets(user["chat_id"]) or [15]
            for entry in build_day_entries(user["chat_id"], group, today):
                if entry.get("cancelled"):
                    continue
                start = lesson_start_time(entry)
                if not start:
                    continue
                start_dt = datetime.combine(today, start, tzinfo=KYIV_TZ)
                minutes_until = (start_dt - now).total_seconds() / 60

                # Спершу збираємо ВСІ офсети, що зараз "дозріли" й ще не
                # надсилались. Якщо тестова пара створена за кілька хвилин
                # до початку (чи пінг сильно спізнився), кілька рубежів
                # (15/10/5) можуть опинитись у вікні одночасно — раніше це
                # означало 3 окремих повідомлення підряд. Тепер шлемо ОДНЕ
                # повідомлення з реальним часом до пари й одразу позначаємо
                # всі "дозрілі" рубежі опрацьованими, щоб вони не спливли
                # повторно на наступному проході.
                due_offsets = []
                for offset in offsets:
                    if not (-GRACE_MINUTES <= minutes_until <= offset):
                        continue
                    key = f"{entry['note_key']}@{offset}"
                    if db.was_reminder_sent(user["chat_id"], key, today.isoformat()):
                        continue
                    due_offsets.append(offset)

                if not due_offsets:
                    continue

                if minutes_until > 0:
                    # Показуємо САМ НАЛАШТОВАНИЙ рубіж (напр. 15), а не сирий
                    # live-відлік — бо той залежить від випадкового моменту
                    # перевірки (cron раз/хв — поріг "15 хв" може бути
                    # пійманий десь між 15.0 і 14.0, і тоді жива хвилина
                    # показала б "14" замість очікуваних "15"). Якщо кілька
                    # рубежів зійшлись одночасно — беремо найближчий (менший).
                    minutes_display = min(due_offsets)
                    text = f"⏰ Через {minutes_display} хв:\n\n{format_entry(entry)}"
                else:
                    text = (
                        f"⏰ Пара вже почалась {abs(round(minutes_until))} хв тому "
                        f"(затримка пінгу):\n\n{format_entry(entry)}"
                    )
                zoom_keyboard = None
                # Вимикається адміном командою /zoom_reminders off (напр. коли
                # група перейшла на очне навчання) — тоді нагадування йде без
                # кнопки Zoom, навіть якщо посилання для викладача є в базі.
                if db.zoom_in_reminders_enabled():
                    found = find_zoom(entry.get("teacher"))
                    if found:
                        _surname, info = found
                        zoom_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="🎥 Приєднатись до Zoom", url=info["link"])
                        ]])
                try:
                    await bot.send_message(user["chat_id"], text, reply_markup=zoom_keyboard)
                    reminders_sent += 1
                except Exception:
                    log.exception("Не вдалось надіслати нагадування %s", user["chat_id"])
                    errors += 1
                for offset in due_offsets:
                    db.mark_reminder_sent(user["chat_id"], f"{entry['note_key']}@{offset}", today.isoformat())
        except Exception:
            log.exception(
                "Збій обробки нагадувань для chat_id=%s (група=%s) — інші користувачі не постраждали",
                user.get("chat_id"),
                user.get("group"),
            )
            errors += 1

    # Вечірній розклад. Зовнішній cron викликає цей код щохвилини; ключ у
    # sent_reminders гарантує, що кожен отримає лише одне повідомлення за вечір.
    if now.hour == 20 and now.minute <= 10:
        tomorrow = today + timedelta(days=1)
        for user in db.tomorrow_users():
            try:
                if db.was_reminder_sent(user["chat_id"], "tomorrow_schedule", today.isoformat()):
                    continue
                text = "🌙 <b>Розклад на завтра</b>\n\n" + format_day_for_chat(
                    user["chat_id"], user["group"], tomorrow
                )
                await bot.send_message(user["chat_id"], text)
                db.mark_reminder_sent(user["chat_id"], "tomorrow_schedule", today.isoformat())
            except Exception:
                log.exception("Не вдалось надіслати розклад на завтра %s", user["chat_id"])
                errors += 1

    _last_cron_run["at"] = now.isoformat()
    _last_cron_run["users_checked"] = users_checked
    _last_cron_run["reminders_sent"] = reminders_sent
    _last_cron_run["errors"] = errors




# --------------------------------------------------------------------- HTTP

async def cron_handler(request: web.Request):
    if request.query.get("secret") != CRON_SECRET:
        return web.Response(status=403, text="forbidden")
    await check_reminders()
    return web.Response(text="ok")


async def health_handler(request: web.Request):
    return web.Response(text="ok")


async def cron_status_handler(request: web.Request):
    """Швидка перевірка, чи зовнішній пінгер (cron-job.org, GitHub Actions
    тощо) реально стукає в /cron. Відкрий у браузері:
    https://твій-бот.onrender.com/cron-status
    Секрет тут не потрібен — дані не чутливі (лише час і лічильники)."""
    info = dict(_last_cron_run)
    if info["at"]:
        last_dt = datetime.fromisoformat(info["at"])
        minutes_ago = round((now_kyiv() - last_dt).total_seconds() / 60, 1)
        info["minutes_since_last_run"] = minutes_ago
        info["looks_healthy"] = minutes_ago < 15  # має бути значно частіше
    else:
        info["minutes_since_last_run"] = None
        info["looks_healthy"] = False
        info["note"] = "check_reminders() ще жодного разу не викликався з моменту старту сервісу"
    return web.json_response(info)


async def on_startup(app: web.Application):
    if WEBHOOK_BASE_URL:
        await bot.set_webhook(
            f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
        log.info("Webhook встановлено на %s%s", WEBHOOK_BASE_URL, WEBHOOK_PATH)
    else:
        log.warning("WEBHOOK_BASE_URL не задано — вебхук не встановлено!")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Постав змінну середовища BOT_TOKEN (токен від @BotFather)")

    db.init_db()

    app = web.Application()
    app.on_startup.append(on_startup)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.router.add_get("/cron", cron_handler)
    app.router.add_get("/cron-status", cron_status_handler)
    app.router.add_get("/", health_handler)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
