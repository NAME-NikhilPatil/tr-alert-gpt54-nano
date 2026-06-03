"""Canonical financial cell annotations and generic safety checks.

This module is intentionally company-agnostic.  It converts the existing
``label``/``values`` extraction shape into auditable cells with row semantics
and provenance, then returns critical issues that must block rendering.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pl_image import row_key, to_number
from unit_detector import canonical_currency_unit, monetary_scale_for_source


@dataclass(slots=True)
class CanonicalFinancialCell:
    company_name: str
    source_pdf: str
    source_page: int | None
    source_section: str
    statement_basis: str
    raw_table_title: str
    raw_row_label: str
    raw_column_label: str
    canonical_row_key: str
    canonical_period: str
    period_type: str
    raw_value: str
    raw_unit: str
    normalized_value: str
    normalized_unit: str
    is_blank: bool
    is_eps: bool
    is_percentage: bool
    is_total_row: bool
    is_pdf_reported_total_expenses: bool
    is_calculated_by_pipeline: bool
    source_confidence: float | None
    evidence_snippet: str


def annotate_extraction_with_cell_model(extraction: dict[str, Any], *, source_pdf: str = "") -> dict[str, Any]:
    """Attach canonical cell annotations to an extraction payload."""

    output = dict(extraction or {})
    cells = build_canonical_cells(output, source_pdf=source_pdf)
    output["canonical_financial_cells"] = [asdict(cell) for cell in cells]
    output["canonical_financial_cell_count"] = len(cells)
    output["canonical_semantic_summary"] = semantic_summary(cells)
    return output


def build_canonical_cells(extraction: dict[str, Any], *, source_pdf: str = "") -> list[CanonicalFinancialCell]:
    """Build canonical cells from every supported financial section."""

    company = str(extraction.get("company_name") or "").strip()
    pdf = str(source_pdf or extraction.get("pdf_path") or extraction.get("pdf_name") or "").strip()
    basis = str(extraction.get("statement_basis") or "unknown").strip().lower()
    raw_unit = str(extraction.get("source_currency_unit") or extraction.get("currency_unit") or "").strip()
    display_unit = str(extraction.get("currency_unit") or "").strip()
    cells: list[CanonicalFinancialCell] = []

    def add_rows(rows: Any, *, section: str, table_title: str = "") -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or row.get("raw_label") or "").strip()
            values = row.get("values")
            raw_values = row.get("raw_values") if isinstance(row.get("raw_values"), dict) else {}
            if not label or not isinstance(values, dict):
                continue
            source_page = _source_page(row)
            table = str(row.get("table_title") or row.get("raw_table_title") or table_title or section).strip()
            confidence = _source_confidence(row)
            canonical = canonical_row_key(label)
            for period, value in values.items():
                normalized_value = "" if value is None else str(value).strip()
                raw_value = "" if raw_values.get(period) is None else str(raw_values.get(period)).strip()
                if not raw_value:
                    raw_value = normalized_value
                is_blank = normalized_value == "" or normalized_value.lower() in {"null", "none"}
                cells.append(
                    CanonicalFinancialCell(
                        company_name=company,
                        source_pdf=pdf,
                        source_page=source_page,
                        source_section=section,
                        statement_basis=str(row.get("statement_basis") or basis).strip().lower() or basis,
                        raw_table_title=table,
                        raw_row_label=label,
                        raw_column_label=str(period or "").strip(),
                        canonical_row_key=canonical,
                        canonical_period=str(period or "").strip(),
                        period_type=period_type(str(period or "")),
                        raw_value=raw_value,
                        raw_unit=str(row.get("unit") or row.get("raw_unit") or raw_unit).strip(),
                        normalized_value=normalized_value,
                        normalized_unit=display_unit,
                        is_blank=is_blank,
                        is_eps=is_eps_label(label),
                        is_percentage=is_percentage_label(label) or "%" in normalized_value,
                        is_total_row=is_total_label(label),
                        is_pdf_reported_total_expenses=is_pdf_reported_total_expenses(label),
                        is_calculated_by_pipeline=bool(row.get("is_calculated_by_pipeline") or row.get("formula_basis") == "calculated"),
                        source_confidence=confidence,
                        evidence_snippet=str(row.get("evidence_snippet") or label).strip()[:240],
                    )
                )

    add_rows(extraction.get("financial_rows"), section="profit_and_loss")
    add_rows(extraction.get("cash_flow_variables"), section="cash_flow")
    add_rows(extraction.get("key_variables"), section="key_variables")
    for section in extraction.get("balance_sheet_variables") or []:
        if isinstance(section, dict):
            add_rows(section.get("rows"), section="balance_sheet", table_title=str(section.get("section") or "Balance Sheet"))
    for table in extraction.get("segment_tables") or []:
        if isinstance(table, dict):
            add_rows(table.get("rows"), section="segment", table_title=str(table.get("title") or "Segment"))
    return cells


def canonical_cell_issues(extraction: dict[str, Any]) -> list[str]:
    """Return generic critical issues detected from canonical cells."""

    cells_payload = extraction.get("canonical_financial_cells")
    if not isinstance(cells_payload, list):
        cells = build_canonical_cells(extraction, source_pdf=str(extraction.get("pdf_path") or ""))
    else:
        cells = [_cell_from_dict(item) for item in cells_payload if isinstance(item, dict)]

    issues: list[str] = []
    if not cells:
        return issues
    conversion_provenance = extraction.get("conversion_provenance") if isinstance(extraction.get("conversion_provenance"), dict) else {}
    values_were_already_display_unit = bool(conversion_provenance.get("values_were_already_display_unit"))

    if _requires_provenance(extraction):
        for cell in cells:
            if cell.is_blank:
                continue
            missing = []
            if cell.source_page is None:
                missing.append("source_page")
            if not cell.raw_row_label:
                missing.append("raw_row_label")
            if not cell.raw_column_label:
                missing.append("raw_column_label")
            if not cell.statement_basis:
                missing.append("statement_basis")
            if missing:
                issues.append(
                    "canonical_cell_missing_provenance:"
                    + cell.source_section
                    + ":"
                    + cell.canonical_row_key
                    + ":"
                    + ",".join(missing)
                )

    for cell in cells:
        if cell.is_eps and not _eps_value_preserved(cell.raw_value, cell.normalized_value):
            issues.append(f"eps_converted:{cell.canonical_period}:{cell.raw_row_label}")
        if (
            not cell.is_blank
            and not cell.is_eps
            and not cell.is_percentage
            and cell.raw_value not in {"", "-", "--"}
        ):
            source_unit = canonical_currency_unit(str(cell.raw_unit or ""))
            raw_number = to_number(str(cell.raw_value))
            normalized_number = to_number(str(cell.normalized_value))
            if (
                source_unit
                and raw_number is not None
                and normalized_number is not None
                and not (values_were_already_display_unit and abs(raw_number - normalized_number) <= 0.0001)
            ):
                expected = raw_number * monetary_scale_for_source(source_unit)
                tolerance = max(0.01, abs(expected) * 0.0001)
                if abs(normalized_number - expected) > tolerance:
                    issues.append(
                        "unit_conversion_failure:"
                        + cell.source_section
                        + ":"
                        + cell.canonical_period
                        + ":"
                        + cell.raw_row_label
                    )
        if cell.is_pdf_reported_total_expenses:
            # Reported PDF total expenses are valid source rows, but the P&L
            # formula layer must never treat them as direct expense components.
            if str(cell.evidence_snippet or "").lower().find("formula_role=gross_component") >= 0:
                issues.append(f"pdf_total_expenses_used_as_direct_expense:{cell.canonical_period}")

    labels = {cell.canonical_row_key for cell in cells if cell.source_section == "profit_and_loss" and not cell.is_blank}
    if "total_income" in labels and "revenue_from_operations" not in labels and "revenue" not in labels:
        issues.append("revenue_operating_row_missing_total_income_present")

    return _dedupe(issues)


def semantic_summary(cells: list[CanonicalFinancialCell]) -> dict[str, Any]:
    labels_by_section: dict[str, set[str]] = {}
    for cell in cells:
        if cell.is_blank:
            continue
        labels_by_section.setdefault(cell.source_section, set()).add(cell.canonical_row_key)
    return {section: sorted(values) for section, values in labels_by_section.items()}


def canonical_row_key(label: str) -> str:
    key = row_key(label)
    if not key:
        return "unknown"
    if is_eps_label(label):
        if "diluted" in key:
            return "eps_diluted"
        return "eps_basic"
    if key in {"revenue", "revenuefromoperations", "incomefromoperations", "netsales", "netsalesincomefromoperations", "sales"}:
        return "revenue_from_operations"
    if key in {"totalincome", "totalrevenue"}:
        return "total_income"
    if "otherincome" in key:
        return "other_income"
    if "costofmaterial" in key or "rawmaterial" in key or "packingmaterial" in key:
        return "cost_of_materials_consumed"
    if "purchase" in key and ("stockintrade" in key or "tradedgoods" in key or "goods" in key):
        return "purchase_of_stock_in_trade"
    if "changesininventor" in key or "changeininventor" in key:
        return "changes_in_inventories"
    if "employeebenefit" in key or "employeecost" in key:
        return "employee_benefits_expense"
    if key in {"otherexpenses", "otherexpense", "operatingandotherexpenses", "administrativeandmanufacturingexpenses"}:
        return "other_expenses"
    if "depreciation" in key or "amortisation" in key or "amortization" in key:
        return "depreciation"
    if key in {"financecost", "financecosts"} or "borrowingcost" in key:
        return "finance_cost"
    if is_pdf_reported_total_expenses(label):
        return "pdf_reported_total_expenses"
    if "grossprofit" in key:
        return "gross_profit"
    if "ebitda" in key:
        return "ebitda"
    if "profitbeforeexceptionalitemsotherincome" in key or key == "profitbeforeexceptionalitems":
        return "profit_before_exceptional_items_other_income"
    if "exceptionalitem" in key:
        return "exceptional_item"
    if "shareof" in key and ("associate" in key or "jointventure" in key or "jv" in key):
        return "share_of_associate_or_jv"
    if key in {"profitbeforetax", "profitlossbeforetax", "profitbeforetaxpbt"}:
        return "profit_before_tax"
    if "currenttax" in key:
        return "tax_current"
    if "deferredtax" in key:
        return "tax_deferred"
    if "prioryear" in key or "earlieryear" in key or "earlierperiod" in key or "adjustmentoftax" in key or "shortexcess" in key:
        return "tax_prior_year"
    if "totaltax" in key or key in {"taxexpense", "taxexpenses"}:
        return "tax_total"
    if "continuingoperations" in key:
        return "pat_continuing"
    if "discontinuedoperations" in key:
        return "discontinued_operation"
    if key in {"pat", "profitaftertax", "profitfortheperiod", "profitlossfortheperiod", "netprofit"}:
        return "pat_final"
    if "netcash" in key and "operating" in key:
        return "cash_flow_operating_final"
    if "netcash" in key and "investing" in key:
        return "cash_flow_investing_final"
    if "netcash" in key and "financing" in key:
        return "cash_flow_financing_final"
    if key == "totalassets" or key.endswith("totalassets"):
        return "total_assets"
    if key in {"totalequity", "equity"}:
        return "total_equity"
    if key in {"totalliabilities", "totalcurrentliabilities", "totalnoncurrentliabilities"}:
        return "total_liabilities"
    if "totalequityandliabilities" in key or "totalequityliabilities" in key:
        return "total_equity_and_liabilities"
    if "segment" in key and "revenue" in key:
        return "segment_revenue"
    if "segment" in key and ("result" in key or "profit" in key):
        return "segment_result"
    if "segment" in key and "asset" in key:
        return "segment_assets"
    if "segment" in key and "liabilit" in key:
        return "segment_liabilities"
    return key or "unknown"


def is_eps_label(label: str) -> bool:
    key = row_key(label)
    text = str(label or "").lower()
    return (
        "eps" in key
        or "earningpershare" in key
        or "earningspershare" in key
        or key in {"basic", "diluted", "basicanddiluted", "basiceps", "dilutedeps", "epsbasic", "epsdiluted"}
        or ("per share" in text and ("basic" in text or "diluted" in text))
    )


def is_percentage_label(label: str) -> bool:
    key = row_key(label)
    return "%" in str(label or "") or "margin" in key or "percentage" in key


def is_total_label(label: str) -> bool:
    return row_key(label).startswith("total")


def is_pdf_reported_total_expenses(label: str) -> bool:
    key = row_key(label)
    return key in {
        "totalexpenses",
        "totalexpense",
        "totalexpensesiv",
        "totalexpensesab",
        "totalexpensesatoh",
        "totalexpensesatog",
    }


def period_type(period: str) -> str:
    text = str(period or "").strip().upper()
    if re.match(r"Q[1-4]\s+FY\d{2}", text):
        return "quarter"
    if re.match(r"H[12]\s+FY\d{2}", text):
        return "half_year"
    if re.match(r"FY\d{2}", text):
        return "year"
    if "NINE" in text or "9M" in text:
        return "nine_months"
    return "unknown"


def _source_page(row: dict[str, Any]) -> int | None:
    for key in ("source_page", "page_no", "page", "source_page_no"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _source_confidence(row: dict[str, Any]) -> float | None:
    value = row.get("confidence") or row.get("source_confidence")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cell_from_dict(data: dict[str, Any]) -> CanonicalFinancialCell:
    fields = {field: data.get(field) for field in CanonicalFinancialCell.__dataclass_fields__}
    fields["source_page"] = _safe_int(fields.get("source_page"))
    fields["source_confidence"] = _safe_float(fields.get("source_confidence"))
    for key, value in list(fields.items()):
        if key in {"source_page", "source_confidence"}:
            continue
        if isinstance(value, bool):
            continue
        fields[key] = "" if value is None else value
    return CanonicalFinancialCell(**fields)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _requires_provenance(extraction: dict[str, Any]) -> bool:
    metadata = extraction.get("gpt54_execution_metadata")
    if isinstance(metadata, dict) and metadata.get("mock") is True:
        return False
    layer = str(extraction.get("extraction_layer") or "").lower()
    if "mock" in layer:
        return False
    if extraction.get("canonical_cell_provenance_required") is not None:
        return _truthy(extraction.get("canonical_cell_provenance_required"), True)
    status = str(extraction.get("gpt_json_status") or "").lower()
    return layer.startswith("gpt54") or status == "valid"


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _eps_value_preserved(raw_value: str, normalized_value: str) -> bool:
    raw = to_number(raw_value)
    normalized = to_number(normalized_value)
    if raw is None or normalized is None:
        return True
    return abs(raw - normalized) <= 0.0001


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def write_canonical_cells(path: str | Path, extraction: dict[str, Any]) -> None:
    """Persist canonical cell logs for local debugging."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    import json

    target.write_text(
        json.dumps(extraction.get("canonical_financial_cells") or [], indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
