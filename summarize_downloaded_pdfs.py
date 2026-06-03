"""Summarize already-downloaded board meeting outcome PDFs into Excel."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from excel_writer import write_excel
from main import parse_pdf_with_timeout
from models import Announcement, FinancialData
from utils import ensure_directories, setup_logging


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for local PDF summarization."""

    parser = argparse.ArgumentParser(description="Summarize existing downloaded NSE/BSE PDFs.")
    parser.add_argument("--source", choices=("nse", "bse", "both"), default="both")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Run date for output/log names.")
    parser.add_argument("--pdf-timeout", type=int, default=120)
    parser.add_argument("--output", default="", help="Optional output .xlsx path.")
    parser.add_argument("--max-files", type=int, help="Optional limit for smoke tests.")
    parser.add_argument("--resume-cache", default="", help="Optional JSON checkpoint path for parsed local PDFs.")
    parser.add_argument("--no-resume", action="store_true", help="Ignore any existing local PDF checkpoint.")
    parser.add_argument("--flush-every", type=int, default=10, help="Write a partial workbook every N parsed PDFs.")
    return parser


def main() -> None:
    """Parse existing PDFs and write an all-PDF summary workbook."""

    args = build_parser().parse_args()
    run_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    ensure_directories()
    setup_logging(run_date)

    pdfs = _collect_pdfs(args.source)
    if args.max_files:
        pdfs = pdfs[: args.max_files]
    logging.info("Processing %s local PDFs.", len(pdfs))

    output = Path(args.output) if args.output else Path("output") / f"downloaded_pdf_summaries_{run_date.isoformat()}.xlsx"
    checkpoint_path = Path(args.resume_cache) if args.resume_cache else output.with_suffix(".checkpoint.json")
    checkpoint = {} if args.no_resume else _load_checkpoint(checkpoint_path)
    records: list[tuple[Announcement, FinancialData]] = []
    for index, pdf_path in enumerate(pdfs, start=1):
        source = pdf_path.parent.name.upper()
        company = _company_from_pdf_name(pdf_path)
        logging.info("Parsing %s/%s %s %s", index, len(pdfs), source, pdf_path.name)
        announcement = Announcement(
            source=source,
            company_name=company,
            identifier="",
            announcement_datetime=args.date,
            subject="Outcome of Board Meeting",
            pdf_url="",
            pdf_path=pdf_path,
        )
        record_key = _checkpoint_key(pdf_path)
        if record_key in checkpoint:
            financials = _financial_from_checkpoint(checkpoint[record_key])
            logging.info("Loaded checkpoint for %s", pdf_path)
        else:
            financials = parse_pdf_with_timeout(pdf_path, args.pdf_timeout)
            checkpoint[record_key] = _checkpoint_record(announcement, financials)
            _save_checkpoint(checkpoint_path, checkpoint)
        records.append((announcement, financials))
        if args.flush_every and index % args.flush_every == 0:
            write_excel(records, run_date, output)
            logging.info("Flushed partial workbook with %s records to %s", len(records), output)

    output_path = write_excel(records, run_date, output)
    accuracy = _overall_accuracy(records)
    logging.info("Wrote %s records to %s", len(records), output_path)
    logging.info("Estimated extraction accuracy/confidence: %.2f%%", accuracy)
    print(output_path)
    print(f"records={len(records)}")
    print(f"estimated_accuracy={accuracy:.2f}%")


def _collect_pdfs(source: str) -> list[Path]:
    """Collect local PDFs for the requested source."""

    folders = []
    if source in {"nse", "both"}:
        folders.append(Path("downloads") / "NSE")
    if source in {"bse", "both"}:
        folders.append(Path("downloads") / "BSE")
    pdfs: list[Path] = []
    for folder in folders:
        pdfs.extend(sorted(folder.glob("*.pdf"), key=lambda path: path.stat().st_mtime, reverse=True))
    return pdfs


def _company_from_pdf_name(pdf_path: Path) -> str:
    """Infer a company name from the local PDF filename."""

    stem = pdf_path.stem
    stem = stem.replace("_2026-05-15", "")
    stem = stem.replace("_2", "")
    return stem.replace("_", " ").strip()


def _checkpoint_key(pdf_path: Path) -> str:
    """Build a checkpoint key that changes when the local PDF changes."""

    stat = pdf_path.stat()
    return "|".join([str(pdf_path), str(stat.st_size), str(int(stat.st_mtime))])


def _checkpoint_record(announcement: Announcement, financials: FinancialData) -> dict[str, object]:
    """Serialize one parsed local PDF record for resumable corpus runs."""

    return {
        "announcement": asdict(announcement),
        "financials": asdict(financials),
    }


def _load_checkpoint(path: Path) -> dict[str, dict[str, object]]:
    """Load a local parsing checkpoint if it exists."""

    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        logging.exception("Could not load checkpoint %s; continuing without it.", path)
        return {}


def _save_checkpoint(path: Path, checkpoint: dict[str, dict[str, object]]) -> None:
    """Persist the local parsing checkpoint after each PDF."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(checkpoint, handle, default=str)
    temp_path.replace(path)


def _financial_from_checkpoint(record: dict[str, object]) -> FinancialData:
    """Deserialize FinancialData from a checkpoint record."""

    financials = record.get("financials", {}) if isinstance(record, dict) else {}
    if isinstance(financials, dict):
        return FinancialData(**financials)
    return FinancialData(parser_status="parse_error", parser_message="Invalid checkpoint record.")


def _overall_accuracy(records: list[tuple[Announcement, FinancialData]]) -> float:
    """Calculate average estimated extraction confidence across all records."""

    if not records:
        return 0.0
    from excel_writer import _estimated_accuracy

    return sum(_estimated_accuracy(financials) for _, financials in records) / len(records)


if __name__ == "__main__":
    main()
