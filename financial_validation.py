"""Deterministic validation for LLM financial extraction payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs_cf_image import build_bs_cf_rows
from financial_cell_model import (
    annotate_extraction_with_cell_model,
    canonical_cell_issues,
)
from pl_image import (
    build_pl_rows,
    change_for_row,
    has_repeated_value_vector_artifact,
    normalize_rows,
    result_display_columns,
    row_has_value,
    row_key,
    to_number,
    variable_display_columns,
    _looks_like_model_calculated_row,
)
from segment_image import build_segment_rows
from table_repair_engine import repair_financial_payload
from unit_detector import normalize_extraction_units


@dataclass(slots=True)
class ValidationResult:
    """Validation status attached before image generation."""

    status: str
    allows_images: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "validation_status": self.status,
            "validation_allows_images": self.allows_images,
            "renderer_input_validation_status": "PASS" if self.allows_images else "FAIL",
            "render_gate": "PASS" if self.allows_images else "BLOCK_RENDER",
            "validation_errors": self.issues,
            "validation_warnings": self.warnings,
            "validation_failure_categories": classify_validation_issues(self.issues, self.warnings),
        }
        payload.update(self.metadata)
        return payload


def validate_financial_payload(extraction: dict[str, Any], announcement: Any | None = None) -> ValidationResult:
    """Validate one extraction payload before images are generated."""

    issues: list[str] = []
    warnings: list[str] = []
    data = dict(extraction or {})
    company = str(data.get("company_name") or getattr(announcement, "company_name", "") or "").strip()
    date = str(data.get("board_meeting_date") or getattr(announcement, "announcement_datetime", "") or "").strip()
    if _is_llm_values_first_mode(data):
        return _validate_llm_values_first_payload(data, announcement)
    if not company:
        issues.append("company_name_missing")
    if _requires_gpt54_execution_check(data) and not _gpt54_execution_verified(data):
        issues.append("gpt54_execution_unverified")
    auditor = data.get("financial_auditor") if isinstance(data.get("financial_auditor"), dict) else {}
    auditor_status = str(data.get("auditor_validation_status") or auditor.get("validation_status") or "").strip().upper()
    if auditor_status == "FAIL":
        failed_checks = [str(item) for item in (auditor.get("failed_checks") or []) if str(item).strip()]
        if not failed_checks:
            failed_checks = ["financial_auditor_validation_failed"]
        issues.extend(f"auditor_validation_failed:{item}" for item in failed_checks)

    normalized, source_unit, display_unit, unit_warnings = normalize_extraction_units(
        data,
        company=company,
        announcement_date=date,
    )
    if not normalized.get("table_repair_metadata"):
        normalized = repair_financial_payload(
            normalized,
            company=company,
            source_pdf=str(data.get("pdf_path") or getattr(announcement, "pdf_path", "") or ""),
        )
    normalized = annotate_extraction_with_cell_model(
        normalized,
        source_pdf=str(data.get("pdf_path") or getattr(announcement, "pdf_path", "") or ""),
    )
    warnings.extend(unit_warnings)
    if not display_unit:
        issues.append("currency_unit_missing")

    repair_issues = [
        str(item)
        for item in (normalized.get("repair_critical_issues") or [])
        if str(item).strip()
    ]
    issues.extend(repair_issues)
    issues.extend(canonical_cell_issues(normalized))

    basis = str(normalized.get("statement_basis") or "unknown").strip().lower()
    if basis == "standalone":
        warnings.append("only_standalone_data_found")
    elif basis not in {"consolidated", "single_statement", "unknown"}:
        warnings.append(f"unexpected_statement_basis:{basis}")
    elif basis == "unknown":
        warnings.append("statement_basis_unknown")

    financial_rows = normalize_rows(normalized.get("financial_rows"))
    if financial_rows and has_repeated_value_vector_artifact(financial_rows):
        issues.append("repeated_identical_value_vector_artifact")

    duplicate_labels = _duplicate_row_labels(financial_rows)
    if duplicate_labels:
        warnings.append(f"duplicate_financial_rows:{','.join(duplicate_labels[:8])}")

    all_value_rows = _all_value_rows(normalized)
    if not all_value_rows:
        issues.append("no_financial_values_found")
    if _mostly_non_numeric_values(all_value_rows):
        issues.append("numeric_parsing_failed_for_most_values")

    periods = _periods_from_rows(financial_rows)
    if financial_rows and not periods:
        issues.append("period_columns_missing")
    generic_period_issue = _generic_period_label_issue(periods)
    if generic_period_issue:
        issues.append(generic_period_issue)
    if periods and not str(normalized.get("result_period") or "").strip():
        warnings.append("result_period_missing")

    pl_rows = build_pl_rows(financial_rows)
    if financial_rows and not pl_rows:
        issues.append("pnl_rows_rejected_by_renderer")
    issues.extend(_source_pnl_formula_issues(financial_rows, str(normalized.get("result_period") or ""), company))
    formula_issues = _pnl_formula_issues(pl_rows, str(normalized.get("result_period") or ""), company)
    issues.extend(formula_issues)

    try:
        bs_cf_rows = build_bs_cf_rows(normalized)
    except Exception as exc:
        warnings.append(f"bs_cf_validation_exception:{type(exc).__name__}")
        bs_cf_rows = []
    try:
        segment_rows = build_segment_rows(normalized)
    except Exception as exc:
        warnings.append(f"segment_validation_exception:{type(exc).__name__}")
        segment_rows = []

    available_sections = _available_render_sections(pl_rows, bs_cf_rows, segment_rows)
    if not available_sections:
        issues.append("no_renderable_financial_image_section")

    render_blocked_sections = _render_blocked_sections_for_issues(issues)
    global_issues = [
        issue
        for issue in issues
        if not _is_section_specific_render_issue(issue)
    ]
    renderable_after_section_blocks = sorted(section for section in available_sections if section not in render_blocked_sections)
    critical_issues = [issue for issue in issues if _is_critical_render_issue(issue)]
    if critical_issues:
        render_blocked_sections.update(available_sections or {"pnl", "bs_cf", "segments"})
        renderable_after_section_blocks = []
    blocking = bool(global_issues) or bool(critical_issues) or (bool(issues) and not renderable_after_section_blocks)
    if blocking:
        status = "failed"
    elif issues or warnings:
        status = "needs_review"
    else:
        status = "ok"
    allows_images = not blocking
    pnl_columns = result_display_columns(pl_rows, str(normalized.get("result_period") or ""))
    bs_cf_columns = variable_display_columns(bs_cf_rows)
    segment_columns = result_display_columns(segment_rows, str(normalized.get("result_period") or ""))
    approved_pnl_rows = _rows_approved_for_render(pl_rows, pnl_columns) if allows_images and "pnl" in renderable_after_section_blocks else []
    approved_bs_cf_rows = _rows_approved_for_render(
        bs_cf_rows,
        bs_cf_columns,
    ) if allows_images and "bs_cf" in renderable_after_section_blocks else []
    approved_segment_rows = _rows_approved_for_render(
        segment_rows,
        segment_columns,
    ) if allows_images and "segments" in renderable_after_section_blocks else []
    metadata = {
        "source_currency_unit": source_unit,
        "currency_unit": display_unit or str(normalized.get("currency_unit") or ""),
        "conversion_provenance": normalized.get("conversion_provenance") or {},
        "discovery_metadata": normalized.get("discovery_metadata") or {},
        "table_repair_metadata": normalized.get("table_repair_metadata") or {},
        "repair_critical_issues": normalized.get("repair_critical_issues") or [],
        "repair_warning_categories": normalized.get("repair_warning_categories") or [],
        "column_identities": normalized.get("column_identities") or [],
        "financial_auditor": normalized.get("financial_auditor") or auditor or {},
        "auditor_validation_status": normalized.get("auditor_validation_status") or auditor_status,
        "canonical_financial_cell_count": normalized.get("canonical_financial_cell_count") or 0,
        "canonical_semantic_summary": normalized.get("canonical_semantic_summary") or {},
        "critical_validation_issues": _dedupe(critical_issues),
        "render_blocked_sections": sorted(render_blocked_sections),
        "renderable_sections": renderable_after_section_blocks,
        "approved_pnl_rows": approved_pnl_rows,
        "approved_bs_cf_rows": approved_bs_cf_rows,
        "approved_segment_rows": approved_segment_rows,
        "approved_pnl_columns": pnl_columns if approved_pnl_rows else [],
        "approved_bs_cf_columns": bs_cf_columns if approved_bs_cf_rows else [],
        "approved_segment_columns": segment_columns if approved_segment_rows else [],
    }
    return ValidationResult(
        status=status,
        allows_images=allows_images,
        issues=_dedupe(issues),
        warnings=_dedupe(warnings),
        metadata=metadata,
    )


def _is_llm_values_first_mode(data: dict[str, Any]) -> bool:
    mode = str(data.get("extraction_mode") or "").strip().lower()
    return bool(data.get("llm_values_first_mode")) or mode in {
        "llm_values_first_mode",
        "llm-values-first",
        "llm_values_first",
        "values_first",
    }


def _validate_llm_values_first_payload(extraction: dict[str, Any], announcement: Any | None = None) -> ValidationResult:
    """Warning-only validation for GPT-render-payload mode.

    This mode intentionally does not run formula, provenance, strict unit, or
    repair gates. GPT returns final image values; Python only blocks unusable
    output that cannot be rendered safely.
    """

    data = dict(extraction or {})
    company = str(data.get("company_name") or getattr(announcement, "company_name", "") or "").strip()
    issues: list[str] = []
    warnings: list[str] = [str(item) for item in (data.get("warnings") or []) if str(item or "").strip()]
    if not company:
        issues.append("company_name_missing")

    render_decision = data.get("render_decision") if isinstance(data.get("render_decision"), dict) else {}
    should_render = render_decision.get("should_render")
    if isinstance(should_render, str):
        should_render = should_render.strip().lower() not in {"0", "false", "no", "n"}
    if should_render is False:
        reason = str(render_decision.get("reason") or "llm_render_decision_false").strip()
        issues.append(f"llm_render_decision_false:{reason}")

    approved_pnl_rows = data.get("approved_pnl_rows") if isinstance(data.get("approved_pnl_rows"), list) else []
    approved_bs_cf_rows = data.get("approved_bs_cf_rows") if isinstance(data.get("approved_bs_cf_rows"), list) else []
    approved_segment_rows = data.get("approved_segment_rows") if isinstance(data.get("approved_segment_rows"), list) else []
    approved_pnl_columns = data.get("approved_pnl_columns") if isinstance(data.get("approved_pnl_columns"), list) else []
    approved_bs_cf_columns = data.get("approved_bs_cf_columns") if isinstance(data.get("approved_bs_cf_columns"), list) else []
    approved_segment_columns = data.get("approved_segment_columns") if isinstance(data.get("approved_segment_columns"), list) else []

    issues.extend(_llm_values_first_total_income_issues(approved_pnl_rows, approved_pnl_columns))

    has_pnl = bool(approved_pnl_rows and approved_pnl_columns and any(row_has_value(row) for row in approved_pnl_rows))
    has_bs_cf = bool(approved_bs_cf_rows and approved_bs_cf_columns and any(row_has_value(row) for row in approved_bs_cf_rows))
    has_segment = bool(
        approved_segment_rows
        and approved_segment_columns
        and any(row_has_value(row) for row in approved_segment_rows)
    )
    if not has_pnl:
        issues.append("llm_values_first_no_pnl_rows")
    if not any((has_pnl, has_bs_cf, has_segment)):
        issues.append("llm_values_first_all_sections_empty")

    renderable_sections = []
    if has_pnl:
        renderable_sections.append("pnl")
    if has_bs_cf:
        renderable_sections.append("bs_cf")
    if has_segment:
        renderable_sections.append("segments")

    allows_images = not issues
    metadata = {
        "source_currency_unit": str(data.get("source_currency_unit") or data.get("source_unit") or ""),
        "currency_unit": str(data.get("currency_unit") or data.get("display_unit") or ""),
        "strict_validation": False,
        "render_with_warnings": False,
        "llm_values_first_warning_only_validation": True,
        "critical_validation_issues": [],
        "render_blocked_sections": [] if allows_images else ["pnl", "bs_cf", "segments"],
        "renderable_sections": renderable_sections if allows_images else [],
        "approved_pnl_rows": approved_pnl_rows if allows_images else [],
        "approved_bs_cf_rows": approved_bs_cf_rows if allows_images else [],
        "approved_segment_rows": approved_segment_rows if allows_images else [],
        "approved_pnl_columns": approved_pnl_columns if allows_images else [],
        "approved_bs_cf_columns": approved_bs_cf_columns if allows_images else [],
        "approved_segment_columns": approved_segment_columns if allows_images else [],
        "llm_values_first_debug": data.get("llm_values_first_debug") or {},
    }
    if warnings:
        metadata["llm_values_first_warning_log"] = _dedupe(warnings)
    return ValidationResult(
        status="failed" if issues else ("needs_review" if warnings else "ok"),
        allows_images=allows_images,
        issues=_dedupe(issues),
        warnings=_dedupe(warnings),
        metadata=metadata,
    )


def _llm_values_first_total_income_issues(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
) -> list[str]:
    """Block render payloads whose returned income components cannot reconcile."""

    mapped = _row_map(rows)
    revenue_row = mapped.get("revenue") or mapped.get("revenuefromoperations")
    other_row = mapped.get("otherincome")
    total_row = mapped.get("totalincome")
    if not revenue_row or not other_row or not total_row:
        return []
    periods = [str(column.get("period") or column.get("label") or "").strip() for column in columns]
    issues: list[str] = []
    for period in periods:
        if not period:
            continue
        revenue = to_number((revenue_row.get("values") or {}).get(period))
        other_income = to_number((other_row.get("values") or {}).get(period))
        total_income = to_number((total_row.get("values") or {}).get(period))
        if revenue is None or other_income is None or total_income is None:
            continue
        expected = revenue + other_income
        tolerance = max(0.05, abs(total_income) * 0.005)
        if abs(total_income - expected) > tolerance:
            issues.append(f"llm_values_first_total_income_component_mismatch:{period}")
    return issues


def classify_validation_issues(issues: list[str], warnings: list[str] | None = None) -> list[str]:
    """Return stable high-level failure buckets for logs, reports, and Telegram text."""

    categories: list[str] = []
    all_items = [str(item or "") for item in (issues or []) + (warnings or [])]
    checks = (
        ("UNIT_ERROR", ("unit_not_detected", "currency_unit_missing", "unit_conversion_failure", "eps_converted")),
        ("STATEMENT_BASIS_ERROR", ("consolidated_available_but_standalone", "statement_basis")),
        ("COLUMN_SHIFT_ERROR", ("q4_equals_fy", "column_mapping_failure", "repeated_value_collision", "repeated_identical_value")),
        ("PERIOD_LAYOUT_ERROR", ("period_columns_missing", "expected_layout_missing", "period_column_mapping_unknown")),
        ("CASH_FLOW_MISSING_ERROR", ("cash_flow_period_missing", "cash_flow_header", "cash_flow_closing_cash")),
        ("BALANCE_SHEET_ERROR", ("balance_sheet_total_mismatch", "balance_sheet")),
        ("FORMULA_MISMATCH_ERROR", ("formula_mismatch",)),
        ("NO_DATA", ("no_financial_values_found", "no_renderable_financial_image_section", "pnl_rows_rejected_by_renderer")),
        ("NUMERIC_PARSE_ERROR", ("numeric_parsing_failed",)),
        ("REPEATED_VALUE_ERROR", ("repeated_value",)),
        ("PROVENANCE_ERROR", ("canonical_cell_missing_provenance",)),
        ("REVENUE_MAPPING_ERROR", ("revenue_operating_row_missing_total_income_present",)),
        ("TOTAL_EXPENSES_ERROR", ("pdf_total_expenses_used_as_direct_expense",)),
        ("GPT_VERIFICATION_ERROR", ("gpt54_execution_unverified",)),
        ("AUDITOR_VALIDATION_ERROR", ("auditor_validation_failed",)),
    )
    for category, needles in checks:
        if any(any(needle in item for needle in needles) for item in all_items):
            categories.append(category)
    if issues and not categories:
        categories.append("VALIDATION_ERROR")
    return _dedupe(categories)


def attach_validation(extraction: dict[str, Any], result: ValidationResult) -> dict[str, Any]:
    """Return a copy of extraction with validation metadata attached."""

    output = dict(extraction or {})
    output.update(result.as_dict())
    return output


def _all_value_rows(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(normalize_rows(extraction.get("financial_rows")))
    rows.extend(normalize_rows(extraction.get("cash_flow_variables")))
    rows.extend(normalize_rows(extraction.get("key_variables")))
    for section in extraction.get("balance_sheet_variables") or []:
        if isinstance(section, dict):
            rows.extend(normalize_rows(section.get("rows")))
    for table in extraction.get("segment_tables") or []:
        if isinstance(table, dict):
            rows.extend(normalize_rows(table.get("rows")))
    return [row for row in rows if row_has_value(row)]


def _rows_approved_for_render(rows: list[dict[str, Any]], columns: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Freeze display rows and change percentages before they reach renderers."""

    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        changes = dict(item.get("changes") or {}) if isinstance(item.get("changes"), dict) else {}
        for column in columns:
            if column.get("kind") != "change":
                continue
            from_period = str(column.get("from") or "")
            to_period = str(column.get("to") or "")
            changes[f"{from_period}->{to_period}"] = change_for_row(item, from_period, to_period)
        item["changes"] = changes
        item["_approved_for_render"] = True
        output.append(item)
    return output


def _available_render_sections(
    pl_rows: list[dict[str, Any]],
    bs_cf_rows: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
) -> set[str]:
    sections: set[str] = set()
    if any(row_has_value(row) for row in pl_rows):
        sections.add("pnl")
    if any(row_has_value(row) for row in bs_cf_rows):
        sections.add("bs_cf")
    if any(row_has_value(row) for row in segment_rows):
        sections.add("segments")
    return sections


def _render_blocked_sections_for_issues(issues: list[str]) -> set[str]:
    sections: set[str] = set()
    for issue in issues:
        if _is_critical_render_issue(issue):
            sections.update({"pnl", "bs_cf", "segments"})
        elif issue.startswith("cash_flow_") or issue.startswith("balance_sheet_"):
            sections.add("bs_cf")
        elif issue.startswith("formula_mismatch:"):
            sections.add("pnl")
    return sections


def _is_section_specific_render_issue(issue: str) -> bool:
    if _is_critical_render_issue(issue):
        return False
    return (
        issue.startswith("cash_flow_")
        or issue.startswith("balance_sheet_")
        or issue.startswith("formula_mismatch:")
    )


def _is_critical_render_issue(issue: str) -> bool:
    text = str(issue or "")
    critical_prefixes = (
        "company_name_missing",
        "gpt54_execution_unverified",
        "auditor_validation_failed",
        "currency_unit_missing",
        "unit_not_detected",
        "wrong_unit",
        "wrong_basis",
        "missing_provenance",
        "unit_conversion_failure",
        "eps_converted",
        "canonical_cell_missing_provenance",
        "pdf_total_expenses_used_as_direct_expense",
        "revenue_operating_row_missing_total_income_present",
        "revenue_uses_total_income",
        "other_income_missing",
        "repair_critical",
        "repeated_identical_value_vector_artifact",
        "no_financial_values_found",
        "numeric_parsing_failed_for_most_values",
        "period_columns_missing",
        "period_column_mapping_unknown",
        "pnl_rows_rejected_by_renderer",
        "no_renderable_financial_image_section",
        "formula_mismatch:",
        "cash_flow_",
        "balance_sheet_",
        "segment_required_missing",
        "exceptional_item_visible_missing",
        "associate_or_jv_visible_missing",
        "discontinued_operations_visible_missing",
        "generated_images_zero",
        "gross_profit_formula_failure",
        "ebitda_formula_failure",
        "pbt_formula_failure",
        "pat_formula_failure",
    )
    if text.startswith(critical_prefixes):
        return True
    critical_needles = (
        "q4_equals_fy",
        "column_mapping_failure",
        "consolidated_available_but_standalone",
        "standalone_segment_used_while_consolidated_exists",
        "raw_lakh_values_rendered_as_crores",
        "total_expenses_includes_depreciation_or_finance",
        "pdf total expenses",
        "total expenses used as direct",
        "exceptional_items_visible_but_missing",
        "share_of_associate_or_jv_visible_but_missing",
        "discontinued_operations_visible_but_missing",
        "segment_exists_but_missing",
        "balance sheet exists but no",
        "cash flow exists but no",
        "intermediate cash flow",
    )
    return any(needle in text for needle in critical_needles)


def _duplicate_row_labels(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        key = row_key(str(row.get("label") or ""))
        if not key:
            continue
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    return duplicates


def _mostly_non_numeric_values(rows: list[dict[str, Any]]) -> bool:
    total = 0
    numeric = 0
    for row in rows:
        for value in (row.get("values") or {}).values():
            text = str(value or "").strip()
            if not text:
                continue
            total += 1
            if to_number(text) is not None or text.endswith("%"):
                numeric += 1
    return total >= 6 and numeric / total < 0.5


def _periods_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    periods: list[str] = []
    for row in rows:
        for period in (row.get("values") or {}):
            if period not in periods:
                periods.append(str(period))
    return periods


def _requires_gpt54_execution_check(extraction: dict[str, Any]) -> bool:
    layer = str(extraction.get("extraction_layer") or "").strip().lower()
    status = str(extraction.get("gpt_json_status") or "").strip().lower()
    return layer.startswith("gpt54") or status in {"valid", "mock_valid_json"}


def _gpt54_execution_verified(extraction: dict[str, Any]) -> bool:
    metadata = extraction.get("gpt54_execution_metadata")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("schema_valid") is not True:
        return False
    if metadata.get("mock") is True:
        return True
    if not str(metadata.get("model") or "").strip():
        return False
    if not str(metadata.get("responses_url_host") or "").strip():
        return False
    return True


def _generic_period_label_issue(periods: list[str]) -> str:
    """Block unsafe render when model output did not map source headers to Q/H/FY periods."""

    if not periods:
        return ""
    generic = [period for period in periods if _is_generic_period_label(period)]
    if not generic:
        return ""
    canonical = [period for period in periods if _is_canonical_period_label(period)]
    if len(canonical) < max(2, len(periods) - len(generic)):
        return f"period_column_mapping_unknown:{','.join(generic[:5])}"
    if generic:
        return f"period_column_mapping_unknown:{','.join(generic[:5])}"
    return ""


def _is_generic_period_label(period: str) -> bool:
    text = re.sub(r"[_\s]+", " ", str(period or "").strip().upper())
    if not text:
        return False
    return text in {
        "CURRENT QUARTER",
        "PREVIOUS QUARTER",
        "CORRESPONDING QUARTER",
        "CORRESPONDING CURRENT QUARTER",
        "YEAR TO DATE",
        "YEAR TO DATE FIGURES",
        "YEAR TO DATE FIGURES FOR PREVIOUS YEAR",
        "CURRENT YEAR",
        "PREVIOUS YEAR",
    }


def _is_canonical_period_label(period: str) -> bool:
    text = str(period or "").strip().upper()
    return bool(re.fullmatch(r"(?:Q[1-4]|H[12]|9M)\s+FY\d{2}", text) or re.fullmatch(r"FY\d{2}", text))


def _source_pnl_formula_issues(rows: list[dict[str, Any]], result_period: str, company: str) -> list[str]:
    """Validate formula rows against the raw mapped extraction before renderer rewrites them."""

    if not rows:
        return []
    columns = result_display_columns(rows, result_period)
    periods = [str(column.get("period") or "") for column in columns if column.get("kind") == "value"]
    mapped = _row_map(rows)
    issues: list[str] = []
    for period in periods:
        revenue = _first_number(
            _row_number(mapped, "revenue", period),
            _row_number(mapped, "revenuefromoperations", period),
            _row_number(mapped, "incomefromoperations", period),
            _row_number(mapped, "netsales", period),
        )
        gross_profit = None if _looks_like_model_calculated_row(mapped.get("grossprofit")) else _row_number(mapped, "grossprofit", period)
        direct_sum, has_direct = _source_direct_expense_sum(rows, period)
        employee = _first_number(_row_number(mapped, "employeebenefitsexpense", period), _row_number(mapped, "employeebenefitexpense", period)) or 0.0
        other_expense = _row_number(mapped, "otherexpenses", period) or 0.0
        ebitda = None if _looks_like_model_calculated_row(mapped.get("ebitda")) else _row_number(mapped, "ebitda", period)
        depreciation = _first_number(_row_number(mapped, "depreciationandamortisationexpense", period), _row_number(mapped, "depreciation", period)) or 0.0
        finance = _first_number(_row_number(mapped, "financecosts", period), _row_number(mapped, "financecost", period)) or 0.0
        pbei = None if _looks_like_model_calculated_row(mapped.get("profitbeforeexceptionalitemsotherincome")) else _first_number(_row_number(mapped, "profitbeforeexceptionalitemsotherincome", period), _row_number(mapped, "profitbeforeexceptionalitems", period))
        other_income = _row_number(mapped, "otherincome", period) or 0.0
        associate_share = _first_number(_row_number(mapped, "shareofassociatesjv", period), _row_number(mapped, "shareofprofitorlossofassociatesandjointventures", period)) or 0.0
        exceptional = _row_number(mapped, "exceptionalitems", period) or 0.0
        pbt = _row_number(mapped, "profitbeforetax", period)
        tax = _first_number(_row_number(mapped, "totaltaxexpense", period), _row_number(mapped, "taxexpense", period)) or 0.0
        pat = _first_number(_row_number(mapped, "pat", period), _row_number(mapped, "profitaftertax", period)) 
        source_checks = [
            ("source_gross_profit", gross_profit, None if revenue is None or not has_direct else revenue - direct_sum),
            ("source_ebitda", ebitda, None if gross_profit is None else gross_profit - employee - other_expense),
            ("source_profit_before_exceptional", pbei, None if ebitda is None else ebitda - depreciation - finance),
            ("source_pbt", pbt, None if pbei is None else pbei + other_income + associate_share + exceptional),
            ("source_pat", pat, None if pbt is None else pbt - tax),
        ]
        for label, actual, expected in source_checks:
            if actual is None or expected is None:
                continue
            if abs(actual - expected) > 0.06:
                issues.append(f"formula_mismatch:{company}:{period}:{label}:actual={actual:.4f}:expected={expected:.4f}")
    return issues


def _source_direct_expense_sum(rows: list[dict[str, Any]], period: str) -> tuple[float, bool]:
    total = 0.0
    found = False
    for row in rows:
        key = row_key(str(row.get("label") or ""))
        if not key or not _is_source_direct_expense_key(key):
            continue
        value = to_number((row.get("values") or {}).get(period))
        if value is None:
            continue
        total += value
        found = True
    return total, found


def _is_source_direct_expense_key(key: str) -> bool:
    if any(
        needle in key
        for needle in (
            "totalexpenses",
            "totalexpense",
            "employee",
            "otherexpenses",
            "depreciation",
            "financecost",
            "otherincome",
            "totalincome",
            "profit",
            "tax",
            "eps",
            "grossprofit",
            "ebitda",
        )
    ):
        return False
    direct_needles = (
        "costofmaterials",
        "costofraw",
        "purchaseofstock",
        "purchasesofstock",
        "purchaseofgoods",
        "costoftradedgoods",
        "costofsales",
        "costofproduction",
        "constructionexpenses",
        "landdevelopment",
        "changesininventories",
        "feesandcommission",
        "impairmentonfinancial",
        "directexpenses",
        "powerandfuel",
    )
    return any(needle in key for needle in direct_needles)


def _pnl_formula_issues(rows: list[dict[str, Any]], result_period: str, company: str) -> list[str]:
    if not rows:
        return []
    columns = result_display_columns(rows, result_period)
    periods = [str(column.get("period") or "") for column in columns if column.get("kind") == "value"]
    mapped = _row_map(rows)
    issues: list[str] = []
    for period in periods:
        issues.extend(_period_formula_issues(company, period, rows, mapped))
    return issues


def _period_formula_issues(
    company: str,
    period: str,
    rows: list[dict[str, Any]],
    mapped: dict[str, dict[str, Any]],
) -> list[str]:
    revenue = _first_number(_role_number(rows, "revenue", period), _row_number(mapped, "revenue", period), _row_number(mapped, "totalincome", period))
    gross_component_sum, has_gross_components = _role_sum(rows, "gross_component", period)
    gross_profit = _row_number(mapped, "grossprofit", period)
    employee = _first_number(_role_number(rows, "employee", period), _row_number(mapped, "employeebenefitsexpense", period)) or 0.0
    other_expense = _first_number(_role_number(rows, "operating_expense", period), _row_number(mapped, "otherexpenses", period)) or 0.0
    ebitda = _row_number(mapped, "ebitda", period)
    depreciation = _first_number(
        _role_number(rows, "depreciation", period),
        _row_number(mapped, "depreciationandamortisationexpense", period),
        _row_number(mapped, "depreciation", period),
    ) or 0.0
    finance = _first_number(_role_number(rows, "finance", period), _row_number(mapped, "financecosts", period), _row_number(mapped, "financecost", period)) or 0.0
    total_expenses = _first_number(_role_number(rows, "total_expenses", period), _row_number(mapped, "totalexpenses", period), _row_number(mapped, "totalexpense", period))
    pbei = _row_number(mapped, "profitbeforeexceptionalitemsotherincome", period)
    other_income = _first_number(_role_number(rows, "other_income", period), _row_number(mapped, "otherincome", period)) or 0.0
    associate_share = _first_number(_role_number(rows, "associate_share", period), _row_number(mapped, "shareofassociatesjv", period)) or 0.0
    exceptional = _first_number(_role_number(rows, "exceptional", period), _row_number(mapped, "exceptionalitems", period)) or 0.0
    pbt = _row_number(mapped, "profitbeforetax", period)
    tax = _first_number(_role_number(rows, "tax", period), _row_number(mapped, "totaltaxexpense", period), _row_number(mapped, "totaltaxexpenses", period)) or 0.0
    pat = _row_number(mapped, "pat", period)
    gross_margin = _row_number(mapped, "grossprofitmargin", period)
    ebitda_margin = _row_number(mapped, "ebitdamargin", period)
    pat_margin = _row_number(mapped, "patmargin", period)

    pbei_basis = _row_basis(mapped, "profitbeforeexceptionalitemsotherincome")
    if pbei_basis == "direct":
        pbei_expected = None
    elif pbei_basis == "revenue_minus_total_expenses":
        pbei_expected = None
    else:
        pbei_expected = None if ebitda is None else ebitda - depreciation - finance

    checks = [
        ("gross_profit", gross_profit, None if _row_basis(mapped, "grossprofit") == "direct" or revenue is None or not has_gross_components else revenue - gross_component_sum),
        ("ebitda", ebitda, None if _row_basis(mapped, "ebitda") == "direct" or gross_profit is None else gross_profit - employee - other_expense),
        ("total_expenses_excluding_dep_finance", total_expenses, None if total_expenses is None or not has_gross_components else gross_component_sum + employee + other_expense),
        ("profit_before_exceptional", pbei, pbei_expected),
        ("pbt", pbt, None if _row_basis(mapped, "profitbeforetax") == "direct" or pbei is None else pbei + other_income + associate_share + exceptional),
        ("pat", pat, None if _row_basis(mapped, "pat") == "direct" or pbt is None else pbt - tax),
        ("gross_profit_margin", gross_margin, _margin_expected(gross_profit, revenue)),
        ("ebitda_margin", ebitda_margin, _margin_expected(ebitda, revenue)),
        ("pat_margin", pat_margin, _margin_expected(pat, revenue)),
    ]
    issues: list[str] = []
    for label, actual, expected in checks:
        if actual is None or expected is None:
            continue
        if abs(actual - expected) > 0.06:
            issues.append(f"formula_mismatch:{company}:{period}:{label}:actual={actual:.4f}:expected={expected:.4f}")
    if pbt is not None and pat is not None and abs(tax) > 0.005 and abs(pbt - pat) <= 0.005:
        issues.append(f"formula_mismatch:{company}:{period}:pat_equals_pbt_with_tax:actual={pat:.4f}:pbt={pbt:.4f}:tax={tax:.4f}")
    return issues


def _margin_expected(numerator: float | None, denominator: float | None) -> float | None:
    """Return expected percentage margin, or None when not applicable."""

    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator) * 100.0


def _row_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aliases = {
        "employeebenefitexpense": "employeebenefitsexpense",
        "financecost": "financecosts",
        "taxexpense": "totaltaxexpense",
        "taxexpenses": "totaltaxexpense",
        "profitaftertax": "pat",
        "depreciation": "depreciationandamortisationexpense",
        "totalexpensesexcluding": "totalexpenses",
        "totalexpensesexcludingdepreciationandfinancecosts": "totalexpenses",
        "profitbeforeexceptionalitems": "profitbeforeexceptionalitemsotherincome",
    }
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row_key(str(row.get("label") or ""))
        mapped[key] = row
        if key in aliases:
            mapped[aliases[key]] = row
    return mapped


def _row_number(mapped: dict[str, dict[str, Any]], key: str, period: str) -> float | None:
    row = mapped.get(key)
    if not row:
        return None
    return to_number((row.get("values") or {}).get(period))


def _row_basis(mapped: dict[str, dict[str, Any]], key: str) -> str:
    return str((mapped.get(key) or {}).get("formula_basis") or "")


def _role_number(rows: list[dict[str, Any]], role: str, period: str) -> float | None:
    for row in rows:
        if str(row.get("formula_role") or "") != role:
            continue
        number = to_number((row.get("values") or {}).get(period))
        if number is not None:
            return number
    return None


def _role_sum(rows: list[dict[str, Any]], role: str, period: str) -> tuple[float, bool]:
    total = 0.0
    matched = False
    for row in rows:
        if str(row.get("formula_role") or "") != role:
            continue
        number = to_number((row.get("values") or {}).get(period))
        if number is None:
            continue
        total += number
        matched = True
    return total, matched


def _first_number(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output
