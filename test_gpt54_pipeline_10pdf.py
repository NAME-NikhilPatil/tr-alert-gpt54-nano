"""10-PDF smoke runner for the OCR -> GPT-5.4 -> validation -> image pipeline.

Default behavior is safe: if live API credentials are missing, the runner uses
mock OCR/GPT/Telegram stages and labels the result as mock-only. It never sends
real Telegram messages unless --send-telegram is explicitly passed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from gpt54_extractor import gpt54_is_configured
from financial_pipeline import FinancialPipelineResult
from financial_pipeline import process_financial_pdf
from financial_pipeline import announcement_from_pdf
from telegram_sender import TelegramSender


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a 10-PDF GPT-5.4 financial pipeline smoke test.")
    parser.add_argument("--pdf", action="append", default=[], help="Specific PDF path. Can be passed multiple times.")
    parser.add_argument("--downloads-root", default="downloads", help="Folder to scan when --pdf is not provided.")
    parser.add_argument("--limit", type=int, default=10, help="Number of PDFs to process.")
    parser.add_argument("--offset", type=int, default=0, help="Offset into sorted PDF list.")
    parser.add_argument("--mock-apis", action="store_true", help="Force mock OCR and GPT stages.")
    parser.add_argument("--send-telegram", action="store_true", help="Actually send Telegram photos if credentials are configured.")
    parser.add_argument("--output-root", default="output/gpt54_pipeline_test_images", help="Image output root.")
    args = parser.parse_args()

    if load_dotenv is not None:
        load_dotenv()

    pdfs = _selected_pdfs(args)
    if not pdfs:
        print("No PDFs found.")
        return 1

    live_ready = _live_api_ready()
    use_mock = args.mock_apis or not live_ready
    if use_mock:
        missing = []
        if not os.environ.get("MISTRAL_API_KEY", "").strip():
            missing.append("MISTRAL_API_KEY")
        if not gpt54_is_configured():
            missing.append("GPT54_RESPONSES_URL/GPT54_API_KEY")
        reason = f"mock-only: missing {', '.join(missing)}" if missing else "mock-only: forced by --mock-apis"
        print(reason)
    else:
        print("live OCR/GPT mode: credentials detected")

    sender = _telegram_sender(args.send_telegram)
    rows: list[FinancialPipelineResult] = []
    for pdf_path in pdfs:
        rows.append(
            process_financial_pdf(
                pdf_path,
                announcement_from_pdf(pdf_path),
                output_root=args.output_root,
                mock_apis=use_mock,
                send_telegram=args.send_telegram,
                telegram_sender=sender,
            )
        )

    _print_table(rows)
    failed = [row for row in rows if row.final_status == "FAIL"]
    no_data = [row for row in rows if row.final_status == "NO_DATA"]
    passed = [row for row in rows if row.final_status == "PASS"]
    print(f"\nSummary: {len(passed)} PASS, {len(no_data)} NO_DATA, {len(failed)} FAIL / {len(rows)} total")
    return 1 if failed else 0


def _selected_pdfs(args: argparse.Namespace) -> list[Path]:
    if args.pdf:
        return [Path(item) for item in args.pdf if Path(item).exists()][: args.limit]
    root = Path(args.downloads_root)
    return sorted(path for path in root.rglob("*.pdf") if path.is_file())[args.offset : args.offset + args.limit]


def _live_api_ready() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY", "").strip() and gpt54_is_configured())


def _telegram_sender(send_telegram: bool) -> TelegramSender | None:
    if not send_telegram:
        return None
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_ids:
        return None
    return TelegramSender(token, chat_ids)


def _print_table(rows: list[FinancialPipelineResult]) -> None:
    headers = ["PDF name", "OCR", "GPT JSON", "Validation", "Images", "Telegram", "Final", "Error reason"]
    data = [row.table_row() for row in rows]
    widths = [min(42, max(len(headers[i]), *(len(str(item[i])) for item in data))) for i in range(len(headers))]
    print(_line(widths))
    print(_row(headers, widths))
    print(_line(widths))
    for item in data:
        print(_row(item, widths))
    print(_line(widths))


def _row(values: list[str], widths: list[int]) -> str:
    cells = []
    for value, width in zip(values, widths):
        text = str(value)
        if len(text) > width:
            text = text[: max(0, width - 3)] + "..."
        cells.append(text.ljust(width))
    return "| " + " | ".join(cells) + " |"


def _line(widths: list[int]) -> str:
    return "+-" + "-+-".join("-" * width for width in widths) + "-+"


if __name__ == "__main__":
    sys.exit(main())
