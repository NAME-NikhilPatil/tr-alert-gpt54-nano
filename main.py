"""Command-line orchestration for the board meeting outcome scraper."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from datetime import date
from datetime import datetime
from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from typing import Any
from zoneinfo import ZoneInfo

from bse_scraper import download_bse_pdf, fetch_bse_announcements
from db_manager import deactivate_telegram_subscriber
from db_manager import get_active_telegram_chat_ids
from db_manager import get_telegram_state
from db_manager import init_seen_db
from db_manager import is_seen
from db_manager import mark_processed
from db_manager import reserve_seen
from db_manager import seed_telegram_subscribers
from db_manager import set_telegram_state
from db_manager import upsert_telegram_subscriber
from excel_writer import write_excel
from excel_writer import _estimated_accuracy
from logger import DailySummary, log_daily_summary, setup_live_logging
from live_debugger import LiveDebugger
from gpt54_extractor import extract_pdf_with_gpt54
from gpt54_extractor import gpt54_is_configured
from image_generator import GeneratedFinancialImages
from image_generator import generate_financial_images
from mistral_parser import format_mistral_output
from mistral_parser import mistral_confidence
from models import Announcement, FinancialData
from nse_scraper import download_nse_pdf, fetch_nse_announcements
from pdf_job_queue import DONE
from pdf_job_queue import SKIPPED_NON_FINANCIAL_DISCLOSURE
from pdf_job_queue import enqueue_pdf_job
from pdf_job_queue import init_pdf_job_db
from pdf_job_queue import job_to_announcement
from pdf_job_queue import log_pdf_job_event
from pdf_job_queue import queue_counts
from pdf_job_queue import reset_processing_jobs
from pdf_job_queue import update_job_local_path
from pdf_job_worker import PdfJobRuntime
from pdf_job_worker import PdfJobWorkerConfig
from pdf_job_worker import PdfJobWorkerPool
from pdf_job_worker import RetryablePdfJobError
from pdf_parser import parse_pdf
from telegram_sender import TelegramSender
from utils import (
    cache_key,
    financial_from_cache,
    get_cached_announcement,
    save_cached_announcement,
    ensure_directories,
    normalize_date,
    parse_cli_date,
    setup_logging,
)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - handled at runtime when dependency is missing.
    load_dotenv = None

DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 75
EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""

    parser = argparse.ArgumentParser(description="Scrape NSE/BSE Outcome of Board Meeting PDFs into Excel.")
    parser.add_argument("--date", help="Announcement date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--days", type=int, default=1, help="Number of days ending at --date to scrape. Defaults to 1.")
    parser.add_argument("--source", choices=("nse", "bse", "both"), default="both", help="Exchange source to scrape.")
    parser.add_argument("--limit", type=int, help="Process only the latest N matching announcements per source.")
    parser.add_argument("--output", help="Optional output .xlsx path.")
    parser.add_argument("--company", help="Filter matching announcements by company-name substring.")
    parser.add_argument("--identifier", help="Filter by NSE symbol or BSE scrip code.")
    parser.add_argument(
        "--require-financial-data",
        action="store_true",
        help="Continue through announcements until the output limit has rows with extracted numeric financial data.",
    )
    parser.add_argument(
        "--pdf-timeout",
        type=int,
        default=DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
        help="Maximum seconds to spend parsing each PDF. Defaults to 75.",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable SQLite download/extraction cache.")
    parser.add_argument("--no-dedupe", action="store_true", help="Disable NSE/BSE duplicate merging.")
    return parser


async def run_scraper(
    run_date: date,
    source: str,
    pdf_timeout: int = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    limit: int | None = None,
    require_financial_data: bool = False,
    company_filter: str | None = None,
    identifier_filter: str | None = None,
    use_cache: bool = True,
    dedupe: bool = True,
) -> list[tuple[Announcement, FinancialData]]:
    """Run selected exchange scrapers, download PDFs, parse them, and return records."""

    ensure_directories()
    announcements: list[Announcement] = []
    if source in {"nse", "both"}:
        nse_items = await fetch_nse_announcements(run_date)
        nse_items = _filter_announcements(nse_items, company_filter, identifier_filter)
        logging.info("Fetched %s NSE matching announcements.", len(nse_items))
        announcements.extend(nse_items if require_financial_data else (nse_items[:limit] if limit else nse_items))
    if source in {"bse", "both"}:
        bse_items = await fetch_bse_announcements(run_date, identifier_filter if source == "bse" else None)
        bse_items = _filter_announcements(bse_items, company_filter, identifier_filter)
        logging.info("Fetched %s BSE matching announcements.", len(bse_items))
        announcements.extend(bse_items if require_financial_data else (bse_items[:limit] if limit else bse_items))

    records: list[tuple[Announcement, FinancialData]] = []
    max_records = _expected_record_limit(source, limit)
    for announcement in announcements:
        try:
            key = cache_key(
                announcement.source,
                announcement.company_name,
                announcement.identifier,
                run_date,
                announcement.pdf_url,
            )
            if use_cache:
                cached = get_cached_announcement(key)
                cached_path = Path(str(cached.get("pdf_path", ""))) if cached else None
                cached_financials = financial_from_cache(str(cached.get("financial_json", ""))) if cached else None
                cached_confidence = float(cached.get("confidence", 0) or 0) if cached else 0
                if cached_path and cached_path.exists():
                    announcement.pdf_path = cached_path
                if cached_financials and cached_confidence >= 70:
                    logging.info("Using high-confidence cached extraction for %s %s.", announcement.source, announcement.company_name)
                    records.append((announcement, cached_financials))
                    if max_records and len(records) >= max_records:
                        break
                    continue
            if not announcement.pdf_path:
                if announcement.source.upper() == "NSE":
                    announcement.pdf_path = await download_nse_pdf(announcement, run_date)
                else:
                    announcement.pdf_path = await download_bse_pdf(announcement, run_date)
            financials = parse_pdf_with_timeout(announcement.pdf_path, pdf_timeout)
            if use_cache:
                save_cached_announcement(
                    key,
                    announcement.source,
                    announcement.company_name,
                    announcement.identifier,
                    run_date,
                    announcement.pdf_url,
                    announcement.pdf_path,
                    _estimated_accuracy(financials),
                    financials,
                )
            if require_financial_data and not _has_extracted_financial_values(financials):
                logging.info(
                    "Continuing past %s %s: no extracted numeric financial data.",
                    announcement.source,
                    announcement.company_name,
                )
                continue
            records.append((announcement, financials))
            if max_records and len(records) >= max_records:
                break
        except Exception as exc:
            logging.exception("Failed to process announcement: %s", announcement)
            records.append(
                (
                    announcement,
                    FinancialData(parser_status="processing_error", parser_message=str(exc)),
                )
            )
    return _dedupe_records(records) if dedupe and source == "both" else records


async def async_main() -> None:
    """Parse CLI arguments and execute the scraper workflow."""

    args = build_parser().parse_args()
    run_date = parse_cli_date(args.date)
    log_path = setup_logging(run_date)
    logging.info("Starting scraper for source=%s date=%s days=%s", args.source, run_date.isoformat(), args.days)
    records: list[tuple[Announcement, FinancialData]] = []
    for offset in range(max(args.days, 1)):
        day = run_date - timedelta(days=offset)
        records.extend(
            await run_scraper(
                day,
                args.source,
                args.pdf_timeout,
                args.limit,
                args.require_financial_data,
                args.company,
                args.identifier,
                not args.no_cache,
                not args.no_dedupe,
            )
        )
    if not args.no_dedupe and args.source == "both":
        records = _dedupe_records(records)
    output_path = write_excel(records, run_date, Path(args.output) if args.output else None)
    logging.info("Wrote %s records to %s", len(records), output_path)
    logging.info("Log file: %s", log_path)


def scraper_main() -> None:
    """Run the one-shot scraper CLI entrypoint."""

    asyncio.run(async_main())


def main() -> None:
    """Run the live Telegram polling loop for `python main.py`."""

    run_live_polling_loop()


def run_live_polling_loop() -> None:
    """Poll NSE/BSE every configured interval and send new PDFs to Telegram."""

    _load_environment()
    _configure_runtime_data_dir()
    _apply_live_pipeline_env_defaults()
    log_path = setup_live_logging()
    ensure_directories()
    Path("output/excel").mkdir(parents=True, exist_ok=True)
    Path("downloads/nse").mkdir(parents=True, exist_ok=True)
    Path("downloads/bse").mkdir(parents=True, exist_ok=True)
    init_seen_db()
    init_pdf_job_db()
    reset_count = reset_processing_jobs()
    if reset_count:
        logging.info("Requeued %s PDF job(s) left PROCESSING by a previous shutdown.", reset_count)

    poll_interval = int(os.environ.get("SCRAPER_POLL_INTERVAL_SECONDS") or os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    max_workers = int(os.environ.get("MAX_CONCURRENT_PDF_JOBS", "1"))
    pdfs_per_request = int(os.environ.get("PDFS_PER_GPT_REQUEST", "1"))
    retry_limit = int(os.environ.get("PDF_JOB_RETRY_LIMIT", "1"))
    telegram_enabled = _truthy_env("TELEGRAM_ENABLED", True) and not _truthy_env("DRY_RUN", False)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    seed_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if telegram_enabled and not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set in .env")
    if not gpt54_is_configured():
        raise RuntimeError("GPT54_RESPONSES_URL and GPT54_API_KEY must be set in .env for GPT-5.4 PDF extraction")
    if telegram_enabled and seed_chat_ids:
        seeded = seed_telegram_subscribers(seed_chat_ids)
        if seeded:
            logging.info("Seeded %s Telegram subscriber(s) from environment.", seeded)
    _read_agents_state()

    sender = TelegramSender(bot_token, get_active_telegram_chat_ids()) if telegram_enabled else None
    if sender:
        _sync_telegram_subscribers(sender)
        sender.set_chat_ids(get_active_telegram_chat_ids())
    summary = DailySummary()
    debugger = LiveDebugger.from_env()
    worker_pool = PdfJobWorkerPool(
        process_job=lambda job, runtime: _process_live_pdf_job(job, runtime, sender, debugger),
        config=PdfJobWorkerConfig(
            max_concurrent_pdf_jobs=max_workers,
            pdfs_per_gpt_request=pdfs_per_request,
            retry_limit=retry_limit,
            heartbeat_interval_seconds=60.0,
        ),
    )
    worker_pool.start()
    logging.info("Starting live Telegram scraper. Log file: %s", log_path)
    logging.info(
        "PDF worker pool started max_concurrent_pdf_jobs=%s pdfs_per_gpt_request=%s retry_limit=%s",
        max_workers,
        pdfs_per_request,
        retry_limit,
    )
    logging.info("Active Telegram subscribers: %s", len(sender.chat_ids) if sender else 0)
    if sender and sender.chat_ids:
        replay_sent = _send_startup_result_replay(sender, summary)
        if replay_sent:
            logging.info("Startup result replay sent %s Telegram message(s).", replay_sent)
        sender.send_text(_startup_message_plain(poll_interval))
    elif telegram_enabled:
        logging.warning("No active Telegram subscribers yet. Users must press /start in the bot.")
    else:
        logging.info("Telegram disabled by TELEGRAM_ENABLED/DRY_RUN; live scraper will enqueue/process without sending.")

    try:
        while True:
            try:
                if sender:
                    changed_subscribers = _sync_telegram_subscribers(sender)
                    if changed_subscribers:
                        sender.set_chat_ids(get_active_telegram_chat_ids())
                        logging.info("Active Telegram subscribers refreshed: %s", len(sender.chat_ids))
                    sent_from_queue = sender.drain_queue() if sender.chat_ids else 0
                    summary.telegram_messages_sent += sent_from_queue
                discovered, queued = asyncio.run(_poll_once(summary, debugger))
                counts = queue_counts()
                logging.info(
                    "Live poll complete: discovered=%s queued=%s active_gpt_jobs_count=%s queued_jobs_count=%s done_count=%s failed_count=%s skipped_count=%s",
                    discovered,
                    queued,
                    worker_pool.active_count(),
                    counts.queued,
                    counts.done,
                    counts.failed,
                    counts.skipped,
                )
                log_daily_summary(summary)
            except KeyboardInterrupt:
                logging.info("Live polling stopped by user.")
                raise
            except Exception:
                logging.exception("Live scraper loop crashed. Telegram error alert suppressed.")
            finally:
                time.sleep(poll_interval)
    finally:
        worker_pool.stop(wait=False)


def _load_environment() -> None:
    """Load live scraper configuration from .env."""

    if load_dotenv is None:
        raise RuntimeError("python-dotenv is not installed. Run: pip install python-dotenv")
    env_path = Path(__file__).resolve().with_name(".env")
    load_dotenv(dotenv_path=env_path, override=True, encoding="utf-8-sig")


def _configure_runtime_data_dir() -> Path:
    """Move all relative runtime state under the configured persistent data root."""

    configured = os.environ.get("TR_ALERT_DATA_DIR", "").strip()
    if not configured:
        return Path.cwd()
    data_dir = Path(configured).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(data_dir)
    return data_dir


def _apply_live_pipeline_env_defaults() -> None:
    """Apply queue/GPT defaults without changing existing extraction mode."""

    os.environ.setdefault("MAX_CONCURRENT_PDF_JOBS", "1")
    os.environ.setdefault("PDFS_PER_GPT_REQUEST", "1")
    os.environ.setdefault("GPT_MODEL", os.environ.get("GPT54_MODEL", "gpt-5.4-nano"))
    os.environ.setdefault("GPT54_MODEL", os.environ.get("GPT_MODEL", "gpt-5.4-nano"))
    os.environ.setdefault("PRIMARY_MODEL", os.environ.get("GPT54_MODEL", "gpt-5.4-nano"))
    os.environ.setdefault("GPT_REQUEST_TIMEOUT_SECONDS", os.environ.get("GPT54_TIMEOUT_SECONDS", "1800"))
    os.environ.setdefault("GPT54_TIMEOUT_SECONDS", os.environ.get("GPT_REQUEST_TIMEOUT_SECONDS", "1800"))
    os.environ.setdefault("GPT54_COMPLEX_TIMEOUT_SECONDS", "2700")
    os.environ.setdefault("GPT54_HTTP_RETRIES", "1")
    os.environ.setdefault("GPT54_BACKGROUND_MODE", "true")
    os.environ.setdefault("GPT54_BACKGROUND_POLL_SECONDS", "5")
    os.environ.setdefault("GPT54_MAX_OUTPUT_TOKENS", "48000")
    os.environ.setdefault("GPT54_DEFAULT_REASONING_EFFORT", "high")
    os.environ.setdefault("GPT54_COMPLEX_REASONING_EFFORT", "high")
    os.environ.setdefault("GPT54_USE_XHIGH_FOR_COMPLEX", "false")
    os.environ.setdefault("GPT54_RETRY_XHIGH_ON_VALUES_FIRST_WARNINGS", "false")
    os.environ.setdefault("SCRAPER_POLL_INTERVAL_SECONDS", os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    os.environ.setdefault("POLL_INTERVAL_SECONDS", os.environ.get("SCRAPER_POLL_INTERVAL_SECONDS", "60"))
    os.environ.setdefault("PDF_JOB_RETRY_LIMIT", "1")
    os.environ.setdefault("DRY_RUN", "false")
    os.environ.setdefault("TELEGRAM_ENABLED", os.environ.get("LIVE_TELEGRAM_SEND", "true"))
    if os.environ.get("PDFS_PER_GPT_REQUEST") != "1":
        raise RuntimeError("PDFS_PER_GPT_REQUEST must be 1; multiple PDFs per GPT request are not allowed.")


def _truthy_env(name: str, default: bool = False) -> bool:
    """Return a boolean environment flag."""

    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _read_agents_state() -> None:
    """Read AGENTS.md on startup so live runs have the latest handoff context in logs."""

    agents_path = Path("AGENTS.md")
    if not agents_path.exists():
        logging.warning("AGENTS.md was not found at startup.")
        return
    text = agents_path.read_text(encoding="utf-8", errors="replace")
    logging.info("Read AGENTS.md at startup (%s characters).", len(text))


def _startup_message_plain(poll_interval: int) -> str:
    """Return startup text using plain ASCII."""

    return f"Scraper is LIVE. Monitoring NSE + BSE every {poll_interval} seconds."


def _sync_telegram_subscribers(sender: TelegramSender) -> int:
    """Process Telegram /start and /stop updates into the local subscriber table."""

    offset_text = get_telegram_state("telegram_update_offset", "")
    try:
        offset = int(offset_text) if offset_text else None
    except ValueError:
        offset = None
    updates = sender.get_updates(offset)
    if not updates:
        return 0

    changed = 0
    max_update_id: int | None = None
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = str(chat.get("id", "")).strip()
        if not chat_id:
            continue
        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if command in {"/start", "/subscribe"}:
            was_new = upsert_telegram_subscriber(
                chat_id,
                str(chat.get("username", "") or ""),
                str(chat.get("first_name", "") or ""),
                str(chat.get("last_name", "") or ""),
            )
            changed += 1
            welcome = (
                "Subscribed. You will receive live NSE/BSE Outcome of Board Meeting alerts here.\n"
                "Send /stop anytime to unsubscribe."
            )
            sender.send_text_to_chat(chat_id, welcome)
            logging.info("Telegram subscriber %s %s.", chat_id, "added" if was_new else "refreshed")
        elif command in {"/stop", "/unsubscribe"}:
            deactivate_telegram_subscriber(chat_id)
            changed += 1
            sender.send_text_to_chat(chat_id, "Unsubscribed. Send /start anytime to receive alerts again.")
            logging.info("Telegram subscriber %s deactivated.", chat_id)
        elif command in {"/help", "/status"}:
            sender.send_text_to_chat(chat_id, _telegram_help_text())

    if max_update_id is not None:
        set_telegram_state("telegram_update_offset", str(max_update_id + 1))
    return changed


def _telegram_help_text() -> str:
    """Return subscriber command help."""

    return (
        "Send /start to subscribe to live NSE/BSE board meeting outcome alerts.\n"
        "Send /stop to unsubscribe."
    )


def _command_payload(text: str) -> str:
    """Return text after the command word."""

    parts = str(text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _poll_once(
    summary: DailySummary,
    debugger: LiveDebugger | None = None,
) -> tuple[int, int]:
    """Run one live polling pass and enqueue discovered PDFs only."""

    today = _exchange_today()
    announcements: list[Announcement] = []
    nse_items, bse_items = await _fetch_exchange_announcements(today)
    raw_counts = {"NSE": len(nse_items), "BSE": len(bse_items)}
    announcements.extend(nse_items)
    announcements.extend(bse_items)
    announcements = _dedupe_announcements(announcements)
    stale_live_count = 0
    if _strict_live_announcement_date_enabled():
        before_live_filter = len(announcements)
        announcements = _filter_live_announcements(announcements, today)
        stale_live_count = before_live_filter - len(announcements)
    deduped_count = len(announcements)
    logging.info(
        "Fetched live announcements: raw_nse=%s raw_bse=%s deduped=%s stale_skipped=%s.",
        raw_counts["NSE"],
        raw_counts["BSE"],
        deduped_count,
        stale_live_count,
    )
    if debugger:
        debugger.record_poll_start(announcements, raw_counts=raw_counts)

    discovered_count = 0
    queued_count = 0
    for announcement in announcements:
        try:
            if is_seen(announcement):
                log_pdf_job_event(
                    "PDF_ALREADY_EXISTS",
                    status="already_seen",
                    extra={
                        "company_name": announcement.company_name,
                        "pdf_url": announcement.pdf_url,
                        "exchange": announcement.source,
                    },
                )
                logging.info("PDF skipped, already seen: %s %s", announcement.source, announcement.company_name)
                if debugger:
                    debugger.record_skip(announcement, "already_seen")
                continue
            job, inserted = enqueue_pdf_job(announcement)
            discovered_count += 1
            if inserted:
                queued_count += 1
                reserve_seen(announcement)
                summary.processed += 1
                if job:
                    log_pdf_job_event("PDF_DISCOVERED", job=job, status="DISCOVERED")
                    log_pdf_job_event("PDF_QUEUED", job=job, status="QUEUED")
                logging.info("PDF queued for worker processing: %s %s", announcement.source, announcement.company_name)
            else:
                if job:
                    log_pdf_job_event("PDF_ALREADY_EXISTS", job=job, status=job.status)
                logging.info("PDF skipped, already queued/processed: %s %s", announcement.source, announcement.company_name)
                if debugger:
                    debugger.record_skip(announcement, "already_queued")
        except Exception as exc:
            logging.exception("Failed to enqueue live PDF for %s %s", announcement.source, announcement.company_name)
            summary.failed_pdfs.append(f"{announcement.source} {announcement.company_name}: {exc}")
    if debugger:
        debugger.record_poll_complete(
            processed=queued_count,
            telegram_messages_sent=0,
            average_confidence=0.0,
        )
    return discovered_count, queued_count


async def _fetch_exchange_announcements(run_date: date) -> tuple[list[Announcement], list[Announcement]]:
    """Fetch both exchanges concurrently while isolating a failure on either side."""

    results = await asyncio.gather(
        fetch_nse_announcements(run_date),
        fetch_bse_announcements(run_date),
        return_exceptions=True,
    )
    items: list[list[Announcement]] = []
    for source, result in zip(("NSE", "BSE"), results):
        if isinstance(result, BaseException):
            logging.error("%s announcement discovery failed; the other exchange will continue: %s", source, result)
            items.append([])
        else:
            items.append(result)
    return items[0], items[1]


def _process_live_pdf_job(
    job: Any,
    runtime: PdfJobRuntime,
    sender: TelegramSender | None,
    debugger: LiveDebugger | None = None,
) -> str:
    """Process exactly one queued PDF job in one worker."""

    item_start = time.perf_counter()
    announcement = job_to_announcement(job)
    timings: dict[str, float] = {}
    extraction: dict[str, Any] = {}
    try:
        if not _queued_job_is_current(announcement):
            runtime.log_event("PDF_PROCESSING_SKIPPED_STALE_DATE", status="PROCESSING")
            logging.info(
                "Skipping queued announcement outside current India date: %s %s announcement_datetime=%s expected=%s",
                announcement.source,
                announcement.company_name,
                announcement.announcement_datetime,
                _exchange_today().isoformat(),
            )
            mark_processed(announcement)
            if debugger:
                debugger.record_skip(announcement, "stale_queued_announcement_date")
            return DONE
        if not announcement.pdf_path or not Path(announcement.pdf_path).exists():
            step_start = time.perf_counter()
            run_date = _job_run_date(announcement)
            if announcement.source.upper() == "NSE":
                announcement.pdf_path = asyncio.run(download_nse_pdf(announcement, run_date))
            else:
                announcement.pdf_path = asyncio.run(download_bse_pdf(announcement, run_date))
            if not announcement.pdf_path:
                raise RuntimeError("PDF download returned no local path.")
            update_job_local_path(job.id, announcement.pdf_path)
            timings["download"] = round(time.perf_counter() - step_start, 3)

        step_start = time.perf_counter()
        runtime.log_event("GPT_REQUEST_STARTED", status="PROCESSING", pdfs_per_gpt_request=1)
        extraction = extract_pdf_with_gpt54(announcement.pdf_path, announcement)
        timings["gpt54_pdf"] = round(time.perf_counter() - step_start, 3)
        runtime.log_event("GPT_REQUEST_FINISHED", status="PROCESSING", elapsed_seconds=timings["gpt54_pdf"], pdfs_per_gpt_request=1)
        extraction_failure = _gpt_extraction_failure_reason(extraction)
        if extraction_failure:
            runtime.log_event(
                "GPT_REQUEST_FAILED",
                status="PROCESSING",
                elapsed_seconds=timings["gpt54_pdf"],
                error=extraction_failure[:500],
            )
            error_type = RetryablePdfJobError if _is_retryable_gpt_transport_failure(extraction_failure) else RuntimeError
            raise error_type(f"GPT extraction failed: {extraction_failure[:500]}")
        extracted_at = datetime.now()

        if not _extraction_date_matches_live_run(extraction, announcement, _job_run_date(announcement)):
            timings["total"] = round(time.perf_counter() - item_start, 3)
            mark_processed(announcement)
            if debugger:
                debugger.record_skip(announcement, "non_live_pdf_date")
            return DONE

        confidence_score = mistral_confidence(extraction)
        step_start = time.perf_counter()
        runtime.log_event("IMAGE_RENDER_STARTED", status="PROCESSING")
        generated_images = generate_financial_images(extraction, announcement)
        image_paths = generated_images.paths
        timings["render"] = round(time.perf_counter() - step_start, 3)
        runtime.log_event(
            "IMAGE_RENDER_FINISHED",
            status="PROCESSING",
            elapsed_seconds=timings["render"],
            images_generated=len(image_paths),
        )

        step_start = time.perf_counter()
        runtime.log_event("TELEGRAM_SEND_STARTED", status="PROCESSING", telegram_enabled=sender is not None)
        sent = _send_live_extraction_output(sender, extraction, announcement, extracted_at, generated_images)
        timings["send"] = round(time.perf_counter() - step_start, 3)
        runtime.log_event(
            "TELEGRAM_SEND_FINISHED",
            status="PROCESSING",
            elapsed_seconds=timings["send"],
            telegram_messages_sent=sent,
            telegram_enabled=sender is not None,
        )

        timings["total"] = round(time.perf_counter() - item_start, 3)
        if debugger:
            debugger.record_processed(
                announcement=announcement,
                extraction=extraction,
                confidence_score=float(confidence_score),
                image_paths=image_paths,
                telegram_messages_sent=sent,
                timings=timings,
            )
        if confidence_score < 70:
            logging.warning("Low confidence extraction %.2f for %s", confidence_score, announcement.company_name)
        mark_processed(announcement)
        if str(extraction.get("status") or extraction.get("parser_status") or "") == SKIPPED_NON_FINANCIAL_DISCLOSURE:
            return SKIPPED_NON_FINANCIAL_DISCLOSURE
        return DONE
    except Exception as exc:
        timings["total"] = round(time.perf_counter() - item_start, 3)
        if debugger:
            debugger.record_error(announcement=announcement, error=exc, timings=timings)
        retry_limit = max(0, int(os.environ.get("PDF_JOB_RETRY_LIMIT", "1")))
        retry_scheduled = (
            isinstance(exc, RetryablePdfJobError)
            and int(getattr(job, "attempt_count", 1) or 1) <= retry_limit
        )
        logging.exception(
            "PDF processing failed for %s %s; detailed error stays in logs retry_scheduled=%s generic_notice_deferred=%s.",
            announcement.source,
            announcement.company_name,
            retry_scheduled,
            retry_scheduled,
        )
        notice_sent = False
        if sender is not None and not retry_scheduled:
            try:
                notice_sent = bool(sender.send_text(_no_financial_data_message(extraction, announcement)))
            except Exception:
                logging.exception(
                    "Failed to send generic no-data Telegram message for %s %s.",
                    announcement.source,
                    announcement.company_name,
                )
        runtime.log_event(
            "TELEGRAM_GENERIC_FAILURE_NOTICE",
            status="PROCESSING",
            telegram_enabled=sender is not None,
            telegram_message_sent=notice_sent,
            retry_scheduled=retry_scheduled,
        )
        raise


def _send_live_extraction_output(
    sender: TelegramSender | None,
    extraction: dict[str, Any],
    announcement: Announcement,
    extracted_at: datetime,
    generated_images: GeneratedFinancialImages,
) -> int:
    """Send Telegram output for one processed PDF, or no-op when Telegram is disabled."""

    if sender is None:
        return 0
    image_paths = generated_images.paths
    if image_paths:
        sent = 0
        if sender.send_text(_mistral_image_intro_message(extraction, announcement, extracted_at, generated_images)):
            sent += 1
        sent += _send_generated_financial_images(sender, generated_images, announcement)
        return sent
    logging.warning(
        "No financial image produced for %s; Telegram detail suppressed. parser_status=%s validation_errors=%s warnings=%s missing_sections=%s",
        announcement.company_name,
        extraction.get("parser_status") or extraction.get("status") or "",
        extraction.get("validation_errors") or [],
        generated_images.warnings,
        generated_images.missing_sections,
    )
    return 1 if sender.send_text(_no_financial_data_message(extraction, announcement)) else 0


def _job_run_date(announcement: Announcement) -> date:
    """Return the download date for a queued announcement."""

    return _parse_date_value(announcement.announcement_datetime) or _exchange_today()


def _exchange_today(instant: datetime | None = None) -> date:
    """Return the current NSE/BSE calendar date in India."""

    if instant is None:
        return datetime.now(EXCHANGE_TIMEZONE).date()
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(EXCHANGE_TIMEZONE).date()


def _queued_job_is_current(announcement: Announcement, current_date: date | None = None) -> bool:
    """Return whether a persisted live job still belongs to today's India date."""

    if not _strict_live_announcement_date_enabled():
        return True
    announcement_date = _parse_date_value(announcement.announcement_datetime)
    return announcement_date is None or announcement_date == (current_date or _exchange_today())


def _send_startup_result_replay(sender: TelegramSender, summary: DailySummary) -> int:
    """Send one-time refreshed normal outputs for the latest local PDFs."""

    replay_key = "startup_result_replay_latest25_client_feedback_v1"
    if os.environ.get("STARTUP_RESULT_REPLAY", "0").strip().lower() in {"0", "false", "no", "off"}:
        return 0
    if get_telegram_state(replay_key):
        return 0

    announcements = _startup_replay_announcements(_startup_replay_count())
    if not announcements:
        logging.info("Startup result replay skipped because no local PDFs were found.")
        set_telegram_state(replay_key, datetime.now().isoformat(timespec="seconds"))
        return 0

    sender.send_text(
        f"Startup smoke replay: sending {len(announcements)} latest local PDFs "
        "for client feedback, then normal live polling will continue."
    )
    sent_total = 0
    for index, announcement in enumerate(announcements, start=1):
        if not announcement.pdf_path or not announcement.pdf_path.exists():
            logging.warning("Startup result replay PDF missing: %s", announcement.pdf_path)
            continue
        try:
            sender.send_text(f"Startup smoke replay {index}/{len(announcements)}: {announcement.company_name}")
            sent_total += _send_local_mistral_result(sender, announcement)
        except Exception as exc:
            logging.exception("Startup result replay failed for %s. Telegram error alert suppressed.", announcement.company_name)
    if sent_total:
        summary.telegram_messages_sent += sent_total
    set_telegram_state(replay_key, datetime.now().isoformat(timespec="seconds"))
    return sent_total


def _startup_replay_count() -> int:
    """Return how many latest local PDFs should be replayed on startup."""

    try:
        return max(0, int(os.environ.get("STARTUP_RESULT_REPLAY_COUNT", "25")))
    except ValueError:
        return 25


def _startup_replay_announcements(limit: int) -> list[Announcement]:
    """Return latest local PDF announcements, deduped by company/date."""

    if limit <= 0:
        return []
    pdfs = [path for path in Path("downloads").rglob("*.pdf") if path.is_file()]
    output: list[Announcement] = []
    seen_company_dates: set[tuple[str, str]] = set()
    for pdf_path in sorted(pdfs, key=lambda item: item.stat().st_mtime, reverse=True):
        announcement = _announcement_from_local_pdf(pdf_path, len(output) + 1)
        company_date_key = (
            _normalize_company_key(announcement.company_name),
            _announcement_date_key(announcement.announcement_datetime),
        )
        if company_date_key in seen_company_dates:
            continue
        seen_company_dates.add(company_date_key)
        output.append(announcement)
        if len(output) >= limit:
            break
    return output


def _announcement_from_local_pdf(pdf_path: Path, index: int) -> Announcement:
    """Build a local replay announcement for a downloaded PDF."""

    source = "BSE" if any(part.lower() == "bse" for part in pdf_path.parts) else "NSE"
    stem = re.sub(r"_\d+$", "", pdf_path.stem)
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", stem)
    announced_at = date_match.group(1) if date_match else datetime.fromtimestamp(pdf_path.stat().st_mtime).strftime("%Y-%m-%d")
    company_stem = stem[: date_match.start()].rstrip("_") if date_match else stem
    company = company_stem.replace("_", " ").strip() or pdf_path.stem
    return Announcement(
        source=source,
        company_name=company,
        identifier=f"STARTUP-REPLAY-{index}-{pdf_path.stem}",
        announcement_datetime=announced_at,
        subject="Outcome of Board Meeting",
        pdf_url=f"local-startup-replay://{pdf_path.as_posix()}",
        pdf_path=pdf_path,
    )


def _send_local_mistral_result(sender: TelegramSender, announcement: Announcement) -> int:
    """Extract, render, and send one local announcement using the normal live output format."""

    extraction = extract_pdf_with_gpt54(announcement.pdf_path, announcement)
    generated_images = generate_financial_images(extraction, announcement)
    extracted_at = datetime.now()
    return _send_live_extraction_output(sender, extraction, announcement, extracted_at, generated_images)


def _send_rendered_images(
    sender: TelegramSender,
    image_paths: list[Path],
    announcement: Announcement,
    confidence_score: float,
) -> int:
    """Send Excel-style rendered PNG images and return successful image count."""

    sent = 0
    for index, image_path in enumerate(image_paths, start=1):
        caption = (
            f"{announcement.company_name}\n"
            f"Source: {announcement.source.upper()}"
            if index == 1
            else ""
        )
        if sender.send_photo(image_path, caption):
            sent += 1
    return sent


def _send_generated_financial_images(
    sender: TelegramSender,
    generated_images: GeneratedFinancialImages,
    announcement: Announcement,
) -> int:
    """Send generated financial photos; warnings stay in logs only."""

    sent = 0
    if generated_images.warnings:
        logging.warning(
            "Telegram generated-image warning text suppressed for %s: %s",
            announcement.company_name,
            generated_images.warnings,
        )
    for image in generated_images.images:
        caption = image.caption
        if sender.send_photo(image.path, caption):
            sent += 1
    return sent


def _mistral_image_intro_message(
    extraction: dict[str, Any],
    announcement: Announcement,
    extracted_at: datetime,
    generated_images: GeneratedFinancialImages,
) -> str:
    """Return a short metadata message before available financial images."""

    company = str(extraction.get("company_name") or announcement.company_name)
    source = str(extraction.get("source") or announcement.source.upper())
    meeting_date = str(extraction.get("board_meeting_date") or normalize_date(announcement.announcement_datetime) or "")
    attached = _image_sections_text(generated_images)
    return "\n".join(
        [
            company,
            f"Date: {meeting_date}",
            f"Source: {source}",
            attached,
            f"Extracted At: {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}",
        ]
    )


def _image_sections_text(generated_images: GeneratedFinancialImages) -> str:
    """Return the generated section list for the Telegram intro message."""

    section_by_kind = {
        "pnl": "P&L",
        "bs_cf": "Balance Sheet + Cash Flow",
        "segments": "Segment Performance",
    }
    attached = [section_by_kind.get(image.kind, image.kind) for image in generated_images.images]
    if not attached:
        return "Financial images attached: none."
    return "Financial images attached: " + ", ".join(attached) + "."


def _is_non_financial_skip(extraction: dict[str, Any]) -> bool:
    """Return true when the PDF was classified as a non-financial disclosure."""

    return str(extraction.get("status") or extraction.get("parser_status") or "") == SKIPPED_NON_FINANCIAL_DISCLOSURE


def _gpt_extraction_failure_reason(extraction: dict[str, Any]) -> str:
    """Return the detailed internal GPT failure reason that must stay in logs."""

    if _is_non_financial_skip(extraction):
        return ""
    if str(extraction.get("gpt_json_status") or "").strip().lower() != "failed":
        return ""
    message = str(extraction.get("parser_message") or "").strip()
    if not message:
        warnings = extraction.get("warnings") if isinstance(extraction.get("warnings"), list) else []
        message = str(warnings[0] if warnings else "").strip()
    return message or str(extraction.get("parser_status") or "GPT extraction returned no valid JSON")


def _is_retryable_gpt_transport_failure(message: str) -> bool:
    """Return true only for short-lived upstream/network failures, not long read timeouts."""

    normalized = str(message or "").strip().lower()
    if not normalized or "timed out" in normalized or "timeout" in normalized:
        return False
    retryable_markers = (
        "http 502",
        "http 503",
        "http 504",
        "status_code=502",
        "status_code=503",
        "status_code=504",
        "connection termination",
        "connection reset",
        "forcibly closed by the remote host",
        "remote host reset",
        "getaddrinfo failed",
        "name resolution",
        "temporary failure in name resolution",
    )
    return any(marker in normalized for marker in retryable_markers)


def _no_financial_data_message(extraction: dict[str, Any], announcement: Announcement) -> str:
    """Return the Telegram message for PDFs without financial data."""

    company = str(extraction.get("company_name") or announcement.company_name)
    source = str(extraction.get("source") or announcement.source.upper()).upper()
    return "\n".join(
        [
            company,
            f"Source: {source}",
            "Financial data is not available in the PDF.",
        ]
    )


def _send_formatted_messages(sender: TelegramSender, messages: list[str]) -> int:
    """Send formatted Mistral text messages and return successful message count."""

    sent = 0
    for message in messages:
        if sender.send_text(message):
            sent += 1
    return sent


def _dedupe_announcements(announcements: list[Announcement]) -> list[Announcement]:
    """Deduplicate exact attachments without dropping distinct same-company PDFs."""

    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    deduped: list[Announcement] = []
    for announcement in announcements:
        normalized_url = str(announcement.pdf_url or "").strip().lower()
        if normalized_url and normalized_url in seen_urls:
            continue
        key = "|".join(
            [
                announcement.source.upper(),
                announcement.identifier.lower(),
                announcement.company_name.lower(),
                announcement.pdf_url,
            ]
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if normalized_url:
            seen_urls.add(normalized_url)
        deduped.append(announcement)
    return deduped


def _strict_live_announcement_date_enabled() -> bool:
    """Return whether live polling should reject announcements outside today's date."""

    value = os.environ.get("STRICT_LIVE_ANNOUNCEMENT_DATE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _filter_live_announcements(announcements: list[Announcement], run_date: date) -> list[Announcement]:
    """Keep only announcements whose exchange timestamp belongs to the live poll date."""

    filtered: list[Announcement] = []
    for announcement in announcements:
        parsed = _parse_date_value(announcement.announcement_datetime)
        if parsed is not None and parsed != run_date:
            logging.info(
                "Skipping stale exchange announcement: %s %s announcement_datetime=%s expected=%s",
                announcement.source,
                announcement.company_name,
                announcement.announcement_datetime,
                run_date.isoformat(),
            )
            continue
        filtered.append(announcement)
    return filtered


def _extraction_date_matches_live_run(
    extraction: dict[str, Any],
    announcement: Announcement,
    run_date: date,
) -> bool:
    """Reject PDFs whose extracted board/result date is not the current live date."""

    if not _strict_live_announcement_date_enabled():
        return True
    date_text = str(
        extraction.get("board_meeting_date")
        or extraction.get("announcement_date")
        or ""
    ).strip()
    parsed = _parse_date_value(date_text)
    if parsed is None:
        return True
    if parsed == run_date:
        return True
    logging.warning(
        "Skipping non-live PDF date: %s %s pdf_date=%s expected=%s",
        announcement.source,
        announcement.company_name,
        date_text,
        run_date.isoformat(),
    )
    return False


def _parse_date_value(value: str) -> date | None:
    """Parse common exchange/PDF date text into a date."""

    text = str(value or "").strip()
    if not text:
        return None
    iso_match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text)
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    normalized = normalize_date(text)
    for candidate in (normalized, text[:10]):
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _announcement_date_key(value: str) -> str:
    """Return a coarse date key from an exchange announcement datetime string."""

    normalized = normalize_date(value)
    if normalized:
        return normalized
    return str(value or "")[:10]


def _telegram_summary_message(
    announcement: Announcement,
    financials: FinancialData,
    extracted_at: datetime,
    confidence_score: int,
) -> str:
    """Format one Telegram result summary."""

    has_financial_data = _has_telegram_financial_data(financials)
    lines = [
        f"*{_md_escape(announcement.company_name)}*",
        f"Date: {_md_escape(financials.meeting_date or normalize_date(announcement.announcement_datetime))}",
        f"Source: {_md_escape(announcement.source.upper())}",
        "----------------",
    ]
    if has_financial_data:
        lines.append("Financial result data extracted.")
        lines.append("Excel attachment contains the full Result Summary.")
    else:
        return "\n".join(
            [
                f"*{_md_escape(announcement.company_name)}*",
                f"Source: {_md_escape(announcement.source.upper())}",
                "Financial data is not available in the PDF.",
            ]
        )
    if financials.dividend_declared or financials.dividend_per_share or financials.dividend:
        lines.append(f"Dividend Declared: {_md_escape(financials.dividend_declared or '')}")
        lines.append(f"Dividend Per Share: {_md_escape(financials.dividend_per_share or '')}")
    lines.extend(
        [
            "----------------",
            f"Extracted At: {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}",
        ]
    )
    return "\n".join(lines)[:3900]


def _summary_rows_for_message(financials: FinancialData) -> list[str]:
    """Return compact extracted financial rows for Telegram."""

    from excel_writer import DEFAULT_PERIOD_COLUMNS, TARGET_METRICS, _build_normalized_summary, _change_percent

    normalized = _build_normalized_summary(financials)
    rows: list[str] = []
    for metric in TARGET_METRICS:
        values = normalized.get(metric, {})
        q4 = values.get("Q4 FY26", "")
        q3 = values.get("Q3 FY26", "")
        q4_prev = values.get("Q4 FY25", "")
        fy = values.get("FY26", "")
        fy_prev = values.get("FY25", "")
        if not any((q4, q3, q4_prev, fy, fy_prev)):
            continue
        change_qoq = "" if "Margin" in metric else _change_percent(q4, q3)
        change_yoy = "" if "Margin" in metric else _change_percent(q4, q4_prev)
        change_fy = "" if "Margin" in metric else _change_percent(fy, fy_prev)
        rows.append(
            f"{_md_escape(metric)}: Q4 {q4 or '-'} | Q3 {q3 or '-'} | QoQ {change_qoq or '-'} | "
            f"Q4PY {q4_prev or '-'} | YoY {change_yoy or '-'} | FY {fy or '-'} | FYPY {fy_prev or '-'} | FY% {change_fy or '-'}"
        )
    return rows


def _has_telegram_financial_data(financials: FinancialData) -> bool:
    """Return whether the alert has enough financial values to justify an Excel attachment."""

    return bool(_summary_rows_for_message(financials))


def _md_escape(value: object) -> str:
    """Escape minimal Telegram Markdown-sensitive characters."""

    text = str(value or "")
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def parse_pdf_with_timeout(pdf_path: Path | None, timeout_seconds: int) -> FinancialData:
    """Parse a PDF in a child process and return a timeout status if it stalls."""

    if not pdf_path:
        return parse_pdf(None)
    context = get_context("spawn")
    queue = context.Queue(maxsize=1)
    process = context.Process(target=_parse_pdf_worker, args=(str(pdf_path), queue))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        return FinancialData(
            parser_status="parse_timeout",
            parser_message=f"PDF parsing exceeded {timeout_seconds} seconds.",
        )
    try:
        result = queue.get(timeout=2)
    except Empty:
        return FinancialData(
            parser_status="parse_error",
            parser_message=f"PDF parser exited with code {process.exitcode} without returning data.",
        )
    if isinstance(result, FinancialData):
        return result
    return FinancialData(parser_status="parse_error", parser_message=str(result))


def _filter_announcements(
    announcements: list[Announcement],
    company_filter: str | None,
    identifier_filter: str | None,
) -> list[Announcement]:
    """Filter announcements by company name and/or exchange identifier."""

    filtered = announcements
    if company_filter:
        needle = company_filter.lower()
        filtered = [item for item in filtered if needle in item.company_name.lower()]
    if identifier_filter:
        needle = identifier_filter.lower()
        filtered = [item for item in filtered if needle == item.identifier.lower()]
    return filtered


def _expected_record_limit(source: str, limit: int | None) -> int | None:
    """Return total output rows expected for a per-source limit."""

    if not limit:
        return None
    return limit * (2 if source == "both" else 1)


def _has_extracted_financial_values(financials: FinancialData) -> bool:
    """Return whether a parsed PDF produced numeric financial statement values."""

    return any(values for values in financials.rows.values())


def _dedupe_records(records: list[tuple[Announcement, FinancialData]]) -> list[tuple[Announcement, FinancialData]]:
    """Merge NSE/BSE duplicate company-date outcomes, keeping higher field coverage."""

    grouped: dict[tuple[str, str], tuple[Announcement, FinancialData]] = {}
    sources: dict[tuple[str, str], set[str]] = {}
    for announcement, financials in records:
        company_key = _normalize_company_key(announcement.company_name)
        meeting_date = financials.meeting_date or announcement.announcement_datetime[:10]
        key = (company_key, meeting_date)
        sources.setdefault(key, set()).add(announcement.source.upper())
        existing = grouped.get(key)
        if not existing or _coverage_score(financials) > _coverage_score(existing[1]):
            grouped[key] = (announcement, financials)
    merged: list[tuple[Announcement, FinancialData]] = []
    for key, (announcement, financials) in grouped.items():
        source_set = sources.get(key, {announcement.source.upper()})
        if len(source_set) > 1:
            announcement.source = "+".join(sorted(source_set))
        merged.append((announcement, financials))
    return merged


def _normalize_company_key(value: str) -> str:
    """Normalize company names for duplicate detection."""

    cleaned = value.lower()
    cleaned = re.sub(r"\b(limited|ltd|ltd\.|industries|industry)\b", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
    return cleaned


def _coverage_score(financials: FinancialData) -> int:
    """Return a simple field coverage score for dedupe decisions."""

    return sum(len(values) for values in financials.rows.values())


def _parse_pdf_worker(pdf_path: str, queue: Any) -> None:
    """Child-process worker for guarded PDF parsing."""

    try:
        queue.put(parse_pdf(Path(pdf_path)))
    except Exception as exc:
        queue.put(FinancialData(parser_status="parse_error", parser_message=str(exc)))


if __name__ == "__main__":
    main()
