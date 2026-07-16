"""BSE board meeting outcome scraper."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import async_playwright

from models import Announcement
from stealth import apply_stealth
from utils import BSE_BASE_URL, BSE_HEADERS, absolutize_url, exchange_date, request_with_retries

BSE_ANNOUNCEMENTS_URL = "https://www.bseindia.com/corporates/ann"
BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"


async def fetch_bse_announcements(run_date: date, scrip_code: str | None = None) -> list[Announcement]:
    """Fetch BSE board meeting outcome announcements for a given date."""

    try:
        announcements = await _fetch_bse_via_api(run_date, scrip_code)
        if announcements:
            return announcements
    except Exception:
        logging.exception("BSE API path failed; falling back to browser scraping.")
    return await _fetch_bse_via_browser(run_date)


async def _fetch_bse_via_api(run_date: date, scrip_code: str | None = None) -> list[Announcement]:
    """Fetch BSE announcements through the public JSON endpoint."""

    announcements: list[Announcement] = []
    async with httpx.AsyncClient(headers=BSE_HEADERS, follow_redirects=True, timeout=45) as client:
        await request_with_retries(client, "GET", BSE_BASE_URL, headers=BSE_HEADERS)
        for base_params in _bse_api_param_sets(run_date, scrip_code):
            candidate_announcements: list[Announcement] = []
            empty_pages = 0
            seen_page_signatures: set[tuple[str, ...]] = set()
            for page_no in range(1, 45):
                params = {**base_params, "pageno": str(page_no)}
                response = await request_with_retries(client, "GET", BSE_API_URL, params=params, headers=BSE_HEADERS)
                payload = response.json()
                rows = _extract_bse_rows(payload)
                if not rows:
                    break
                signature = _bse_page_signature(rows)
                if signature in seen_page_signatures:
                    break
                seen_page_signatures.add(signature)
                page_announcements = [_bse_row_to_announcement(row) for row in rows]
                matched = [item for item in page_announcements if item]
                candidate_announcements.extend(matched)
                empty_pages = empty_pages + 1 if not matched else 0
                if len(rows) < 50 or empty_pages >= 5:
                    break
            if candidate_announcements:
                announcements.extend(candidate_announcements)
    return _dedupe_announcements(announcements)


def _bse_page_signature(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """Build a duplicate-detection signature for a BSE API page."""

    signature: list[str] = []
    for row in rows:
        signature.append(
            "|".join(
                _first_value(row, key)
                for key in ("NEWSID", "SCRIP_CD", "NEWSSUB", "ATTACHMENTNAME", "DT_TM")
            )
        )
    return tuple(signature)


def _bse_api_param_sets(run_date: date, scrip_code: str | None = None) -> list[dict[str, str]]:
    """Return BSE API parameter variants for exact and broad announcement scans."""

    compact_date = run_date.strftime("%Y%m%d")
    slash_date = exchange_date(run_date, "/")
    common = {
        "strscrip": scrip_code or "",
        "strSearch": "P",
        "strType": "C",
    }
    return [
        {
            **common,
            "strCat": "Board Meeting",
            "subcategory": "Outcome of Board Meeting",
            "strPrevDate": compact_date,
            "strToDate": compact_date,
        },
        {
            **common,
            "strCat": "Board Meeting",
            "subcategory": "Outcome of Board Meeting",
            "strPrevDate": slash_date,
            "strToDate": slash_date,
        },
        {
            **common,
            "strCat": "Board Meeting",
            "subcategory": "-1",
            "strPrevDate": compact_date,
            "strToDate": compact_date,
        },
        {
            **common,
            "strCat": "-1",
            "subcategory": "-1",
            "strPrevDate": compact_date,
            "strToDate": compact_date,
        },
    ]


async def _fetch_bse_via_browser(run_date: date) -> list[Announcement]:
    """Scrape BSE announcements from the browser-rendered filtered page."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=BSE_HEADERS["User-Agent"],
            locale="en-US",
            extra_http_headers=BSE_HEADERS,
        )
        page = await context.new_page()
        await apply_stealth(page)
        captured: list[dict[str, Any]] = []

        async def capture_response(response: Any) -> None:
            """Capture BSE announcement JSON responses emitted by the page."""

            if not _is_bse_announcement_response_url(response.url):
                return
            try:
                payload = await response.json()
                captured.extend(_extract_bse_rows(payload))
            except Exception:
                logging.debug("Ignored non-JSON BSE response: %s", response.url)

        page.on("response", capture_response)
        await page.goto(BSE_ANNOUNCEMENTS_URL, wait_until="networkidle", timeout=90000)
        await _bse_apply_filters(page, run_date)
        await page.wait_for_timeout(5000)

        announcements = [_bse_row_to_announcement(row) for row in captured]
        announcements = [item for item in announcements if item]
        if announcements:
            await browser.close()
            return _dedupe_announcements(announcements)

        browser_announcements = await _parse_bse_dom_rows(page)
        await browser.close()
        return _dedupe_announcements(browser_announcements)


def _is_bse_announcement_response_url(url: str) -> bool:
    """Return whether a browser response is from a known BSE announcement API."""

    lowered = str(url or "").lower()
    return "annsubcategorygetdata" in lowered or "anngetdata" in lowered


async def _bse_apply_filters(page: Any, run_date: date) -> None:
    """Best-effort interaction with BSE date, category, and subcategory filters."""

    formatted = exchange_date(run_date, "/")
    for selector in ("input[ng-model='fromdate']", "#fromdate", "input[placeholder*='From']"):
        locator = page.locator(selector).first
        if await locator.count():
            try:
                await locator.fill(formatted)
                break
            except Exception:
                logging.debug("BSE from-date selector could not be filled: %s", selector)
    for selector in ("input[ng-model='todate']", "#todate", "input[placeholder*='To']"):
        locator = page.locator(selector).first
        if await locator.count():
            try:
                await locator.fill(formatted)
                break
            except Exception:
                logging.debug("BSE to-date selector could not be filled: %s", selector)

    await _select_option_by_text(page, "select", "Board Meeting")
    await _select_option_by_text(page, "select", "Outcome of Board Meeting")
    submit = page.locator("input[value='Submit'], button:has-text('Submit')").first
    if await submit.count():
        try:
            await submit.click()
        except Exception:
            logging.debug("BSE submit click failed.")


async def _select_option_by_text(page: Any, selector: str, text: str) -> None:
    """Select the first option containing the requested visible text."""

    selects = await page.locator(selector).all()
    for select in selects:
        try:
            options = await select.locator("option").all()
            for option in options:
                label = (await option.inner_text()).strip()
                value = await option.get_attribute("value")
                if text.lower() in label.lower() and value is not None:
                    await select.select_option(value=value)
                    return
        except Exception:
            continue


async def _parse_bse_dom_rows(page: Any) -> list[Announcement]:
    """Parse BSE announcements from visible DOM rows as a last resort."""

    rows = await page.locator("table tr, .TTRow, .ng-scope").all()
    announcements: list[Announcement] = []
    for row in rows:
        text = ""
        try:
            text = await row.inner_text()
        except Exception:
            continue
        if not _is_bse_board_meeting_outcome(text):
            continue
        pdf_url = ""
        pdf_link = row.locator("a[href*='.pdf'], a:has-text('PDF')").first
        if await pdf_link.count():
            pdf_url = absolutize_url(await pdf_link.get_attribute("href") or "", BSE_BASE_URL)
        title = compact_bse_title(text)
        company, code = split_bse_company_code(title)
        announcements.append(
            Announcement(
                source="BSE",
                company_name=company,
                identifier=code,
                announcement_datetime=_extract_bse_datetime(text),
                subject=title,
                pdf_url=pdf_url,
                details=text,
            )
        )
    return announcements


def _extract_bse_rows(payload: Any) -> list[dict[str, Any]]:
    """Extract row dictionaries from known BSE API payload shapes."""

    if isinstance(payload, list):
        return [row for row in payload if _looks_like_bse_announcement_row(row)]
    if isinstance(payload, dict):
        combined_rows: list[dict[str, Any]] = []
        for key in ("Table", "Table1", "data", "Data", "rows"):
            rows = payload.get(key)
            if isinstance(rows, list):
                combined_rows.extend(row for row in rows if _looks_like_bse_announcement_row(row))
        if combined_rows:
            return combined_rows
    return []


def _looks_like_bse_announcement_row(row: Any) -> bool:
    """Return whether a payload row has BSE announcement fields."""

    if not isinstance(row, dict):
        return False
    keys = {key.upper() for key in row}
    return bool(keys & {"NEWSID", "NEWSSUB", "SCRIP_CD", "ATTACHMENTNAME", "NEWS_DT", "DT_TM"})


def _bse_row_to_announcement(row: dict[str, Any]) -> Announcement | None:
    """Convert a BSE API row into a normalized announcement."""

    title = _first_value(row, "NEWSSUB", "News_Sub", "HEADLINE", "SLONGNAME", "SUBJECT")
    details = _strip_html(_first_value(row, "MORE", "NEWSBODY", "TEXT", "DETAILS"))
    combined = f"{title} {details}"
    if not _is_bse_board_meeting_outcome(combined):
        return None
    scrip_code = _first_value(row, "SCRIP_CD", "SCRIPCODE", "CODE", "scrip_cd")
    company = _first_value(row, "SLONGNAME", "CompanyName", "COMPANY_NAME", "company")
    if not company:
        company, code_from_title = split_bse_company_code(title)
        scrip_code = scrip_code or code_from_title
    attachment = _first_value(row, "ATTACHMENTNAME", "ATTACHMENT", "PDFURL", "NSURL")
    pdf_url = _bse_attachment_url(attachment)
    timestamp = _first_value(row, "NEWS_DT", "DissemDT", "DT_TM", "date_time", "time")
    return Announcement(
        source="BSE",
        company_name=company,
        identifier=scrip_code,
        announcement_datetime=timestamp,
        subject=title,
        pdf_url=pdf_url,
        details=details,
    )


def _bse_attachment_url(value: str) -> str:
    """Build an absolute BSE attachment URL from a row attachment value."""

    if not value:
        return ""
    if value.startswith("http"):
        return value
    if "/" in value:
        return absolutize_url(value, BSE_BASE_URL)
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{value}"


def _strip_html(value: str) -> str:
    """Remove simple HTML tags from BSE API text fields."""

    return re.sub(r"<[^>]+>", " ", value or "").strip()


def compact_bse_title(text: str) -> str:
    """Return the first meaningful line from a BSE result block."""

    for line in (text or "").splitlines():
        cleaned = line.strip()
        if _is_bse_board_meeting_outcome(cleaned):
            return cleaned
    return (text or "").splitlines()[0].strip() if text else ""


def _is_bse_board_meeting_outcome(text: str) -> bool:
    """Recognize both legacy and current BSE board-outcome subject wording."""

    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    return (
        "outcome of board meeting" in normalized
        or "board meeting outcome" in normalized
    )


def split_bse_company_code(title: str) -> tuple[str, str]:
    """Split BSE result titles into company name and scrip code when possible."""

    match = re.match(r"(.+?)\s*-\s*(\d{5,6})\s*-", title or "")
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return title or "", ""


def _extract_bse_datetime(text: str) -> str:
    """Extract exchange received or disseminated time from a BSE result block."""

    match = re.search(r"(?:Disseminated|Received)\s+Time\s+(\d{1,2}[-/]\d{1,2}[-/]\d{4}\s+\d{1,2}:\d{2}:\d{2})", text)
    return match.group(1) if match else ""


def _first_value(row: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string value from possible row keys."""

    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _dedupe_announcements(items: list[Announcement]) -> list[Announcement]:
    """Remove duplicate BSE rows based on identifier, timestamp, and PDF URL."""

    seen: set[tuple[str, str, str]] = set()
    deduped: list[Announcement] = []
    for item in items:
        key = (item.identifier, item.announcement_datetime, item.pdf_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def download_bse_pdf(announcement: Announcement, run_date: date) -> Path | None:
    """Download a BSE announcement PDF."""

    from utils import download_pdf

    return await download_pdf("BSE", announcement.company_name, run_date, announcement.pdf_url, BSE_HEADERS)
