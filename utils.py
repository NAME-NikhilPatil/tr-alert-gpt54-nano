"""Common utility helpers for exchange scrapers."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
import sqlite3
from datetime import date, datetime
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin

import httpx

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nseindia.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bseindia.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

NSE_BASE_URL = "https://www.nseindia.com"
BSE_BASE_URL = "https://www.bseindia.com"


def ensure_directories() -> None:
    """Create runtime directories used by the scraper."""

    for folder in ("downloads/NSE", "downloads/BSE", "output", "logs"):
        Path(folder).mkdir(parents=True, exist_ok=True)


def init_cache(db_path: Path = Path("announcement_cache.db")) -> None:
    """Initialize the announcement cache SQLite database."""

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                cache_key TEXT PRIMARY KEY,
                source TEXT,
                company_name TEXT,
                identifier TEXT,
                announcement_date TEXT,
                pdf_url TEXT,
                pdf_path TEXT,
                confidence REAL,
                financial_json TEXT,
                updated_at TEXT
            )
            """
        )


def cache_key(source: str, company_name: str, identifier: str, announcement_date: date | str, pdf_url: str = "") -> str:
    """Build a stable cache key for one announcement."""

    date_text = announcement_date.isoformat() if isinstance(announcement_date, date) else str(announcement_date)
    return "|".join([source.upper(), company_name.strip().lower(), identifier.strip().lower(), date_text, pdf_url])


def get_cached_announcement(key: str, db_path: Path = Path("announcement_cache.db")) -> dict[str, object] | None:
    """Return cached announcement parse data when present."""

    init_cache(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM announcements WHERE cache_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def save_cached_announcement(
    key: str,
    source: str,
    company_name: str,
    identifier: str,
    announcement_date: date,
    pdf_url: str,
    pdf_path: Path | None,
    confidence: float,
    financials: object | None,
    db_path: Path = Path("announcement_cache.db"),
) -> None:
    """Persist a parsed announcement and PDF path in the SQLite cache."""

    init_cache(db_path)
    financial_json = json.dumps(asdict(financials), default=str) if financials else ""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO announcements
            (cache_key, source, company_name, identifier, announcement_date, pdf_url, pdf_path, confidence, financial_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                source,
                company_name,
                identifier,
                announcement_date.isoformat(),
                pdf_url,
                str(pdf_path or ""),
                confidence,
                financial_json,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def financial_from_cache(payload: str) -> object | None:
    """Deserialize cached FinancialData without importing models at module import time."""

    if not payload:
        return None
    try:
        from models import FinancialData

        data = json.loads(payload)
        return FinancialData(**data)
    except Exception:
        logging.exception("Could not deserialize cached financial data.")
        return None


def setup_logging(run_date: date) -> Path:
    """Configure file and console logging for a scraper run."""

    ensure_directories()
    log_path = Path("logs") / f"scraper_{run_date.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return log_path


def parse_cli_date(value: str | None) -> date:
    """Parse an optional YYYY-MM-DD command-line date."""

    if not value:
        return datetime.now().date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def exchange_date(value: date, separator: str = "-") -> str:
    """Format a date as DD-MM-YYYY or DD/MM/YYYY for exchange inputs."""

    return value.strftime(f"%d{separator}%m{separator}%Y")


def normalize_date(value: str) -> str:
    """Normalize common Indian exchange date strings to DD-MM-YYYY."""

    if not value:
        return ""

    cleaned = re.sub(r"\s+", " ", value.strip())
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
    try:
        iso_value = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        return datetime.fromisoformat(iso_value).strftime("%d-%m-%Y")
    except ValueError:
        pass
    formats = (
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d-%b-%Y",
        "%d.%b.%Y",
        "%d %B %Y",
        "%d-%B-%Y",
        "%d.%B.%Y",
        "%d %B, %Y",
        "%d %b, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue

    match = re.search(
        r"(?<!\d)(?P<day>\d{1,2})[-/.](?P<month>\d{1,2})[-/.](?P<year>\d{2,4})(?!\d)",
        cleaned,
    )
    if match:
        day = match.group("day")
        month = match.group("month")
        year = match.group("year")
        if len(year) == 2:
            year = f"20{year}"
        return f"{int(day):02d}-{int(month):02d}-{year}"
    return cleaned


def sanitize_filename(value: str, max_length: int = 90) -> str:
    """Return a filesystem-safe filename fragment."""

    cleaned = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return (cleaned or "unknown")[:max_length]


def absolutize_url(url: str, base_url: str) -> str:
    """Return an absolute URL for an exchange attachment link."""

    if not url:
        return ""
    return urljoin(base_url, url)


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retries: int = 3,
    **kwargs: object,
) -> httpx.Response:
    """Run an HTTP request with exponential backoff for transient failures."""

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = httpx.HTTPStatusError(
                f"Transient HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_error = exc
        if attempt < retries:
            await asyncio.sleep(2 ** (attempt - 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"Request failed without a captured exception: {url}")


async def download_pdf(
    announcement_source: str,
    company_name: str,
    announcement_date: date,
    pdf_url: str,
    headers: dict[str, str],
) -> Path | None:
    """Download a PDF attachment into the source-specific downloads folder."""

    if not pdf_url:
        return None

    source = announcement_source.upper()
    folder = Path("downloads") / source
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{sanitize_filename(company_name)}_{announcement_date.isoformat()}"
    target = folder / f"{stem}.pdf"
    suffix = 2
    while target.exists() and target.stat().st_size > 0:
        target = folder / f"{stem}_{suffix}.pdf"
        suffix += 1

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=45) as client:
        try:
            if source == "NSE":
                try:
                    await request_with_retries(client, "GET", NSE_BASE_URL, headers=headers)
                except httpx.HTTPError as exc:
                    logging.warning("NSE homepage warm-up failed; downloading archive PDF directly: %s", exc)
            elif source == "BSE":
                await request_with_retries(client, "GET", BSE_BASE_URL, headers=headers)
            try:
                response = await request_with_retries(client, "GET", pdf_url, headers=headers)
            except Exception:
                # Some exchange attachment URLs are backed by short-lived sessions; refresh cookies and retry once.
                if source == "NSE":
                    try:
                        await request_with_retries(client, "GET", NSE_BASE_URL, headers=headers)
                    except httpx.HTTPError as exc:
                        logging.warning("NSE homepage refresh failed; retrying archive PDF directly: %s", exc)
                elif source == "BSE":
                    await request_with_retries(client, "GET", BSE_BASE_URL, headers=headers)
                response = await request_with_retries(client, "GET", pdf_url, headers=headers)
            validation_error = _pdf_payload_validation_error(response.content)
            for download_attempt in range(2, 4):
                if not validation_error:
                    break
                logging.warning(
                    "Incomplete PDF response for %s (attempt %s/3): %s; retrying.",
                    pdf_url,
                    download_attempt - 1,
                    validation_error,
                )
                await asyncio.sleep(2 ** (download_attempt - 2))
                response = await request_with_retries(client, "GET", pdf_url, headers=headers)
                validation_error = _pdf_payload_validation_error(response.content)
            if validation_error:
                raise RuntimeError(f"Incomplete PDF response after 3 attempts: {validation_error}")
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
                logging.warning("Attachment did not look like a PDF: %s", pdf_url)
            target.write_bytes(response.content)
            return target
        except Exception as exc:
            _log_failed_download(source, company_name, announcement_date, pdf_url, exc)
            logging.exception("Failed to download PDF: %s", pdf_url)
            return None


def _pdf_payload_validation_error(content: bytes) -> str:
    """Return why a downloaded PDF is incomplete, or an empty string when usable."""

    if not content.startswith(b"%PDF"):
        return "missing PDF header"
    linearized_length = re.search(rb"/Linearized\s+1(?:\.0)?\b.{0,160}?/L\s+(\d+)", content[:2048], re.DOTALL)
    if linearized_length:
        expected_length = int(linearized_length.group(1))
        if len(content) < expected_length:
            return f"linearized PDF declared {expected_length} bytes but received {len(content)}"
    eof_position = content.rfind(b"%%EOF")
    if eof_position < 0:
        return "missing PDF EOF marker"
    if len(content) - eof_position > 4096:
        return f"PDF EOF marker is not near the end of the {len(content)}-byte response"
    return ""


def _log_failed_download(source: str, company_name: str, announcement_date: date, pdf_url: str, exc: Exception) -> None:
    """Append permanent download failures to a CSV for manual review."""

    ensure_directories()
    path = Path("logs") / "failed_downloads.csv"
    write_header = not path.exists()
    status_code = ""
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = str(getattr(response, "status_code", ""))
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(["timestamp", "source", "company_name", "announcement_date", "pdf_url", "error_code", "error"])
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                source,
                company_name,
                announcement_date.isoformat(),
                pdf_url,
                status_code,
                str(exc),
            ]
        )


def compact_text(value: str) -> str:
    """Collapse repeated whitespace in extracted page or table text."""

    return re.sub(r"\s+", " ", value or "").strip()
