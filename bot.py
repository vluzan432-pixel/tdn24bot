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
import html
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

import db
from schedule_data import (
    DAY_NAMES,
    GROUPS,
    find_zoom,
    lesson_key,
    lesson_start_time,
    lessons_for_date,
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


def is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_IDS

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class NoteState(StatesGroup):
    waiting_text = State()


class GroupEditState(StatesGroup):
    waiting_value = State()


class IndividualState(StatesGroup):
    waiting_value = State()


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


IMPORTANT_TYPES = {"пр", "сем", "контр", "мк"}  # практичне, семінар, контрольний захід, модульний контроль

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def today_kyiv() -> date:
    """Render-сервер працює за UTC, а розклад/пари — за київським часом.
    Використовуй цю функцію замість date.today() всюди, де йдеться про
    'сьогодні' для студента."""
    return datetime.now(KYIV_TZ).date()


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)


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
        note_key = lesson_key(group, day_name, lesson)

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


def format_day_for_chat(chat_id: int, group: str, d: date) -> str:
    day_name = DAY_NAMES[d.weekday()]
    header = f"📅 <b>{day_name}, {d.strftime('%d.%m.%Y')}</b>\n👥 Група: {html.escape(group)}"

    entries = build_day_entries(chat_id, group, d)
    if not entries:
        return header + "\n\nПар немає 🎉"

    blocks = []
    for entry in entries:
        text = format_entry(entry)
        notes = db.get_notes(chat_id, entry["note_key"])
        if notes:
            note_lines = "\n".join(f"  📝 {html.escape(n['text'])}" for n in notes)
            text += f"\n{note_lines}"
        blocks.append(text)

    divider = "\n➖➖➖➖➖➖➖➖\n"
    return header + "\n" + divider + divider.join(blocks)


async def broadcast_group(group: str, text: str):
    for chat_id in db.users_in_group(group):
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не вдалось розіслати повідомлення %s", chat_id)


def upcoming_important_text(group: str) -> str:
    today = today_kyiv()
    lines = []
    for offset in range(7):
        d = today + timedelta(days=offset)
        day_name = DAY_NAMES[d.weekday()]
        for lesson, matches in lessons_for_date(group, d):
            for m in matches:
                type_key = m["type"].strip().lower().rstrip(".")
                if type_key not in IMPORTANT_TYPES:
                    continue
                label, emoji = type_label(m["type"])
                pk_suffix = " ⚠️ ПК" if m["pk"] else ""
                subject = html.escape(lesson.get("subject") or "")
                lines.append(
                    f"{emoji} <b>{d.strftime('%d.%m')} ({day_name})</b> {html.escape(lesson.get('time', ''))} "
                    f"— {subject} · {label}{pk_suffix}"
                )
    if not lines:
        return "🗓 На найближчий тиждень семінарів/практичних/контролів не знайдено 🎉"
    return "🗓 <b>Найближчі семінари / практичні / контролі (7 днів):</b>\n\n" + "\n".join(lines)


def keyboard_for_day(chat_id: int, group: str, d: date) -> InlineKeyboardMarkup:
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()
    rows = [
        [
            InlineKeyboardButton(text="◀ Назад", callback_data=f"day:{prev_day}"),
            InlineKeyboardButton(text="Вперед ▶", callback_data=f"day:{next_day}"),
        ],
        [InlineKeyboardButton(text="📅 На сьогодні", callback_data="today")],
    ]

    entries = build_day_entries(chat_id, group, d)
    if any(find_zoom(e.get("teacher")) for e in entries if not e.get("cancelled")):
        rows.append([InlineKeyboardButton(text="🎥 Посилання в Zoom", callback_data=f"zoom_menu:{d.isoformat()}")])

    if entries:
        rows.append([InlineKeyboardButton(text="📝 Нотатки", callback_data=f"notes_menu:{d.isoformat()}")])

    rows.append([InlineKeyboardButton(text="🔔 Нагадування", callback_data="reminders_menu")])
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


def notes_menu_keyboard(chat_id: int, group: str, d: date) -> InlineKeyboardMarkup:
    rows = []
    for idx, entry in enumerate(build_day_entries(chat_id, group, d)):
        count = len(db.get_notes(chat_id, entry["note_key"]))
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
        "📅 <b>Розклад пар</b> — заняття на день, гортання по датах\n"
        "🗓 <b>Найближчі сем./практ./контролі</b> — важливе на тиждень наперед\n"
        "🎓 <b>Індивідуальні заняття</b> — твій особистий розклад\n"
        "✏️ <b>Редагувати розклад</b> — виправити пару, якщо її перенесли\n"
        "👥 <b>Вибір групи</b> — змінити групу"
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Розклад пар", callback_data="today")],
        [InlineKeyboardButton(text="🗓 Найближчі сем./практ./контролі", callback_data="upcoming")],
        [InlineKeyboardButton(text="🎓 Індивідуальні заняття", callback_data="individual_menu")],
        [InlineKeyboardButton(text="✏️ Редагувати розклад", callback_data="edit_menu")],
        [InlineKeyboardButton(text="👥 Вибір групи", callback_data="choose_group")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]])


def individual_menu_text() -> str:
    return (
        "🎓 <b>Індивідуальні заняття</b>\n\n"
        "Тут можна додати власні заняття (інструмент, вокал тощо), які бачиш "
        "тільки ти — вони з'являться в твоєму розкладі дня поруч із парами групи."
    )


def individual_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Додати заняття", callback_data="ind_add")],
        [InlineKeyboardButton(text="👀 Мої заняття", callback_data="ind_view")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ]
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


def reminders_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    user = db.get_user(chat_id)
    on = user["reminders_on"] if user else True
    minutes = user["reminder_minutes"] if user else 15
    rows = [
        [
            InlineKeyboardButton(text=("✅ 5 хв" if minutes == 5 else "5 хв"), callback_data="setmin:5"),
            InlineKeyboardButton(text=("✅ 15 хв" if minutes == 15 else "15 хв"), callback_data="setmin:15"),
            InlineKeyboardButton(text=("✅ 30 хв" if minutes == 30 else "30 хв"), callback_data="setmin:30"),
        ],
        [InlineKeyboardButton(text=("🔕 Вимкнути нагадування" if on else "🔔 Увімкнути нагадування"),
                               callback_data="toggle_reminders")],
        [InlineKeyboardButton(text="🔙 До розкладу", callback_data="today")],
    ]
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
    await callback.message.edit_text(individual_menu_text(), reply_markup=individual_menu_keyboard())
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
    await callback.message.edit_text(upcoming_important_text(group), reply_markup=back_to_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "individual_menu")
async def cb_individual_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    await callback.message.edit_text(individual_menu_text(), reply_markup=individual_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "ind_add")
async def cb_ind_add(callback: CallbackQuery, state: FSMContext):
    await state.update_data(field_idx=0, values={})
    await state.set_state(IndividualState.waiting_value)
    await callback.message.answer(IND_PROMPTS["subject"])
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
    await message.answer("Заняття додано ✅", reply_markup=individual_menu_keyboard())


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
    role = "адміністратор ✅" if is_admin(message.from_user.id) else "звичайний користувач"
    await message.answer(f"Твій chat_id: <code>{message.from_user.id}</code>\nСтатус: {role}")


@dp.message(Command("notes"))
async def cmd_notes(message: Message):
    notes = db.all_notes(message.from_user.id)
    if not notes:
        await message.answer("У тебе поки немає нотаток. Додай їх кнопкою «📝 Нотатка» під парою.")
        return
    lines = ["📝 <b>Твої нотатки:</b>\n"]
    for n in notes:
        lines.append(f"• <b>{html.escape(n['lesson_label'])}</b>: {html.escape(n['text'])} (id {n['id']})")
    lines.append("\nВидалити: <code>/delnote ID</code>")
    await message.answer("\n".join(lines))


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
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, today),
        reply_markup=keyboard_for_day(callback.from_user.id, group, today),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("day:"))
async def cb_day(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, d),
        reply_markup=keyboard_for_day(callback.from_user.id, group, d),
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


@dp.callback_query(F.data == "reminders_menu")
async def cb_reminders_menu(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    status = "увімкнені 🔔" if (not user or user["reminders_on"]) else "вимкнені 🔕"
    minutes = user["reminder_minutes"] if user else 15
    await callback.message.edit_text(
        f"Нагадування зараз {status}, за {minutes} хв до пари.\nОбери інтервал або вимкни:",
        reply_markup=reminders_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("setmin:"))
async def cb_setmin(callback: CallbackQuery):
    minutes = int(callback.data.split(":", 1)[1])
    db.set_reminder_minutes(callback.from_user.id, minutes)
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))
    await callback.answer(f"Нагадування за {minutes} хв ✅")


@dp.callback_query(F.data == "toggle_reminders")
async def cb_toggle_reminders(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    currently_on = user["reminders_on"] if user else True
    db.toggle_reminders(callback.from_user.id, not currently_on)
    await callback.message.edit_reply_markup(reply_markup=reminders_keyboard(callback.from_user.id))
    await callback.answer("Готово ✅")


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
    await callback.message.edit_text("📝 Обери предмет:", reply_markup=notes_menu_keyboard(callback.from_user.id, group, d))
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

async def check_reminders():
    now = now_kyiv()
    today = now.date()

    for user in db.all_users():
        group = user["group"]
        lead = user["reminder_minutes"]
        for entry in build_day_entries(user["chat_id"], group, today):
            if entry.get("cancelled"):
                continue
            start = lesson_start_time(entry)
            if not start:
                continue
            start_dt = datetime.combine(today, start, tzinfo=KYIV_TZ)
            minutes_until = (start_dt - now).total_seconds() / 60
            if not (0 < minutes_until <= lead):
                continue
            key = entry["note_key"]
            if db.was_reminder_sent(user["chat_id"], key, today.isoformat()):
                continue
            text = f"⏰ Через {int(minutes_until)} хв:\n\n{format_entry(entry)}"
            try:
                await bot.send_message(user["chat_id"], text)
            except Exception:
                log.exception("Не вдалось надіслати нагадування %s", user["chat_id"])
            db.mark_reminder_sent(user["chat_id"], key, today.isoformat())


# --------------------------------------------------------------------- HTTP

async def cron_handler(request: web.Request):
    if request.query.get("secret") != CRON_SECRET:
        return web.Response(status=403, text="forbidden")
    await check_reminders()
    return web.Response(text="ok")


async def health_handler(request: web.Request):
    return web.Response(text="ok")


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
    app.router.add_get("/", health_handler)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
