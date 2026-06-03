"""Logging setup and daily summary helpers for the live scraper."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class DailySummary:
    """In-memory counters for one live scraper process."""

    processed: int = 0
    telegram_messages_sent: int = 0
    confidence_scores: list[float] = field(default_factory=list)
    failed_pdfs: list[str] = field(default_factory=list)

    def average_confidence(self) -> float:
        """Return the current average confidence percentage."""

        if not self.confidence_scores:
            return 0.0
        return sum(self.confidence_scores) / len(self.confidence_scores)


def setup_live_logging() -> Path:
    """Configure live scraper logging to logs/scraper_YYYY-MM-DD.log."""

    Path("logs").mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = Path("logs") / f"scraper_{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return log_path


def log_daily_summary(summary: DailySummary) -> None:
    """Write the current daily summary counters to the log."""

    logging.info(
        "Daily summary: processed=%s telegram_messages_sent=%s average_confidence=%.2f failed_pdfs=%s",
        summary.processed,
        summary.telegram_messages_sent,
        summary.average_confidence(),
        len(summary.failed_pdfs),
    )
