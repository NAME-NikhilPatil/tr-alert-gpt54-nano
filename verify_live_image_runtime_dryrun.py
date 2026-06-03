"""Runtime dry-run for the live GPT image Telegram path.

This exercises the same helper used by startup replay/live image delivery, but
with a fake Telegram sender and a synthetic extraction payload. It renders real
PNG output through ``generate_financial_images`` and verifies that the live path
would send photos, without contacting Telegram or GPT.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import main
from image_generator import generate_financial_images
from image_validation import validate_financial_png
from models import Announcement

OUTPUT_ROOT = Path("output") / "_test" / "live_image_runtime_dryrun"


class FakeTelegramSender:
    """Record live helper sends without network access."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.photos: list[tuple[Path, str]] = []
        self.documents: list[tuple[Path, str]] = []

    def send_text(self, text: str, **_: object) -> bool:
        self.texts.append(text)
        return True

    def send_photo(self, file_path: Path, caption: str = "", **_: object) -> bool:
        path = Path(file_path)
        if not path.exists():
            raise AssertionError(f"photo path does not exist: {path}")
        issue = validate_financial_png(path)
        if issue:
            raise AssertionError(f"photo failed validation: {path} | {issue}")
        self.photos.append((path, caption))
        return True

    def send_document(self, file_path: Path, caption: str = "", **_: object) -> bool:
        self.documents.append((Path(file_path), caption))
        raise AssertionError("live image path must not send documents")


def _row(label: str, q4: str, q3: str, py_q4: str, fy: str, py_fy: str) -> dict[str, Any]:
    return {
        "label": label,
        "values": {
            "Q4 FY26": q4,
            "Q3 FY26": q3,
            "Q4 FY25": py_q4,
            "FY26": fy,
            "FY25": py_fy,
        },
    }


def _synthetic_dynamic_extraction() -> dict[str, Any]:
    """Return a Balaji-style dynamic P&L payload with actual source line items."""

    rows = [
        _row("Revenue from operations", "100", "90", "80", "350", "300"),
        _row("Cost of Production / Acquisition and Telecast Fees", "60", "50", "45", "220", "200"),
        _row("Changes in Inventories", "-5", "-4", "-3", "-15", "-10"),
        _row("Marketing and Distribution Expense", "10", "8", "7", "30", "25"),
        _row("Employee benefits expense", "12", "10", "9", "40", "35"),
        _row("Other expenses", "8", "7", "6", "30", "25"),
        _row("Depreciation and amortisation expense", "5", "4", "4", "18", "16"),
        _row("Finance costs", "2", "2", "2", "7", "6"),
        _row("Other income", "3", "2", "2", "8", "7"),
        _row("Exceptional items", "0", "0", "0", "0", "0"),
        _row("Total tax expense", "2", "2", "1", "6", "5"),
        _row("Profit for the period", "6", "5", "4", "22", "20"),
        _row("EPS (Basic)", "1.20", "1.00", "0.90", "4.00", "3.60"),
    ]
    columns = [
        {"kind": "value", "label": "Q4 FY26", "period": "Q4 FY26"},
        {"kind": "value", "label": "Q3 FY26", "period": "Q3 FY26"},
        {"kind": "value", "label": "Q4 FY25", "period": "Q4 FY25"},
        {"kind": "value", "label": "FY26", "period": "FY26"},
        {"kind": "value", "label": "FY25", "period": "FY25"},
    ]
    return {
        "company_name": "Runtime Dryrun Telefilms Limited",
        "source": "TEST",
        "board_meeting_date": "27-05-2026",
        "parser_status": "gpt54_pdf_direct",
        "parser_message": "Synthetic runtime dry-run payload.",
        "statement_basis": "consolidated",
        "currency_unit": "Rs in Cr",
        "source_currency_unit": "Rs in Cr",
        "result_period": "Q4 FY26",
        "ocr_markdown": "Consolidated financial results. Rs. in Cr.",
        "renderer_input_validation_status": "PASS",
        "validation_allows_images": True,
        "financial_rows": rows,
        "approved_pnl_rows": rows,
        "approved_pnl_columns": columns,
        "approved_bs_cf_rows": [],
        "approved_bs_cf_columns": [],
        "approved_segment_rows": [],
        "approved_segment_columns": [],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
    }


def main_check() -> None:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    announcement = Announcement(
        source="TEST",
        company_name="Runtime Dryrun Telefilms Limited",
        identifier="RUNTIME-DRYRUN",
        announcement_datetime="2026-05-27",
        subject="Outcome of Board Meeting",
        pdf_url="runtime-dryrun://synthetic",
        pdf_path=Path("downloads") / "runtime_dryrun.pdf",
    )
    dryrun_extraction = _synthetic_dynamic_extraction()
    rendered_labels = [str(row.get("label") or "") for row in dryrun_extraction["approved_pnl_rows"]]
    if "Cost of Production / Acquisition and Telecast Fees" not in rendered_labels:
        raise AssertionError(f"dynamic source line item was not preserved: {rendered_labels}")
    if "Cost of materials consumed" in rendered_labels:
        raise AssertionError(f"old hardcoded material-cost row leaked into dynamic P&L: {rendered_labels}")

    original_extract = main.extract_pdf_with_gpt54
    original_generate = main.generate_financial_images
    try:
        main.extract_pdf_with_gpt54 = lambda *_args, **_kwargs: dryrun_extraction
        main.generate_financial_images = lambda extraction, ann: generate_financial_images(
            extraction,
            ann,
            output_root=OUTPUT_ROOT,
        )
        sender = FakeTelegramSender()
        sent_count = main._send_local_mistral_result(sender, announcement)
    finally:
        main.extract_pdf_with_gpt54 = original_extract
        main.generate_financial_images = original_generate

    if sent_count != len(sender.texts) + len(sender.photos):
        raise AssertionError(f"sent count mismatch: {sent_count}, texts={len(sender.texts)}, photos={len(sender.photos)}")
    if not sender.texts:
        raise AssertionError("live path did not send intro text")
    if not sender.photos:
        raise AssertionError("live path did not send any photos")
    if sender.documents:
        raise AssertionError("live path sent documents")
    labels_in_text = "\n".join(sender.texts)
    if "Financial images attached: P&L." not in labels_in_text:
        raise AssertionError(f"intro text did not report the generated P&L image: {labels_in_text}")
    pnl_photos = [path for path, caption in sender.photos if "P&L Statement" in caption]
    if not pnl_photos:
        raise AssertionError(f"no P&L photo caption found: {sender.photos}")

    print(f"runtime dry-run passed: texts={len(sender.texts)} photos={len(sender.photos)} documents={len(sender.documents)}")
    for path, caption in sender.photos:
        print(f"photo={path} caption={caption}")


if __name__ == "__main__":
    main_check()
