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
from datetime import date

import libsql

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
LOCAL_DB_PATH = os.environ.get("DB_PATH", "bot.db")

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
"""


def _connect():
    if TURSO_URL:
        return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    return libsql.connect(LOCAL_DB_PATH)


def init_db():
    conn = _connect()
    for statement in SCHEMA.strip().split(";\n\n"):
        if statement.strip():
            conn.execute(statement)
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
        "SELECT chat_id, group_name, reminder_minutes, reminders_on FROM users WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"chat_id": row[0], "group": row[1], "reminder_minutes": row[2], "reminders_on": bool(row[3])}


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


def add_note(chat_id: int, lesson_key: str, lesson_label: str, text: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO notes (chat_id, lesson_key, lesson_label, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, lesson_key, lesson_label, text, date.today().isoformat()),
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


def all_notes(chat_id: int):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, lesson_label, text, created_at FROM notes WHERE chat_id = ? ORDER BY created_at DESC, id DESC",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "lesson_label": r[1], "text": r[2], "created_at": r[3]} for r in rows]


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
