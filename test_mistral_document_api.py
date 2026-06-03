"""Smoke-test Mistral/Azure Foundry document extraction on local PDFs."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from models import Announcement
from mistral_parser import extract_with_mistral, format_mistral_output, has_mistral_financial_data, mistral_confidence

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dependency is required by the app.
    load_dotenv = None


def main() -> None:
    """Run Mistral document extraction for N local PDFs and write an audit JSON."""

    if load_dotenv is not None:
        load_dotenv()
    logging.disable(logging.ERROR)
    limit = int(os.environ.get("MISTRAL_TEST_LIMIT", "10"))
    offset = int(os.environ.get("MISTRAL_TEST_OFFSET", "0"))
    all_pdfs = sorted(Path("downloads").rglob("*.pdf"))
    pdfs = all_pdfs[offset : offset + limit]
    if not pdfs:
        raise SystemExit("No PDFs found under downloads/.")

    results = []
    started = datetime.now()
    for index, pdf_path in enumerate(pdfs, start=1):
        source = _source_from_path(pdf_path)
        company = pdf_path.stem
        announcement = Announcement(
            source=source,
            company_name=company,
            identifier=company,
            announcement_datetime="",
            subject="Mistral document API smoke test",
            pdf_url="",
            pdf_path=pdf_path,
        )
        print(f"[{index}/{len(pdfs)}] Testing {source} {pdf_path.name} ...", flush=True)
        item_started = time.perf_counter()
        extraction = extract_with_mistral(pdf_path, announcement)
        messages = format_mistral_output(extraction, announcement)
        duration = round(time.perf_counter() - item_started, 2)
        financial_rows = extraction.get("financial_rows") if isinstance(extraction.get("financial_rows"), list) else []
        segment_tables = extraction.get("segment_tables") if isinstance(extraction.get("segment_tables"), list) else []
        balance_sections = (
            extraction.get("balance_sheet_variables")
            if isinstance(extraction.get("balance_sheet_variables"), list)
            else []
        )
        result = {
            "pdf": str(pdf_path),
            "source": source,
            "parser_status": extraction.get("parser_status", ""),
            "parser_message": str(extraction.get("parser_message", ""))[:300],
            "confidence": mistral_confidence(extraction),
            "has_financial_data": has_mistral_financial_data(extraction),
            "financial_row_count": len(financial_rows),
            "segment_table_count": len(segment_tables),
            "balance_sheet_section_count": len(balance_sections),
            "telegram_message_count": len(messages),
            "duration_seconds": duration,
        }
        results.append(result)
        print(
            "    status={parser_status} confidence={confidence}% rows={financial_row_count} "
            "messages={telegram_message_count} duration={duration_seconds}s".format(**result),
            flush=True,
        )

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"mistral_document_api_test_{started.strftime('%Y-%m-%d_%H-%M-%S')}.json"
    output_path.write_text(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "limit": limit,
                "offset": offset,
                "total_local_pdfs": len(all_pdfs),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    successes = sum(1 for item in results if item["parser_status"] == "parsed_mistral")
    with_data = sum(1 for item in results if item["has_financial_data"])
    avg_confidence = round(sum(float(item["confidence"]) for item in results) / len(results), 2)
    print(f"RESULT_FILE={output_path}")
    print(f"SUMMARY tested={len(results)} parsed_mistral={successes} with_financial_data={with_data} avg_confidence={avg_confidence}%")


def _source_from_path(path: Path) -> str:
    """Infer exchange source from a local downloads path."""

    parts = {part.upper() for part in path.parts}
    if "NSE" in parts:
        return "NSE"
    if "BSE" in parts:
        return "BSE"
    return "LOCAL"


if __name__ == "__main__":
    main()
