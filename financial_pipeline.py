"""Reusable OCR -> GPT -> validation -> image -> Telegram pipeline."""

from __future__ import annotations

import re
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fitz
except Exception:  # pragma: no cover - optional dependency is in requirements.
    fitz = None  # type: ignore[assignment]

from financial_validation import attach_validation, validate_financial_payload
from financial_filing_classifier import SKIPPED_NON_FINANCIAL_DISCLOSURE
from financial_filing_classifier import analyze_financial_complexity
from financial_filing_classifier import classify_pdf_filing
from financial_filing_classifier import non_financial_skip_message
from gpt54_extractor import (
    extract_pdf_with_gpt54,
    extract_structured_with_gpt54,
    repair_structured_with_gpt54_fallback,
    repair_structured_with_gpt54_vision_fallback,
)
from image_generator import GeneratedFinancialImages, generate_financial_images
from models import Announcement
from pl_image import safe_filename
from telegram_sender import TelegramSender


@dataclass(slots=True)
class FinancialPipelineResult:
    """One PDF pipeline result suitable for logs and test tables."""

    pdf_name: str
    metadata: dict[str, Any]
    ocr_status: str
    gpt_json_status: str
    validation_status: str
    image_status: str
    telegram_status: str
    final_status: str
    error_reason: str = ""
    extraction: dict[str, Any] = field(default_factory=dict)
    generated_images: GeneratedFinancialImages | None = None

    def table_row(self) -> list[str]:
        return [
            self.pdf_name,
            self.ocr_status,
            self.gpt_json_status,
            self.validation_status,
            self.image_status,
            self.telegram_status,
            self.final_status,
            self.error_reason,
        ]


def process_financial_pdf(
    pdf_path: str | Path,
    announcement: Announcement | None = None,
    *,
    output_root: str | Path = "output/gpt54_pipeline_test_images",
    mock_apis: bool = False,
    send_telegram: bool = False,
    telegram_sender: TelegramSender | None = None,
) -> FinancialPipelineResult:
    """Process one PDF through the production architecture stages."""

    path = Path(pdf_path)
    announcement = announcement or announcement_from_pdf(path)
    metadata = pdf_metadata(path, announcement)
    started_at = datetime.now()
    metadata["started_at"] = started_at.isoformat(timespec="seconds")
    try:
        filing_classification = classify_pdf_filing(path, announcement)
        metadata["filing_classification"] = filing_classification.to_dict()
        if not filing_classification.is_financial_results:
            skip_payload = extract_pdf_with_gpt54(
                path,
                announcement,
                mock=False,
                filing_classification=filing_classification,
            )
            metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
            result = _result(
                path,
                metadata,
                "skipped_non_financial",
                "not_run",
                SKIPPED_NON_FINANCIAL_DISCLOSURE,
                "skipped_non_financial",
                _telegram_status_for_non_financial(send_telegram, skip_payload, announcement, telegram_sender),
                SKIPPED_NON_FINANCIAL_DISCLOSURE,
                str(skip_payload.get("reason") or filing_classification.reason),
                extraction=skip_payload,
                generated_images=None,
            )
            _write_pipeline_audit_bundle(path, output_root, announcement, result)
            return result

        financial_complexity = analyze_financial_complexity(path, filing_classification)
        metadata["financial_complexity"] = financial_complexity.to_dict()
        if mock_apis:
            ocr_payload = mock_ocr_payload(path, announcement)
            ocr_status = "mock_ocr"
            gpt_payload = extract_structured_with_gpt54(ocr_payload, announcement, mock=True)
        else:
            ocr_payload = {
                "ocr_status": "not_used",
                "parser_status": "gpt54_pdf_direct",
                "pdf_path": str(path),
                "source_page_count": metadata.get("page_count", 0),
                "page_count": metadata.get("page_count", 0),
                "ocr_markdown": "",
                "ocr_tables": [],
                "table_payload": {},
            }
            ocr_status = "not_used"
            gpt_payload = extract_pdf_with_gpt54(
                path,
                announcement,
                mock=False,
                filing_classification=filing_classification,
                financial_complexity=financial_complexity,
            )

        if ocr_status not in {"ok", "mock_ocr", "not_used"}:
            return _result(
                path,
                metadata,
                ocr_status,
                "not_run",
                "not_run",
                "not_run",
                _telegram_status(send_telegram, None, announcement, telegram_sender),
                "FAIL",
                str(ocr_payload.get("parser_message") or "OCR failed"),
            )

        gpt_status = str(gpt_payload.get("gpt_json_status") or gpt_payload.get("parser_status") or "unknown")
        validation = validate_financial_payload(gpt_payload, announcement)
        if _should_escalate_after_first_pass(validation, gpt_payload, filing_classification):
            escalated_payload = extract_pdf_with_gpt54(
                path,
                announcement,
                mock=False,
                filing_classification=filing_classification,
                financial_complexity=financial_complexity,
                force_complex_reason="first_pass_validation_warning",
            )
            escalated_validation = validate_financial_payload(escalated_payload, announcement)
            if _fallback_improved(validation, escalated_validation):
                escalated_payload["gpt54_escalation_metadata"] = {
                    "accepted": True,
                    "previous_validation_errors": validation.issues,
                    "new_validation_errors": escalated_validation.issues,
                    "reason": "first_pass_validation_warning",
                }
                gpt_payload = escalated_payload
                validation = escalated_validation
                gpt_status = str(gpt_payload.get("gpt_json_status") or gpt_payload.get("parser_status") or "unknown")
            else:
                gpt_payload["gpt54_escalation_metadata"] = {
                    "accepted": False,
                    "previous_validation_errors": validation.issues,
                    "candidate_validation_errors": escalated_validation.issues,
                    "reason": "first_pass_validation_warning",
                }
        if _should_run_validation_fallback(validation, mock_apis):
            fallback_payload = repair_structured_with_gpt54_fallback(
                ocr_payload,
                gpt_payload,
                validation_errors=validation.issues,
                validation_warnings=validation.warnings,
                announcement=announcement,
                mock=mock_apis,
            )
            fallback_validation = validate_financial_payload(fallback_payload, announcement)
            if _fallback_improved(validation, fallback_validation):
                fallback_payload["gpt54_fallback_metadata"] = {
                    **(fallback_payload.get("gpt54_fallback_metadata") or {}),
                    "accepted": True,
                    "previous_validation_errors": validation.issues,
                    "new_validation_errors": fallback_validation.issues,
                }
                gpt_payload = fallback_payload
                validation = fallback_validation
                gpt_status = str(gpt_payload.get("gpt_json_status") or gpt_payload.get("parser_status") or "unknown")
            else:
                gpt_payload["gpt54_fallback_metadata"] = {
                    **(fallback_payload.get("gpt54_fallback_metadata") or {}),
                    "accepted": False,
                    "previous_validation_errors": validation.issues,
                    "candidate_validation_errors": fallback_validation.issues,
                }
                vision_payload = repair_structured_with_gpt54_vision_fallback(
                    path,
                    ocr_payload,
                    gpt_payload,
                    validation_errors=validation.issues,
                    validation_warnings=validation.warnings,
                    announcement=announcement,
                    mock=mock_apis,
                )
                vision_validation = validate_financial_payload(vision_payload, announcement)
                if _fallback_improved(validation, vision_validation):
                    vision_payload["gpt54_vision_fallback_metadata"] = {
                        **(vision_payload.get("gpt54_vision_fallback_metadata") or {}),
                        "accepted": True,
                        "previous_validation_errors": validation.issues,
                        "new_validation_errors": vision_validation.issues,
                    }
                    gpt_payload = vision_payload
                    validation = vision_validation
                    gpt_status = str(gpt_payload.get("gpt_json_status") or gpt_payload.get("parser_status") or "unknown")
                else:
                    gpt_payload["gpt54_vision_fallback_metadata"] = {
                        **(vision_payload.get("gpt54_vision_fallback_metadata") or {}),
                        "accepted": False,
                        "previous_validation_errors": validation.issues,
                        "candidate_validation_errors": vision_validation.issues,
                    }
        validated_payload = attach_validation(gpt_payload, validation)

        generated: GeneratedFinancialImages | None = None
        image_status = "blocked_by_validation"
        image_count = 0
        if validation.allows_images:
            generated = generate_financial_images(validated_payload, announcement, output_root)
            _copy_source_pdf_artifacts(path, generated, metadata, validated_payload)
            image_count = len(generated.images)
            image_status = f"ok:{image_count}" if image_count else _skipped_image_status(generated)
            if _should_escalate_after_zero_images(validation, gpt_payload, image_count, filing_classification):
                escalated_payload = extract_pdf_with_gpt54(
                    path,
                    announcement,
                    mock=False,
                    filing_classification=filing_classification,
                    financial_complexity=financial_complexity,
                    force_complex_reason="first_pass_generated_zero_images",
                )
                escalated_validation = validate_financial_payload(escalated_payload, announcement)
                escalated_validated_payload = attach_validation(escalated_payload, escalated_validation)
                escalated_generated: GeneratedFinancialImages | None = None
                escalated_count = 0
                if escalated_validation.allows_images:
                    escalated_generated = generate_financial_images(escalated_validated_payload, announcement, output_root)
                    _copy_source_pdf_artifacts(path, escalated_generated, metadata, escalated_validated_payload)
                    escalated_count = len(escalated_generated.images)
                if escalated_count > image_count:
                    escalated_payload["gpt54_escalation_metadata"] = {
                        "accepted": True,
                        "previous_image_count": image_count,
                        "new_image_count": escalated_count,
                        "reason": "first_pass_generated_zero_images",
                    }
                    gpt_payload = escalated_payload
                    validation = escalated_validation
                    validated_payload = attach_validation(gpt_payload, validation)
                    generated = escalated_generated
                    image_count = escalated_count
                    image_status = f"ok:{image_count}"
                    gpt_status = str(gpt_payload.get("gpt_json_status") or gpt_payload.get("parser_status") or "unknown")
                else:
                    gpt_payload["gpt54_escalation_metadata"] = {
                        "accepted": False,
                        "previous_image_count": image_count,
                        "candidate_image_count": escalated_count,
                        "reason": "first_pass_generated_zero_images",
                    }

        telegram_status = _telegram_status(send_telegram, generated, announcement, telegram_sender)
        error_reason = ""
        final_status = "PASS"
        if ocr_status not in {"ok", "mock_ocr", "not_used"}:
            final_status = "FAIL"
        if gpt_status not in {"valid", "mock_valid_json"}:
            final_status = "FAIL"
        if not validation.allows_images:
            if gpt_status in {"valid", "mock_valid_json"} and _is_no_data_result(validation.issues):
                final_status = "NO_DATA"
                image_status = "skipped:no_financial_data"
                error_reason = "No financial result table values found; no images generated."
            else:
                final_status = "FAIL"
                error_reason = "; ".join(validation.issues[:4])
        if image_count <= 0:
            if final_status == "PASS":
                final_status = "FAIL"
                error_reason = error_reason or image_status
        if final_status == "FAIL" and not error_reason:
            error_reason = f"ocr={ocr_status}; gpt={gpt_status}; validation={validation.status}; images={image_status}"

        metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
        result = _result(
            path,
            metadata,
            ocr_status,
            gpt_status,
            validation.status,
            image_status,
            telegram_status,
            final_status,
            error_reason,
            extraction=validated_payload,
            generated_images=generated,
        )
        _write_pipeline_audit_bundle(path, output_root, announcement, result)
        return result
    except Exception as exc:
        metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
        result = _result(
            path,
            metadata,
            "exception",
            "exception",
            "exception",
            "exception",
            _telegram_status(False, None, announcement, None),
            "FAIL",
            f"{type(exc).__name__}: {exc}",
        )
        _write_pipeline_audit_bundle(path, output_root, announcement, result)
        return result


def process_financial_pdfs(
    pdf_paths: list[str | Path],
    *,
    output_root: str | Path = "output/gpt54_pipeline_test_images",
    mock_apis: bool = False,
    send_telegram: bool = False,
    telegram_sender: TelegramSender | None = None,
) -> list[FinancialPipelineResult]:
    """Process a batch of PDFs while preserving every PDF as a result row."""

    results: list[FinancialPipelineResult] = []
    for pdf_path in pdf_paths:
        announcement = announcement_from_pdf(Path(pdf_path))
        results.append(
            process_financial_pdf(
                pdf_path,
                announcement,
                output_root=output_root,
                mock_apis=mock_apis,
                send_telegram=send_telegram,
                telegram_sender=telegram_sender,
            )
        )
    return results


def announcement_from_pdf(pdf_path: Path) -> Announcement:
    """Build deterministic announcement metadata for a local PDF."""

    source = "BSE" if any(part.lower() == "bse" for part in pdf_path.parts) else "NSE"
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", pdf_path.stem)
    date = date_match.group(1) if date_match else datetime.fromtimestamp(pdf_path.stat().st_mtime).strftime("%Y-%m-%d")
    company_stem = pdf_path.stem[: date_match.start()].rstrip("_") if date_match else pdf_path.stem
    company = company_stem.replace("_", " ").strip() or pdf_path.stem
    return Announcement(
        source=source,
        company_name=company,
        identifier=f"GPT54-PIPELINE-{pdf_path.stem}",
        announcement_datetime=date,
        subject="Outcome of Board Meeting",
        pdf_url=f"local-pipeline://{pdf_path.as_posix()}",
        pdf_path=pdf_path,
    )


def pdf_metadata(pdf_path: Path, announcement: Announcement) -> dict[str, Any]:
    """Return PDF ingestion metadata required for audit/debugging."""

    return {
        "pdf_path": str(pdf_path),
        "pdf_name": pdf_path.name,
        "company_name": announcement.company_name,
        "source": announcement.source,
        "announcement_date": announcement.announcement_datetime,
        "page_count": pdf_page_count(pdf_path),
        "file_size_bytes": pdf_path.stat().st_size if pdf_path.exists() else 0,
    }


def mock_ocr_payload(pdf_path: Path, announcement: Announcement) -> dict[str, Any]:
    """Return a deterministic OCR payload for offline architecture tests."""

    page_count = pdf_page_count(pdf_path)
    pages = list(range(1, min(page_count, 5) + 1)) if page_count else []
    return {
        "ocr_status": "mock_ocr",
        "parser_status": "mock_ocr",
        "parser_message": "Mock OCR payload generated for offline pipeline testing.",
        "pdf_path": str(pdf_path),
        "company_name": announcement.company_name,
        "source": announcement.source,
        "board_meeting_date": announcement.announcement_datetime,
        "source_page_count": page_count,
        "mistral_sent_page_count": page_count,
        "mistral_selected_pages": pages,
        "page_numbers_used": pages,
        "ocr_markdown": f"{announcement.company_name}\nStatement of consolidated financial results\nRs in Cr\n",
        "ocr_tables": [],
        "table_payload": {},
    }


def pdf_page_count(pdf_path: Path) -> int:
    """Return page count, preserving the PDF in the batch if this fails."""

    if fitz is None:
        return 0
    try:
        with fitz.open(pdf_path) as doc:
            return int(doc.page_count)
    except Exception:
        return 0


def _telegram_status(
    send_telegram: bool,
    generated: GeneratedFinancialImages | None,
    announcement: Announcement,
    sender: TelegramSender | None,
) -> str:
    image_count = len(generated.images) if generated else 0
    if not send_telegram:
        return f"mock_sent:{image_count}"
    if sender is None:
        return "mock_sent:credentials_missing"
    if not generated or not generated.images:
        return "live_skipped:no_images"
    sent = 0
    for image in generated.images:
        caption = image.caption or f"{announcement.company_name} | {image.kind}"
        if sender.send_photo(image.path, caption, queue_on_failure=False):
            sent += 1
    return f"live_sent:{sent}/{len(generated.images)}"


def _telegram_status_for_non_financial(
    send_telegram: bool,
    extraction: dict[str, Any],
    announcement: Announcement,
    sender: TelegramSender | None,
) -> str:
    if not send_telegram:
        return "mock_skipped_non_financial:0"
    if sender is None:
        return "live_skipped:credentials_missing"
    return "live_sent:1" if sender.send_text(non_financial_skip_message(extraction), queue_on_failure=False) else "live_failed:non_financial_skip"


def _should_run_validation_fallback(validation: Any, mock_apis: bool) -> bool:
    """Return whether a focused GPT fallback should try to recover table data."""

    if mock_apis:
        return False
    if str(os.environ.get("ENABLE_FAILED_CELL_RETRY", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return False
    if not validation.issues:
        return False
    recoverable_prefixes = (
        "cash_flow_",
        "balance_sheet_",
        "column_mapping_failure",
        "period_column_mapping_unknown",
        "q4_equals_fy_column_collision",
        "formula_mismatch:",
        "consolidated_available_but_standalone_selected",
    )
    return any(str(issue).startswith(recoverable_prefixes) for issue in validation.issues)


def _should_escalate_after_first_pass(validation: Any, extraction: dict[str, Any], classification: Any) -> bool:
    """Return whether a financial result needs xhigh retry after first pass."""

    if not getattr(classification, "is_financial_results", False):
        return False
    routing = extraction.get("model_routing") if isinstance(extraction.get("model_routing"), dict) else {}
    if routing.get("reasoning_effort_requested") == "xhigh":
        return False
    if str(os.environ.get("GPT54_USE_XHIGH_FOR_COMPLEX", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return False
    joined = " ".join(str(item) for item in (getattr(validation, "issues", []) or []) + (getattr(validation, "warnings", []) or [])).lower()
    triggers = ("basis", "eps", "segment", "balance_sheet", "cash_flow", "formula")
    return any(trigger in joined for trigger in triggers)


def _should_escalate_after_zero_images(
    validation: Any,
    extraction: dict[str, Any],
    image_count: int,
    classification: Any,
) -> bool:
    if image_count > 0 or not getattr(validation, "allows_images", False):
        return False
    if not getattr(classification, "is_financial_results", False):
        return False
    routing = extraction.get("model_routing") if isinstance(extraction.get("model_routing"), dict) else {}
    if routing.get("reasoning_effort_requested") == "xhigh":
        return False
    return str(os.environ.get("GPT54_USE_XHIGH_FOR_COMPLEX", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _fallback_improved(original: Any, candidate: Any) -> bool:
    """Accept fallback only when validation evidence improves."""

    original_score = _validation_acceptance_score(original)
    candidate_score = _validation_acceptance_score(candidate)
    return candidate_score > original_score


def _validation_acceptance_score(validation: Any) -> tuple[int, int, int]:
    """Return comparable validation quality; larger is better."""

    allows = 1 if getattr(validation, "allows_images", False) else 0
    renderable = len((getattr(validation, "metadata", {}) or {}).get("renderable_sections") or [])
    issue_penalty = -len(getattr(validation, "issues", []) or [])
    return allows, renderable, issue_penalty


def _copy_source_pdf_artifacts(
    source_pdf: Path,
    generated: GeneratedFinancialImages | None,
    metadata: dict[str, Any],
    extraction: dict[str, Any] | None = None,
) -> None:
    """Place the exact source PDF and source metadata next to generated PNGs."""

    if not generated or not generated.images or not source_pdf.exists():
        return
    for folder in sorted({image.path.parent for image in generated.images}):
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"SOURCE_PDF__{source_pdf.name}"
        if not destination.exists() or destination.stat().st_size != source_pdf.stat().st_size:
            shutil.copy2(source_pdf, destination)
        info = [
            "Temporary manual verification bundle source",
            f"source_pdf_name: {source_pdf.name}",
            f"source_pdf_path: {source_pdf.resolve()}",
            f"copied_pdf_name: {destination.name}",
            f"company_name: {metadata.get('company_name', '')}",
            f"source_exchange: {metadata.get('source', '')}",
            f"announcement_date: {metadata.get('announcement_date', '')}",
            f"page_count: {metadata.get('page_count', '')}",
            f"file_size_bytes: {metadata.get('file_size_bytes', '')}",
            f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        ]
        (folder / "SOURCE_INFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
        if extraction:
            (folder / "VALIDATION_REPORT.json").write_text(
                json.dumps(_validation_report_payload(metadata, extraction), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )


def _write_pipeline_audit_bundle(
    source_pdf: Path,
    output_root: str | Path,
    announcement: Announcement,
    result: FinancialPipelineResult,
) -> None:
    """Always save local source and validation/quarantine metadata for manual review."""

    folders = sorted({image.path.parent for image in result.generated_images.images}) if result.generated_images else []
    if not folders:
        folders = [_audit_output_dir(output_root, announcement, result)]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        if source_pdf.exists():
            destination = folder / f"SOURCE_PDF__{source_pdf.name}"
            if not destination.exists() or destination.stat().st_size != source_pdf.stat().st_size:
                shutil.copy2(source_pdf, destination)
        if not (folder / "SOURCE_INFO.txt").exists():
            info = [
                "Temporary manual verification bundle source",
                f"source_pdf_name: {source_pdf.name}",
                f"source_pdf_path: {source_pdf.resolve()}",
                f"company_name: {result.metadata.get('company_name', '')}",
                f"source_exchange: {result.metadata.get('source', '')}",
                f"announcement_date: {result.metadata.get('announcement_date', '')}",
                f"page_count: {result.metadata.get('page_count', '')}",
                f"file_size_bytes: {result.metadata.get('file_size_bytes', '')}",
                f"final_status: {result.final_status}",
                f"error_reason: {result.error_reason}",
                f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
            ]
            (folder / "SOURCE_INFO.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
        (folder / "VALIDATION_REPORT.json").write_text(
            json.dumps(_result_report_payload(result), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


def _audit_output_dir(output_root: str | Path, announcement: Announcement, result: FinancialPipelineResult) -> Path:
    """Return the quarantine/manual-review output folder for blocked or failed PDFs."""

    company = safe_filename(
        str((result.extraction or {}).get("company_name") or announcement.company_name or result.metadata.get("company_name") or "Company"),
        max_length=56,
    )
    date = safe_filename(str(result.metadata.get("announcement_date") or announcement.announcement_datetime or "unknown_date"), max_length=40)
    return Path(output_root) / company / date


def _result_report_payload(result: FinancialPipelineResult) -> dict[str, Any]:
    """Build a compact local-only validation report without raw OCR text."""

    extraction = result.extraction or {}
    report = _validation_report_payload(result.metadata, extraction)
    report["pipeline_status"] = {
        "ocr_status": result.ocr_status,
        "gpt_json_status": result.gpt_json_status,
        "validation_status": result.validation_status,
        "image_status": result.image_status,
        "telegram_status": result.telegram_status,
        "final_status": result.final_status,
        "error_reason": result.error_reason,
    }
    report["generated_images"] = [str(image.path) for image in result.generated_images.images] if result.generated_images else []
    return report


def _validation_report_payload(metadata: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    """Build the stable local audit JSON saved beside every PDF output."""

    return {
        "report_generated_at": datetime.now().isoformat(timespec="seconds"),
        "pdf_metadata": metadata,
        "company_name": extraction.get("company_name"),
        "statement_basis": extraction.get("statement_basis"),
        "result_period": extraction.get("result_period"),
        "source_currency_unit": extraction.get("source_currency_unit"),
        "display_currency_unit": extraction.get("currency_unit"),
        "conversion_provenance": extraction.get("conversion_provenance") or {},
        "filing_classification": extraction.get("filing_classification") or metadata.get("filing_classification") or {},
        "non_financial_skip_report": extraction.get("non_financial_skip_report") or {},
        "financial_complexity": extraction.get("financial_complexity") or metadata.get("financial_complexity") or {},
        "model_routing": extraction.get("model_routing") or {},
        "discovery_metadata": extraction.get("discovery_metadata") or {},
        "gpt54_execution_metadata": extraction.get("gpt54_execution_metadata") or {},
        "gpt54_escalation_metadata": extraction.get("gpt54_escalation_metadata") or {},
        "gpt54_fallback_metadata": extraction.get("gpt54_fallback_metadata") or {},
        "gpt54_vision_fallback_metadata": extraction.get("gpt54_vision_fallback_metadata") or {},
        "validation": {
            "status": extraction.get("validation_status"),
            "allows_images": extraction.get("validation_allows_images"),
            "errors": extraction.get("validation_errors") or [],
            "warnings": extraction.get("validation_warnings") or [],
            "failure_categories": extraction.get("validation_failure_categories") or [],
            "render_blocked_sections": extraction.get("render_blocked_sections") or [],
            "renderable_sections": extraction.get("renderable_sections") or [],
        },
        "table_repair_metadata": extraction.get("table_repair_metadata") or {},
        "column_identities": extraction.get("column_identities") or [],
        "section_counts": {
            "financial_rows": len(extraction.get("financial_rows") or []),
            "balance_sheet_sections": len(extraction.get("balance_sheet_variables") or []),
            "cash_flow_rows": len(extraction.get("cash_flow_variables") or []),
            "segment_tables": len(extraction.get("segment_tables") or []),
        },
    }


def _skipped_image_status(generated: GeneratedFinancialImages | None) -> str:
    if generated and generated.warnings:
        return "skipped:" + "; ".join(generated.warnings[:1])
    if generated and generated.missing_sections:
        return "skipped:" + ",".join(generated.missing_sections)
    return "skipped:no_images"


def _is_no_data_result(issues: list[str]) -> bool:
    """Return whether validation failed only because the PDF has no financial table."""

    return "no_financial_values_found" in issues and "no_renderable_financial_image_section" in issues


def _result(
    path: Path,
    metadata: dict[str, Any],
    ocr_status: str,
    gpt_status: str,
    validation_status: str,
    image_status: str,
    telegram_status: str,
    final_status: str,
    error_reason: str,
    *,
    extraction: dict[str, Any] | None = None,
    generated_images: GeneratedFinancialImages | None = None,
) -> FinancialPipelineResult:
    return FinancialPipelineResult(
        pdf_name=path.name,
        metadata=metadata,
        ocr_status=ocr_status,
        gpt_json_status=gpt_status,
        validation_status=validation_status,
        image_status=image_status,
        telegram_status=telegram_status,
        final_status=final_status,
        error_reason=error_reason,
        extraction=extraction or {},
        generated_images=generated_images,
    )
