"""
db.py — сховище бота: групи користувачів, нотатки, лог надісланих нагадувань.

Працює через пакет `libsql`, який має практично той самий API, що й вбудований
sqlite3, але вміє підключатись і до звичайного файлу (для локальної розробки),
і до віддаленої бази Turso (для продакшену на Render, де локальний файл
стирається при кожному сні/рестарті сервісу).

Змінні середовища:
    TURSO_DATABASE_URL — напр. libsql://schedule-bot-you.turso.io (продакшн)
    TURSO_AUTH_TOKEN   — токен доступу до цієї бази
    DB_PATH            — локальний файл, якщо TURSO_DATABASE_URL не задано
                          (за замовчуванням "bot.db"; підходить лише для
                          локального тестування — на Render цей файл не
                          зберігається між рестартами)
"""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import libsql

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
LOCAL_DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Кожне нове підключення до Turso — це мережевий запит. До оптимізації одна
# дія в Telegram могла створити 5–10 таких підключень, що особливо боляче на
# безкоштовному Render. Один процес бота обробляє ці синхронні операції
# послідовно, тож з'єднання можна безпечно перевикористовувати.
_REMOTE_CONNECTION = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    reminder_minutes INTEGER NOT NULL DEFAULT 15,
    reminders_on INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    lesson_key TEXT NOT NULL,
    lesson_label TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    chat_id INTEGER NOT NULL,
    lesson_key TEXT NOT NULL,
    sent_date TEXT NOT NULL,
    PRIMARY KEY (chat_id, lesson_key, sent_date)
);

CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    base_pair TEXT,
    base_time TEXT,
    pair TEXT,
    time TEXT,
    subject TEXT,
    teacher TEXT,
    room TEXT,
    note TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS individual_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    weekday TEXT,
    date TEXT,
    time TEXT NOT NULL,
    subject TEXT NOT NULL,
    teacher TEXT,
    room TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff_permissions (
    chat_id INTEGER PRIMARY KEY,
    permissions TEXT NOT NULL,
    granted_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


KYIV_TZ = ZoneInfo("Europe/Kyiv")


def _today_kyiv() -> date:
    return datetime.now(KYIV_TZ).date()


class _SharedRemoteConnection:
    """Обгортка: старий код може викликати close(), але спільний канал Turso
    лишається відкритим для наступного запиту."""

    def __init__(self, connection):
        self._connection = connection

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _connect():
    global _REMOTE_CONNECTION
    if TURSO_URL:
        if _REMOTE_CONNECTION is None:
            _REMOTE_CONNECTION = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        return _SharedRemoteConnection(_REMOTE_CONNECTION)
    return libsql.connect(LOCAL_DB_PATH)


def init_db():
    conn = _connect()
    for statement in SCHEMA.strip().split(";\n\n"):
        if statement.strip():
            conn.execute(statement)
    # Безпечна міграція для вже створених баз.
    for column in (
        "tomorrow_on INTEGER NOT NULL DEFAULT 0",
        "announcements_on INTEGER NOT NULL DEFAULT 1",
    ):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column}")
        except Exception:
            pass  # Стовпець уже існує.
    try:
        conn.execute("ALTER TABLE individual_lessons ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    except Exception:
        pass  # Стовпець уже існує.
    conn.commit()
    conn.close()


def set_group(chat_id: int, group_name: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO users (chat_id, group_name) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET group_name = excluded.group_name",
        (chat_id, group_name),
    )
    conn.commit()
    conn.close()


def get_user(chat_id: int):
    conn = _connect()
    row = conn.execute(
        "SELECT chat_id, group_name, reminder_minutes, reminders_on, tomorrow_on, announcements_on "
        "FROM users WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "chat_id": row[0], "group": row[1], "reminder_minutes": row[2], "reminders_on": bool(row[3]),
        "tomorrow_on": bool(row[4]), "announcements_on": bool(row[5]),
    }


def all_users():
    conn = _connect()
    rows = conn.execute(
        "SELECT chat_id, group_name, reminder_minutes, reminders_on FROM users WHERE reminders_on = 1"
    ).fetchall()
    conn.close()
    return [{"chat_id": r[0], "group": r[1], "reminder_minutes": r[2], "reminders_on": bool(r[3])} for r in rows]


def set_reminder_minutes(chat_id: int, minutes: int):
    conn = _connect()
    conn.execute("UPDATE users SET reminder_minutes = ? WHERE chat_id = ?", (minutes, chat_id))
    conn.commit()
    conn.close()


def toggle_reminders(chat_id: int, on: bool):
    conn = _connect()
    conn.execute("UPDATE users SET reminders_on = ? WHERE chat_id = ?", (1 if on else 0, chat_id))
    conn.commit()
    conn.close()


def toggle_tomorrow(chat_id: int, on: bool):
    conn = _connect()
    conn.execute("UPDATE users SET tomorrow_on = ? WHERE chat_id = ?", (1 if on else 0, chat_id))
    conn.commit()
    conn.close()


def toggle_announcements(chat_id: int, on: bool):
    conn = _connect()
    conn.execute("UPDATE users SET announcements_on = ? WHERE chat_id = ?", (1 if on else 0, chat_id))
    conn.commit()
    conn.close()


def add_note(chat_id: int, lesson_key: str, lesson_label: str, text: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO notes (chat_id, lesson_key, lesson_label, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, lesson_key, lesson_label, text, _today_kyiv().isoformat()),
    )
    conn.commit()
    conn.close()


def get_notes(chat_id: int, lesson_key: str):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, text, created_at FROM notes WHERE chat_id = ? AND lesson_key = ? ORDER BY id",
        (chat_id, lesson_key),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "text": r[1], "created_at": r[2]} for r in rows]


def notes_for_lessons(chat_id: int, lesson_keys: list[str]):
    """Нотатки для кількох пар одним запитом.

    Turso — віддалена БД, тому один спільний запит суттєво швидший за
    окреме з'єднання для кожної пари в розкладі.
    """
    if not lesson_keys:
        return {}
    # Ключі формуються самим ботом; прибираємо дублікати, зберігаючи порядок.
    keys = list(dict.fromkeys(lesson_keys))
    placeholders = ", ".join("?" for _ in keys)
    conn = _connect()
    rows = conn.execute(
        f"SELECT id, lesson_key, text, created_at FROM notes "
        f"WHERE chat_id = ? AND lesson_key IN ({placeholders}) ORDER BY id",
        [chat_id, *keys],
    ).fetchall()
    conn.close()
    result = {key: [] for key in keys}
    for note_id, key, text, created_at in rows:
        result[key].append({"id": note_id, "text": text, "created_at": created_at})
    return result


def all_notes(chat_id: int):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, lesson_label, text, created_at FROM notes WHERE chat_id = ? ORDER BY created_at DESC, id DESC",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "lesson_label": r[1], "text": r[2], "created_at": r[3]} for r in rows]


def get_note(chat_id: int, note_id: int):
    conn = _connect()
    row = conn.execute(
        "SELECT id, text, created_at FROM notes WHERE chat_id = ? AND id = ?",
        (chat_id, note_id),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "text": row[1], "created_at": row[2]}


def update_note(chat_id: int, note_id: int, text: str):
    conn = _connect()
    conn.execute(
        "UPDATE notes SET text = ? WHERE chat_id = ? AND id = ?",
        (text, chat_id, note_id),
    )
    conn.commit()
    conn.close()


def delete_note(chat_id: int, note_id: int):
    conn = _connect()
    conn.execute("DELETE FROM notes WHERE chat_id = ? AND id = ?", (chat_id, note_id))
    conn.commit()
    conn.close()


def was_reminder_sent(chat_id: int, lesson_key: str, day_iso: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM sent_reminders WHERE chat_id = ? AND lesson_key = ? AND sent_date = ?",
        (chat_id, lesson_key, day_iso),
    ).fetchone()
    conn.close()
    return row is not None


def mark_reminder_sent(chat_id: int, lesson_key: str, day_iso: str):
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO sent_reminders (chat_id, lesson_key, sent_date) VALUES (?, ?, ?)",
        (chat_id, lesson_key, day_iso),
    )
    conn.commit()
    conn.close()


def users_in_group(group_name: str):
    """Усі користувачі групи, незалежно від того, увімкнені в них нагадування
    чи ні — використовується для розсилки повідомлень про зміни в розкладі."""
    conn = _connect()
    rows = conn.execute("SELECT chat_id FROM users WHERE group_name = ?", (group_name,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def announcement_users_in_group(group_name: str):
    conn = _connect()
    rows = conn.execute(
        "SELECT chat_id FROM users WHERE group_name = ? AND announcements_on = 1", (group_name,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def tomorrow_users():
    conn = _connect()
    rows = conn.execute("SELECT chat_id, group_name FROM users WHERE tomorrow_on = 1").fetchall()
    conn.close()
    return [{"chat_id": r[0], "group": r[1]} for r in rows]


# -------------------------------------------------------------- права / матеріали


def set_staff_permissions(chat_id: int, permissions: set[str], granted_by: int):
    conn = _connect()
    conn.execute(
        "INSERT INTO staff_permissions (chat_id, permissions, granted_by, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET permissions = excluded.permissions, granted_by = excluded.granted_by, "
        "created_at = excluded.created_at",
        (chat_id, ",".join(sorted(permissions)), granted_by, _today_kyiv().isoformat()),
    )
    conn.commit()
    conn.close()


def staff_permissions(chat_id: int) -> set[str]:
    conn = _connect()
    row = conn.execute("SELECT permissions FROM staff_permissions WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return set(filter(None, row[0].split(","))) if row else set()


def all_staff():
    conn = _connect()
    rows = conn.execute("SELECT chat_id, permissions FROM staff_permissions ORDER BY chat_id").fetchall()
    conn.close()
    return [{"chat_id": r[0], "permissions": set(filter(None, r[1].split(",")))} for r in rows]


def remove_staff(chat_id: int):
    conn = _connect()
    conn.execute("DELETE FROM staff_permissions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def add_material(group_name: str, subject: str, title: str, url: str, created_by: int):
    conn = _connect()
    conn.execute(
        "INSERT INTO materials (group_name, subject, title, url, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (group_name, subject, title, url, created_by, _today_kyiv().isoformat()),
    )
    conn.commit()
    conn.close()


def materials_for_group(group_name: str, query: str | None = None):
    conn = _connect()
    if query:
        rows = conn.execute(
            "SELECT id, subject, title, url FROM materials WHERE group_name = ? AND subject LIKE ? ORDER BY id DESC",
            (group_name, f"%{query}%"),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, subject, title, url FROM materials WHERE group_name = ? ORDER BY subject, id DESC", (group_name,)
        ).fetchall()
    conn.close()
    return [{"id": r[0], "subject": r[1], "title": r[2], "url": r[3]} for r in rows]


def delete_material(material_id: int):
    conn = _connect()
    conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))
    conn.commit()
    conn.close()


# ------------------------------------------------------------- overrides


def add_override(
    group_name: str,
    date_str: str,
    kind: str,
    created_by: int,
    base_pair: str | None = None,
    base_time: str | None = None,
    pair: str | None = None,
    time: str | None = None,
    subject: str | None = None,
    teacher: str | None = None,
    room: str | None = None,
    note: str | None = None,
):
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO overrides "
        "(group_name, date, kind, base_pair, base_time, pair, time, subject, teacher, room, note, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_name,
            date_str,
            kind,
            base_pair,
            base_time,
            pair,
            time,
            subject,
            teacher,
            room,
            note,
            created_by,
            _today_kyiv().isoformat(),
        ),
    )
    conn.commit()
    override_id = cur.lastrowid
    conn.close()
    return override_id


def overrides_for(group_name: str, date_str: str):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, kind, base_pair, base_time, pair, time, subject, teacher, room, note "
        "FROM overrides WHERE group_name = ? AND date = ?",
        (group_name, date_str),
    ).fetchall()
    conn.close()
    cols = ["id", "kind", "base_pair", "base_time", "pair", "time", "subject", "teacher", "room", "note"]
    return [dict(zip(cols, r)) for r in rows]


def delete_override(override_id: int):
    conn = _connect()
    conn.execute("DELETE FROM overrides WHERE id = ?", (override_id,))
    conn.commit()
    conn.close()


# ------------------------------------------------------- індивідуальні заняття


def add_individual_lesson(
    chat_id: int,
    subject: str,
    time: str,
    weekday: str | None = None,
    date: str | None = None,
    teacher: str | None = None,
    room: str | None = None,
    note: str | None = None,
    source: str = "manual",
):
    conn = _connect()
    conn.execute(
        "INSERT INTO individual_lessons "
        "(chat_id, weekday, date, time, subject, teacher, room, note, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, weekday, date, time, subject, teacher, room, note, source, _today_kyiv().isoformat()),
    )
    conn.commit()
    conn.close()


def individual_lessons_for(chat_id: int, weekday: str, date_str: str):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, weekday, date, time, subject, teacher, room, note, source "
        "FROM individual_lessons WHERE chat_id = ? AND (weekday = ? OR date = ?)",
        (chat_id, weekday, date_str),
    ).fetchall()
    conn.close()
    cols = ["id", "weekday", "date", "time", "subject", "teacher", "room", "note", "source"]
    return [dict(zip(cols, r)) for r in rows]


def all_individual_lessons(chat_id: int):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, weekday, date, time, subject, teacher, room, note, source "
        "FROM individual_lessons WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    cols = ["id", "weekday", "date", "time", "subject", "teacher", "room", "note", "source"]
    return [dict(zip(cols, r)) for r in rows]


def delete_individual_lesson(chat_id: int, lesson_id: int):
    conn = _connect()
    conn.execute("DELETE FROM individual_lessons WHERE chat_id = ? AND id = ?", (chat_id, lesson_id))
    conn.commit()
    conn.close()


def delete_all_individual_lessons(chat_id: int):
    """Видаляє геть усі індивідуальні заняття користувача — і ручні, і
    імпортовані з Excel. Використовується кнопкою "Видалити всі", зокрема
    перед повторним імпортом оновленого файлу."""
    conn = _connect()
    conn.execute("DELETE FROM individual_lessons WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def has_imported_individual_lessons(chat_id: int) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM individual_lessons WHERE chat_id = ? AND source = 'import' LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row is not None


def replace_imported_individual_lessons(chat_id: int, lessons: list[dict]):
    """Оновлює лише заняття, створені імпортом Excel. Ручні записи лишаються."""
    conn = _connect()
    conn.execute("DELETE FROM individual_lessons WHERE chat_id = ? AND source = 'import'", (chat_id,))
    for lesson in lessons:
        conn.execute(
            "INSERT INTO individual_lessons "
            "(chat_id, weekday, date, time, subject, teacher, room, note, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'import', ?)",
            (
                chat_id, lesson.get("weekday"), lesson.get("date"), lesson["time"], lesson["subject"],
                lesson.get("teacher"), lesson.get("room"), lesson.get("note"), _today_kyiv().isoformat(),
            ),
        )
    conn.commit()
    conn.close()
