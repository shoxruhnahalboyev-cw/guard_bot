import sqlite3
from datetime import datetime

DB_NAME = "bot.db"


def connect():
    return sqlite3.connect(DB_NAME)


def create_tables():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,   -- Telegram user ID
                first_name  TEXT,
                last_name   TEXT,
                username    TEXT,
                phone       TEXT,
                bio         TEXT,
                joined_at   TEXT
            )
        """)


def add_user(user_id, first_name, last_name, username, bio):
    with connect() as db:
        # Yangi bo'lsa qo'shadi, oldin bor bo'lsa ma'lumotlarini yangilaydi
        db.execute("""
            INSERT INTO users (id, first_name, last_name, username, bio, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                username   = excluded.username,
                bio        = excluded.bio
        """, (user_id, first_name, last_name, username, bio,
              datetime.now().strftime("%Y-%m-%d %H:%M")))


def has_phone(user_id):
    with connect() as db:
        row = db.execute("SELECT phone FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and row[0] is not None


def set_phone(user_id, phone):
    with connect() as db:
        db.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))


def count_users():
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def count_with_phone():
    with connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM users WHERE phone IS NOT NULL"
        ).fetchone()[0]


def count_today():
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (today + "%",)
        ).fetchone()[0]


def last_users(limit=20):
    with connect() as db:
        return db.execute(
            "SELECT id, first_name, last_name, username, phone, joined_at "
            "FROM users ORDER BY joined_at DESC LIMIT ?", (limit,)
        ).fetchall()


def all_users():
    with connect() as db:
        return db.execute(
            "SELECT id, first_name, last_name, username, phone, bio, joined_at FROM users"
        ).fetchall()