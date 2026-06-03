"""Convert financial result screenshots into the same Excel output format."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from excel_writer import write_excel
from models import Announcement
from pdf_parser import parse_financial_screenshots
from utils import ensure_directories


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for screenshot OCR conversion."""

    parser = argparse.ArgumentParser(description="Parse financial table screenshots into Excel.")
    parser.add_argument("images", nargs="+", help="Screenshot image paths in page order.")
    parser.add_argument("--company", default="Screenshot Input", help="Company name for Excel metadata.")
    parser.add_argument("--source", default="SCREENSHOT", help="Source label for Excel metadata.")
    parser.add_argument("--identifier", default="", help="Symbol or scrip code for Excel metadata.")
    parser.add_argument("--subject", default="Outcome of Board Meeting", help="Subject for Excel metadata.")
    parser.add_argument("--output", required=True, help="Output .xlsx path.")
    return parser


def main() -> None:
    """OCR screenshots, parse financial rows, and write Excel."""

    args = build_parser().parse_args()
    ensure_directories()
    image_paths = [Path(path) for path in args.images]
    financials = parse_financial_screenshots(image_paths)
    announcement = Announcement(
        source=args.source,
        company_name=args.company,
        identifier=args.identifier,
        announcement_datetime=datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        subject=args.subject,
        pdf_url="",
        details="Parsed from screenshots",
    )
    output_path = write_excel([(announcement, financials)], datetime.now().date(), Path(args.output))
    print(output_path)
    print(financials.parser_status)
    print(financials.parser_message)


if __name__ == "__main__":
    main()

