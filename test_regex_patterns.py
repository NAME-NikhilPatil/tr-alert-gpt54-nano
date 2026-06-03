"""Audit hardened number/date patterns against local NSE/BSE PDF text."""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from pdf_parser import DATE_RE, NUMBER_RE, _normalize_financial_value
from utils import normalize_date

SAMPLE_VALUES = [
    "1,23,456.78",
    "123456.78",
    "1.23 Cr",
    "12.50%",
    "12.5 %",
    "(1,234.56)",
    "-1,234.56",
    "(1234.56)",
    "Rs. 2,345.00 lakhs",
    "INR 4.5 crore",
]

SAMPLE_DATES = [
    "01/04/2025",
    "01-04-2025",
    "April 1 2025",
    "1st April 2025",
    "31 Mar 2026",
    "31-March-2026",
    "15.05.2026",
    "May 16, 2026",
    "16th May, 2026",
    "2026-05-16",
]


def _extract_text(path: Path, max_pages: int = 3) -> str:
    """Read a small text sample from a PDF using PyMuPDF."""

    document = fitz.open(path)
    try:
        return "\n".join(document[idx].get_text("text") for idx in range(min(document.page_count, max_pages)))
    finally:
        document.close()


def run_audit() -> None:
    """Run pattern checks against synthetic edge cases and ten real PDFs."""

    pdf_paths = sorted(Path("downloads").glob("*/*.pdf"))[:10]
    print("static_value_cases")
    for sample in SAMPLE_VALUES:
        match = NUMBER_RE.search(sample)
        normalized = _normalize_financial_value(sample)
        print(f"{sample!r}: match={bool(match)} normalized={normalized!r}")

    print("static_date_cases")
    for sample in SAMPLE_DATES:
        regex_match = bool(DATE_RE.search(sample))
        normalized = normalize_date(sample)
        print(f"{sample!r}: regex_match={regex_match} normalized={normalized!r}")

    print("real_pdf_cases")
    for path in pdf_paths:
        try:
            text = _extract_text(path)
        except Exception as exc:
            print(f"{path}: error={exc}")
            continue
        number_hits = len(NUMBER_RE.findall(text))
        date_hits = len(DATE_RE.findall(text))
        indian_commas = bool(re.search(r"\d{1,3},\d{2},\d{3}", text))
        negative_hits = bool(re.search(r"\(\s*\d[\d,]*(?:\.\d+)?\s*\)|-\s*\d", text))
        print(
            f"{path}: chars={len(text)} number_hits={number_hits} date_hits={date_hits} "
            f"indian_commas={indian_commas} negatives={negative_hits}"
        )


if __name__ == "__main__":
    run_audit()
