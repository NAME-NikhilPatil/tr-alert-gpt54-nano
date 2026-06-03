"""NSE board meeting outcome scraper."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import async_playwright

from models import Announcement
from stealth import apply_stealth
from utils import NSE_BASE_URL, NSE_HEADERS, absolutize_url, exchange_date, request_with_retries

NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_API_URL = "https://www.nseindia.com/api/corporate-announcements"


async def fetch_nse_announcements(run_date: date) -> list[Announcement]:
    """Fetch NSE announcements for Outcome of Board Meeting on a given date."""

    try:
        announcements = await _fetch_nse_via_api(run_date)
        if announcements:
            return announcements
    except Exception:
        logging.exception("NSE API path failed; falling back to browser scraping.")
    try:
        return await _fetch_nse_via_browser(run_date)
    except Exception:
        logging.exception("NSE browser path failed.")
        return []


async def _fetch_nse_via_api(run_date: date) -> list[Announcement]:
    """Fetch NSE corporate announcements through the JSON endpoint."""

    async with httpx.AsyncClient(headers=NSE_HEADERS, follow_redirects=True, timeout=45) as client:
        await request_with_retries(client, "GET", NSE_BASE_URL, headers=NSE_HEADERS)
        for params in _nse_api_param_sets(run_date):
            response = await request_with_retries(client, "GET", NSE_API_URL, params=params, headers=NSE_HEADERS)
            try:
                payload = response.json()
            except ValueError:
                logging.warning("NSE API returned non-JSON content for params=%s", params)
                continue
            rows = _extract_nse_rows(payload)
            logging.info("NSE API returned %s rows for params=%s", len(rows), params)
            if rows:
                logging.debug("NSE first row keys: %s", sorted(rows[0].keys()))
                logging.debug("NSE first row sample: %s", _sample_nse_row(rows[0]))
            announcements = [_nse_row_to_announcement(row) for row in rows]
            matched = [item for item in announcements if item and _is_outcome_subject(item.subject)]
            if matched:
                return _dedupe_announcements(matched)
    return []


def _sample_nse_row(row: dict[str, Any]) -> dict[str, str]:
    """Return a compact diagnostic sample for an NSE row."""

    sample: dict[str, str] = {}
    for key in sorted(row.keys())[:20]:
        value = row.get(key)
        text = str(value).strip()
        if text:
            sample[key] = text[:120]
    return sample


def _nse_api_param_sets(run_date: date) -> list[dict[str, str]]:
    """Return NSE API parameter variants used by the public announcements page."""

    dashed = exchange_date(run_date)
    slashed = exchange_date(run_date, "/")
    return [
        {"index": "equities", "from_date": dashed, "to_date": dashed},
        {"index": "equities", "from_date": slashed, "to_date": slashed},
        {"index": "all", "from_date": dashed, "to_date": dashed},
        {"from_date": dashed, "to_date": dashed},
    ]


def _extract_nse_rows(payload: Any) -> list[dict[str, Any]]:
    """Extract row dictionaries from known NSE API payload shapes."""

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "Table"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


async def _fetch_nse_via_browser(run_date: date) -> list[Announcement]:
    """Scrape NSE announcements from the browser-rendered table."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=NSE_HEADERS["User-Agent"],
            locale="en-US",
            extra_http_headers=NSE_HEADERS,
        )
        page = await context.new_page()
        await apply_stealth(page)
        captured: list[dict[str, Any]] = []

        async def capture_response(response: Any) -> None:
            """Capture NSE announcement JSON responses emitted by the page."""

            if "corporate-announcements" not in response.url:
                return
            try:
                payload = await response.json()
                rows = payload if isinstance(payload, list) else payload.get("data", [])
                captured.extend(rows)
            except Exception:
                logging.debug("Ignored non-JSON NSE response: %s", response.url)

        page.on("response", capture_response)
        await page.goto(NSE_ANNOUNCEMENTS_URL, wait_until="networkidle", timeout=90000)
        await _nse_set_date_filters(page, run_date)
        await page.wait_for_timeout(5000)

        announcements = [_nse_row_to_announcement(row) for row in captured]
        announcements = [item for item in announcements if item and _is_outcome_subject(item.subject)]
        if announcements:
            await browser.close()
            return _dedupe_announcements(announcements)

        table_rows = await page.locator("table tbody tr").all()
        browser_announcements: list[Announcement] = []
        for row in table_rows:
            cells = [cell.strip() for cell in await row.locator("td").all_inner_texts()]
            if len(cells) < 6 or not _is_outcome_subject(" ".join(cells)):
                continue
            pdf_url = ""
            pdf_link = row.locator("a[href*='.pdf'], a:has-text('PDF')").first
            if await pdf_link.count():
                pdf_url = absolutize_url(await pdf_link.get_attribute("href") or "", NSE_BASE_URL)
            browser_announcements.append(
                Announcement(
                    source="NSE",
                    company_name=cells[1],
                    identifier=cells[0],
                    subject=cells[2],
                    details=cells[3] if len(cells) > 3 else "",
                    pdf_url=pdf_url,
                    announcement_datetime=cells[-1],
                )
            )
        await browser.close()
        return _dedupe_announcements(browser_announcements)


async def _nse_set_date_filters(page: Any, run_date: date) -> None:
    """Best-effort interaction with NSE date controls and subject search."""

    formatted = exchange_date(run_date)
    selectors = [
        "input[placeholder*='From']",
        "input[aria-label*='From']",
        "input[name*='from']",
        "input[placeholder*='To']",
        "input[aria-label*='To']",
        "input[name*='to']",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count():
            try:
                await locator.fill(formatted)
            except Exception:
                logging.debug("NSE date selector could not be filled: %s", selector)
    keyword = page.locator("input[placeholder*='Keyword'], input[placeholder*='Subject']").first
    if await keyword.count():
        try:
            await keyword.fill("Outcome of Board Meeting")
        except Exception:
            logging.debug("NSE keyword selector could not be filled.")
    refresh = page.locator("text=Refresh").first
    if await refresh.count():
        try:
            await refresh.click()
        except Exception:
            logging.debug("NSE refresh click failed.")


def _nse_row_to_announcement(row: dict[str, Any]) -> Announcement | None:
    """Convert an NSE API row into a normalized announcement."""

    if not isinstance(row, dict):
        return None
    subject = _first_value(row, "subject", "desc", "attchmntText", "sm_name")
    if not _is_outcome_subject(subject):
        return None
    company = _first_value(row, "companyName", "sm_name", "company", "compName", "name")
    symbol = _first_value(row, "symbol", "sym", "sm_symbol")
    timestamp = _first_value(row, "broadcastDate", "broadcast_date", "dateTime", "an_dt", "dt_tm", "disseminationTime")
    attachment = _first_value(row, "attchmntFile", "attachment", "file", "pdf", "ATTACHMENT")
    details = _first_value(row, "attchmntText", "details", "desc", "sm_desc")
    pdf_url = absolutize_url(attachment, "https://nsearchives.nseindia.com/")
    return Announcement(
        source="NSE",
        company_name=company,
        identifier=symbol,
        announcement_datetime=timestamp,
        subject=subject,
        pdf_url=pdf_url,
        details=details,
    )


def _first_value(row: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string value from possible row keys."""

    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        text = str(value).strip()
        if text:
            return text
    return ""


def _is_outcome_subject(value: str) -> bool:
    """Check whether text refers to an Outcome of Board Meeting announcement."""

    lowered = (value or "").lower()
    return "outcome" in lowered and "board meeting" in lowered


def _dedupe_announcements(items: list[Announcement]) -> list[Announcement]:
    """Remove duplicate NSE rows based on identifier, timestamp, and PDF URL."""

    seen: set[tuple[str, str, str]] = set()
    deduped: list[Announcement] = []
    for item in items:
        key = (item.identifier, item.announcement_datetime, item.pdf_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def download_nse_pdf(announcement: Announcement, run_date: date) -> Path | None:
    """Download an NSE announcement PDF."""

    from utils import download_pdf

    return await download_pdf("NSE", announcement.company_name, run_date, announcement.pdf_url, NSE_HEADERS)
