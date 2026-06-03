"""Balance sheet and cash-flow image renderer."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from models import Announcement
from pl_image import (
    RenderBlockedError,
    approved_display_columns,
    approved_display_rows,
    assert_renderer_input_approved,
    company_name,
    data_row,
    normalize_rows,
    quarter_fy_from_columns,
    render_table_png,
    row_has_value,
    row_key,
    rows_to_table,
    safe_filename,
    section_row,
    source_name,
    footer_basis_label,
    title_with_basis,
    variable_display_columns,
)

CASH_FLOW_LABELS = [
    "Net cash inflow (outflow) from operating activities",
    "Net cash inflow (outflow) from investing activities",
    "Net cash inflow (outflow) from financing activities",
]


def render_bs_cf_image(
    extraction: dict[str, Any],
    announcement: Announcement | None,
    output_dir: Path,
    unit_label: str,
    *,
    standalone_tag: bool = False,
    approved_rows: list[dict[str, Any]] | None = None,
    approved_columns: list[dict[str, str]] | None = None,
) -> Path:
    """Render already-approved balance-sheet variables plus cash-flow rows."""

    assert_renderer_input_approved(extraction)
    output_dir.mkdir(parents=True, exist_ok=True)
    company = company_name(extraction, announcement)
    source = source_name(extraction, announcement)
    extracted_at = datetime.now()
    rows = approved_display_rows(approved_rows if approved_rows is not None else extraction.get("approved_bs_cf_rows"))
    if not rows:
        raise RenderBlockedError("approved Balance Sheet/Cash Flow rows missing")
    columns = approved_display_columns(
        approved_columns if approved_columns is not None else extraction.get("approved_bs_cf_columns")
    )
    if not columns:
        raise RenderBlockedError("approved Balance Sheet/Cash Flow columns missing")
    quarter, fy = quarter_fy_from_columns(columns, str(extraction.get("result_period") or ""))
    path = output_dir / f"{safe_filename(company, max_length=56)}_{quarter}_{fy}_BS_CF.png"
    footer_left = f"Data Source: {source} | {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}"
    footer_right = unit_label or ""
    title = title_with_basis(company, extraction, standalone_tag)
    footer_basis = footer_basis_label(extraction, standalone_tag)
    if footer_basis:
        footer_right = f"{footer_right} | {footer_basis}".strip(" |")
    if standalone_tag:
        footer_right = f"{footer_right} | ONLY STANDALONE FOUND".strip(" |")

    if not any(row_has_value(row) for row in rows):
        raise ValueError("Balance sheet and cash flow data not available in this PDF")

    header_label = "Cash Flow Variables" if _cash_flow_only_rows(rows) else "Balance Sheet Variables"
    headers = [header_label] + [column["label"] for column in columns]
    table_rows = rows_to_table(rows, columns)
    row_styles = [str(row.get("style") or "alternate") for row in rows]
    return render_table_png(
        title=f"{title} - Key Changes in Variables",
        headers=headers,
        rows=table_rows,
        row_styles=row_styles,
        columns=columns,
        path=path,
        footer_left=footer_left,
        footer_right=footer_right,
        unit_note=unit_label,
        first_col_fraction=0.30,
        title_size=17,
        cell_size=11,
        header_size=11,
    )


def build_bs_cf_rows(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    """Build dynamic balance-sheet rows and exactly three cash-flow rows."""

    rows: list[dict[str, Any]] = []
    extracted_cash_flow_rows: list[dict[str, Any]] = []
    for section in extraction.get("balance_sheet_variables") or []:
        if not isinstance(section, dict):
            continue
        raw_rows = normalize_rows(section.get("rows"))
        if _looks_like_cash_flow_rows(raw_rows):
            extracted_cash_flow_rows.extend(raw_rows)
            continue
        section_name = str(section.get("section") or "Variables").strip() or "Variables"
        display_section = _section_name(section_name)
        section_style = "section_green" if display_section in {"Assets", "Liabilities"} else "section"
        section_rows: list[dict[str, Any]] = []
        skipped_pnl_like = False
        for row in raw_rows:
            raw_label = str(row.get("label") or "")
            values = _drop_numbering_artifact_values(raw_label, row.get("values") or {})
            label = clean_variable_label(raw_label)
            if not label:
                continue
            if _is_pnl_like_variable_label(label):
                skipped_pnl_like = True
                continue
            if skipped_pnl_like and row_key(label) == "total":
                continue
            if not values and _is_structural_variable_label(label):
                continue
            section_rows.append(data_row(label, values, _variable_row_style(label)))
        if section_rows:
            rows.append(section_row(display_section) | {"style": section_style})
            rows.extend(section_rows)

    cash_flow_rows = normalize_rows(extraction.get("cash_flow_variables")) + extracted_cash_flow_rows
    if cash_flow_rows or rows:
        cash_lookup = _cash_flow_lookup(cash_flow_rows)
        if not rows and not any(cash_lookup.get(row_key(label)) for label in CASH_FLOW_LABELS):
            return rows
        if rows:
            rows.append(section_row("Cash Flow Variables") | {"style": "section"})
        for label in CASH_FLOW_LABELS:
            rows.append(data_row(label, cash_lookup.get(row_key(label), {}), "important"))
    return rows


def clean_variable_label(label: str) -> str:
    """Remove table markers while keeping the financial label readable."""

    text = re.sub(r"\s+", " ", label.replace("\xa0", " ")).strip()
    text = re.sub(r"^\(\s*[a-z]\s*\)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\(\s*\d+\s*\)\s*", "", text)
    text = re.sub(r"^[a-z]\)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\(\s*[ivxlcdm]{1,5}\s*\)\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[ivxlcdm]{1,5}[.)]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d+[.)]\s*", "", text)
    if re.fullmatch(r"[ivxlcdm]{1,6}", text, flags=re.IGNORECASE):
        return ""
    return text.strip(":- ")


def _drop_numbering_artifact_values(label: str, values: dict[str, str]) -> dict[str, str]:
    """Remove OCR values that are just subsection numbering, e.g. ``(2) Current Assets``."""

    match = re.match(r"^\(\s*(\d+)\s*\)\s+", str(label or ""))
    if not match:
        return dict(values)
    number = match.group(1)
    cleaned: dict[str, str] = {}
    for period, value in values.items():
        if str(value).strip() == number:
            continue
        cleaned[str(period)] = str(value)
    return cleaned


def _is_structural_variable_label(label: str) -> bool:
    """Return true for balance-sheet subsection labels, not monetary line items."""

    key = row_key(label)
    structural = {
        "currentassets",
        "noncurrentassets",
        "noncurrentassetsheldforsale",
        "equity",
        "noncurrentliabilities",
        "currentliabilities",
    }
    return key in structural


def _is_pnl_like_variable_label(label: str) -> bool:
    """Return true for P&L-only rows that leaked into a BS/CF table candidate."""

    key = row_key(label)
    pnl_exact = {
        "revenue",
        "revenuefromoperations",
        "otherincome",
        "totalincome",
        "costofmaterialsconsumed",
        "purchasesofstockintrade",
        "purchaseofstockintrade",
        "changesininventories",
        "changesininventoriesoffinishedgoodsworkinprogressandstockintrade",
        "employeebenefitexpense",
        "employeebenefitexpenses",
        "employeebenefitsexpense",
        "financecost",
        "financecosts",
        "depreciation",
        "depreciationandamortisationexpense",
        "depreciationandamortizationexpense",
        "otherexpenses",
        "totalexpenses",
        "profitbeforetax",
        "profitaftertax",
        "epsbasic",
        "epsdiluted",
    }
    pnl_needles = (
        "otherdirectcost",
        "projectboughtouts",
        "profitfortheperiod",
        "profitfortheyear",
        "profitlossbefore",
        "profitlossafter",
        "earningspershare",
    )
    return key in pnl_exact or any(needle in key for needle in pnl_needles)


def _looks_like_cash_flow_rows(rows: list[dict[str, Any]]) -> bool:
    """Return true when OCR filed a cash-flow statement as BS variables."""

    matched = 0
    for row in rows:
        if _standard_cash_flow_label(str(row.get("label") or "")):
            matched += 1
    return matched >= 2


def _cash_flow_only_rows(rows: list[dict[str, Any]]) -> bool:
    """Return true when the image has cash-flow values but no BS variables."""

    value_rows = [row for row in rows if row_has_value(row)]
    return bool(value_rows) and all(_standard_cash_flow_label(str(row.get("label") or "")) for row in value_rows)


def _section_name(section: str) -> str:
    """Normalize section names to the reference layout."""

    lowered = section.lower()
    if "asset" in lowered:
        return "Assets"
    if "liabil" in lowered or "equity" in lowered:
        return "Liabilities"
    return section


def _variable_row_style(label: str) -> str:
    """Return important-row styling for common key variables and totals."""

    key = row_key(label)
    important_needles = [
        "propertyplant",
        "capitalwork",
        "rightofuse",
        "investment",
        "inventories",
        "tradereceivables",
        "cashandcash",
        "bankbalances",
        "equitysharecapital",
        "borrowings",
        "financialliabilities",
        "provisions",
        "deferredtax",
        "total",
    ]
    return "important" if any(needle in key for needle in important_needles) else "alternate"


def _cash_flow_lookup(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Map cash-flow rows into the three required labels."""

    lookup: dict[str, dict[str, str]] = {}
    for row in rows:
        label = str(row.get("label") or "")
        target = _standard_cash_flow_label(label)
        if not target:
            continue
        key = row_key(target)
        lookup.setdefault(key, {}).update(row.get("values") or {})
    return lookup


def _standard_cash_flow_label(label: str) -> str:
    """Return one of the three canonical cash-flow labels."""

    lowered = label.lower()
    is_cash_flow_context = "cash" in lowered or "activit" in lowered
    if is_cash_flow_context and re.search(r"\boperat(?:ing|ional|e|ed|es|ions)?\b", lowered):
        return CASH_FLOW_LABELS[0]
    if is_cash_flow_context and re.search(r"\binvesting\b", lowered):
        return CASH_FLOW_LABELS[1]
    if is_cash_flow_context and re.search(r"\bfinancing\b", lowered):
        return CASH_FLOW_LABELS[2]
    return ""
