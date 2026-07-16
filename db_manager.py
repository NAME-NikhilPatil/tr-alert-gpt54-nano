"""SQLite duplicate tracker for live Telegram announcement polling."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from models import Announcement

DB_PATH = Path("seen_announcements.db")


@contextmanager
def _db_connection(db_path: Path) -> Any:
    """Open a SQLite connection and always close it on Windows."""

    connection = sqlite3.connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_seen_db(db_path: Path = DB_PATH) -> None:
    """Create the live tracker database tables if they do not exist."""

    with _db_connection(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                company_name TEXT,
                announcement_id TEXT UNIQUE NOT NULL,
                pdf_url TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed INTEGER DEFAULT 0
            )
            """
        )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_pdfs_pdf_url ON seen_pdfs(pdf_url)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def seed_telegram_subscribers(chat_ids: str | list[str], db_path: Path = DB_PATH) -> int:
    """Add initial subscriber chat IDs from environment configuration."""

    init_seen_db(db_path)
    if isinstance(chat_ids, str):
        parsed = [part.strip() for part in chat_ids.split(",") if part.strip()]
    else:
        parsed = [str(chat_id).strip() for chat_id in chat_ids if str(chat_id).strip()]
    added = 0
    for chat_id in parsed:
        if upsert_telegram_subscriber(chat_id, db_path=db_path):
            added += 1
    return added


def upsert_telegram_subscriber(
    chat_id: str,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
    db_path: Path = DB_PATH,
) -> bool:
    """Create or reactivate one Telegram subscriber. Return True when newly active."""

    init_seen_db(db_path)
    chat_id = str(chat_id).strip()
    if not chat_id:
        return False
    with _db_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT active FROM telegram_subscribers WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO telegram_subscribers (chat_id, username, first_name, last_name, active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                active = 1,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (chat_id, username, first_name, last_name),
        )
    return existing is None or int(existing[0] or 0) == 0


def deactivate_telegram_subscriber(chat_id: str, db_path: Path = DB_PATH) -> None:
    """Deactivate one Telegram subscriber after a /stop command."""

    init_seen_db(db_path)
    with _db_connection(db_path) as connection:
        connection.execute(
            "UPDATE telegram_subscribers SET active = 0, last_seen_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
            (str(chat_id).strip(),),
        )


def get_active_telegram_chat_ids(db_path: Path = DB_PATH) -> list[str]:
    """Return all active Telegram subscriber chat IDs."""

    init_seen_db(db_path)
    with _db_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT chat_id FROM telegram_subscribers WHERE active = 1 ORDER BY subscribed_at, id",
        ).fetchall()
    return [str(row[0]) for row in rows]


def get_telegram_state(key: str, default: str = "", db_path: Path = DB_PATH) -> str:
    """Read a small Telegram polling state value from SQLite."""

    init_seen_db(db_path)
    with _db_connection(db_path) as connection:
        row = connection.execute("SELECT value FROM telegram_state WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_telegram_state(key: str, value: str, db_path: Path = DB_PATH) -> None:
    """Persist a small Telegram polling state value to SQLite."""

    init_seen_db(db_path)
    with _db_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO telegram_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def announcement_id(announcement: Announcement) -> str:
    """Build a stable announcement id for duplicate prevention."""

    parts = [
        announcement.source.upper(),
        announcement.identifier.strip(),
        announcement.company_name.strip().lower(),
        announcement.announcement_datetime.strip(),
        announcement.pdf_url.strip(),
    ]
    return "|".join(parts)


def is_seen(announcement: Announcement, db_path: Path = DB_PATH) -> bool:
    """Return whether an announcement id or PDF URL has already been seen."""

    init_seen_db(db_path)
    item_id = announcement_id(announcement)
    pdf_url = str(announcement.pdf_url or "").strip()
    with _db_connection(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM seen_pdfs WHERE announcement_id = ? OR (? <> '' AND pdf_url = ?) LIMIT 1",
            (item_id, pdf_url, pdf_url),
        ).fetchone()
    return row is not None


def reserve_seen(announcement: Announcement, db_path: Path = DB_PATH) -> str:
    """Record a new announcement before downloading its PDF."""

    init_seen_db(db_path)
    item_id = announcement_id(announcement)
    pdf_url = str(announcement.pdf_url or "").strip()
    with _db_connection(db_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO seen_pdfs
            (source, company_name, announcement_id, pdf_url, processed)
            VALUES (?, ?, ?, ?, 0)
            """,
            (announcement.source.upper(), announcement.company_name, item_id, pdf_url or None),
        )
    return item_id


def mark_processed(announcement: Announcement, db_path: Path = DB_PATH) -> None:
    """Mark an announcement as fully processed after Telegram delivery succeeds."""

    init_seen_db(db_path)
    item_id = announcement_id(announcement)
    pdf_url = str(announcement.pdf_url or "").strip()
    with _db_connection(db_path) as connection:
        connection.execute(
            "UPDATE seen_pdfs SET processed = 1 WHERE announcement_id = ? OR (? <> '' AND pdf_url = ?)",
            (item_id, pdf_url, pdf_url),
        )
