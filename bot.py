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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from openpyxl import load_workbook

import db
from schedule_data import (
    DAY_NAMES,
    GROUPS,
    TIME_START_RE,
    classify_type,
    find_zoom,
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

PERMISSIONS = {"schedule", "announce", "materials", "polls"}
PERMISSION_LABELS = {
    "schedule": "розклад", "announce": "оголошення", "materials": "матеріали", "polls": "опитування",
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

    for lesson, matches in lessons_for_date(group, d):
        pk = (lesson["pair"], lesson["time"])
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
                    "id": f"b:{lesson['pair']}",
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
            "id": f"b:{lesson['pair']}",
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
    if "announce" in rights:
        lines.append("• <code>/announce текст</code> — звичайне оголошення")
        lines.append("• <code>/urgent текст</code> — термінове оголошення")
    if "materials" in rights:
        lines.append("• <code>/material Предмет | Назва | https://посилання</code>")
    if "polls" in rights:
        lines.append("• <code>/poll Питання | Варіант 1 | Варіант 2</code>")
    if is_owner(message.from_user.id):
        lines.append("• <code>/staff</code> — права заступників")
    await message.answer("\n".join(lines))


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
        "Доступні: schedule, announce, materials, polls\n"
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
        await message.answer("Формат: <code>/material Предмет | Назва | https://посилання</code>")
        return
    db.add_material(group, parts[0], parts[1], parts[2], message.from_user.id)
    await message.answer("Матеріал додано ✅")


@dp.message(Command("materials"))
async def cmd_materials(message: Message):
    group = await require_group(message)
    if group:
        await message.answer(materials_text(group, message.text.partition(" ")[2].strip() or None), disable_web_page_preview=True)


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


def materials_text(group: str, query: str | None = None) -> str:
    materials = db.materials_for_group(group, query)
    if not materials:
        return "📚 Матеріалів поки немає." if not query else "📚 За таким предметом матеріалів не знайдено."
    lines = ["📚 <b>Матеріали групи:</b>\n"]
    for item in materials:
        lines.append(
            f"• <b>{html.escape(item['subject'])}</b> — "
            f"<a href=\"{html.escape(item['url'])}\">{html.escape(item['title'])}</a>"
        )
    return "\n".join(lines)


@dp.callback_query(F.data == "materials_menu")
async def cb_materials_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(materials_text(group), reply_markup=back_to_menu_keyboard(), disable_web_page_preview=True)
    await callback.answer()


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


async def check_reminders():
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

                minutes_display = round(minutes_until)
                if minutes_until > 0:
                    text = f"⏰ Через {minutes_display} хв:\n\n{format_entry(entry)}"
                else:
                    text = (
                        f"⏰ Пара вже почалась {abs(minutes_display)} хв тому "
                        f"(затримка пінгу):\n\n{format_entry(entry)}"
                    )
                zoom_keyboard = None
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
