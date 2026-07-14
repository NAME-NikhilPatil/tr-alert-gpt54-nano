"""NSE board meeting outcome scraper."""

from __future__ import annotations

import json
import logging
import random
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
NSE_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
NSE_EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.nseindia.com/",
}
NSE_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-http2",
    "--ignore-certificate-errors",
]


async def fetch_nse_announcements(run_date: date) -> list[Announcement]:
    """Fetch NSE announcements for Outcome of Board Meeting on a given date."""

    try:
        announcements = await _fetch_nse_via_api(run_date)
        return announcements
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 403:
            logging.warning("NSE API path returned HTTP 403; falling back to browser scraping.")
        else:
            logging.exception("NSE API path failed with HTTP %s; falling back to browser scraping.", status)
    except Exception:
        logging.exception("NSE API path failed; falling back to browser scraping.")
    try:
        return await _fetch_nse_via_browser(run_date)
    except Exception as exc:
        logging.error("NSE browser path failed after retries; returning no NSE announcements: %s", exc)
        return []


async def _fetch_nse_via_api(run_date: date) -> list[Announcement]:
    """Fetch NSE corporate announcements through the JSON endpoint."""

    headers = _nse_request_headers(accept_json=True)
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=45) as client:
        try:
            await request_with_retries(client, "GET", NSE_BASE_URL, headers=headers)
        except httpx.HTTPError as exc:
            logging.warning(
                "NSE homepage warm-up failed (%s); trying the public announcements API directly.",
                exc,
            )
        for params in _nse_api_param_sets(run_date):
            response = await request_with_retries(client, "GET", NSE_API_URL, params=params, headers=headers)
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
        browser = await playwright.chromium.launch(headless=True, args=NSE_CHROMIUM_ARGS)
        try:
            context = await browser.new_context(
                user_agent=NSE_CHROME_USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="Asia/Kolkata",
                accept_downloads=True,
                ignore_https_errors=True,
                extra_http_headers=_nse_request_headers(),
            )
            page = await context.new_page()
            await apply_stealth(page)
            await _install_nse_resource_blocking(page)
            captured: list[dict[str, Any]] = []

            async def capture_response(response: Any) -> None:
                """Capture NSE announcement JSON responses emitted by the page."""

                if "corporate-announcements" not in response.url:
                    return
                try:
                    payload = await response.json()
                    rows = payload if isinstance(payload, list) else payload.get("data", [])
                    if isinstance(rows, list):
                        captured.extend(row for row in rows if isinstance(row, dict))
                except Exception:
                    logging.debug("Ignored non-JSON NSE response: %s", response.url)

            page.on("response", capture_response)
            await _warm_nse_home_page(page)
            try:
                await safe_goto_nse(page, NSE_ANNOUNCEMENTS_URL, timeout=90000)
            except Exception as exc:
                logging.warning("NSE announcements page navigation failed; retrying via home page first: %s", exc)
                await _warm_nse_home_page(page)
                await safe_goto_nse(page, NSE_ANNOUNCEMENTS_URL, timeout=90000)
            await _nse_human_delay(page, 1800, 3200)
            await _nse_set_date_filters(page, run_date)
            await _nse_human_delay(page, 3500, 5500)

            announcements = [_nse_row_to_announcement(row) for row in captured]
            announcements = [item for item in announcements if item and _is_outcome_subject(item.subject)]
            if announcements:
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
            return _dedupe_announcements(browser_announcements)
        finally:
            await browser.close()


async def safe_goto_nse(page: Any, url: str, timeout: int) -> Any:
    """Navigate to an NSE page using staged waits that work better in containers."""

    last_error: Exception | None = None
    for wait_until in ("domcontentloaded", "load", "commit"):
        try:
            logging.info("NSE browser goto url=%s wait_until=%s", url, wait_until)
            return await page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            last_error = exc
            logging.warning("NSE browser goto failed url=%s wait_until=%s error=%s", url, wait_until, exc)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"NSE browser goto failed without captured error: {url}")


async def _warm_nse_home_page(page: Any) -> None:
    """Load NSE home page once to establish cookies before the announcements page."""

    await safe_goto_nse(page, NSE_BASE_URL, timeout=60000)
    await _nse_human_delay(page, 3000, 4200)


async def _nse_human_delay(page: Any, minimum_ms: int, maximum_ms: int) -> None:
    """Sleep for a small randomized delay without blocking the event loop."""

    await page.wait_for_timeout(random.randint(minimum_ms, maximum_ms))


async def _install_nse_resource_blocking(page: Any) -> None:
    """Block heavyweight resources while preserving scripts and XHR/fetch."""

    async def route_handler(route: Any) -> None:
        resource_type = route.request.resource_type
        if resource_type in {"image", "media", "font"}:
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", route_handler)


def _nse_request_headers(*, accept_json: bool = False) -> dict[str, str]:
    """Return NSE request headers suitable for Azure/container traffic."""

    headers = dict(NSE_HEADERS)
    headers.update(NSE_EXTRA_HEADERS)
    headers["User-Agent"] = NSE_CHROME_USER_AGENT
    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
    return headers


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
