"""Shared data models for board meeting outcome scraping."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Announcement:
    """Exchange announcement metadata and attachment location."""

    source: str
    company_name: str
    identifier: str
    announcement_datetime: str
    subject: str
    pdf_url: str
    details: str = ""
    pdf_path: Path | None = None


@dataclass(slots=True)
class FinancialData:
    """Financial result fields extracted from an announcement PDF."""

    currency_unit: str = "Rs in Cr"
    periods: list[str] = field(default_factory=list)
    rows: dict[str, dict[str, str]] = field(default_factory=dict)
    field_confidence: dict[str, dict[str, float]] = field(default_factory=dict)
    meeting_date: str = ""
    dividend: str = ""
    dividend_per_share: str = ""
    dividend_declared: str = ""
    board_meeting_start_time: str = ""
    board_meeting_end_time: str = ""
    parser_status: str = ""
    parser_message: str = ""
    text_excerpt: str = ""
    screenshots: list[str] = field(default_factory=list)
    parser_layers: list[str] = field(default_factory=list)
    extraction_layer: str = ""
    preprocessing_flags: list[str] = field(default_factory=list)
    language: str = ""
    document_type: str = ""
    validation_status: str = ""
    validation_errors: list[str] = field(default_factory=list)
    llm_status: str = ""
