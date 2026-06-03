"""Temporary live debugging telemetry for the Telegram scraper.

This module is intentionally standalone and controlled by environment flags so
it can be removed or disabled before deployment without touching scraper logic.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from models import Announcement
from mistral_parser import has_mistral_financial_data


def _truthy(value: str | None) -> bool:
    """Return True when an environment value should enable a feature."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _json_safe(value: Any) -> Any:
    """Convert common runtime objects into JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _short_text(value: Any, limit: int = 500) -> str:
    """Return a compact single-line value for CSV/debug summaries."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _pdf_stats(path: Path | str | None) -> dict[str, Any]:
    """Return size and SHA-256 details for a downloaded PDF when available."""
    if not path:
        return {"pdf_size_bytes": None, "pdf_sha256": None}
    pdf_path = Path(path)
    if not pdf_path.exists() or not pdf_path.is_file():
        return {"pdf_size_bytes": None, "pdf_sha256": None}

    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "pdf_size_bytes": pdf_path.stat().st_size,
        "pdf_sha256": digest.hexdigest(),
    }


def _count_rows(table: Any) -> int:
    """Count non-empty row dictionaries in a list-like table."""
    if not isinstance(table, list):
        return 0
    return sum(1 for row in table if isinstance(row, dict) and any(row.values()))


def _count_section_rows(sections: Any) -> int:
    """Count rows nested under variable sections."""
    if not isinstance(sections, list):
        return 0
    total = 0
    for section in sections:
        if isinstance(section, dict):
            total += _count_rows(section.get("rows"))
    return total


def _count_period_values(rows: Any) -> int:
    """Count extracted financial values across dynamic period columns."""
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        periods = row.get("periods") or row.get("values") or {}
        if isinstance(periods, dict):
            total += sum(1 for value in periods.values() if value not in (None, ""))
    return total


def _count_section_values(sections: Any) -> int:
    """Count extracted values nested under variable sections."""
    if not isinstance(sections, list):
        return 0
    total = 0
    for section in sections:
        if isinstance(section, dict):
            total += _count_period_values(section.get("rows"))
    return total


def _summarize_extraction(extraction: dict[str, Any] | None, confidence_score: float | None) -> dict[str, Any]:
    """Build compact metrics from a Mistral extraction payload."""
    if not isinstance(extraction, dict):
        return {
            "confidence_score": confidence_score,
            "has_financial_data": False,
            "financial_row_count": 0,
            "financial_value_count": 0,
            "segment_table_count": 0,
            "segment_row_count": 0,
            "segment_value_count": 0,
            "balance_sheet_row_count": 0,
            "balance_sheet_value_count": 0,
            "cash_flow_row_count": 0,
            "cash_flow_value_count": 0,
            "key_variable_row_count": 0,
            "key_variable_value_count": 0,
            "total_value_count": 0,
        }

    financial_rows = extraction.get("financial_rows") or extraction.get("rows") or []
    segment_tables = extraction.get("segment_tables") or []
    balance_sheet = extraction.get("balance_sheet_variables") or []
    cash_flow = extraction.get("cash_flow_variables") or []
    key_variables = extraction.get("key_variables") or []

    segment_row_count = 0
    segment_value_count = 0
    if isinstance(segment_tables, list):
        for table in segment_tables:
            if isinstance(table, dict):
                rows = table.get("rows")
                segment_row_count += _count_rows(rows)
                segment_value_count += _count_period_values(rows)
    balance_sheet_row_count = _count_section_rows(balance_sheet)
    balance_sheet_value_count = _count_section_values(balance_sheet)
    cash_flow_row_count = _count_rows(cash_flow)
    cash_flow_value_count = _count_period_values(cash_flow)
    key_variable_row_count = _count_rows(key_variables)
    key_variable_value_count = _count_period_values(key_variables)
    total_value_count = (
        _count_period_values(financial_rows)
        + segment_value_count
        + balance_sheet_value_count
        + cash_flow_value_count
        + key_variable_value_count
    )

    return {
        "confidence_score": confidence_score,
        "has_financial_data": has_mistral_financial_data(extraction),
        "financial_row_count": _count_rows(financial_rows),
        "financial_value_count": _count_period_values(financial_rows),
        "segment_table_count": len(segment_tables) if isinstance(segment_tables, list) else 0,
        "segment_row_count": segment_row_count,
        "segment_value_count": segment_value_count,
        "balance_sheet_row_count": balance_sheet_row_count,
        "balance_sheet_value_count": balance_sheet_value_count,
        "cash_flow_row_count": cash_flow_row_count,
        "cash_flow_value_count": cash_flow_value_count,
        "key_variable_row_count": key_variable_row_count,
        "key_variable_value_count": key_variable_value_count,
        "total_variable_row_count": balance_sheet_row_count + cash_flow_row_count + key_variable_row_count,
        "total_value_count": total_value_count,
        "parser_status": extraction.get("parser_status") or extraction.get("status"),
        "parser_message": _short_text(extraction.get("parser_message") or extraction.get("message")),
        "model_name": extraction.get("model") or extraction.get("model_name"),
    }


@dataclass
class LiveDebugger:
    """Write temporary live scraper telemetry to JSONL, CSV, and latest snapshot files."""

    enabled: bool
    debug_dir: Path
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "LiveDebugger":
        """Create a debugger from LIVE_DEBUGGER_* environment variables."""
        enabled = _truthy(os.getenv("LIVE_DEBUGGER_ENABLED"))
        debug_dir = Path(os.getenv("LIVE_DEBUGGER_DIR", "logs/debug"))
        debugger = cls(enabled=enabled, debug_dir=debug_dir)
        if enabled:
            debugger.debug_dir.mkdir(parents=True, exist_ok=True)
            logging.info("Live debugger enabled. Writing telemetry to %s", debugger.debug_dir)
        return debugger

    @property
    def jsonl_path(self) -> Path:
        """Return today's JSONL debug path."""
        return self.debug_dir / f"live_debug_{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    @property
    def csv_path(self) -> Path:
        """Return today's CSV debug path."""
        return self.debug_dir / f"live_debug_{datetime.now().strftime('%Y-%m-%d')}.csv"

    @property
    def latest_path(self) -> Path:
        """Return the latest-event snapshot path."""
        return self.debug_dir / "live_debug_latest.json"

    def record_poll_start(self, announcements: list[Announcement], raw_counts: dict[str, int] | None = None) -> None:
        """Record the announcement set fetched for a poll."""
        if not self.enabled:
            return
        event = {
            "event": "poll_start",
            "timestamp": datetime.now(),
            "announcement_count": len(announcements),
            "raw_counts": raw_counts or {},
            "announcements": [self._announcement_payload(item) for item in announcements],
        }
        self._write_event(event)

    def record_skip(self, announcement: Announcement, reason: str) -> None:
        """Record a skipped announcement."""
        if not self.enabled:
            return
        event = {
            "event": "skip",
            "timestamp": datetime.now(),
            "reason": reason,
            "announcement": self._announcement_payload(announcement),
        }
        self._write_event(event)

    def record_processed(
        self,
        *,
        announcement: Announcement,
        extraction: dict[str, Any] | None,
        confidence_score: float | None,
        image_paths: list[Path] | list[str] | None,
        telegram_messages_sent: int,
        timings: dict[str, float],
    ) -> None:
        """Record a successfully processed PDF and the extraction payload."""
        if not self.enabled:
            return
        pdf_stats = _pdf_stats(getattr(announcement, "pdf_path", None))
        metrics = _summarize_extraction(extraction, confidence_score)
        event = {
            "event": "processed",
            "timestamp": datetime.now(),
            "announcement": self._announcement_payload(announcement),
            "pdf": {
                "path": getattr(announcement, "pdf_path", None),
                **pdf_stats,
            },
            "metrics": metrics,
            "timings_seconds": timings,
            "telegram_messages_sent": telegram_messages_sent,
            "rendered_images": [str(path) for path in (image_paths or [])],
            "extraction_payload": extraction,
        }
        self._write_event(event)
        logging.info(
            "DEBUG processed %s %s confidence=%.2f has_data=%s values=%s images=%s",
            announcement.source,
            announcement.company_name,
            confidence_score or 0.0,
            metrics["has_financial_data"],
            metrics["total_value_count"],
            len(image_paths or []),
        )

    def record_error(
        self,
        *,
        announcement: Announcement,
        error: BaseException,
        timings: dict[str, float] | None = None,
    ) -> None:
        """Record an exception raised while processing a PDF."""
        if not self.enabled:
            return
        event = {
            "event": "error",
            "timestamp": datetime.now(),
            "announcement": self._announcement_payload(announcement),
            "pdf": {
                "path": getattr(announcement, "pdf_path", None),
                **_pdf_stats(getattr(announcement, "pdf_path", None)),
            },
            "error_type": type(error).__name__,
            "error_message": str(error),
            "timings_seconds": timings or {},
        }
        self._write_event(event)

    def record_poll_complete(self, *, processed: int, telegram_messages_sent: int, average_confidence: float) -> None:
        """Record a compact poll completion summary."""
        if not self.enabled:
            return
        event = {
            "event": "poll_complete",
            "timestamp": datetime.now(),
            "processed": processed,
            "telegram_messages_sent": telegram_messages_sent,
            "average_confidence": average_confidence,
        }
        self._write_event(event)

    def _announcement_payload(self, announcement: Announcement) -> dict[str, Any]:
        """Return safe announcement metadata for debug files."""
        return {
            "source": announcement.source,
            "company_name": announcement.company_name,
            "symbol": getattr(announcement, "symbol", None),
            "announcement_id": getattr(announcement, "announcement_id", None),
            "date": getattr(announcement, "date", None),
            "time": getattr(announcement, "time", None),
            "subject": _short_text(getattr(announcement, "subject", None), 1000),
            "pdf_url": getattr(announcement, "pdf_url", None),
            "pdf_path": getattr(announcement, "pdf_path", None),
        }

    def _write_event(self, event: dict[str, Any]) -> None:
        """Append an event to debug outputs."""
        safe_event = _json_safe(event)
        with self._write_lock:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe_event, ensure_ascii=False) + "\n")
            self.latest_path.write_text(json.dumps(safe_event, indent=2, ensure_ascii=False), encoding="utf-8")
            self._append_csv(safe_event)

    def _append_csv(self, event: dict[str, Any]) -> None:
        """Append a compact row to the daily debug CSV."""
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        announcement = event.get("announcement") if isinstance(event.get("announcement"), dict) else {}
        pdf = event.get("pdf") if isinstance(event.get("pdf"), dict) else {}
        timings = event.get("timings_seconds") if isinstance(event.get("timings_seconds"), dict) else {}
        row = {
            "timestamp": event.get("timestamp"),
            "event": event.get("event"),
            "source": announcement.get("source"),
            "company_name": announcement.get("company_name"),
            "announcement_id": announcement.get("announcement_id"),
            "date": announcement.get("date"),
            "confidence_score": metrics.get("confidence_score"),
            "has_financial_data": metrics.get("has_financial_data"),
            "financial_row_count": metrics.get("financial_row_count"),
            "financial_value_count": metrics.get("financial_value_count"),
            "segment_table_count": metrics.get("segment_table_count"),
            "segment_row_count": metrics.get("segment_row_count"),
            "segment_value_count": metrics.get("segment_value_count"),
            "balance_sheet_row_count": metrics.get("balance_sheet_row_count"),
            "balance_sheet_value_count": metrics.get("balance_sheet_value_count"),
            "cash_flow_row_count": metrics.get("cash_flow_row_count"),
            "cash_flow_value_count": metrics.get("cash_flow_value_count"),
            "key_variable_row_count": metrics.get("key_variable_row_count"),
            "key_variable_value_count": metrics.get("key_variable_value_count"),
            "total_variable_row_count": metrics.get("total_variable_row_count"),
            "total_value_count": metrics.get("total_value_count"),
            "parser_status": metrics.get("parser_status"),
            "pdf_size_bytes": pdf.get("pdf_size_bytes"),
            "pdf_sha256": pdf.get("pdf_sha256"),
            "telegram_messages_sent": event.get("telegram_messages_sent"),
            "download_seconds": timings.get("download"),
            "mistral_seconds": timings.get("mistral"),
            "render_seconds": timings.get("render"),
            "send_seconds": timings.get("send"),
            "total_seconds": timings.get("total"),
            "error_type": event.get("error_type"),
            "error_message": _short_text(event.get("error_message")),
        }
        fieldnames = list(row.keys())
        write_header = not self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
