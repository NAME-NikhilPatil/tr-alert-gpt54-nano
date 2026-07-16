"""Persistent PDF job queue for live financial extraction workers."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from db_manager import DB_PATH
from db_manager import _db_connection as _shared_db_connection
from db_manager import init_seen_db
from models import Announcement

DISCOVERED = "DISCOVERED"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
DONE = "DONE"
FAILED = "FAILED"
SKIPPED_NON_FINANCIAL_DISCLOSURE = "SKIPPED_NON_FINANCIAL_DISCLOSURE"

TERMINAL_STATUSES = {DONE, FAILED, SKIPPED_NON_FINANCIAL_DISCLOSURE}
ACTIVE_OR_COMPLETED_STATUSES = {QUEUED, PROCESSING, DONE, SKIPPED_NON_FINANCIAL_DISCLOSURE}


@contextmanager
def _db_connection(db_path: Path, *, timeout: float | None = None) -> Any:
    """Reuse the tracker database's lock-tolerant connection policy."""

    with _shared_db_connection(db_path, timeout=timeout) as connection:
        yield connection


@dataclass(slots=True)
class PdfJob:
    """One queued PDF job."""

    id: int
    unique_key: str
    exchange: str
    company_name: str
    identifier: str
    announcement_datetime: str
    subject: str
    pdf_url: str
    local_pdf_path: str
    status: str
    attempt_count: int
    last_error: str
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str


@dataclass(slots=True)
class PdfJobCounts:
    """Current queue status counters."""

    queued: int = 0
    processing: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


def init_pdf_job_db(db_path: Path = DB_PATH) -> None:
    """Create the persistent PDF job queue table if needed."""

    init_seen_db(db_path)
    with _db_connection(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pdf_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_key TEXT UNIQUE NOT NULL,
                exchange TEXT NOT NULL,
                company_name TEXT,
                identifier TEXT,
                announcement_datetime TEXT,
                subject TEXT,
                pdf_url TEXT,
                local_pdf_path TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pdf_jobs_unique_key ON pdf_jobs(unique_key)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_pdf_jobs_status_id ON pdf_jobs(status, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_pdf_jobs_pdf_url ON pdf_jobs(pdf_url)")


def unique_key_for_announcement(announcement: Announcement) -> str:
    """Return a stable unique key for a PDF announcement."""

    exchange = str(announcement.source or "").upper().strip() or "UNKNOWN"
    url = str(announcement.pdf_url or "").strip()
    if url:
        digest_source = url
    else:
        digest_source = "|".join(
            [
                exchange,
                str(announcement.identifier or "").strip(),
                str(announcement.company_name or "").strip().lower(),
                str(announcement.announcement_datetime or "").strip(),
            ]
        )
    digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()
    return f"{exchange}:{digest}"


def enqueue_pdf_job(announcement: Announcement, db_path: Path = DB_PATH) -> tuple[PdfJob | None, bool]:
    """Persist a PDF job unless the same URL/key already exists."""

    init_pdf_job_db(db_path)
    unique_key = unique_key_for_announcement(announcement)
    pdf_url = str(announcement.pdf_url or "").strip()
    existing = get_job_by_key_or_url(unique_key, pdf_url, db_path=db_path)
    if existing is not None:
        return existing, False

    with _db_connection(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO pdf_jobs (
                unique_key, exchange, company_name, identifier, announcement_datetime,
                subject, pdf_url, local_pdf_path, status, attempt_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                unique_key,
                str(announcement.source or "").upper(),
                announcement.company_name,
                announcement.identifier,
                announcement.announcement_datetime,
                announcement.subject,
                pdf_url,
                str(announcement.pdf_path or ""),
                QUEUED,
            ),
        )
        job_id = int(cursor.lastrowid)
    return get_job_by_id(job_id, db_path=db_path), True


def get_job_by_key_or_url(unique_key: str, pdf_url: str, db_path: Path = DB_PATH) -> PdfJob | None:
    """Return an existing job by stable key or PDF URL."""

    init_pdf_job_db(db_path)
    with _db_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM pdf_jobs
            WHERE unique_key = ? OR (? <> '' AND pdf_url = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (unique_key, pdf_url, pdf_url),
        ).fetchone()
    return _job_from_row(row) if row else None


def get_job_by_id(job_id: int, db_path: Path = DB_PATH) -> PdfJob | None:
    """Return one job by integer id."""

    init_pdf_job_db(db_path)
    with _db_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pdf_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def reset_processing_jobs(db_path: Path = DB_PATH) -> int:
    """Requeue jobs left PROCESSING by an unclean shutdown."""

    init_pdf_job_db(db_path)
    with _db_connection(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE pdf_jobs
            SET status = ?, updated_at = CURRENT_TIMESTAMP, started_at = NULL
            WHERE status = ?
            """,
            (QUEUED, PROCESSING),
        )
    return int(cursor.rowcount or 0)


def claim_next_pdf_job(db_path: Path = DB_PATH) -> PdfJob | None:
    """Atomically claim the oldest queued job and mark it PROCESSING."""

    init_pdf_job_db(db_path)
    with _db_connection(db_path, timeout=30) as connection:
        connection.isolation_level = None
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM pdf_jobs WHERE status = ? ORDER BY id LIMIT 1",
                (QUEUED,),
            ).fetchone()
            if not row:
                connection.execute("COMMIT")
                return None
            job = _job_from_row(row)
            connection.execute(
                """
                UPDATE pdf_jobs
                SET status = ?, attempt_count = attempt_count + 1,
                    updated_at = CURRENT_TIMESTAMP, started_at = CURRENT_TIMESTAMP,
                    finished_at = NULL, last_error = ''
                WHERE id = ? AND status = ?
                """,
                (PROCESSING, job.id, QUEUED),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return get_job_by_id(job.id, db_path=db_path)


def update_job_local_path(job_id: int, local_pdf_path: str | Path, db_path: Path = DB_PATH) -> None:
    """Persist the downloaded PDF path for a job."""

    with _db_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE pdf_jobs
            SET local_pdf_path = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(local_pdf_path or ""), job_id),
        )


def mark_job_done(job_id: int, db_path: Path = DB_PATH) -> None:
    """Mark a job DONE."""

    _mark_job_terminal(job_id, DONE, "", db_path=db_path)


def mark_job_skipped(job_id: int, reason: str = "", db_path: Path = DB_PATH) -> None:
    """Mark a non-financial job skipped."""

    _mark_job_terminal(job_id, SKIPPED_NON_FINANCIAL_DISCLOSURE, reason, db_path=db_path)


def mark_job_failed(job_id: int, error: str, db_path: Path = DB_PATH) -> None:
    """Mark a job FAILED with the final error."""

    _mark_job_terminal(job_id, FAILED, error, db_path=db_path)


def requeue_job(job_id: int, error: str, db_path: Path = DB_PATH) -> None:
    """Return a failed attempt to QUEUED for retry."""

    with _db_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE pdf_jobs
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP,
                started_at = NULL, finished_at = NULL
            WHERE id = ?
            """,
            (QUEUED, str(error)[:2000], job_id),
        )


def queue_counts(db_path: Path = DB_PATH) -> PdfJobCounts:
    """Return queue status counters."""

    init_pdf_job_db(db_path)
    counts = PdfJobCounts()
    with _db_connection(db_path) as connection:
        rows = connection.execute("SELECT status, COUNT(*) FROM pdf_jobs GROUP BY status").fetchall()
    for status, count in rows:
        value = int(count or 0)
        if status == QUEUED:
            counts.queued = value
        elif status == PROCESSING:
            counts.processing = value
        elif status == DONE:
            counts.done = value
        elif status == FAILED:
            counts.failed = value
        elif status == SKIPPED_NON_FINANCIAL_DISCLOSURE:
            counts.skipped = value
    return counts


def job_to_announcement(job: PdfJob) -> Announcement:
    """Convert a queue row back into the Announcement model."""

    return Announcement(
        source=job.exchange,
        company_name=job.company_name,
        identifier=job.identifier,
        announcement_datetime=job.announcement_datetime,
        subject=job.subject,
        pdf_url=job.pdf_url,
        pdf_path=Path(job.local_pdf_path) if job.local_pdf_path else None,
    )


def log_pdf_job_event(
    event: str,
    *,
    job: PdfJob | None = None,
    worker_id: str = "",
    status: str = "",
    active_gpt_jobs_count: int = 0,
    queued_jobs_count: int | None = None,
    elapsed_seconds: float = 0.0,
    db_path: Path = DB_PATH,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one structured queue log line."""

    counts = queue_counts(db_path)
    queued = counts.queued if queued_jobs_count is None else queued_jobs_count
    fields: dict[str, Any] = {
        "event": event,
        "job_id": job.id if job else "",
        "unique_key": job.unique_key if job else "",
        "company_name": job.company_name if job else "",
        "pdf_url": job.pdf_url if job else "",
        "exchange": job.exchange if job else "",
        "worker_id": worker_id,
        "status": status or (job.status if job else ""),
        "active_gpt_jobs_count": active_gpt_jobs_count,
        "queued_jobs_count": queued,
        "elapsed_seconds": round(float(elapsed_seconds or 0), 3),
    }
    if extra:
        fields.update(extra)
    logging.info("PDF_JOB_EVENT %s", " ".join(f"{key}={_log_value(value)}" for key, value in fields.items()))


def _mark_job_terminal(job_id: int, status: str, error: str, db_path: Path = DB_PATH) -> None:
    with _db_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE pdf_jobs
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, str(error)[:2000], job_id),
        )


def _job_from_row(row: Any) -> PdfJob:
    return PdfJob(
        id=int(row[0]),
        unique_key=str(row[1] or ""),
        exchange=str(row[2] or ""),
        company_name=str(row[3] or ""),
        identifier=str(row[4] or ""),
        announcement_datetime=str(row[5] or ""),
        subject=str(row[6] or ""),
        pdf_url=str(row[7] or ""),
        local_pdf_path=str(row[8] or ""),
        status=str(row[9] or ""),
        attempt_count=int(row[10] or 0),
        last_error=str(row[11] or ""),
        created_at=str(row[12] or ""),
        updated_at=str(row[13] or ""),
        started_at=str(row[14] or ""),
        finished_at=str(row[15] or ""),
    )


def _log_value(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return "''"
    if any(char.isspace() for char in text):
        return repr(text)
    return text
