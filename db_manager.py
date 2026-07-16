"""SQLite duplicate tracker for live Telegram announcement polling."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from models import Announcement

DB_PATH = Path(os.environ.get("TR_ALERT_DB_PATH", "").strip() or "seen_announcements.db")
_INITIALIZED_DB_PATHS: set[str] = set()
_DB_INIT_LOCK = threading.RLock()
_DB_OPERATION_LOCK = threading.RLock()


def _positive_float_env(name: str, default: float) -> float:
    """Return a positive finite timeout from the environment."""

    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 0 < value < 86_400 else default


def _database_key(db_path: Path) -> str:
    """Resolve a relative database path after the runtime data-dir chdir."""

    return str(db_path.expanduser().resolve())


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _snapshot_directory() -> Path | None:
    configured = os.environ.get("TR_ALERT_DB_SNAPSHOT_DIR", "").strip()
    return Path(configured).expanduser() if configured else None


def _snapshot_prefix(db_path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", db_path.name).strip("-.") or "tracker"


def _snapshot_candidates(db_path: Path) -> list[Path]:
    snapshot_dir = _snapshot_directory()
    if snapshot_dir is None or not snapshot_dir.exists():
        return []
    prefix = _snapshot_prefix(db_path)
    return sorted(snapshot_dir.glob(f"{prefix}-*.sqlite3"), reverse=True)


def _sqlite_quick_check(db_path: Path) -> bool:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    finally:
        connection.close()


def _restore_database_snapshot(db_path: Path) -> Path | None:
    """Restore the newest valid snapshot into local container storage."""

    if db_path.exists():
        return None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    candidates = _snapshot_candidates(db_path)
    legacy_path = os.environ.get("TR_ALERT_DB_LEGACY_PATH", "").strip()
    if legacy_path:
        legacy = Path(legacy_path).expanduser()
        if legacy.exists():
            candidates.append(legacy)

    restore_temp = db_path.with_name(f".{db_path.name}.restore-{os.getpid()}.tmp")
    for source in candidates:
        try:
            shutil.copyfile(source, restore_temp)
            if not _sqlite_quick_check(restore_temp):
                logging.error("Ignoring invalid SQLite state snapshot: %s", source)
                restore_temp.unlink(missing_ok=True)
                continue
            os.replace(restore_temp, db_path)
            logging.info("Restored SQLite tracker state from %s", source)
            return source
        except (OSError, sqlite3.DatabaseError):
            logging.exception("Unable to restore SQLite tracker state from %s", source)
            restore_temp.unlink(missing_ok=True)
    return None


def _snapshot_database(db_path: Path) -> Path | None:
    """Persist a closed local SQLite database as an immutable Azure Files snapshot."""

    snapshot_dir = _snapshot_directory()
    if snapshot_dir is None or not db_path.exists():
        return None
    try:
        if not _sqlite_quick_check(db_path):
            logging.error("SQLite tracker quick_check failed; persistent snapshot was not written.")
            return None
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        prefix = _snapshot_prefix(db_path)
        stamp = time.time_ns()
        final_path = snapshot_dir / f"{prefix}-{stamp:020d}.sqlite3"
        staging_path = snapshot_dir / f".{prefix}-{stamp:020d}-{os.getpid()}.tmp"
        shutil.copyfile(db_path, staging_path)
        os.replace(staging_path, final_path)

        try:
            keep_count = max(2, min(100, int(os.environ.get("TR_ALERT_DB_SNAPSHOT_KEEP", "20"))))
        except ValueError:
            keep_count = 20
        for old_snapshot in _snapshot_candidates(db_path)[keep_count:]:
            old_snapshot.unlink(missing_ok=True)
        return final_path
    except OSError:
        logging.exception("Unable to persist SQLite tracker snapshot to %s", snapshot_dir)
        return None


@contextmanager
def _db_connection(db_path: Path, *, timeout: float | None = None) -> Any:
    """Open a lock-tolerant SQLite connection and always close it."""

    with _DB_OPERATION_LOCK:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        effective_timeout = timeout or _positive_float_env("SQLITE_BUSY_TIMEOUT_SECONDS", 30.0)
        connection = sqlite3.connect(db_path, timeout=effective_timeout)
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(effective_timeout * 1000))}")
        changes_before = connection.total_changes
        committed_changes = False
        try:
            yield connection
            connection.commit()
            committed_changes = connection.total_changes > changes_before
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if committed_changes:
            _snapshot_database(db_path)


def init_seen_db(db_path: Path = DB_PATH) -> None:
    """Create tracker tables once, retrying transient rollout locks."""

    db_key = _database_key(db_path)
    with _DB_INIT_LOCK:
        _restore_database_snapshot(db_path)
        if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
            return
        _INITIALIZED_DB_PATHS.discard(db_key)

        retry_window = _positive_float_env("SQLITE_INIT_RETRY_SECONDS", 300.0)
        base_delay = _positive_float_env("SQLITE_INIT_RETRY_DELAY_SECONDS", 0.5)
        deadline = time.monotonic() + retry_window
        attempt = 0
        while True:
            attempt += 1
            remaining = max(0.0, deadline - time.monotonic())
            busy_timeout = min(
                _positive_float_env("SQLITE_BUSY_TIMEOUT_SECONDS", 30.0),
                max(0.05, remaining),
            )
            try:
                with _db_connection(db_path, timeout=busy_timeout) as connection:
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
                    connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_pdfs_pdf_url ON seen_pdfs(pdf_url)"
                    )
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
                _INITIALIZED_DB_PATHS.add(db_key)
                _snapshot_database(db_path)
                if attempt > 1:
                    logging.info("SQLite tracker initialization acquired the database lock after %s attempts.", attempt)
                return
            except sqlite3.OperationalError as exc:
                remaining = deadline - time.monotonic()
                if not _is_lock_error(exc) or remaining <= 0:
                    raise
                delay = min(base_delay * (2 ** min(attempt - 1, 4)), 5.0, remaining)
                logging.warning(
                    "SQLite tracker database is temporarily locked during startup; retrying in %.2fs "
                    "(attempt %s).",
                    delay,
                    attempt,
                )
                time.sleep(delay)


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
