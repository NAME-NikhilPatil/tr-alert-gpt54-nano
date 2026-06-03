"""Render images from a saved LLM values-first payload without an API call."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from financial_validation import attach_validation, validate_financial_payload
from gpt54_extractor import _normalize_llm_values_first_payload
from image_generator import generate_financial_images
from models import Announcement


def main() -> int:
    parser = argparse.ArgumentParser(description="Render saved LLM values-first payload.")
    parser.add_argument("--payload", required=True, help="Path to llm_values_first_payload.json.")
    parser.add_argument("--pdf", required=True, help="Official source PDF path.")
    parser.add_argument("--company", required=True, help="Company name for output metadata.")
    parser.add_argument("--date", required=True, help="Announcement date, e.g. 2026-05-30.")
    parser.add_argument("--source", default="NSE", help="Exchange/source label.")
    parser.add_argument("--output-root", required=True, help="Output root for generated images.")
    args = parser.parse_args()

    payload_path = Path(args.payload)
    pdf_path = Path(args.pdf)
    payload = json.loads(payload_path.read_text(encoding="utf-8", errors="ignore"))
    announcement = Announcement(
        source=args.source,
        company_name=args.company,
        identifier=pdf_path.stem,
        announcement_datetime=args.date,
        subject="Financial Results",
        pdf_url="",
        pdf_path=pdf_path,
    )
    extraction = _normalize_llm_values_first_payload(payload, pdf_path, announcement, {})
    validation = validate_financial_payload(extraction, announcement)
    extraction = attach_validation(extraction, validation)
    generated = generate_financial_images(extraction, announcement, Path(args.output_root))
    print(
        json.dumps(
            {
                "company": extraction.get("company_name"),
                "status": validation.status,
                "allows_images": validation.allows_images,
                "images": [str(image.path) for image in generated.images],
                "warnings": generated.warnings,
            },
            indent=2,
        )
    )
    return 0 if generated.images else 1


if __name__ == "__main__":
    raise SystemExit(main())
