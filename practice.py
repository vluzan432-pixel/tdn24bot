"""
practice.py — розділ «Практика» + «Мій профіль» (окремий aiogram Router).

Підключається в bot.py двома рядками:
    practice.configure(has_permission=has_permission)
    dp.include_router(practice.router)
і викликається з cron:
    await practice.check_practice_reminders(bot, now)

Усі callback_data тут починаються з префіксів pr_ / pf_ / prrem_ / pra_, тож
з наявними хендлерами bot.py конфліктів немає.
"""

import asyncio
import html
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import ai_service as ai
import db
import docx_generator
import practice_ai
from schedule_data import GROUPS

log = logging.getLogger("schedule-bot.practice")
router = Router(name="practice")

_has_permission = lambda chat_id, perm: False  # підставляється через configure()


def configure(*, has_permission):
    global _has_permission
    _has_permission = has_permission


# ------------------------------------------------------------------ утиліти

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    built = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
    return InlineKeyboardMarkup(inline_keyboard=[r for r in built if r])


def esc(text) -> str:
    return html.escape(str(text or ""))


def chunks(text: str, size: int = 3800) -> list:
    out, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > size and current:
            out.append(current)
            current = ""
        current += para + "\n"
    if current.strip():
        out.append(current)
    return out


async def show(target, text: str, markup=None):
    """Редагує повідомлення (callback) або надсилає нове (message)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
            return
    await target.answer(text, reply_markup=markup)


def msg_of(target) -> Message:
    return target.message if isinstance(target, CallbackQuery) else target


CANCEL = btn("❌ Скасувати", "pr_cancel")


def _group_of(chat_id: int):
    user = db.get_user(chat_id)
    return user["group"] if user else None


def _ctx(chat_id: int):
    group = _group_of(chat_id)
    cfg = db.get_practice_cfg(group) if group else db._practice_defaults()
    cfg["group"] = group or ""
    return db.get_profile(chat_id) or {}, cfg


def parse_date(text: str):
    t = (text or "").strip().lower().replace("/", ".").replace("-", ".")
    t = re.sub(r"\s*р\.?$", "", t).strip(". ")
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", t)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (2000 + int(year)) if year and len(year) == 2 else int(year) if year else date.today().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def today_kyiv() -> date:
    return db._today_kyiv()


def fmt_ua(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def surname(profile: dict) -> str:
    name = (profile.get("full_name") or "студент").split()[0]
    return re.sub(r"[^\w\-]", "", name) or "студент"


# =================================================================== МЕНЮ ПРАКТИКИ

def practice_menu_keyboard() -> InlineKeyboardMarkup:
    return kb(
        btn("📝 Додати запис у щоденник", "pr_add"),
        btn("📋 Мої записи", "pr_list:0"),
        btn("📄 Отримати щоденник (.docx)", "pr_diary"),
        btn("📑 Сформувати звіт практики (.docx)", "pr_report"),
        btn("📰 Новини практики", "pr_news:0"),
        btn("⏰ Нагадування", "pr_rem"),
        btn("🏠 Меню", "main_menu"),
    )


PRACTICE_MENU_TEXT = (
    "🎼 <b>Практика</b>\n\n"
    "📝 <b>Додати запис</b> — ШІ допоможе оформити запис щоденника\n"
    "📋 <b>Мої записи</b> — перегляд, редагування, видалення\n"
    "📄 <b>Щоденник</b> — готовий .docx за вимогами кафедри\n"
    "📑 <b>Звіт</b> — повний звіт про практику (.docx)\n"
    "📰 <b>Новини</b> — оголошення по практиці\n"
    "⏰ <b>Нагадування</b> — щоденне та суботнє"
)


async def _ensure_profile(target) -> bool:
    chat_id = target.from_user.id
    if not _group_of(chat_id):
        await show(target, "Спершу обери свою групу: /start", None)
        return False
    if db.profile_is_complete(chat_id):
        return True
    await show(
        target,
        "👤 Щоб користуватись практикою, спершу заповни профіль — він потрібен для титульної сторінки "
        "й щоб ШІ писав саме про твою спеціалізацію.",
        kb(btn("▶️ Заповнити профіль", "pf_start"), btn("🏠 Меню", "main_menu")),
    )
    return False


@router.callback_query(F.data == "practice_menu")
async def cb_practice_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if await _ensure_profile(callback):
        await show(callback, PRACTICE_MENU_TEXT, practice_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "pr_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if db.profile_is_complete(callback.from_user.id):
        await show(callback, PRACTICE_MENU_TEXT, practice_menu_keyboard())
    else:
        await show(callback, "Скасовано.", kb(btn("🏠 Меню", "main_menu")))
    await callback.answer("Скасовано")


# =================================================================== ПРОФІЛЬ

class ProfileState(StatesGroup):
    waiting_name = State()
    waiting_gen_manual = State()
    waiting_spec = State()
    waiting_base = State()
    waiting_collective = State()
    waiting_supervisor = State()
    waiting_value = State()  # редагування одного поля


FIELD_LABELS = {
    "full_name": "ПІБ",
    "specialization": "Спеціалізація / ролі на практиці",
    "practice_base": "Назва бази практики",
    "collective": "Основний творчий колектив",
    "base_supervisor": "ПІБ та посада керівника від бази практики",
}
OPTIONAL_FIELDS = {"collective", "base_supervisor"}
FIELD_HINTS = {
    "full_name": "Введи ПІБ повністю, напр.: <i>Лузан Владислав Олександрович</i>",
    "specialization": (
        "Яка твоя спеціалізація / ролі на практиці? Напр.: <i>вокаліст, диригент/керівник ансамблю, "
        "артист хору, інструменталіст, аранжувальник, організатор культурно-мистецьких заходів</i> "
        "(можна кілька)."
    ),
    "practice_base": "Назва бази практики (за розподілом):",
    "collective": "Основний творчий колектив, у якому працюєш на практиці (або пропусти):",
    "base_supervisor": "ПІБ та посада керівника від бази практики (можна пропустити й додати пізніше):",
}


def profile_text(chat_id: int) -> str:
    p = db.get_profile(chat_id) or {}
    group = _group_of(chat_id) or "—"
    cfg = db.get_practice_cfg(group) if group != "—" else db._practice_defaults()
    course = p.get("course") or f"{cfg['course']} (як у групі)"
    kind = p.get("practice_kind") or f"{cfg['practice_kind']} (як у групі)"

    def v(key):
        return esc(p.get(key)) if p.get(key) else "<i>не вказано</i>"

    return (
        "👤 <b>Мій профіль</b>\n\n"
        f"<b>ПІБ:</b> {v('full_name')}\n"
        f"<b>Родовий відмінок:</b> {v('full_name_gen')}\n"
        f"<b>Група:</b> {esc(group)}\n"
        f"<b>Курс:</b> {esc(course)}\n"
        f"<b>Вид практики:</b> {esc(kind)}\n"
        f"<b>Спеціалізація:</b> {v('specialization')}\n"
        f"<b>База практики:</b> {v('practice_base')}\n"
        f"<b>Колектив:</b> {v('collective')}\n"
        f"<b>Керівник від бази:</b> {v('base_supervisor')}"
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return kb(
        [btn("✏️ ПІБ", "pf_edit:full_name"), btn("🎼 Спеціалізація", "pf_edit:specialization")],
        [btn("🏫 База практики", "pf_edit:practice_base"), btn("🎭 Колектив", "pf_edit:collective")],
        [btn("👨‍🏫 Керівник від бази", "pf_edit:base_supervisor")],
        [btn("🎓 Курс", "pf_course"), btn("📋 Вид практики", "pf_kind")],
        [btn("👥 Змінити групу", "choose_group")],
        [btn("🏠 Меню", "main_menu")],
    )


@router.callback_query(F.data == "profile_menu")
async def cb_profile_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not _group_of(callback.from_user.id):
        await show(callback, "Спершу обери свою групу: /start", None)
    elif not db.profile_is_complete(callback.from_user.id):
        await show(
            callback,
            "👤 <b>Мій профіль</b>\n\nПрофіль ще не заповнено. Це займе хвилину: ПІБ, спеціалізація, "
            "база практики.",
            kb(btn("▶️ Заповнити профіль", "pf_start"), btn("👥 Змінити групу", "choose_group"),
               btn("🏠 Меню", "main_menu")),
        )
    else:
        await show(callback, profile_text(callback.from_user.id), profile_keyboard())
    await callback.answer()


@router.callback_query(F.data == "pf_cancel")
async def cb_pf_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_profile_menu(callback, state)


def pf_cancel_row():
    return btn("❌ Скасувати", "pf_cancel")


@router.callback_query(F.data == "pf_start")
async def cb_pf_start(callback: CallbackQuery, state: FSMContext):
    if not _group_of(callback.from_user.id):
        await callback.answer("Спершу обери групу: /start", show_alert=True)
        return
    await state.clear()
    await state.set_state(ProfileState.waiting_name)
    await state.update_data(onboarding=True)
    await show(callback, "👋 Давай заповнимо профіль.\n\n" + FIELD_HINTS["full_name"], kb(pf_cancel_row()))
    await callback.answer()


@router.callback_query(F.data.startswith("pf_edit:"))
async def cb_pf_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    if field not in FIELD_LABELS:
        await callback.answer()
        return
    await state.clear()
    if field == "full_name":
        await state.set_state(ProfileState.waiting_name)
        await state.update_data(onboarding=False)
    else:
        await state.set_state(ProfileState.waiting_value)
        await state.update_data(field=field)
    rows = []
    if field in OPTIONAL_FIELDS:
        rows.append(btn("🗑 Очистити", "pf_clear"))
    rows.append(pf_cancel_row())
    await show(callback, f"✏️ <b>{FIELD_LABELS[field]}</b>\n\n{FIELD_HINTS[field]}", kb(*rows))
    await callback.answer()


@router.callback_query(F.data == "pf_clear", StateFilter(ProfileState.waiting_value))
async def cb_pf_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    db.save_profile(callback.from_user.id, **{data["field"]: None})
    await state.clear()
    await show(callback, profile_text(callback.from_user.id), profile_keyboard())
    await callback.answer("Очищено")


@router.message(StateFilter(ProfileState.waiting_value), F.text)
async def pf_value_received(message: Message, state: FSMContext):
    data = await state.get_data()
    value = message.text.strip()
    if len(value) > 300:
        await message.answer("Задовго (макс. 300 символів). Скороти, будь ласка.")
        return
    db.save_profile(message.from_user.id, **{data["field"]: value})
    await state.clear()
    await message.answer("Збережено ✅\n\n" + profile_text(message.from_user.id), reply_markup=profile_keyboard())


# --- ПІБ + родовий відмінок

@router.message(StateFilter(ProfileState.waiting_name), F.text)
async def pf_name_received(message: Message, state: FSMContext):
    name = re.sub(r"\s+", " ", message.text.strip())
    if len(name.split()) < 2 or len(name) > 120 or re.search(r"\d", name):
        await message.answer("Введи ПІБ повністю (прізвище, ім'я, по батькові), без цифр.")
        return
    await state.update_data(name=name)
    status = await message.answer("⏳ Формую родовий відмінок…")
    try:
        gen = await ai.to_genitive_fio(name)
    except ai.AIUnavailable:
        await state.set_state(ProfileState.waiting_gen_manual)
        await status.edit_text(
            "ШІ зараз недоступний 😔 Напиши ПІБ у родовому відмінку сам(а) (кого? — «Лузана Владислава "
            "Олександровича»):",
            reply_markup=kb(pf_cancel_row()),
        )
        return
    await _confirm_gen(status, state, gen)


async def _confirm_gen(target_message: Message, state: FSMContext, gen: str):
    await state.update_data(gen=gen)
    await target_message.edit_text(
        f"На титульній сторінці буде:\n\n<b>Здобувача {esc(gen)}</b>\n\nВсе написано правильно?",
        reply_markup=kb(
            [btn("✅ Так", "pf_gen_ok"), btn("✏️ Виправити", "pf_gen_fix")],
            [pf_cancel_row()],
        ),
    )


@router.callback_query(F.data == "pf_gen_fix")
async def cb_gen_fix(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileState.waiting_gen_manual)
    await show(callback, "Напиши ПІБ у родовому відмінку (кого?):", kb(pf_cancel_row()))
    await callback.answer()


@router.message(StateFilter(ProfileState.waiting_gen_manual), F.text)
async def pf_gen_manual(message: Message, state: FSMContext):
    gen = re.sub(r"\s+", " ", message.text.strip())
    if len(gen.split()) < 2 or len(gen) > 120:
        await message.answer("Введи ПІБ повністю у родовому відмінку.")
        return
    status = await message.answer("…")
    await _confirm_gen(status, state, gen)


@router.callback_query(F.data == "pf_gen_ok")
async def cb_gen_ok(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("name") or not data.get("gen"):
        await callback.answer("Почни спочатку: Мій профіль", show_alert=True)
        return
    db.save_profile(callback.from_user.id, full_name=data["name"], full_name_gen=data["gen"])
    if data.get("onboarding"):
        await state.set_state(ProfileState.waiting_spec)
        await show(callback, "Збережено ✅\n\n" + FIELD_HINTS["specialization"], kb(pf_cancel_row()))
    else:
        await state.clear()
        await show(callback, "Збережено ✅\n\n" + profile_text(callback.from_user.id), profile_keyboard())
    await callback.answer()


# --- решта кроків онбордингу

def _skip_row():
    return [btn("⏭ Пропустити", "pf_skip"), pf_cancel_row()]


@router.message(StateFilter(ProfileState.waiting_spec), F.text)
async def pf_spec(message: Message, state: FSMContext):
    db.save_profile(message.from_user.id, specialization=message.text.strip()[:300])
    await state.set_state(ProfileState.waiting_base)
    await message.answer(FIELD_HINTS["practice_base"], reply_markup=kb(_skip_row()))


@router.message(StateFilter(ProfileState.waiting_base), F.text)
async def pf_base(message: Message, state: FSMContext):
    db.save_profile(message.from_user.id, practice_base=message.text.strip()[:300])
    await state.set_state(ProfileState.waiting_collective)
    await message.answer(FIELD_HINTS["collective"], reply_markup=kb(_skip_row()))


@router.message(StateFilter(ProfileState.waiting_collective), F.text)
async def pf_collective(message: Message, state: FSMContext):
    db.save_profile(message.from_user.id, collective=message.text.strip()[:300])
    await state.set_state(ProfileState.waiting_supervisor)
    await message.answer(FIELD_HINTS["base_supervisor"], reply_markup=kb(_skip_row()))


async def _finish_onboarding(target, state: FSMContext):
    await state.clear()
    await show(
        target,
        "Профіль готовий ✅\n\n" + profile_text(target.from_user.id),
        kb(btn("🎼 До практики", "practice_menu"), btn("✏️ Редагувати профіль", "profile_menu")),
    )


@router.message(StateFilter(ProfileState.waiting_supervisor), F.text)
async def pf_supervisor(message: Message, state: FSMContext):
    db.save_profile(message.from_user.id, base_supervisor=message.text.strip()[:300])
    await _finish_onboarding(message, state)


@router.callback_query(F.data == "pf_skip")
async def cb_pf_skip(callback: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current == ProfileState.waiting_base.state:
        await state.set_state(ProfileState.waiting_collective)
        await show(callback, FIELD_HINTS["collective"], kb(_skip_row()))
    elif current == ProfileState.waiting_collective.state:
        await state.set_state(ProfileState.waiting_supervisor)
        await show(callback, FIELD_HINTS["base_supervisor"], kb(_skip_row()))
    elif current == ProfileState.waiting_supervisor.state:
        await _finish_onboarding(callback, state)
    await callback.answer()


# --- курс / вид практики

@router.callback_query(F.data == "pf_course")
async def cb_pf_course(callback: CallbackQuery):
    await show(
        callback,
        "🎓 Обери свій курс:",
        kb([btn("ІІ", "pf_setcourse:ІІ"), btn("ІІІ", "pf_setcourse:ІІІ"), btn("ІV", "pf_setcourse:ІV")],
           [btn("Як у налаштуваннях групи", "pf_setcourse:")], [btn("🔙 Профіль", "profile_menu")]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pf_setcourse:"))
async def cb_pf_setcourse(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1] or None
    db.save_profile(callback.from_user.id, course=value)
    await show(callback, profile_text(callback.from_user.id), profile_keyboard())
    await callback.answer("Збережено ✅")


@router.callback_query(F.data == "pf_kind")
async def cb_pf_kind(callback: CallbackQuery):
    await show(
        callback,
        "📋 Вид практики:",
        kb([btn("Навчальна", "pf_setkind:навчальна"), btn("Виробнича", "pf_setkind:виробнича")],
           [btn("Як у налаштуваннях групи", "pf_setkind:")], [btn("🔙 Профіль", "profile_menu")]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pf_setkind:"))
async def cb_pf_setkind(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1] or None
    db.save_profile(callback.from_user.id, practice_kind=value)
    await show(callback, profile_text(callback.from_user.id), profile_keyboard())
    await callback.answer("Збережено ✅")


# =================================================================== ЩОДЕННИК: НОВИЙ ЗАПИС

class DiaryState(StatesGroup):
    waiting_date = State()
    waiting_desc = State()
    waiting_answers = State()
    waiting_extra = State()
    waiting_photos = State()


AI_DOWN_TEXT = (
    "😔 ШІ тимчасово недоступний або перевищено ліміт запитів. Нічого страшного — "
    "спробуй ще раз за хвилину-дві."
)


@router.callback_query(F.data == "pr_add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_profile(callback):
        await callback.answer()
        return
    await state.clear()
    await show(
        callback,
        "📝 <b>Новий запис у щоденник</b>\n\nНа яку дату запис?",
        kb([btn("Сьогодні", "pr_date:today"), btn("Вчора", "pr_date:yest")],
           [btn("📅 Ввести дату вручну", "pr_date:manual")], [CANCEL]),
    )
    await callback.answer()


async def _ask_description(target, state: FSMContext, iso: str):
    await state.update_data(date=iso)
    await state.set_state(DiaryState.waiting_desc)
    await show(
        target,
        f"📅 Дата: <b>{fmt_ua(iso)}</b>\n\nРозкажи своїми словами, чим займався(лась) цього дня "
        "(напр.: <i>«сьогодні займався з ансамблем»</i> або <i>«був на вебінарі з доброчесності»</i>). "
        "Що більше деталей — то кращий запис.",
        kb(CANCEL),
    )


@router.callback_query(F.data.startswith("pr_date:"))
async def cb_date(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    if choice == "manual":
        await state.set_state(DiaryState.waiting_date)
        await show(callback, "Введи дату у форматі <b>ДД.ММ.РР</b> (напр. 24.11.25):", kb(CANCEL))
    else:
        d = today_kyiv() - timedelta(days=1 if choice == "yest" else 0)
        await _ask_description(callback, state, d.isoformat())
    await callback.answer()


@router.message(StateFilter(DiaryState.waiting_date), F.text)
async def date_received(message: Message, state: FSMContext):
    d = parse_date(message.text)
    if not d:
        await message.answer("Не розпізнав дату. Формат: <b>ДД.ММ.РР</b>, напр. <code>24.11.25</code>.")
        return
    if d > today_kyiv():
        await message.answer("Ця дата ще не настала 🙂 Введи дату не пізніше сьогоднішньої.")
        return
    await _ask_description(message, state, d.isoformat())


@router.message(StateFilter(DiaryState.waiting_desc), F.text)
async def desc_received(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 5:
        await message.answer("Опиши трохи детальніше, будь ласка (хоча б одне речення).")
        return
    await state.update_data(desc=text[:4000])
    await _run_analyze(message, state)


async def _run_analyze(target, state: FSMContext):
    # PRACTICE_SKIP_ANALYSIS=1 (змінна Render) — не ставити навідні запитання, одразу генерувати
    # запис: економить 1 запит до ШІ на кожен запис, коли ліміт Gemini вузький.
    if os.environ.get("PRACTICE_SKIP_ANALYSIS") == "1":
        await _run_generate(target, state)
        return
    data = await state.get_data()
    profile, cfg = _ctx(target.from_user.id)
    status = await msg_of(target).answer("⏳ Аналізую опис…")
    try:
        questions = await practice_ai.analyze_entry(profile, cfg, data["desc"])
    except ai.AIUnavailable as e:
        log.warning("analyze_entry: %s", e)
        await status.edit_text(AI_DOWN_TEXT, reply_markup=kb([btn("🔁 Спробувати ще раз", "pr_retry_analyze")], [CANCEL]))
        return
    if not questions:
        await status.delete()
        await _run_generate(target, state)
        return
    await state.update_data(questions=questions)
    await state.set_state(DiaryState.waiting_answers)
    numbered = "\n".join(f"{i}. {esc(q)}" for i, q in enumerate(questions, 1))
    await status.edit_text(
        "Щоб запис вийшов змістовним, уточни, будь ласка (можна однією відповіддю):\n\n" + numbered,
        reply_markup=kb([btn("⏩ Згенерувати з тим, що є", "pr_gen_now")], [CANCEL]),
    )


@router.callback_query(F.data == "pr_retry_analyze", StateFilter(DiaryState.waiting_desc))
async def cb_retry_analyze(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _run_analyze(callback, state)


@router.message(StateFilter(DiaryState.waiting_answers), F.text)
async def answers_received(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(desc=(data["desc"] + "\nВідповіді на уточнення: " + message.text.strip())[:6000])
    await _run_generate(message, state)


@router.callback_query(F.data == "pr_gen_now", StateFilter(DiaryState.waiting_answers))
async def cb_gen_now(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _run_generate(callback, state)


async def _run_generate(target, state: FSMContext):
    data = await state.get_data()
    profile, cfg = _ctx(target.from_user.id)
    status = await msg_of(target).answer("⏳ Генерую запис…")
    try:
        draft = await practice_ai.generate_entry(profile, cfg, fmt_ua(data["date"]), data["desc"])
    except ai.AIUnavailable as e:
        log.warning("generate_entry: %s", e)
        await status.edit_text(AI_DOWN_TEXT, reply_markup=kb([btn("🔁 Спробувати ще раз", "pr_retry_gen")], [CANCEL]))
        await state.set_state(DiaryState.waiting_extra)
        return
    await state.update_data(draft=draft)
    await state.set_state(None)
    await status.edit_text(_draft_text(data["date"], draft), reply_markup=_draft_keyboard())


def _draft_text(iso: str, draft: dict) -> str:
    return (
        f"📅 <b>{fmt_ua(iso)}</b>\n\n"
        f"<b>Перелік виконаних завдань та робот:</b>\n{esc(draft['tasks'])}\n\n"
        f"<b>Анотація:</b>\n{esc(draft['annotation'])}"
    )


def _draft_keyboard():
    return kb(
        [btn("✅ Зберегти", "pr_save")],
        [btn("🔄 Перегенерувати / доповнити", "pr_regen")],
        [CANCEL],
    )


@router.callback_query(F.data == "pr_retry_gen")
async def cb_retry_gen(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _run_generate(callback, state)


@router.callback_query(F.data == "pr_regen")
async def cb_regen(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DiaryState.waiting_extra)
    await show(
        callback,
        "Напиши, що додати чи змінити (напр. назви творів, колектив, формат) — і я перегенерую запис. "
        "Або натисни кнопку, щоб просто спробувати ще раз.",
        kb([btn("🔄 Перегенерувати без змін", "pr_regen_plain")], [CANCEL]),
    )
    await callback.answer()


@router.callback_query(F.data == "pr_regen_plain")
async def cb_regen_plain(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _run_generate(callback, state)


@router.message(StateFilter(DiaryState.waiting_extra), F.text)
async def extra_received(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(desc=(data["desc"] + "\nУточнення студента: " + message.text.strip())[:6000])
    await _run_generate(message, state)


@router.callback_query(F.data == "pr_save")
async def cb_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft = data.get("draft")
    if not draft:
        await callback.answer("Чернетка застаріла — почни запис знову", show_alert=True)
        return
    entry_id = db.add_practice_entry(callback.from_user.id, data["date"], draft["tasks"], draft["annotation"])
    await state.clear()
    await state.set_state(DiaryState.waiting_photos)
    await state.update_data(entry_id=entry_id, last_photo_id=None)
    await show(
        callback,
        "✅ Запис збережено!\n\n📷 Тепер надішли фото-додатки (скріншоти Zoom, фото з репетицій, сайтів тощо) — "
        "можна кілька поспіль. Для кожного фото можна написати підпис або взяти автопідпис.",
        kb([btn("🏁 Без фото / завершити", "pr_photo_done")]),
    )
    await callback.answer()


# --- фото-додатки
#
# Кожне фото зберігаємо ОДРАЗУ (з автопідписом) — так жодне фото з альбому не губиться,
# навіть якщо Telegram надішле їх майже одночасно. Підпис можна змінити текстом одразу
# після фото (змінюється підпис останнього фото).

_album_replied: set = set()


def _auto_caption(entry: dict) -> str:
    event = (entry["tasks"] or "").strip().rstrip(".").strip()
    return f"{event} {docx_generator.fmt_date_caption(entry['entry_date'])}"


PHOTO_KB = kb([btn("🏁 Завершити", "pr_photo_done")])


@router.message(StateFilter(DiaryState.waiting_photos), F.photo)
async def photo_received(message: Message, state: FSMContext):
    data = await state.get_data()
    entry = db.get_practice_entry(message.from_user.id, data["entry_id"])
    if not entry:
        await state.clear()
        await message.answer("Запис не знайдено.")
        return
    caption = _auto_caption(entry)
    photo_id = db.add_practice_photo(entry["id"], message.photo[-1].file_id, caption)
    await state.update_data(last_photo_id=photo_id)
    group_id = message.media_group_id
    if group_id:
        if group_id in _album_replied:
            return
        if len(_album_replied) > 500:
            _album_replied.clear()
        _album_replied.add(group_id)
    await message.answer(
        f"Фото збережено ✅ Автопідпис:\n<i>{esc(caption)}</i>\n\n"
        "Хочеш свій підпис — напиши його текстом (змінить підпис останнього фото). "
        "Або надішли ще фото чи заверши.",
        reply_markup=PHOTO_KB,
    )


@router.message(StateFilter(DiaryState.waiting_photos), F.text)
async def photo_caption(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_photo_id"):
        await message.answer("Надішли фото 📷 або натисни «Завершити».", reply_markup=PHOTO_KB)
        return
    db.update_practice_photo_caption(data["entry_id"], data["last_photo_id"], message.text.strip()[:300])
    await message.answer("Підпис оновлено ✅ Надішли ще фото або заверши.", reply_markup=PHOTO_KB)


@router.callback_query(F.data == "pr_photo_done", StateFilter(DiaryState.waiting_photos))
async def cb_photo_done(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    total = len(db.practice_entries(callback.from_user.id))
    await show(
        callback,
        f"Готово ✅ Усього записів у щоденнику: <b>{total}</b>.",
        kb([btn("➕ Ще запис", "pr_add")], [btn("📄 Отримати щоденник (.docx)", "pr_diary")],
           [btn("🎼 Практика", "practice_menu")]),
    )
    await callback.answer()


@router.message(StateFilter(DiaryState.waiting_photos))
async def photo_state_other(message: Message):
    await message.answer("Надішли фото 📷 (саме як фото, не файлом) або натисни «Завершити».", reply_markup=PHOTO_KB)


# =================================================================== ЩОДЕННИК: СПИСОК / РЕДАГУВАННЯ

PAGE = 8


@router.callback_query(F.data.startswith("pr_list:"))
async def cb_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await _ensure_profile(callback):
        await callback.answer()
        return
    page = int(callback.data.split(":", 1)[1])
    entries = db.practice_entries(callback.from_user.id)
    if not entries:
        await show(callback, "У щоденнику ще немає записів.",
                   kb([btn("📝 Додати запис", "pr_add")], [btn("🔙 Практика", "practice_menu")]))
        await callback.answer()
        return
    pages = (len(entries) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = []
    for e in entries[page * PAGE:(page + 1) * PAGE]:
        label = f"{docx_generator.fmt_date_short(e['entry_date'])[:8]} — {e['tasks']}"
        rows.append([btn(label[:60], f"pr_view:{e['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"pr_list:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"pr_list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 Практика", "practice_menu")])
    await show(callback, f"📋 <b>Мої записи</b> ({len(entries)}), за датою:", kb(*rows))
    await callback.answer()


@router.callback_query(F.data.startswith("pr_view:"))
async def cb_view(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    entry_id = int(callback.data.split(":", 1)[1])
    e = db.get_practice_entry(callback.from_user.id, entry_id)
    if not e:
        await callback.answer("Запис не знайдено", show_alert=True)
        return
    photos = db.practice_photos(entry_id)
    text = (
        f"📅 <b>{fmt_ua(e['entry_date'])}</b>\n\n<b>Перелік:</b> {esc(e['tasks'])}\n\n"
        f"<b>Анотація:</b>\n{esc(e['annotation'])}\n\n🖼 Фото: {len(photos)}"
    )
    rows = [
        [btn("✏️ Перелік", f"pr_edit:{entry_id}:tasks"), btn("✏️ Анотація", f"pr_edit:{entry_id}:annotation")],
        [btn("📅 Дата", f"pr_edit:{entry_id}:entry_date"), btn("📷 Додати фото", f"pr_addphoto:{entry_id}")],
    ]
    if photos:
        rows.append([btn(f"🖼 Переглянути / видалити фото ({len(photos)})", f"pr_photos:{entry_id}")])
    rows.append([btn("🗑 Видалити запис", f"pr_del:{entry_id}")])
    rows.append([btn("🔙 До списку", "pr_list:0")])
    await show(callback, text[:4000], kb(*rows))
    await callback.answer()


class EditEntryState(StatesGroup):
    waiting_value = State()


@router.callback_query(F.data.startswith("pr_edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    _, entry_id, field = callback.data.split(":")
    if field not in ("tasks", "annotation", "entry_date"):
        await callback.answer()
        return
    await state.set_state(EditEntryState.waiting_value)
    await state.update_data(entry_id=int(entry_id), field=field)
    hint = {"tasks": "новий текст поля «Перелік виконаних завдань та робот»",
            "annotation": "новий текст «Анотації»",
            "entry_date": "нову дату у форматі ДД.ММ.РР"}[field]
    await show(callback, f"Надішли {hint}:", kb([btn("❌ Скасувати", f"pr_view:{entry_id}")]))
    await callback.answer()


@router.message(StateFilter(EditEntryState.waiting_value), F.text)
async def edit_received(message: Message, state: FSMContext):
    data = await state.get_data()
    field, entry_id = data["field"], data["entry_id"]
    value = message.text.strip()
    if field == "entry_date":
        d = parse_date(value)
        if not d or d > today_kyiv():
            await message.answer("Не розпізнав дату (або вона в майбутньому). Формат: ДД.ММ.РР")
            return
        value = d.isoformat()
    db.update_practice_entry(message.from_user.id, entry_id, **{field: value})
    await state.clear()
    await message.answer("Збережено ✅", reply_markup=kb([btn("📋 До запису", f"pr_view:{entry_id}")]))


@router.message(Command("rewrite_entries"))
async def cmd_rewrite_entries(message: Message):
    """Одноразова службова команда (не в меню): переписує ВЛАСНІ збережені записи адміна
    від першої особи. Після того як старі записи оновлено, команду й цей блок можна видалити."""
    if not _admin(message):
        return
    n = len(db.practice_entries(message.from_user.id))
    if not n:
        await message.answer("У тебе ще немає записів.")
        return
    await message.answer(
        f"♻️ Переписати <b>всі твої {n} записів</b> від першої особи? Факти зберігаються, але формулювання "
        "(і ручні правки в анотаціях) будуть замінені. Це може тривати кілька хвилин.",
        reply_markup=kb([btn("♻️ Так, переписати", "pr_rwall_ok")], [btn("❌ Скасувати", "pr_cancel")]),
    )


@router.callback_query(F.data == "pr_rwall_ok")
async def cb_rewrite_all_ok(callback: CallbackQuery):
    chat_id = callback.from_user.id
    if not _admin(callback):
        await callback.answer("Недоступно", show_alert=True)
        return
    if ("rewrite", chat_id) in _busy:
        await callback.answer("Вже переписую, зачекай 🙂", show_alert=True)
        return
    entries = db.practice_entries(chat_id)
    if not entries:
        await callback.answer("Записів немає", show_alert=True)
        return
    _busy.add(("rewrite", chat_id))
    await callback.answer()
    status = await callback.message.answer(f"⏳ Переписую: 0/{len(entries)}")
    done = failed = 0
    try:
        profile, cfg = _ctx(chat_id)
        for i, e in enumerate(entries, 1):
            try:
                draft = await practice_ai.rewrite_entry(profile, cfg, e)
                db.update_practice_entry(chat_id, e["id"], tasks=draft["tasks"], annotation=draft["annotation"])
                done += 1
            except Exception as err:  # будь-яка помилка на одному записі не має зупиняти решту
                log.exception("rewrite_all: запис %s: %s", e["id"], err)
                failed += 1
            try:
                await status.edit_text(f"⏳ Переписую: {i}/{len(entries)}")
            except TelegramBadRequest:
                pass
            await asyncio.sleep(1.5)
    finally:
        _busy.discard(("rewrite", chat_id))
    text = f"Готово ✅ Переписано записів: {done} з {len(entries)}."
    if failed:
        text += f"\n⚠️ {failed} не вдалося (ШІ був недоступний) — запусти ще раз, оновляться лише ті, що лишились; "
        text += "уже переписані ШІ просто обробить повторно."
    await status.edit_text(text, reply_markup=kb([btn("📋 До списку", "pr_list:0")]))


@router.callback_query(F.data.startswith("pr_del:"))
async def cb_del(callback: CallbackQuery):
    entry_id = int(callback.data.split(":", 1)[1])
    await show(callback, "Видалити цей запис разом із фото? Це незворотно.",
               kb([btn("🗑 Так, видалити", f"pr_delok:{entry_id}")], [btn("🔙 Ні", f"pr_view:{entry_id}")]))
    await callback.answer()


@router.callback_query(F.data.startswith("pr_delok:"))
async def cb_delok(callback: CallbackQuery):
    db.delete_practice_entry(callback.from_user.id, int(callback.data.split(":", 1)[1]))
    await callback.answer("Видалено ✅")
    await show(callback, "Запис видалено.", kb([btn("📋 До списку", "pr_list:0")]))


@router.callback_query(F.data.startswith("pr_addphoto:"))
async def cb_addphoto(callback: CallbackQuery, state: FSMContext):
    entry_id = int(callback.data.split(":", 1)[1])
    if not db.get_practice_entry(callback.from_user.id, entry_id):
        await callback.answer("Запис не знайдено", show_alert=True)
        return
    await state.clear()
    await state.set_state(DiaryState.waiting_photos)
    await state.update_data(entry_id=entry_id, last_photo_id=None)
    await show(callback, "📷 Надішли фото (можна кілька поспіль).", PHOTO_KB)
    await callback.answer()


@router.callback_query(F.data.startswith("pr_photos:"))
async def cb_photos(callback: CallbackQuery):
    entry_id = int(callback.data.split(":", 1)[1])
    if not db.get_practice_entry(callback.from_user.id, entry_id):
        await callback.answer("Запис не знайдено", show_alert=True)
        return
    for ph in db.practice_photos(entry_id):
        try:
            await callback.message.answer_photo(
                ph["file_id"], caption=esc(ph["caption"]),
                reply_markup=kb(btn("🗑 Видалити фото", f"pr_phdel:{ph['id']}:{entry_id}")),
            )
        except Exception:
            log.exception("Не вдалось показати фото %s", ph["id"])
    await callback.answer()


@router.callback_query(F.data.startswith("pr_phdel:"))
async def cb_phdel(callback: CallbackQuery):
    _, photo_id, entry_id = callback.data.split(":")
    if db.get_practice_entry(callback.from_user.id, int(entry_id)):  # перевірка власника
        db.delete_practice_photo(int(photo_id))
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await callback.answer("Фото видалено")


# =================================================================== ВИДАЧА .DOCX

_busy: set = set()
_last_report: dict = {}
REPORT_COOLDOWN_SEC = 180


async def _collect_entries_with_photos(bot, chat_id: int):
    entries = db.practice_entries(chat_id)
    failed = 0
    for e in entries:
        e["photos"] = []
        for ph in db.practice_photos(e["id"]):
            try:
                buf = await bot.download(ph["file_id"])
                e["photos"].append({"bytes": buf.getvalue(), "caption": ph["caption"]})
            except Exception:
                log.exception("Не вдалось завантажити фото %s", ph["id"])
                failed += 1
    return entries, failed


@router.callback_query(F.data == "pr_diary")
async def cb_diary(callback: CallbackQuery):
    chat_id = callback.from_user.id
    if not await _ensure_profile(callback):
        await callback.answer()
        return
    if not db.practice_entries(chat_id):
        await callback.answer("У щоденнику ще немає записів", show_alert=True)
        return
    if ("diary", chat_id) in _busy:
        await callback.answer("Файл уже формується…")
        return
    _busy.add(("diary", chat_id))
    await callback.answer()
    status = await callback.message.answer("⏳ Формую щоденник…")
    try:
        profile, cfg = _ctx(chat_id)
        entries, failed = await _collect_entries_with_photos(callback.bot, chat_id)
        data = await asyncio.to_thread(docx_generator.build_diary, profile, cfg, entries)
        caption = f"📄 Щоденник практики — записів: {len(entries)}"
        if failed:
            caption += f"\n⚠️ {failed} фото не вдалося завантажити — додай їх повторно."
        await callback.message.answer_document(
            BufferedInputFile(data, filename=f"Щоденник_практики_{surname(profile)}.docx"), caption=caption
        )
        await status.delete()
    except Exception:
        log.exception("Помилка формування щоденника")
        await status.edit_text("Не вдалося сформувати файл 😔 Спробуй ще раз трохи пізніше.")
    finally:
        _busy.discard(("diary", chat_id))


# =================================================================== ЗВІТ

class ReportState(StatesGroup):
    waiting_extra = State()


@router.callback_query(F.data == "pr_report")
async def cb_report(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    if not await _ensure_profile(callback):
        await callback.answer()
        return
    entries = db.practice_entries(chat_id)
    if not entries:
        await callback.answer("Спершу додай хоча б кілька записів у щоденник", show_alert=True)
        return
    data = await state.get_data()
    gaps = practice_ai.detect_gaps(entries, data.get("extra", ""))
    text = f"📑 <b>Звіт про проходження практики</b>\n\nЗаписів у щоденнику: <b>{len(entries)}</b>."
    rows = []
    if gaps:
        text += (
            "\n\n⚠️ У записах бракує даних для: <b>" + "; ".join(gaps) + "</b>.\n"
            "Можна швидко доповнити кількома повідомленнями (колективи, документи, література) — або згенерувати "
            "з наявного: тоді проблемні місця будуть позначені жовтим <code>[ПЕРЕВІРИТИ]</code> / "
            "<code>[ДОДАТИ]</code> для фінальної перевірки."
        )
        rows.append([btn("✍️ Доповнити зараз", "pr_rep_extra")])
        rows.append([btn("▶️ Згенерувати з наявного", "pr_rep_go")])
    else:
        text += "\n\nЦе займає кілька хвилин — я генеруватиму звіт по розділах."
        rows.append([btn("▶️ Згенерувати звіт", "pr_rep_go")])
        rows.append([btn("✍️ Додати дані (документація, література)", "pr_rep_extra")])
    rows.append([btn("🔙 Практика", "practice_menu")])
    await show(callback, text, kb(*rows))
    await callback.answer()


@router.callback_query(F.data == "pr_rep_extra")
async def cb_rep_extra(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReportState.waiting_extra)
    await show(
        callback,
        "✍️ Надсилай дані повідомленнями (можна кілька):\n\n"
        "• <b>онлайн-документація</b> — які колективи/установи вивчав(ла): назва, рік створення, творче "
        "спрямування, які законодавчі й нормативні документи опрацював(ла);\n"
        "• <b>література</b> з менеджменту в мистецькій сфері та аналізу музичних творів — автор, назва, місто, "
        "видавництво, рік, сторінки;\n"
        "• <b>сайти й відео</b> (назва, URL, дата звернення), майстер-класи, вистави;\n"
        "• репертуар і твори, база практики та колектив (рік створення, структура, розклад занять), афіші/"
        "програми/запрошення, які аналізував(ла).\n\n"
        "Коли закінчиш — натисни «Генерувати». ШІ використовує тільки те, що ти надіслав(ла) тут і в щоденнику.",
        kb([btn("▶️ Генерувати звіт", "pr_rep_go")], [btn("❌ Скасувати", "pr_cancel")]),
    )
    await callback.answer()


@router.message(StateFilter(ReportState.waiting_extra), F.text)
async def rep_extra_received(message: Message, state: FSMContext):
    data = await state.get_data()
    extra = (data.get("extra", "") + "\n" + message.text.strip()).strip()[:15000]
    await state.update_data(extra=extra, extra_n=data.get("extra_n", 0) + 1)
    await message.answer(
        f"Додано ✅ (повідомлень: {data.get('extra_n', 0) + 1}). Надішли ще або натисни «Генерувати».",
        reply_markup=kb([btn("▶️ Генерувати звіт", "pr_rep_go")], [btn("❌ Скасувати", "pr_cancel")]),
    )


@router.callback_query(F.data == "pr_rep_go")
async def cb_rep_go(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    if ("report", chat_id) in _busy:
        await callback.answer("Звіт уже генерується, зачекай 🙂", show_alert=True)
        return
    wait = REPORT_COOLDOWN_SEC - (time.time() - _last_report.get(chat_id, 0))
    if wait > 0:
        await callback.answer(f"Зачекай ~{int(wait)} с перед наступною генерацією (ліміти ШІ спільні)", show_alert=True)
        return
    data = await state.get_data()
    extra = data.get("extra", "")
    await state.set_state(None)  # дані (extra) лишаємо — знадобляться для повтору і тез
    _busy.add(("report", chat_id))
    await callback.answer()
    status = await callback.message.answer("⏳ Починаю генерацію звіту… (це кілька хвилин)")
    try:
        profile, cfg = _ctx(chat_id)
        entries, failed = await _collect_entries_with_photos(callback.bot, chat_id)

        async def progress(step, total, label):
            try:
                await status.edit_text(f"⏳ Генерую звіт: крок {step}/{total}\n<i>{esc(label)}</i>")
            except TelegramBadRequest:
                pass

        sections = await practice_ai.generate_report_sections(profile, cfg, entries, extra, progress)
        await status.edit_text("📦 Збираю .docx…")
        doc = await asyncio.to_thread(docx_generator.build_report, profile, cfg, sections, entries)
        caption = (
            "📑 Звіт про проходження практики готовий.\n\n"
            "🟨 Жовтим підсвічено місця [ПЕРЕВІРИТИ]/[ДОДАТИ] — їх треба уточнити вручну.\n"
            "🔢 Коли Word запитає про оновлення полів — натисни «Так»: з'являться номери сторінок у змісті."
        )
        if failed:
            caption += f"\n⚠️ {failed} фото не вдалося завантажити."
        await callback.message.answer_document(
            BufferedInputFile(doc, filename=f"Звіт_практики_{surname(profile)}.docx"),
            caption=caption,
            reply_markup=kb([btn("🎤 Тези для захисту", "pr_theses")], [btn("🎼 Практика", "practice_menu")]),
        )
        await status.delete()
        _last_report[chat_id] = time.time()
    except ai.AIUnavailable as e:
        log.warning("report: %s", e)
        await status.edit_text(AI_DOWN_TEXT, reply_markup=kb([btn("🔁 Спробувати ще раз", "pr_rep_go")],
                                                             [btn("🎼 Практика", "practice_menu")]))
    except Exception:
        log.exception("Помилка формування звіту")
        await status.edit_text("Не вдалося сформувати звіт 😔 Спробуй ще раз трохи пізніше.",
                               reply_markup=kb([btn("🔁 Спробувати ще раз", "pr_rep_go")]))
    finally:
        _busy.discard(("report", chat_id))


@router.callback_query(F.data == "pr_theses")
async def cb_theses(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    if ("theses", chat_id) in _busy:
        await callback.answer("Вже генерується…")
        return
    entries = db.practice_entries(chat_id)
    if not entries:
        await callback.answer("Немає записів у щоденнику", show_alert=True)
        return
    _busy.add(("theses", chat_id))
    await callback.answer()
    status = await callback.message.answer("⏳ Складаю тези для захисту…")
    try:
        profile, cfg = _ctx(chat_id)
        extra = (await state.get_data()).get("extra", "")
        text = await practice_ai.generate_theses(profile, cfg, entries, extra)
        await status.delete()
        parts = chunks("🎤 <b>Тези для захисту (доповідь 2–3 хв)</b>\n\n" + esc(text))
        for part in parts:
            await callback.message.answer(part)
    except ai.AIUnavailable:
        await status.edit_text(AI_DOWN_TEXT, reply_markup=kb([btn("🔁 Спробувати ще раз", "pr_theses")]))
    except Exception:
        log.exception("Помилка тез")
        await status.edit_text("Не вдалося скласти тези 😔 Спробуй пізніше.")
    finally:
        _busy.discard(("theses", chat_id))


# =================================================================== НОВИНИ ПРАКТИКИ

NEWS_PAGE = 8


@router.callback_query(F.data.startswith("pr_news:"))
async def cb_news(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    group = _group_of(callback.from_user.id)
    if not group:
        await callback.answer("Спершу обери групу: /start", show_alert=True)
        return
    page = max(0, int(callback.data.split(":", 1)[1]))
    items, total = db.practice_news_page(group, page * NEWS_PAGE, NEWS_PAGE)
    if not items and page:
        page = 0
        items, total = db.practice_news_page(group, 0, NEWS_PAGE)
    if not items:
        await show(callback, "Поки що новин та оголошень практики немає.", kb(btn("🔙 Практика", "practice_menu")))
        await callback.answer()
        return
    rows = [[btn(n["title"][:60], f"pr_newsitem:{n['id']}:{page}")] for n in items]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"pr_news:{page - 1}"))
    if (page + 1) * NEWS_PAGE < total:
        nav.append(btn("➡️", f"pr_news:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 Практика", "practice_menu")])
    await show(callback, "📰 <b>Останні новини та оголошення практики:</b>", kb(*rows))
    await callback.answer()


async def _send_news_body(target_message: Message, news: dict, markup):
    text = f"📰 <b>{esc(news['title'])}</b>\n🗓 {news['created_at'][:10][8:10]}.{news['created_at'][5:7]}.{news['created_at'][:4]}\n\n{news['body']}"
    try:
        await target_message.answer(text[:4000], reply_markup=markup)
    except TelegramBadRequest:  # адмін ввів некоректний HTML
        await target_message.answer(esc(text)[:4000], reply_markup=markup)


async def _send_news_attachment(chat_message: Message, news: dict):
    if not news.get("file_id"):
        return
    try:
        if news["file_type"] == "photo":
            await chat_message.answer_photo(news["file_id"])
        else:
            await chat_message.answer_document(news["file_id"])
    except Exception:
        log.exception("Не вдалось надіслати вкладення новини %s", news["id"])


@router.callback_query(F.data.startswith("pr_newsitem:"))
async def cb_newsitem(callback: CallbackQuery):
    _, news_id, page = callback.data.split(":")
    news = db.get_practice_news(int(news_id))
    if not news:
        await callback.answer("Оголошення вже видалено", show_alert=True)
        return
    markup = kb(btn("⬅️ Назад до списку новин", f"pr_news:{page}"))
    await _send_news_attachment(callback.message, news)
    await _send_news_body(callback.message, news, markup)
    await callback.answer()


# =================================================================== НАГАДУВАННЯ

class RemState(StatesGroup):
    waiting_time = State()


DIARY_PRESETS = ["18:00", "20:00", "21:00"]
SAT_PRESETS = ["12:00", "14:00", "18:00"]


def rem_text(chat_id: int) -> str:
    r = db.get_practice_reminder(chat_id)
    return (
        "⏰ <b>Нагадування практики</b> (за київським часом)\n\n"
        f"📝 Щоденне — заповнити щоденник: <b>{'увімкнено, ' + r['diary_time'] if r['diary_on'] else 'вимкнено'}</b>\n"
        f"📄 Суботнє — перевірити щоденник і здати в Google Classroom: "
        f"<b>{'увімкнено, ' + r['sat_time'] if r['sat_on'] else 'вимкнено'}</b>"
    )


def rem_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    r = db.get_practice_reminder(chat_id)

    def times(kind, presets, current):
        row = [btn(("✅ " if t == current else "") + t, f"prrem_time:{kind}:{t.replace(':', '')}") for t in presets]
        row.append(btn("✏️ Свій час", f"prrem_custom:{kind}"))
        return row

    return kb(
        [btn("🔕 Вимкнути щоденне" if r["diary_on"] else "🔔 Увімкнути щоденне", "prrem_toggle:diary")],
        times("diary", DIARY_PRESETS, r["diary_time"]),
        [btn("🔕 Вимкнути суботнє" if r["sat_on"] else "🔔 Увімкнути суботнє", "prrem_toggle:sat")],
        times("sat", SAT_PRESETS, r["sat_time"]),
        [btn("🔙 Налаштування", "settings_menu"), btn("🎼 Практика", "practice_menu")],
    )


@router.callback_query(F.data == "pr_rem")
async def cb_rem(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(callback, rem_text(callback.from_user.id), rem_keyboard(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("prrem_toggle:"))
async def cb_rem_toggle(callback: CallbackQuery):
    kind = callback.data.split(":", 1)[1]
    r = db.get_practice_reminder(callback.from_user.id)
    key = f"{kind}_on"
    db.set_practice_reminder(callback.from_user.id, **{key: not r[key]})
    await show(callback, rem_text(callback.from_user.id), rem_keyboard(callback.from_user.id))
    await callback.answer("Готово ✅")


@router.callback_query(F.data.startswith("prrem_time:"))
async def cb_rem_time(callback: CallbackQuery):
    _, kind, hhmm = callback.data.split(":")
    value = f"{hhmm[:2]}:{hhmm[2:]}"
    db.set_practice_reminder(callback.from_user.id, **{f"{kind}_time": value, f"{kind}_on": True})
    await show(callback, rem_text(callback.from_user.id), rem_keyboard(callback.from_user.id))
    await callback.answer(f"Нагадування на {value} ✅")


@router.callback_query(F.data.startswith("prrem_custom:"))
async def cb_rem_custom(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    await state.set_state(RemState.waiting_time)
    await state.update_data(kind=kind)
    await show(callback, "Введи час у форматі <b>ГГ:ХХ</b> (за Києвом), напр. <code>19:30</code>:",
               kb(btn("❌ Скасувати", "pr_rem")))
    await callback.answer()


@router.message(StateFilter(RemState.waiting_time), F.text)
async def rem_time_received(message: Message, state: FSMContext):
    m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", message.text.strip())
    if not m:
        await message.answer("Формат: <b>ГГ:ХХ</b>, напр. <code>19:30</code>.")
        return
    kind = (await state.get_data())["kind"]
    value = f"{int(m.group(1)):02d}:{m.group(2)}"
    db.set_practice_reminder(message.from_user.id, **{f"{kind}_time": value, f"{kind}_on": True})
    await state.clear()
    await message.answer(rem_text(message.from_user.id), reply_markup=rem_keyboard(message.from_user.id))


async def check_practice_reminders(bot, now: datetime) -> int:
    """Викликається з cron (раз на ~5 хв). Вікно 45 хв — щоб запізнення GitHub Actions
    не «з'їдали» нагадування; ключ у sent_reminders гарантує одне повідомлення на день."""
    sent = 0
    today_iso = now.date().isoformat()
    for kind in ("diary", "sat"):
        if kind == "sat" and now.weekday() != 5:
            continue
        for u in db.practice_reminder_users(kind):
            try:
                hh, mm = (int(x) for x in u["time"].split(":"))
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            except Exception:
                continue
            delta_min = (now - target).total_seconds() / 60
            key = f"practice_{kind}"
            if not (0 <= delta_min <= 45) or db.was_reminder_sent(u["chat_id"], key, today_iso):
                continue
            try:
                if kind == "diary":
                    await bot.send_message(
                        u["chat_id"], "📝 Час заповнити щоденник практики! Додай запис за сьогодні.",
                        reply_markup=kb(btn("📝 Додати запис зараз", "pr_add")),
                    )
                else:
                    await bot.send_message(
                        u["chat_id"],
                        "Сьогодні субота! Не забудь перевірити свій щоденник практики та надіслати його в Google Classroom.",
                        reply_markup=kb(btn("📄 Згенерувати щоденник (.docx)", "pr_diary")),
                    )
                sent += 1
            except Exception:
                log.exception("Не вдалось надіслати нагадування практики %s", u["chat_id"])
            finally:
                db.mark_reminder_sent(u["chat_id"], key, today_iso)
    return sent


# =================================================================== АДМІН: ПРАКТИКА

def _admin(callback_or_message) -> bool:
    return _has_permission(callback_or_message.from_user.id, "practice")


async def _deny(callback: CallbackQuery):
    await callback.answer("Потрібне право «практика» 🔒", show_alert=True)


BACK_ADMIN = btn("🔙 Панель керування", "admin_panel")


class NewsState(StatesGroup):
    waiting_title = State()
    waiting_body = State()
    waiting_attach = State()


def _group_choice_rows(prefix: str, with_all: bool = False):
    rows = [[btn(g, f"{prefix}:{g}")] for g in GROUPS]
    if with_all and len(GROUPS) > 1:
        rows.append([btn("🌐 Усі групи", f"{prefix}:*")])
    return rows


@router.callback_query(F.data == "pra_announce")
async def cb_pra_announce(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    await state.clear()
    if len(GROUPS) > 1:
        await show(callback, "Для якої групи оголошення?", kb(*_group_choice_rows("pra_grp", True), [CANCEL_ADMIN()]))
    else:
        await state.update_data(group=GROUPS[0] if GROUPS else "*")
        await _ask_news_title(callback, state)
    await callback.answer()


def CANCEL_ADMIN():
    return btn("❌ Скасувати", "admin_panel")


@router.callback_query(F.data.startswith("pra_grp:"))
async def cb_pra_grp(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    await state.update_data(group=callback.data.split(":", 1)[1])
    await _ask_news_title(callback, state)
    await callback.answer()


async def _ask_news_title(target, state: FSMContext):
    await state.set_state(NewsState.waiting_title)
    await show(target, "📢 <b>Крок 1/3.</b> Введи <b>заголовок</b> оголошення (він буде на кнопці в «Новинах практики», "
                       "бажано до 50–60 символів):", kb(CANCEL_ADMIN()))


@router.message(StateFilter(NewsState.waiting_title), F.text)
async def news_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if len(title) > 60:
        await message.answer(f"Заголовок {len(title)} символів — скороти до 60 (так він помістится на кнопці).")
        return
    await state.update_data(title=title)
    await state.set_state(NewsState.waiting_body)
    await message.answer("<b>Крок 2/3.</b> Введи <b>основний текст</b> (підтримується HTML: &lt;b&gt;, &lt;i&gt;, "
                         "&lt;a href=\"...\"&gt;, а також звичайне форматування Telegram):", reply_markup=kb(CANCEL_ADMIN()))


@router.message(StateFilter(NewsState.waiting_body), F.text)
async def news_body(message: Message, state: FSMContext):
    body = message.html_text if message.entities else message.text
    await state.update_data(body=body)
    await state.set_state(NewsState.waiting_attach)
    await message.answer("<b>Крок 3/3.</b> Прикріпи фото або документ (або натисни «Без вкладень»):",
                         reply_markup=kb([btn("Без вкладень", "pra_noattach")], [CANCEL_ADMIN()]))


@router.message(StateFilter(NewsState.waiting_attach), F.photo)
async def news_photo(message: Message, state: FSMContext):
    await state.update_data(file_id=message.photo[-1].file_id, file_type="photo")
    await _news_preview(message, state)


@router.message(StateFilter(NewsState.waiting_attach), F.document)
async def news_doc(message: Message, state: FSMContext):
    await state.update_data(file_id=message.document.file_id, file_type="document")
    await _news_preview(message, state)


@router.callback_query(F.data == "pra_noattach", StateFilter(NewsState.waiting_attach))
async def cb_noattach(callback: CallbackQuery, state: FSMContext):
    await state.update_data(file_id=None, file_type=None)
    await callback.answer()
    await _news_preview(callback, state)


async def _news_preview(target, state: FSMContext):
    data = await state.get_data()
    m = msg_of(target)
    fake = {"id": 0, "title": data["title"], "body": data["body"], "file_id": data.get("file_id"),
            "file_type": data.get("file_type"), "created_at": today_kyiv().isoformat()}
    await m.answer("👀 <b>Прев'ю:</b>")
    await _send_news_attachment(m, fake)
    try:
        await _send_news_body(m, fake, None)
    except Exception:
        await m.answer("Не вдалось показати прев'ю — перевір HTML-розмітку. Почни спочатку.")
        await state.clear()
        return
    await m.answer(
        "Що робимо?",
        reply_markup=kb([btn("✅ Опублікувати і розіслати всім", "pra_pub:send")],
                        [btn("💾 Лише зберегти в «Новини практики»", "pra_pub:save")],
                        [CANCEL_ADMIN()]),
    )


@router.callback_query(F.data.startswith("pra_pub:"))
async def cb_pub(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    data = await state.get_data()
    if not data.get("title"):
        await callback.answer("Чернетка застаріла — почни спочатку", show_alert=True)
        return
    news_id = db.add_practice_news(data["group"], data["title"], data["body"], callback.from_user.id,
                                   data.get("file_id"), data.get("file_type"))
    await state.clear()
    mode = callback.data.split(":", 1)[1]
    result = "💾 Збережено в «Новини практики»."
    if mode == "send":
        chat_ids = db.all_registered_chat_ids() if data["group"] == "*" else db.users_in_group(data["group"])
        bot = callback.bot
        delivered = 0

        async def send(chat_id):
            nonlocal delivered
            try:
                await bot.send_message(
                    chat_id, f"📰 <b>Нове оголошення практики:</b>\n{esc(data['title'])}",
                    reply_markup=kb(btn("📖 Відкрити", f"pr_newsitem:{news_id}:0")),
                )
                delivered += 1
            except Exception:
                log.exception("Розсилка новини: не вдалось %s", chat_id)

        await asyncio.gather(*(send(c) for c in chat_ids))
        result = f"✅ Опубліковано і розіслано: {delivered} з {len(chat_ids)}."
    await callback.message.answer(result, reply_markup=kb([BACK_ADMIN]))
    await callback.answer()


async def _render_manage(callback: CallbackQuery):
    items = db.all_practice_news(30)
    rows = [[btn(f"🗑 {n['title']}"[:60], f"pra_del:{n['id']}")] for n in items]
    rows.append([BACK_ADMIN])
    await show(callback, "🗂 <b>Оголошення практики</b>\nНатисни, щоб видалити:" if items else "Оголошень ще немає.", kb(*rows))


@router.callback_query(F.data == "pra_manage")
async def cb_pra_manage(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    await state.clear()
    await _render_manage(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("pra_del:"))
async def cb_pra_del(callback: CallbackQuery):
    if not _admin(callback):
        return await _deny(callback)
    db.delete_practice_news(int(callback.data.split(":", 1)[1]))
    await _render_manage(callback)
    await callback.answer("Видалено ✅")


@router.callback_query(F.data == "pra_students")
async def cb_pra_students(callback: CallbackQuery):
    if not _admin(callback):
        return await _deny(callback)
    parts = []
    for group in GROUPS:
        students = db.practice_students(group)
        parts.append(f"<b>{esc(group)}</b> — зареєстровано: {len(students)}")
        for i, s in enumerate(students, 1):
            name = esc(s["full_name"]) if s["full_name"] else "<i>без профілю</i>"
            parts.append(f"{i}. {name} — записів: {s['entries']}")
        parts.append("")
    text = "👥 <b>Студенти</b>\n\n" + "\n".join(parts)
    pieces = chunks(text)
    await show(callback, pieces[0], kb([BACK_ADMIN]) if len(pieces) == 1 else None)
    for i, part in enumerate(pieces[1:], 2):
        await callback.message.answer(part, reply_markup=kb([BACK_ADMIN]) if i == len(pieces) else None)
    await callback.answer()


class CfgState(StatesGroup):
    waiting_value = State()


def _cfg_text_and_kb(group: str):
    cfg = db.get_practice_cfg(group)
    lines = [f"⚙️ <b>Налаштування практики — {esc(group)}</b>\n"]
    rows = []
    for field, label in db.PRACTICE_CFG_LABELS.items():
        lines.append(f"<b>{esc(label)}:</b> {esc(cfg[field]) or '<i>порожньо</i>'}")
        rows.append([btn(f"✏️ {label.split(' (')[0]}", f"pra_cfg:{group}:{field}")])
    rows.append([BACK_ADMIN])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "pra_settings")
async def cb_pra_settings(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    await state.clear()
    if len(GROUPS) > 1:
        await show(callback, "Для якої групи налаштування?", kb(*_group_choice_rows("pra_setgrp"), [BACK_ADMIN]))
    elif GROUPS:
        text, markup = _cfg_text_and_kb(GROUPS[0])
        await show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pra_setgrp:"))
async def cb_pra_setgrp(callback: CallbackQuery):
    if not _admin(callback):
        return await _deny(callback)
    text, markup = _cfg_text_and_kb(callback.data.split(":", 1)[1])
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pra_cfg:"))
async def cb_pra_cfg(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback):
        return await _deny(callback)
    _, group, field = callback.data.split(":", 2)
    if field not in db.PRACTICE_CFG_LABELS:
        await callback.answer()
        return
    await state.set_state(CfgState.waiting_value)
    await state.update_data(group=group, field=field)
    await show(callback, f"Нове значення для «{esc(db.PRACTICE_CFG_LABELS[field])}»\n"
                         "(надішли «-», щоб скинути до стандартного):",
               kb(btn("❌ Скасувати", f"pra_setgrp:{group}")))
    await callback.answer()


@router.message(StateFilter(CfgState.waiting_value), F.text)
async def cfg_value(message: Message, state: FSMContext):
    if not _admin(message):
        await state.clear()
        return
    data = await state.get_data()
    value = message.text.strip()
    db.set_practice_cfg(data["group"], data["field"], "" if value == "-" else value[:300])
    await state.clear()
    text, markup = _cfg_text_and_kb(data["group"])
    await message.answer("Збережено ✅\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "pra_ping")
async def cb_pra_ping(callback: CallbackQuery):
    if not _admin(callback):
        return await _deny(callback)
    n = len(db.users_without_profile())
    await show(
        callback,
        f"Надіслати нагадування «Заповни профіль» користувачам без профілю? Зараз таких: <b>{n}</b>.",
        kb([btn("📣 Надіслати", "pra_ping_ok")] if n else [], [BACK_ADMIN]),
    )
    await callback.answer()


@router.callback_query(F.data == "pra_ping_ok")
async def cb_pra_ping_ok(callback: CallbackQuery):
    if not _admin(callback):
        return await _deny(callback)
    chat_ids = db.users_without_profile()
    bot, delivered = callback.bot, 0

    async def send(chat_id):
        nonlocal delivered
        try:
            await bot.send_message(
                chat_id,
                "👋 У боті з'явився розділ <b>🎼 Практика</b> (щоденник і звіт з ШІ). Будь ласка, заповни або "
                "онови свій профіль — це хвилина:",
                reply_markup=kb(btn("👤 Мій профіль", "profile_menu")),
            )
            delivered += 1
        except Exception:
            log.exception("Нагадування про профіль: не вдалось %s", chat_id)

    await asyncio.gather(*(send(c) for c in chat_ids))
    await show(callback, f"✅ Надіслано: {delivered} з {len(chat_ids)}.", kb([BACK_ADMIN]))
    await callback.answer()


# =================================================================== запобіжник

@router.message(StateFilter(
    ProfileState.waiting_name, ProfileState.waiting_gen_manual, ProfileState.waiting_spec,
    ProfileState.waiting_base, ProfileState.waiting_collective, ProfileState.waiting_supervisor,
    ProfileState.waiting_value, DiaryState.waiting_date, DiaryState.waiting_desc,
    DiaryState.waiting_answers, DiaryState.waiting_extra, EditEntryState.waiting_value,
    ReportState.waiting_extra, RemState.waiting_time, CfgState.waiting_value,
    NewsState.waiting_title, NewsState.waiting_body,
))
async def non_text_fallback(message: Message):
    await message.answer("Тут потрібен текст ✍️ (або натисни ❌ Скасувати у попередньому повідомленні).")
