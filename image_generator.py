"""Generate the three Telegram financial PNG images after extraction."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bs_cf_image import build_bs_cf_rows
from bs_cf_image import render_bs_cf_image
from models import Announcement
from pl_image import build_pl_rows
from pl_image import normalize_rows
from pl_image import parse_period
from pl_image import result_display_columns
from pl_image import row_key
from pl_image import row_has_value
from pl_image import safe_filename
from pl_image import render_pl_image
from segment_image import build_segment_rows
from segment_image import render_segment_image
from unit_detector import normalize_extraction_units
from utils import normalize_date
from image_validation import validate_financial_png
from financial_validation import attach_validation, validate_financial_payload
from financial_filing_classifier import SKIPPED_NON_FINANCIAL_DISCLOSURE
from financial_filing_classifier import non_financial_skip_message


@dataclass(slots=True)
class GeneratedFinancialImage:
    """One generated image plus its Telegram caption."""

    kind: str
    path: Path
    caption: str


@dataclass(slots=True)
class GeneratedFinancialImages:
    """Generated image set and any warning messages to send before photos."""

    images: list[GeneratedFinancialImage]
    warnings: list[str]
    currency_unit: str
    statement_basis: str
    missing_sections: list[str]

    @property
    def paths(self) -> list[Path]:
        """Return just the image paths for debugger compatibility."""

        return [image.path for image in self.images]


def generate_financial_images(
    extraction: dict[str, Any],
    announcement: Announcement | None = None,
    output_root: str | Path = Path("output") / "images",
) -> GeneratedFinancialImages:
    """
    Generate financial images only for sections with usable extracted values.

    The target sections are P&L, Balance Sheet + Cash Flow, and Segments. If a
    section has no real values in the PDF, no placeholder image is generated;
    the missing section is returned for the Telegram intro message instead.
    """

    extraction = _repair_extraction_from_embedded_ocr_tables(extraction)
    if str(extraction.get("status") or extraction.get("parser_status") or "") == SKIPPED_NON_FINANCIAL_DISCLOSURE:
        return GeneratedFinancialImages(
            images=[],
            warnings=[non_financial_skip_message(extraction)],
            currency_unit="",
            statement_basis="not_applicable",
            missing_sections=[],
        )
    if "validation_allows_images" not in extraction:
        validation = validate_financial_payload(extraction, announcement)
        extraction = attach_validation(extraction, validation)
    company = str(extraction.get("company_name") or (announcement.company_name if announcement else "") or "Company")
    announcement_date = _announcement_date(extraction, announcement)
    if extraction.get("validation_allows_images") is False:
        if not _has_usable_extracted_financial_values(extraction):
            return GeneratedFinancialImages(
                images=[],
                warnings=[],
                currency_unit=str(extraction.get("currency_unit") or ""),
                statement_basis=str(extraction.get("statement_basis") or "unknown"),
                missing_sections=[],
            )
        warning = _manual_verification_warning(company, extraction)
        return GeneratedFinancialImages(
            images=[],
            warnings=[warning],
            currency_unit=str(extraction.get("currency_unit") or ""),
            statement_basis=str(extraction.get("statement_basis") or "unknown"),
            missing_sections=["P&L Statement", "Balance Sheet + Cash Flow", "Segment Performance"],
        )
    normalized, source_unit, display_unit, warnings = normalize_extraction_units(
        extraction,
        company=company,
        announcement_date=announcement_date,
    )
    statement_basis = _statement_basis(normalized)
    single_statement = statement_basis == "single_statement"
    if _standalone_conflicts_with_consolidated_source(normalized):
        warning = (
            f"⚠️ Consolidated section detected but extracted data is standalone for {company}; "
            "financial images skipped for manual verification"
        )
        if warning not in warnings:
            warnings.append(warning)
        logging.warning(
            "Consolidated marker found but only standalone data extracted: %s %s",
            company,
            announcement_date,
        )
        return GeneratedFinancialImages(
            images=[],
            warnings=_dedupe_warnings(warnings),
            currency_unit=display_unit,
            statement_basis="standalone",
            missing_sections=["P&L Statement", "Balance Sheet + Cash Flow", "Segment Performance"],
        )
    if statement_basis == "standalone":
        logging.warning("Only standalone data found in PDF: %s %s", company, announcement_date)

    output_dir = (
        Path(output_root)
        / safe_filename(company, max_length=56)
        / safe_filename(announcement_date or datetime.now().strftime("%Y-%m-%d"), max_length=40)
    )
    output_dir = _disambiguated_output_dir(output_dir, announcement)
    output_dir.mkdir(parents=True, exist_ok=True)
    quarter_label, fy_label = _period_caption_parts(normalized)
    standalone_tag = statement_basis == "standalone" and not single_statement
    render_jobs = _available_render_jobs(
        normalized=normalized,
        announcement=announcement,
        output_dir=output_dir,
        display_unit=display_unit,
        standalone_tag=standalone_tag,
        company=company,
        quarter_label=quarter_label,
        fy_label=fy_label,
    )
    if not any(bool(job["available"]) for job in render_jobs) and not _has_usable_extracted_financial_values(normalized):
        return GeneratedFinancialImages(
            images=[],
            warnings=[],
            currency_unit=display_unit,
            statement_basis=statement_basis,
            missing_sections=[],
        )

    images: list[GeneratedFinancialImage] = []
    missing_sections: list[str] = []
    for job in render_jobs:
        if not job["available"]:
            missing_sections.append(str(job["section"]))
            continue
        try:
            path = job["render"]()
        except Exception as exc:
            logging.exception("Financial image generation failed for %s %s: %s", company, job["kind"], exc)
            continue
        validation_issue = validate_financial_png(path)
        if validation_issue:
            missing_sections.append(str(job["section"]))
            logging.error(
                "Financial image validation failed for %s %s %s: %s",
                company,
                job["kind"],
                path,
                validation_issue,
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not delete invalid generated image: %s", path)
            continue
        images.append(GeneratedFinancialImage(kind=str(job["kind"]), path=path, caption=str(job["caption"])))

    if not images and _has_usable_extracted_financial_values(normalized) and not warnings:
        warnings.append(_manual_verification_warning(company, {**normalized, "validation_allows_images": False}))
        missing_sections = ["P&L Statement", "Balance Sheet + Cash Flow", "Segment Performance"]

    return GeneratedFinancialImages(
        images=images,
        warnings=_dedupe_warnings(warnings),
        currency_unit=display_unit,
        statement_basis=statement_basis,
        missing_sections=missing_sections,
    )


def _disambiguated_output_dir(output_dir: Path, announcement: Announcement | None) -> Path:
    """Avoid overwriting same-company same-date outputs from different PDFs."""

    pdf_path = Path(announcement.pdf_path) if announcement and announcement.pdf_path else None
    if not pdf_path:
        return output_dir
    pdf_stem = safe_filename(pdf_path.stem, max_length=80)
    marker = output_dir / "SOURCE_INFO.txt"
    if not output_dir.exists():
        return output_dir
    if marker.exists():
        try:
            if str(pdf_path.resolve()) in marker.read_text(encoding="utf-8", errors="ignore"):
                return output_dir
        except OSError:
            pass
    if not any(output_dir.glob("*.png")):
        return output_dir
    return output_dir / pdf_stem


def _repair_extraction_from_embedded_ocr_tables(extraction: dict[str, Any]) -> dict[str, Any]:
    """Refresh deterministic table rows from stored OCR markdown when available."""

    markdown = str((extraction or {}).get("ocr_markdown") or "")
    if "<table" not in markdown.lower():
        return extraction
    try:
        from mistral_parser import payload_from_ocr_markdown_tables
    except Exception:
        return extraction
    try:
        table_payload = payload_from_ocr_markdown_tables(markdown)
    except Exception:
        logging.exception("Failed to rebuild OCR table payload for financial images")
        return extraction
    if not table_payload:
        return extraction

    merged = dict(extraction)
    table_rows = normalize_rows(table_payload.get("financial_rows"))
    existing_rows = normalize_rows(merged.get("financial_rows"))
    if table_rows and len(table_rows) >= len(existing_rows):
        merged["financial_rows"] = table_payload["financial_rows"]
        if table_payload.get("result_period"):
            merged["result_period"] = table_payload["result_period"]
        if str(merged.get("parser_status") or "") == "mistral_company_mismatch":
            merged["parser_status"] = table_payload.get("parser_status") or "parsed_mistral"
            merged["parser_message"] = table_payload.get("parser_message") or "Recovered financial rows from OCR tables."

    for key in ("segment_tables", "balance_sheet_variables", "cash_flow_variables", "key_variables"):
        if table_payload.get(key):
            merged[key] = table_payload[key]

    for key in (
        "currency_unit",
        "source_currency_unit",
        "statement_basis",
        "values_display_unit_applied",
        "segment_values_display_unit_applied",
    ):
        if table_payload.get(key) not in (None, ""):
            merged[key] = table_payload[key]
    return merged


def _validation_reason_category(errors: Any) -> str:
    """Return a Telegram-safe validation category without audit details."""

    joined = " ".join(str(item) for item in (errors or [])).lower()
    if "unit" in joined or "currency" in joined:
        return "unit_not_verified"
    if "consolidated" in joined or "standalone" in joined:
        return "statement_basis_check"
    if "column_mapping" in joined or "q4_equals_fy" in joined:
        return "column_mapping_check"
    if "repeated" in joined or "collision" in joined:
        return "repeated_value_check"
    if "balance_sheet" in joined:
        return "balance_sheet_total_check"
    if "cash_flow" in joined:
        return "cash_flow_check"
    if "formula" in joined:
        return "formula_check"
    if "no_financial" in joined or "no_renderable" in joined:
        return "financial_data_not_found"
    return "validation_check"


def _manual_verification_warning(company: str, extraction: dict[str, Any]) -> str:
    """Return a client-friendly validation warning without internal category names."""

    reason_lines = _manual_verification_reason_lines(
        extraction.get("validation_errors") or [],
        statement_basis=str(extraction.get("statement_basis") or ""),
    )
    lines = [
        company,
        "Extraction needs manual verification.",
        "Images were not sent because the following information could not be verified in the PDF:",
    ]
    lines.extend(f"- {line}" for line in reason_lines)
    return "\n".join(lines)


def _manual_verification_reason_lines(errors: Any, *, statement_basis: str = "") -> list[str]:
    """Map validation errors to readable Telegram bullets."""

    joined = " ".join(str(item) for item in (errors or [])).lower()
    lines: list[str] = []
    if "unit" in joined or "currency" in joined:
        lines.append("Unit of figures, such as Lakhs, Crores, or Millions")
    if not str(statement_basis or "").strip() or str(statement_basis).strip().lower() == "unknown":
        lines.append("Statement basis, such as Standalone or Consolidated")
    elif "consolidated" in joined or "standalone" in joined or "statement_basis" in joined:
        lines.append("Correct statement basis, such as Standalone or Consolidated")
    if "cash_flow" in joined:
        lines.append("Cash Flow final net rows")
    if "balance_sheet" in joined:
        lines.append("Balance Sheet totals")
    if "segment" in joined:
        lines.append("Segment Performance section")
    if "column_mapping" in joined or "q4_equals_fy" in joined or "period_column" in joined:
        lines.append("Correct period columns")
    if "formula" in joined:
        lines.append("Calculated financial formula rows")
    if "no_financial" in joined or "no_renderable" in joined:
        lines.append("Required financial table values")
    if not lines:
        lines.append("Required financial data")
    return _dedupe_warnings(lines)


def _has_usable_extracted_financial_values(extraction: dict[str, Any]) -> bool:
    """Return true when the payload has any numeric financial value worth messaging."""

    try:
        rows: list[dict[str, Any]] = []
        rows.extend(build_pl_rows(normalize_rows(extraction.get("financial_rows"))))
        rows.extend(build_bs_cf_rows(extraction))
        rows.extend(build_segment_rows(extraction))
        return any(row_has_value(row) for row in rows)
    except Exception:
        logging.debug("Could not determine extracted financial value availability.", exc_info=True)
        return False


def _available_render_jobs(
    *,
    normalized: dict[str, Any],
    announcement: Announcement | None,
    output_dir: Path,
    display_unit: str,
    standalone_tag: bool,
    company: str,
    quarter_label: str,
    fy_label: str,
) -> list[dict[str, Any]]:
    """Return render jobs with availability checked before drawing images."""

    pl_rows = normalized.get("approved_pnl_rows") if isinstance(normalized.get("approved_pnl_rows"), list) else []
    bs_cf_rows = normalized.get("approved_bs_cf_rows") if isinstance(normalized.get("approved_bs_cf_rows"), list) else []
    segment_rows = normalized.get("approved_segment_rows") if isinstance(normalized.get("approved_segment_rows"), list) else []
    pl_columns = normalized.get("approved_pnl_columns") if isinstance(normalized.get("approved_pnl_columns"), list) else []
    bs_cf_columns = normalized.get("approved_bs_cf_columns") if isinstance(normalized.get("approved_bs_cf_columns"), list) else []
    segment_columns = normalized.get("approved_segment_columns") if isinstance(normalized.get("approved_segment_columns"), list) else []
    segment_value_rows = [row for row in segment_rows if row_has_value(row)]
    blocked_sections = {str(item) for item in (normalized.get("render_blocked_sections") or [])}

    return [
        {
            "kind": "pnl",
            "section": "P&L Statement",
            "available": "pnl" not in blocked_sections and _has_approved_image_data(pl_rows, pl_columns),
            "render": lambda rows=pl_rows, columns=pl_columns: render_pl_image(
                normalized,
                announcement,
                output_dir,
                display_unit,
                standalone_tag=standalone_tag,
                approved_rows=rows,
                approved_columns=columns,
            ),
            "caption": f"P&L Statement | {company} | {_join_period(quarter_label, fy_label)}",
        },
        {
            "kind": "bs_cf",
            "section": "Balance Sheet + Cash Flow",
            "available": "bs_cf" not in blocked_sections and _has_approved_image_data(bs_cf_rows, bs_cf_columns),
            "render": lambda rows=bs_cf_rows, columns=bs_cf_columns: render_bs_cf_image(
                normalized,
                announcement,
                output_dir,
                display_unit,
                standalone_tag=standalone_tag,
                approved_rows=rows,
                approved_columns=columns,
            ),
            "caption": f"Balance Sheet + Cash Flow | {company} | {fy_label or 'FY'}",
        },
        {
            "kind": "segments",
            "section": "Segment Performance",
            "available": "segments" not in blocked_sections and _has_approved_image_data(segment_value_rows, segment_columns),
            "render": lambda rows=segment_rows, columns=segment_columns: render_segment_image(
                normalized,
                announcement,
                output_dir,
                display_unit,
                standalone_tag=standalone_tag,
                approved_rows=rows,
                approved_columns=columns,
            ),
            "caption": f"Segment Performance | {company} | {_join_period(quarter_label, fy_label)}",
        },
    ]


def _has_approved_image_data(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> bool:
    """Return true only for validator-approved rows and columns."""

    return bool(columns) and any(row_has_value(row) for row in rows)


def _has_values_and_columns(rows: list[dict[str, Any]], result_period: str, *, variables: bool = False) -> bool:
    """Return whether rows contain values that can produce visible columns."""

    if not any(row_has_value(row) for row in rows):
        return False
    if variables:
        from pl_image import variable_display_columns

        return bool(variable_display_columns(rows))
    return bool(result_display_columns(rows, result_period))


def _has_bs_cf_image_data(rows: list[dict[str, Any]], result_period: str) -> bool:
    """Return true only when BS/CF has usable annual variable data."""

    if not _has_values_and_columns(rows, result_period, variables=True):
        return False
    non_cash_value_rows = [
        row for row in rows
        if row_has_value(row) and "netcashinflowoutflowfrom" not in row_key(str(row.get("label") or ""))
    ]
    cash_value_rows = [
        row for row in rows
        if row_has_value(row) and "netcashinflowoutflowfrom" in row_key(str(row.get("label") or ""))
    ]
    if non_cash_value_rows and len(cash_value_rows) < 3:
        parsed = parse_period(result_period)
        if not parsed or parsed[0] in {"Q4", "FY", "H2"}:
            return False
    return True


def _has_pnl_image_data(rows: list[dict[str, Any]], result_period: str) -> bool:
    """Return whether P&L data is complete enough to render safely."""

    columns = result_display_columns(rows, result_period)
    if not columns:
        return False
    value_periods = [column.get("period", "") for column in columns if column.get("kind") == "value"]
    revenue = _row_by_role(rows, "revenue") or _row_by_label(rows, "Revenue") or _row_by_label(rows, "Total Income")
    if not revenue:
        return False
    revenue_values = revenue.get("values") or {}
    current_periods = _current_periods_for_result(value_periods, result_period)
    if not any(str(revenue_values.get(period, "")).strip() for period in current_periods):
        return False
    if not _has_any_current_value(rows, current_periods, {"PAT", "Profit Before Tax"}):
        return False

    core_labels = {
        "Gross Profit",
        "EBITDA",
        "Profit Before Tax",
        "PAT",
        "EPS (Basic)",
        "Total Expenses",
    }
    core_count = sum(1 for label in core_labels if _has_label_value(rows, label, current_periods))
    if core_count < 2:
        return False

    supporting_labels = {
        "Profit Before Tax",
        "EPS (Basic)",
        "Cost of materials consumed",
        "Purchases of stock-in-trade",
        "Employee benefits expense",
        "Other expenses",
    }
    supporting_count = 0
    supporting_count += sum(
        1
        for row in rows
        if str(row.get("formula_role") or "")
        in {"gross_component", "employee", "operating_expense", "depreciation", "finance", "tax"}
        and any(str((row.get("values") or {}).get(period, "")).strip() for period in value_periods)
    )
    for label in supporting_labels:
        row = _row_by_label(rows, label)
        if row and any(str((row.get("values") or {}).get(period, "")).strip() for period in value_periods):
            supporting_count += 1
    return supporting_count >= 1


def _has_any_current_value(rows: list[dict[str, Any]], periods: list[str], labels: set[str]) -> bool:
    """Return true when any candidate label has a value in the current result period."""

    return any(_has_label_value(rows, label, periods) for label in labels)


def _has_label_value(rows: list[dict[str, Any]], label: str, periods: list[str]) -> bool:
    """Return true when a normalized row label has any non-empty value."""

    row = _row_by_label(rows, label)
    if not row:
        return False
    values = row.get("values") or {}
    return any(str(values.get(period, "")).strip() for period in periods)


def _current_periods_for_result(value_periods: list[str], result_period: str) -> list[str]:
    """Return the current period(s) that must carry core values for rendering."""

    parsed = parse_period(result_period)
    if parsed:
        kind, year = parsed
        expected = f"{kind} FY{year:02d}" if kind != "FY" else f"FY{year:02d}"
        for period in value_periods:
            if period.strip().upper().replace(" ", "") == expected.upper().replace(" ", ""):
                return [period]
    return value_periods


def _row_by_label(rows: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    """Return a row by normalized label."""

    wanted = row_key(label)
    for row in rows:
        if row_key(str(row.get("label") or "")) == wanted:
            return row
    return None


def _row_by_role(rows: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    """Return the first row tagged with a formula role."""

    for row in rows:
        if str(row.get("formula_role") or "") == role:
            return row
    return None


def _announcement_date(extraction: dict[str, Any], announcement: Announcement | None) -> str:
    """Return the best date string for output path and warning messages."""

    date_value = str(extraction.get("board_meeting_date") or "")
    if not date_value and announcement:
        date_value = normalize_date(announcement.announcement_datetime)
    return date_value or datetime.now().strftime("%Y-%m-%d")


def _statement_basis(extraction: dict[str, Any]) -> str:
    """Detect consolidated vs standalone basis from metadata or OCR markdown."""

    basis = str(extraction.get("statement_basis") or "").strip().lower()
    if basis in {"consolidated", "standalone", "single_statement"}:
        return basis
    parser_message = str(extraction.get("parser_message") or "").lower()
    if "only standalone" in parser_message:
        return "standalone"
    if _ocr_has_consolidated_marker(extraction):
        return "consolidated"
    text = str(extraction.get("ocr_markdown") or extraction.get("raw_ocr_markdown") or "").lower()
    if "standalone" in text or "audited standalone" in text:
        return "standalone"
    return ""


def _standalone_conflicts_with_consolidated_source(extraction: dict[str, Any]) -> bool:
    """Return true when standalone output should be skipped because consolidated exists."""

    if _statement_basis(extraction) != "standalone":
        return False
    discovery = extraction.get("discovery_metadata")
    if isinstance(discovery, dict):
        return bool(discovery.get("consolidated_available"))
    return _ocr_has_consolidated_marker(extraction)


def _ocr_has_consolidated_marker(extraction: dict[str, Any]) -> bool:
    """Return whether OCR text says a consolidated statement exists."""

    text = str(extraction.get("ocr_markdown") or extraction.get("raw_ocr_markdown") or "").lower()
    if any(
        pattern in text
        for pattern in (
            "no subsidiary",
            "does not have any subsidiary",
            "not required to prepare consolidated",
            "consolidated financial statements are not applicable",
            "no consolidated financial statements",
        )
    ):
        return False
    return bool(re.search(r"\b(?:audited\s+)?consolidated\s+financial\s+results?\b|\bconsolidated\s+statement\b", text))


def _period_caption_parts(extraction: dict[str, Any]) -> tuple[str, str]:
    """Return quarter and FY labels for captions."""

    period = str(extraction.get("result_period") or "")
    parsed = parse_period(period)
    if parsed:
        kind, year = parsed
        if kind == "FY":
            return "FY", f"FY{year:02d}"
        return kind, f"FY{year:02d}"
    for row in extraction.get("financial_rows") or []:
        if not isinstance(row, dict):
            continue
        for key in (row.get("values") or {}).keys():
            parsed_key = parse_period(str(key))
            if parsed_key:
                kind, year = parsed_key
                return kind, f"FY{year:02d}"
    return "Result", ""


def _join_period(quarter_label: str, fy_label: str) -> str:
    """Join quarter/FY labels without duplication."""

    if not fy_label:
        return quarter_label
    if quarter_label == "FY":
        return fy_label
    return f"{quarter_label} {fy_label}"


def _dedupe_warnings(warnings: list[str]) -> list[str]:
    """Return warnings in stable order without duplicates."""

    output: list[str] = []
    seen: set[str] = set()
    for warning in warnings:
        cleaned = re.sub(r"\s+", " ", str(warning or "")).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            output.append(cleaned)
    return output
