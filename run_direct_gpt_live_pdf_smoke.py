"""Run fresh NSE/BSE PDFs through GPT-5.4 extraction with Telegram off."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from bse_scraper import download_bse_pdf, fetch_bse_announcements
from financial_pipeline import process_financial_pdf
from gpt54_extractor import _apply_verified_company_corrections
from image_generator import generate_financial_images
from models import Announcement
from nse_scraper import download_nse_pdf, fetch_nse_announcements


def _company_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


async def _download_pdfs(days_back: int, count: int, skip_companies: set[str] | None = None) -> tuple[list[tuple[object, Path, date]], list[str]]:
    notes: list[str] = []
    downloaded: list[tuple[object, Path, date]] = []
    seen: set[str] = set()
    seen_company_dates: set[str] = set()
    skip_companies = skip_companies or set()
    for offset in range(max(1, days_back)):
        run_date = date.today() - timedelta(days=offset)
        announcements = []
        try:
            nse = await fetch_nse_announcements(run_date)
            notes.append(f"{run_date} NSE={len(nse)}")
            announcements.extend(nse)
        except Exception as exc:
            notes.append(f"{run_date} NSE_ERROR={type(exc).__name__}")
        try:
            bse = await fetch_bse_announcements(run_date)
            notes.append(f"{run_date} BSE={len(bse)}")
            announcements.extend(bse)
        except Exception as exc:
            notes.append(f"{run_date} BSE_ERROR={type(exc).__name__}")

        for announcement in announcements[:60]:
            company_key = _company_key(getattr(announcement, "company_name", ""))
            if company_key in skip_companies:
                continue
            company_date_key = f"{company_key}|{run_date.isoformat()}"
            if company_date_key in seen_company_dates:
                notes.append(f"SKIP_DUPLICATE_COMPANY_DATE {announcement.source} {announcement.company_name}")
                continue
            seen_company_dates.add(company_date_key)
            key = "|".join(
                [
                    str(getattr(announcement, "source", "")),
                    str(getattr(announcement, "company_name", "")).lower(),
                    str(getattr(announcement, "pdf_url", "") or getattr(announcement, "announcement_id", "")),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            try:
                if announcement.source.upper() == "NSE":
                    pdf_path = await download_nse_pdf(announcement, run_date)
                else:
                    pdf_path = await download_bse_pdf(announcement, run_date)
            except Exception as exc:
                notes.append(f"DOWNLOAD_ERROR {announcement.source} {announcement.company_name}: {type(exc).__name__}")
                continue
            if pdf_path and Path(pdf_path).exists():
                announcement.pdf_path = Path(pdf_path)
                downloaded.append((announcement, Path(pdf_path), run_date))
                if len(downloaded) >= count:
                    return downloaded, notes
    if downloaded:
        return downloaded, notes
    raise RuntimeError("No fresh NSE/BSE PDF could be downloaded. " + "; ".join(notes[-8:]))


async def main() -> int:
    parser = argparse.ArgumentParser(description="No-Telegram direct GPT-5.4 live PDF smoke test.")
    parser.add_argument("--days-back", type=int, default=5)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--open-folder", action="store_true")
    parser.add_argument("--skip-company", action="append", default=[])
    parser.add_argument("--local-pdf", default="", help="Process an existing local PDF instead of downloading live PDFs.")
    parser.add_argument("--local-company", default="", help="Company name for --local-pdf.")
    parser.add_argument("--local-source", default="PDF", help="Source label for --local-pdf.")
    parser.add_argument("--local-date", default="", help="Announcement date for --local-pdf.")
    parser.add_argument(
        "--verified-local-only",
        action="store_true",
        help="Regenerate images from deterministic verified corrections without calling GPT.",
    )
    args = parser.parse_args()

    if load_dotenv is not None:
        load_dotenv(".env", override=True)

    output_root = Path(args.output_root or f"output/direct_gpt_live_pdf_smoke_{datetime.now():%Y%m%d_%H%M%S}")
    output_root.mkdir(parents=True, exist_ok=True)

    source_dir = output_root / "source_pdf"
    source_dir.mkdir(parents=True, exist_ok=True)

    if args.verified_local_only:
        if not args.local_pdf:
            raise RuntimeError("--verified-local-only requires --local-pdf")
        pdf_path = Path(args.local_pdf)
        company = args.local_company or pdf_path.stem.replace("_", " ")
        announcement_date = args.local_date or datetime.fromtimestamp(pdf_path.stat().st_mtime).strftime("%Y-%m-%d")
        announcement = Announcement(
            source=args.local_source,
            company_name=company,
            identifier=f"VERIFIED-LOCAL-{pdf_path.stem}",
            announcement_datetime=announcement_date,
            subject="Outcome of Board Meeting",
            pdf_url=f"local-verified://{pdf_path.as_posix()}",
            pdf_path=pdf_path,
        )
        source_copy = source_dir / f"01_{pdf_path.name}"
        shutil.copy2(pdf_path, source_copy)
        extraction = _apply_verified_company_corrections(
            {
                "company_name": company,
                "financial_rows": [],
                "balance_sheet_variables": [],
                "cash_flow_variables": [],
                "segment_tables": [],
                "warnings": [],
            }
        )
        generated = generate_financial_images(extraction, announcement, output_root)
        for folder in sorted({image.path.parent for image in generated.images}):
            shutil.copy2(pdf_path, folder / f"SOURCE_PDF__{pdf_path.name}")
        payload = {
            "output_root": str(output_root),
            "processed_count": 1,
            "results": [
                {
                    "company": company,
                    "source": args.local_source,
                    "downloaded_pdf": str(pdf_path),
                    "source_copy": str(source_copy),
                    "final_status": "PASS" if generated.images else "FAIL",
                    "gpt_json_status": "verified_local_no_api",
                    "image_status": f"ok:{len(generated.images)}" if generated.images else "failed:no_images",
                    "telegram_status": f"mock_sent:{len(generated.images)}",
                    "warnings": generated.warnings,
                    "missing_sections": generated.missing_sections,
                }
            ],
            "images": [str(image.path) for image in generated.images],
            "source_pdfs_in_output": [str(path) for path in output_root.rglob("SOURCE_PDF__*.pdf")],
            "validation_reports": [str(path) for path in output_root.rglob("VALIDATION_REPORT.json")],
            "fetch_notes": ["verified_local_only:no_gpt_api_call"],
        }
        print(json.dumps(payload, indent=2))
        if args.open_folder:
            os.startfile(output_root.resolve())  # type: ignore[attr-defined]
        return 0 if generated.images else 1

    skip_companies = {_company_key(value) for value in args.skip_company if str(value).strip()}
    downloaded, notes = await _download_pdfs(args.days_back, max(1, args.count), skip_companies)
    results = []
    for index, (announcement, pdf_path, run_date) in enumerate(downloaded, start=1):
        source_copy = source_dir / f"{index:02d}_{pdf_path.name}"
        shutil.copy2(pdf_path, source_copy)
        result = process_financial_pdf(
            pdf_path,
            announcement,
            output_root=output_root,
            mock_apis=False,
            send_telegram=False,
            telegram_sender=None,
        )
        results.append(
            {
                "company": announcement.company_name,
                "source": announcement.source,
                "run_date_used": str(run_date),
                "downloaded_pdf": str(pdf_path),
                "source_copy": str(source_copy),
                "final_status": result.final_status,
                "ocr_status": result.ocr_status,
                "gpt_json_status": result.gpt_json_status,
                "validation_status": result.validation_status,
                "image_status": result.image_status,
                "telegram_status": result.telegram_status,
                "error_reason": result.error_reason,
            }
        )
    images = [str(path) for path in output_root.rglob("*.png")]
    reports = [str(path) for path in output_root.rglob("VALIDATION_REPORT.json")]
    source_pdfs = [str(path) for path in output_root.rglob("SOURCE_PDF__*.pdf")]
    payload = {
        "output_root": str(output_root),
        "processed_count": len(results),
        "results": results,
        "images": images,
        "source_pdfs_in_output": source_pdfs,
        "validation_reports": reports,
        "fetch_notes": notes,
    }
    print(json.dumps(payload, indent=2))
    if args.open_folder:
        os.startfile(output_root.resolve())  # type: ignore[attr-defined]
    return 0 if all(item["final_status"] in {"PASS", "NO_DATA"} for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
