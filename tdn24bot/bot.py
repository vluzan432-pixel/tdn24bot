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
from datetime import date, datetime, timedelta

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

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class NoteState(StatesGroup):
    waiting_text = State()


# ---------------------------------------------------------------- рендеринг

def format_lesson(group: str, day_name: str, lesson: dict, matches: list) -> str:
    if not lesson.get("parsed_ok", True):
        body = (
            f"🕐 <b>Пара {html.escape(lesson['pair'])}</b> · {html.escape(lesson['time'])}\n"
            + html.escape(lesson["raw"])
        )
    else:
        lines = [f"🕐 <b>Пара {html.escape(lesson['pair'])}</b> · {html.escape(lesson['time'])}"]
        subject = html.escape(lesson["subject"] or "")
        if lesson.get("note"):
            subject += f" <i>{html.escape(lesson['note'])}</i>"
        lines.append(f"📘 {subject}")

        if matches:
            type_strs = []
            for m in matches:
                label, emoji = type_label(m["type"])
                pk_suffix = " ⚠️ ПК" if m["pk"] else ""
                type_strs.append(f"{emoji} {label}{pk_suffix}")
            lines.append(" / ".join(type_strs))

        if lesson.get("teacher"):
            lines.append(f"👤 {html.escape(lesson['teacher'])}")
        if lesson.get("room"):
            lines.append(f"📍 {html.escape(lesson['room'])}")
        body = "\n".join(lines)

    return body


def format_day_for_chat(chat_id: int, group: str, d: date) -> str:
    day_name = DAY_NAMES[d.weekday()]
    header = f"📅 <b>{day_name}, {d.strftime('%d.%m.%Y')}</b>\n👥 Група: {html.escape(group)}"

    entries = lessons_for_date(group, d)
    if not entries:
        return header + "\n\nПар немає 🎉"

    blocks = []
    for lesson, matches in entries:
        key = lesson_key(group, day_name, lesson)
        text = format_lesson(group, day_name, lesson, matches)
        notes = db.get_notes(chat_id, key)
        if notes:
            note_lines = "\n".join(f"  📝 {html.escape(n['text'])}" for n in notes)
            text += f"\n{note_lines}"
        blocks.append(text)

    divider = "\n➖➖➖➖➖➖➖➖\n"
    return header + "\n" + divider + divider.join(blocks)


def keyboard_for_day(group: str, d: date) -> InlineKeyboardMarkup:
    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()
    rows = [
        [
            InlineKeyboardButton(text="◀ Назад", callback_data=f"day:{prev_day}"),
            InlineKeyboardButton(text="Вперед ▶", callback_data=f"day:{next_day}"),
        ],
        [InlineKeyboardButton(text="📅 На сьогодні", callback_data="today")],
    ]

    entries = lessons_for_date(group, d)
    if any(find_zoom(lesson.get("teacher")) for lesson, _ in entries):
        rows.append([InlineKeyboardButton(text="🎥 Посилання в Zoom", callback_data=f"zoom_menu:{d.isoformat()}")])

    for idx, (lesson, _matches) in enumerate(entries):
        subject = lesson.get("subject") or f"Пара {lesson.get('pair')}"
        label = subject if len(subject) <= 30 else subject[:27] + "..."
        rows.append([InlineKeyboardButton(text=f"📝 Нотатка: {label}", callback_data=f"note:{d.isoformat()}:{idx}")])

    rows.append([InlineKeyboardButton(text="🔔 Нагадування", callback_data="reminders_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def zoom_menu_keyboard(group: str, d: date) -> InlineKeyboardMarkup:
    rows = []
    for idx, (lesson, _matches) in enumerate(lessons_for_date(group, d)):
        found = find_zoom(lesson.get("teacher"))
        if not found:
            continue
        subject = lesson.get("subject") or "Пара"
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


def group_choice_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=g, callback_data=f"setgroup:{g}")] for g in GROUPS]
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
    today = date.today()
    await message.answer(format_day_for_chat(message.from_user.id, group, today), reply_markup=keyboard_for_day(group, today))


@dp.message(Command("group"))
async def cmd_group(message: Message):
    await message.answer("Обери свою групу:", reply_markup=group_choice_keyboard())


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
    today = date.today()
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, today), reply_markup=keyboard_for_day(group, today)
    )
    await callback.answer(f"Група {group} збережена ✅")


@dp.callback_query(F.data == "today")
async def cb_today(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    today = date.today()
    await callback.message.edit_text(
        format_day_for_chat(callback.from_user.id, group, today), reply_markup=keyboard_for_day(group, today)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("day:"))
async def cb_day(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    await callback.message.edit_text(format_day_for_chat(callback.from_user.id, group, d), reply_markup=keyboard_for_day(group, d))
    await callback.answer()


@dp.callback_query(F.data.startswith("zoom_menu:"))
async def cb_zoom_menu(callback: CallbackQuery):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    d = date.fromisoformat(callback.data.split(":", 1)[1])
    entries = lessons_for_date(group, d)
    if not any(find_zoom(lesson.get("teacher")) for lesson, _ in entries):
        await callback.answer("На цей день немає збережених посилань", show_alert=True)
        return
    await callback.message.edit_text("🎥 Обери предмет:", reply_markup=zoom_menu_keyboard(group, d))
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
    entries = lessons_for_date(group, d)
    if idx >= len(entries):
        await callback.answer("Не знайдено", show_alert=True)
        return
    lesson, _matches = entries[idx]
    found = find_zoom(lesson.get("teacher"))
    if not found:
        await callback.answer("Посилання не знайдено", show_alert=True)
        return
    _surname, info = found
    subject = html.escape(lesson.get("subject") or "")
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


@dp.callback_query(F.data.startswith("note:"))
async def cb_note(callback: CallbackQuery, state: FSMContext):
    group = await require_group(callback)
    if not group:
        await callback.answer()
        return
    _, iso_date, idx_str = callback.data.split(":", 2)
    await state.update_data(group=group, iso_date=iso_date, idx=int(idx_str))
    await state.set_state(NoteState.waiting_text)
    await callback.message.answer("Напиши текст нотатки чи дедлайн для цієї пари (одним повідомленням):")
    await callback.answer()


@dp.message(StateFilter(NoteState.waiting_text))
async def note_text_received(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    group, iso_date, idx = data["group"], data["iso_date"], data["idx"]
    d = date.fromisoformat(iso_date)
    day_name = DAY_NAMES[d.weekday()]
    entries = lessons_for_date(group, d)
    if idx >= len(entries):
        await message.answer("Ця пара вже не актуальна, спробуй ще раз із поточного розкладу.")
        return
    lesson, _matches = entries[idx]
    key = lesson_key(group, day_name, lesson)
    label = f"{lesson.get('subject') or 'Пара'} ({day_name}, {lesson.get('time')})"
    db.add_note(message.from_user.id, key, label, message.text)
    await message.answer("Збережено ✅")
    await message.answer(format_day_for_chat(message.from_user.id, group, d), reply_markup=keyboard_for_day(group, d))


# --------------------------------------------------------------- нагадування

async def check_reminders():
    now = datetime.now()
    today = now.date()
    day_name = DAY_NAMES[today.weekday()]

    for user in db.all_users():
        group = user["group"]
        lead = user["reminder_minutes"]
        for lesson, matches in lessons_for_date(group, today):
            start = lesson_start_time(lesson)
            if not start:
                continue
            start_dt = datetime.combine(today, start)
            minutes_until = (start_dt - now).total_seconds() / 60
            if not (0 < minutes_until <= lead):
                continue
            key = lesson_key(group, day_name, lesson)
            if db.was_reminder_sent(user["chat_id"], key, today.isoformat()):
                continue
            text = f"⏰ Через {int(minutes_until)} хв:\n\n{format_lesson(group, day_name, lesson, matches)}"
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
