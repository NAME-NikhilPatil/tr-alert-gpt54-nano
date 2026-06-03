"""P&L image renderer and shared table-rendering utilities."""

from __future__ import annotations

import math
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from models import Announcement

HEADER_BLUE = "#1F3864"
TITLE_BLUE = "#DDEBF7"
DARK_GREEN = "#1E5631"
LIGHT_GREEN = "#C6EFCE"
VERY_LIGHT_GREEN = "#C6EFCE"
WHITE = "#FFFFFF"
BLACK = "#000000"
GRID = "#000000"
RED = "#C00000"
SUBTLE = "#F9F9F9"

DPI = 150
MIN_WIDTH_PX = 1920
MIN_HEIGHT_PX = 1080


class RenderBlockedError(RuntimeError):
    """Raised when a renderer is asked to draw unapproved financial data."""


def assert_renderer_input_approved(extraction: dict[str, Any]) -> None:
    """Hard gate: renderers only draw auditor/validator-approved payloads."""

    status = str((extraction or {}).get("renderer_input_validation_status") or "").strip().upper()
    if status != "PASS":
        raise RenderBlockedError("renderer_input.validation_status must equal PASS")


def approved_display_rows(value: Any) -> list[dict[str, Any]]:
    """Return pre-approved display rows without recalculating financial logic."""

    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or item.get("metric") or "").strip()
        if not label:
            continue
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        row = dict(item)
        row["label"] = label
        row["type"] = str(row.get("type") or ("data" if values else "section")).lower()
        row["values"] = {str(key): str(val) for key, val in values.items() if str(val).strip()}
        row["style"] = str(row.get("style") or ("normal" if row["values"] else "section"))
        output.append(row)
    return output


def approved_display_columns(value: Any) -> list[dict[str, str]]:
    """Return pre-approved display columns without deriving layout in renderers."""

    if not isinstance(value, list):
        return []
    output: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        label = str(item.get("label") or "").strip()
        if kind not in {"value", "change"} or not label:
            continue
        column = {str(key): str(val) for key, val in item.items() if val is not None}
        column["kind"] = kind
        column["label"] = compact_column_header(label)
        output.append(column)
    return output


def compact_column_header(label: str) -> str:
    """Return a short render-safe table header for period/change columns."""

    text = re.sub(r"\s+", " ", str(label or "").strip())
    if not text:
        return text
    parsed = parse_period(text)
    if parsed:
        kind, year = parsed
        return f"{kind} FY{year:02d}" if kind != "FY" else f"FY{year:02d}"
    lowered = text.lower()
    if "change" in lowered and "%" in lowered:
        if "qoq" in lowered:
            return "QoQ %"
        if "yoy" in lowered:
            return "YoY %"
        if "fy" in lowered:
            return "FY %"
        return "Change %"
    date_period = _period_from_column_date(text)
    if date_period:
        return date_period
    return (
        text.replace("Quarter ended", "Qtr")
        .replace("Quarter Ended", "Qtr")
        .replace("Qtr ended", "Qtr")
        .replace("Qtr Ended", "Qtr")
        .replace("Year ended", "FY")
        .replace("Year Ended", "FY")
        .replace("Half year ended", "Half Yr")
        .replace("Half Year Ended", "Half Yr")
    )


def _period_from_column_date(label: str) -> str:
    """Map raw date-style PDF headers to compact Indian FY period labels."""

    text = label.replace(".", "-").replace("/", "-").replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s-]+(?P<month>[A-Za-z]{3,9}|\d{1,2})[\s-]+(?P<year>\d{2,4})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?P<month>[A-Za-z]{3,9})[\s-]+(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s-]+(?P<year>\d{2,4})",
            text,
            flags=re.IGNORECASE,
        )
    if not match:
        return ""
    month = _month_number(match.group("month"))
    if not month:
        return ""
    year = int(match.group("year"))
    if year >= 2000:
        year %= 100
    lower = text.lower()
    fy_year = year if month <= 3 else year + 1
    if re.search(r"nine months?|9 months?|\b9m\b", lower):
        return f"9M FY{fy_year:02d}"
    if re.search(r"half year|half-year|six months?|6 months?", lower):
        if month in {9, 10}:
            return f"H1 FY{fy_year:02d}"
        if month in {3, 4}:
            return f"H2 FY{fy_year:02d}"
    if re.search(r"quarter|qtr|three months?|3 months?", lower):
        quarter = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(month)
        return f"{quarter} FY{fy_year:02d}" if quarter else f"FY{fy_year:02d}"
    if re.search(r"year ended|year ending|full year|\bfy\b|as at|as on|balance|cash", lower):
        return f"FY{year:02d}" if re.search(r"year ended|year ending|full year|\bfy\b", lower) else f"FY{fy_year:02d}"
    return ""


def _month_number(value: str) -> int:
    """Return numeric month for common PDF date headers."""

    month_text = str(value or "").strip().lower()
    if month_text.isdigit():
        number = int(month_text)
        return number if 1 <= number <= 12 else 0
    return {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }.get(month_text[:3] if len(month_text) > 3 else month_text, 0)


def render_pl_image(
    extraction: dict[str, Any],
    announcement: Announcement | None,
    output_dir: Path,
    unit_label: str,
    *,
    standalone_tag: bool = False,
    approved_rows: list[dict[str, Any]] | None = None,
    approved_columns: list[dict[str, str]] | None = None,
) -> Path:
    """Render an already-approved canonical P&L statement image."""

    assert_renderer_input_approved(extraction)
    output_dir.mkdir(parents=True, exist_ok=True)
    company = company_name(extraction, announcement)
    source = source_name(extraction, announcement)
    extracted_at = datetime.now()
    result_period = str(extraction.get("result_period") or "")
    display_rows = approved_display_rows(approved_rows if approved_rows is not None else extraction.get("approved_pnl_rows"))
    if not display_rows:
        raise RenderBlockedError("approved P&L rows missing")
    columns = approved_display_columns(
        approved_columns if approved_columns is not None else extraction.get("approved_pnl_columns")
    )
    if not columns:
        raise RenderBlockedError("approved P&L columns missing")
    quarter, fy = quarter_fy_from_columns(columns, result_period)
    path = output_dir / f"{safe_filename(company, max_length=56)}_{quarter}_{fy}_PnL.png"
    footer_left = f"Data source: {source} | Extracted: {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}"
    footer_right = unit_label or ""
    title = title_with_basis(company, extraction, standalone_tag)
    footer_basis = footer_basis_label(extraction, standalone_tag)
    if footer_basis:
        footer_right = f"{footer_right} | {footer_basis}".strip(" |")
    if standalone_tag:
        footer_right = f"{footer_right} | ONLY STANDALONE FOUND".strip(" |")

    if not any(row_has_value(row) for row in display_rows):
        raise ValueError("P&L data not available in this PDF")

    table_rows = rows_to_table(display_rows, columns, skip_margin_changes=True)
    headers = ["Particulars"] + [column["label"] for column in columns]
    row_styles = [str(row.get("style") or "normal") for row in display_rows]
    return render_table_png(
        title=title,
        headers=headers,
        rows=table_rows,
        row_styles=row_styles,
        columns=columns,
        path=path,
        footer_left=footer_left,
        footer_right=footer_right,
        unit_note=unit_label,
        first_col_fraction=0.24,
        cell_size=11,
        header_size=11,
    )


def build_pl_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build P&L rows with formulas applied period-by-period.

    The renderer keeps the canonical subtotal chain, but the expense line
    items are taken from the PDF table itself. This lets media, banking, and
    other non-manufacturing companies show rows such as Cost of Production or
    Interest Expended instead of forcing a blank Cost of materials row.
    """

    source_rows = trusted_pl_source_rows(source_rows)
    if not source_rows:
        return []
    if has_repeated_value_vector_artifact(source_rows):
        return []
    if _looks_like_finance_pl(source_rows):
        finance_rows = build_source_order_pl_rows(source_rows)
        if finance_rows:
            return finance_rows

    periods = available_periods(source_rows)
    revenue_row = pick_row(source_rows, ["Revenue", "Revenue from operations"])
    total_income_row = pick_row(source_rows, ["Total Income", "Total income"])
    income_row = revenue_row or total_income_row
    revenue = row_values(income_row)
    revenue_label = "Revenue" if revenue_row else source_label(total_income_row, "Total Income")
    expense_component_rows = dynamic_expense_component_rows(source_rows)
    expense_components = [row_values(row) for row in expense_component_rows]
    gross_direct = pick_values_if_source_row(source_rows, ["Gross Profit"])
    gross = calc_subtract(
        periods,
        revenue,
        expense_components,
        gross_direct,
        require_any_component=True,
    )
    gross_margin = calc_margin(periods, gross, revenue, pick_values(source_rows, ["Gross Profit Margin"]))
    employee_row = pick_row(
        source_rows,
        [
            "Employee benefits expense",
            "Employees benefits expense",
            "Employee Benefit Expense",
            "Employees Benefit Expense",
            "Employee cost",
            "Employees cost",
        ],
    )
    employee = row_values(employee_row)
    other_expenses_row = pick_row(source_rows, ["Other expenses", "Other Operating Expenses", "Operating and other expenses", "Operating and other expense"])
    other_expenses = row_values(other_expenses_row)
    ebitda_direct = pick_values_if_source_row(source_rows, ["EBITDA"])
    ebitda = calc_subtract(
        periods,
        gross,
        [employee, other_expenses],
        ebitda_direct,
        require_any_component=False,
    )
    ebitda_margin = calc_margin(periods, ebitda, revenue, pick_values(source_rows, ["EBITDA Margin"]))
    depreciation_row = pick_row(source_rows, ["Depreciation and amortisation expense", "Depreciation", "Depreciation and amortization expense"])
    depreciation = row_values(depreciation_row)
    finance_row = pick_row(source_rows, ["Finance costs", "Finance Cost", "Finance cost"])
    finance = row_values(finance_row)
    if not finance:
        finance = sum_matching_rows(source_rows, ["Borrowing Cost", "Exchange Fluctuation", "Foreign exchange"])
    total_expenses_direct = pick_values_exact_if_source_row(
        source_rows,
        ["Total Expenses excluding", "Total Expenses excluding Depreciation and Finance Costs"],
    )
    total_expenses = total_expenses_direct or calc_sum(periods, expense_components + [employee, other_expenses])
    reported_total_expenses = pick_values_exact(source_rows, ["Total expenses", "Total Expenses", "Total Expenses (IV)"])
    pbe_direct = pick_values_exact_if_source_row(
        source_rows,
        [
            "Profit before exceptional items, Other Income",
            "Profit before exceptional items",
            "Profit before exceptional items and Other Income",
            "Profit before other income and exceptional items",
        ],
    )
    statutory_pbe_bridge = (
        not pbe_direct
        and bool(reported_total_expenses)
        and any("profitbefore" in row_key(str(row.get("label") or "")) and "tax" in row_key(str(row.get("label") or "")) for row in source_rows)
    )
    if statutory_pbe_bridge:
        profit_before_exceptional = calc_subtract(periods, revenue, [reported_total_expenses], {}, require_any_component=True)
        profit_before_exceptional_basis = "revenue_minus_total_expenses"
    else:
        profit_before_exceptional = calc_subtract(
            periods,
            ebitda,
            [depreciation, finance],
            pbe_direct,
            require_any_component=False,
        )
        profit_before_exceptional_basis = "direct" if pbe_direct else "calculated"
    other_income_row = pick_row(source_rows, ["Other income", "Other Income"]) if revenue_row else None
    other_income = row_values(other_income_row)
    exceptional_row = pick_row(source_rows, ["Exceptional items", "Exceptional Items"])
    exceptional = row_values(exceptional_row)
    associate_share_row = pick_row(
        source_rows,
        [
            "Share of Profit / Loss of Associates and Joint Ventures",
            "Share of profit/loss of associates and joint ventures",
            "Share of profit of associates and joint ventures",
            "Share of loss of associates and joint ventures",
            "Share of Profit / (Loss) of Associates and Joint Ventures",
        ],
    )
    associate_share = row_values(associate_share_row)
    pbt_direct = pick_values_strict(
        source_rows,
        [
            "Profit Before Tax",
            "Profit before tax",
            "Profit / Loss before tax",
            "Profit/(Loss) before tax",
            "Profit / (Loss) before tax",
            "Profit before tax but after exceptional items",
        ],
    )
    pbt = calc_add(
        periods,
        profit_before_exceptional,
        [other_income, associate_share, exceptional],
        pbt_direct,
        require_any_component=False,
    )
    pat_direct = pick_values_exact(
        source_rows,
        [
            "PAT",
            "Profit After Tax",
            "Profit after tax",
            "Profit / (Loss) after tax",
            "Profit/(Loss) after tax",
            "Profit for the period",
            "Profit/(Loss) for the period",
            "Profit / (Loss) for the period",
        ],
    ) or pick_values(
        source_rows,
        ["PAT", "Profit After Tax", "Profit after tax", "Profit / (Loss) after tax", "Profit/(Loss) after tax", "Profit for the period"],
    )
    tax = pick_values_exact(source_rows, ["Total tax expense", "Total Tax Expense", "Total Tax Expenses"])
    if not tax:
        tax = sum_matching_rows(
            source_rows,
            [
                "current tax",
                "deferred tax",
                "income tax of earlier years",
                "tax relating to earlier",
                "adjustment of tax",
                "adjustment for tax",
                "short excess provision",
                "short/excess provision",
                "previous year",
                "earlier period",
                "earlier years",
            ],
        )
    if not tax:
        tax = pick_values(source_rows, ["Tax Expenses", "Tax expense", "Less: Tax expense"])
    if not tax and pbt and pat_direct:
        tax = calc_subtract(periods, pbt, [pat_direct], {}, require_any_component=False)
    pat = calc_subtract(periods, pbt, [tax], pat_direct, require_any_component=False)
    pat_margin = calc_margin(periods, pat, revenue, pick_values(source_rows, ["PAT Margin"]))
    continuing_operations_row = pick_row(
        source_rows,
        [
            "Profit from continuing operations",
            "Profit/(loss) from continuing operations",
            "Profit / (Loss) from continuing operations",
            "Profit for the period from continuing operations",
        ],
    )
    discontinued_operations_row = pick_row(
        source_rows,
        [
            "Profit or loss from discontinued operations",
            "Profit/(loss) from discontinued operations",
            "Profit / (Loss) from discontinued operations",
            "Profit from discontinued operations",
            "Loss from discontinued operations",
        ],
    )
    continuing_operations = row_values(continuing_operations_row)
    discontinued_operations = row_values(discontinued_operations_row)
    eps_basic = pick_values(source_rows, ["EPS (Basic)", "Basic EPS", "Basic"])
    eps_diluted = pick_values(source_rows, ["EPS (Diluted)", "Diluted EPS", "Diluted"])

    rows: list[dict[str, Any]] = []
    append_value_row(rows, revenue_label, revenue, "key", role="revenue")
    if expense_component_rows or employee or other_expenses or depreciation or finance or total_expenses:
        rows.append(section_row("Expenses") | {"style": "pnl_section"})
    for row in expense_component_rows:
        append_value_row(rows, source_label(row, "Expense"), row_values(row), "normal", role="gross_component")
    append_value_row(rows, "Gross Profit", gross, "key", basis="direct" if gross_direct else "calculated")
    append_value_row(rows, "Gross Profit Margin %", gross_margin, "margin")
    append_value_row(rows, source_label(employee_row, "Employee Benefit Expense"), employee, "normal", role="employee")
    append_value_row(rows, source_label(other_expenses_row, "Other expenses"), other_expenses, "normal", role="operating_expense")
    append_value_row(
        rows,
        "Total Expenses excluding",
        total_expenses,
        "subtotal",
        role="total_expenses",
        basis="direct" if total_expenses_direct else "calculated",
    )
    append_value_row(rows, "EBITDA", ebitda, "key", basis="direct" if ebitda_direct else "calculated")
    append_value_row(rows, "EBITDA Margin %", ebitda_margin, "margin")
    append_value_row(rows, source_label(depreciation_row, "Depreciation and amortisation expense"), depreciation, "normal", role="depreciation")
    append_value_row(rows, source_label(finance_row, "Finance costs"), finance, "normal", role="finance")
    append_value_row(
        rows,
        "Profit before exceptional items, Other Income",
        profit_before_exceptional,
        "subtotal",
        basis=profit_before_exceptional_basis,
    )
    append_value_row(rows, source_label(exceptional_row, "Exceptional items"), exceptional, "normal", role="exceptional")
    if not exceptional and any(row_key(str(row.get("label") or "")).startswith("exceptionalitem") for row in source_rows):
        rows.append(data_row("Exceptional items", {}, "normal", role="exceptional"))
    append_value_row(rows, source_label(other_income_row, "Other income"), other_income, "normal", role="other_income")
    append_value_row(
        rows,
        source_label(associate_share_row, "Share of Profit / Loss of Associates and Joint Ventures"),
        associate_share,
        "normal",
        role="associate_share",
    )
    append_value_row(rows, "Profit Before Tax", pbt, "subtotal", basis="direct" if pbt_direct else "calculated")
    append_value_row(rows, "Total tax expense", tax, "subtotal", role="tax")
    append_value_row(
        rows,
        source_label(continuing_operations_row, "Profit from continuing operations"),
        continuing_operations,
        "subtotal",
        basis="direct",
    )
    append_value_row(
        rows,
        source_label(discontinued_operations_row, "Profit or loss from discontinued operations"),
        discontinued_operations,
        "normal",
        basis="direct",
    )
    append_value_row(rows, "PAT", pat, "key", basis="direct" if pat_direct else "calculated")
    append_value_row(rows, "PAT Margin %", pat_margin, "margin")
    append_value_row(rows, "EPS (Basic)", eps_basic, "subtotal")
    append_value_row(rows, "EPS (Diluted)", eps_diluted, "subtotal")
    return rows


def pick_values_if_source_row(rows: list[dict[str, Any]], labels: list[str]) -> dict[str, str]:
    """Return row values only when the row is a direct PDF row, not a model formula bundle."""

    row = pick_row(rows, labels)
    if _looks_like_model_calculated_row(row):
        return {}
    return row_values(row)


def pick_values_exact_if_source_row(rows: list[dict[str, Any]], labels: list[str]) -> dict[str, str]:
    """Exact-label variant of pick_values_if_source_row."""

    row = pick_row_exact(rows, labels)
    if _looks_like_model_calculated_row(row):
        return {}
    return row_values(row)


def _looks_like_model_calculated_row(row: dict[str, Any] | None) -> bool:
    """Return true when raw_values contains component rows rather than one PDF row."""

    if not row:
        return False
    raw_values = row.get("raw_values")
    if isinstance(raw_values, dict) and any(isinstance(value, (dict, list)) for value in raw_values.values()):
        return True
    return bool(row.get("is_calculated_by_pipeline"))


def trusted_pl_source_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop OCR-misaligned P&L rows before formulas/rendering."""

    output: list[dict[str, Any]] = []
    for row in source_rows:
        label = str(row.get("label") or "")
        values = row_values(row)
        if values and _is_bad_pl_value_label(label):
            continue
        output.append(row)
    return output


def has_repeated_value_vector_artifact(rows: list[dict[str, Any]]) -> bool:
    """Return true when many different rows carry the same multi-period values."""

    vector_counts: dict[tuple[tuple[str, str], ...], int] = {}
    data_rows = [row for row in rows if row_values(row)]
    for row in data_rows:
        normalized_values = tuple(
            sorted(
                (str(period), re.sub(r"\s+", "", str(value)))
                for period, value in row_values(row).items()
                if str(value).strip() and str(value).strip() not in {"0", "0.0", "0.00", "-"}
            )
        )
        if len(normalized_values) < 2:
            continue
        vector_counts[normalized_values] = vector_counts.get(normalized_values, 0) + 1
    if not vector_counts:
        return False
    repeated = max(vector_counts.values())
    return repeated >= 4 and repeated / max(len(data_rows), 1) >= 0.35


def build_source_order_pl_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render a non-industrial P&L in source order without inventing subtotals.

    Banking/NBFC statements often use Interest Earned, Interest Expended,
    Operating Expenses, Provisions and similar rows. Gross Profit and EBITDA are
    not meaningful there, so this path keeps the PDF's own line items and direct
    totals.
    """

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    revenue_values: dict[str, str] = {}
    pat_values: dict[str, str] = {}
    periods = available_periods(source_rows)

    for source_row in source_rows:
        label = source_label(source_row, "")
        key = row_key(label)
        values = row_values(source_row)
        if not label or not key:
            continue
        if not values:
            if key in {"expenses", "expense", "expenditure"}:
                rows.append(section_row("Expenses") | {"style": "pnl_section"})
            continue
        if key in seen:
            continue
        if pat_values and _is_post_pat_non_pnl_label(label):
            continue
        seen.add(key)

        style = _source_order_pl_style(label)
        role = _source_order_pl_role(label)
        basis = "direct" if style in {"key", "subtotal"} else ""
        if role == "revenue":
            revenue_values = values
        if _is_pat_label(label):
            pat_values = values
        rows.append(data_row(label, values, style, role=role, basis=basis))

    if revenue_values and pat_values:
        pat_margin = calc_margin(periods, pat_values, revenue_values, {})
        append_value_row(rows, "PAT Margin %", pat_margin, "margin")
    return rows


def append_value_row(
    rows: list[dict[str, Any]],
    label: str,
    values: dict[str, str],
    style: str,
    *,
    role: str = "",
    basis: str = "",
) -> None:
    """Append a row only when it has display values."""

    if values:
        rows.append(data_row(label, values, style, role=role, basis=basis))


def source_label(row: dict[str, Any] | None, fallback: str) -> str:
    """Return a clean source row label, falling back to a standard label."""

    label = str((row or {}).get("label") or "").strip()
    if not label:
        return fallback
    label = re.sub(
        r"^\s*(?:(?:\(?[ivxlcdm]{1,6}\)?|[0-9]{1,3})(?:[.)]|\s+)|\(?[a-z]\)?[.)])\s*",
        "",
        label,
        flags=re.IGNORECASE,
    )
    label = re.sub(r"\s+", " ", label).strip()
    return label or fallback


def row_values(row: dict[str, Any] | None) -> dict[str, str]:
    """Return cleaned values for a source row."""

    values = (row or {}).get("values")
    if isinstance(values, dict):
        return {str(key): str(value) for key, value in values.items() if str(value).strip()}
    return {}


def pick_row(rows: list[dict[str, Any]], aliases: list[str]) -> dict[str, Any] | None:
    """Return the first matching row, preferring exact aliases."""

    alias_keys = [row_key(alias) for alias in aliases]
    exact_matches: list[dict[str, Any]] = []
    fuzzy_matches: list[dict[str, Any]] = []
    for row in rows:
        label_key = row_key(str(row.get("label") or ""))
        if not label_key:
            continue
        if label_key in alias_keys:
            exact_matches.append(row)
        elif any(alias_key and (alias_key in label_key or label_key in alias_key) for alias_key in alias_keys):
            fuzzy_matches.append(row)
    for row in exact_matches + fuzzy_matches:
        if row_values(row):
            return row
    return None


def pick_row_exact(rows: list[dict[str, Any]], aliases: list[str]) -> dict[str, Any] | None:
    """Return a row only when its normalized label exactly matches an alias."""

    alias_keys = {row_key(alias) for alias in aliases}
    for row in rows:
        if row_key(str(row.get("label") or "")) not in alias_keys:
            continue
        if row_values(row):
            return row
    return None


def dynamic_expense_component_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return actual PDF expense rows used before employee/other/finance subtotals."""

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_expense_block = False
    for row in rows:
        label = str(row.get("label") or "")
        key = row_key(label)
        if _is_expense_section_row(row):
            in_expense_block = True
            continue
        if in_expense_block and _is_expense_block_stop_label(label):
            break
        if not row_has_value(row):
            continue
        if _is_total_expenses_label(label) or _is_known_post_gross_expense_label(label) or _is_bad_pl_value_label(label):
            continue
        if in_expense_block or _is_direct_expense_label(label):
            if key and key not in seen:
                seen.add(key)
                output.append(row)
    return output


def _is_expense_section_row(row: dict[str, Any]) -> bool:
    label = source_label(row, "")
    key = row_key(label)
    return key in {"expenses", "expense"} and not row_has_value(row)


def _is_total_expenses_label(label: str) -> bool:
    key = row_key(source_label({"label": label}, label))
    return key in {"totalexpenses", "totalexpense"} or key.startswith("totalexpensesexcluding")


def _is_known_post_gross_expense_label(label: str) -> bool:
    key = row_key(source_label({"label": label}, label))
    return (
        "employeebenefit" in key
        or "employeesbenefit" in key
        or key.startswith("employeecost")
        or key in {"otherexpenses", "otheroperatingexpenses", "operatingandotherexpenses", "operatingandotherexpense"}
        or "depreciation" in key
        or "amortisation" in key
        or "amortization" in key
        or key in {"financecost", "financecosts"}
        or "borrowingcost" in key
        or "exchangefluctuation" in key
        or "foreignexchange" in key
        or "fairvaluationinvestmentactivity" in key
    )


def _is_expense_block_stop_label(label: str) -> bool:
    key = row_key(source_label({"label": label}, label))
    if key in {"pat", "netprofit", "netloss", "profitfortheperiod", "profitfortheyear"}:
        return True
    stop_needles = (
        "profitbefore",
        "profitlossbefore",
        "profitafter",
        "profitlossafter",
        "grossprofit",
        "ebitda",
        "otherincome",
        "exceptionalitem",
        "taxexpense",
        "taxexpenses",
        "currenttax",
        "deferredtax",
        "othercomprehensive",
        "totalcomprehensive",
        "paidupequity",
        "paidupshare",
        "sharecapital",
        "otherequity",
        "earningspershare",
        "eps",
        "ownersofthecompany",
        "noncontrollinginterest",
    )
    return any(needle in key for needle in stop_needles)


def _is_bad_pl_value_label(label: str) -> bool:
    """Return true for labels that indicate OCR column shift rather than metrics."""

    cleaned = source_label({"label": label}, "").strip()
    key = row_key(cleaned)
    if not key:
        return True
    lower = cleaned.lower()
    if lower in {"reviewed", "audited", "unaudited", "un-audited", "un audited"}:
        return True
    if re.fullmatch(r"[ivxlcdm]+", cleaned, flags=re.IGNORECASE):
        return True
    if not re.search(r"[A-Za-z]", cleaned):
        return True
    alpha_count = len(re.findall(r"[A-Za-z]", cleaned))
    digit_count = len(re.findall(r"\d", cleaned))
    if digit_count > alpha_count * 2 and alpha_count < 4:
        return True
    return False


def _is_direct_expense_label(label: str) -> bool:
    key = row_key(source_label({"label": label}, label))
    direct_needles = (
        "costofmaterial",
        "costofrawandpackingmaterial",
        "rawandpackingmaterial",
        "packingmaterial",
        "costofproduction",
        "productioncost",
        "acquisitionfees",
        "telecastfees",
        "programmingcost",
        "contentcost",
        "projectrelatedexpense",
        "constructionexpense",
        "constructionexpenses",
        "constructioncost",
        "landdevelopment",
        "developmentandconstruction",
        "subcontract",
        "operatingmaintenance",
        "consumptionofstores",
        "purchaseofstock",
        "purchasesofstock",
        "purchaseoftradedgoods",
        "purchaseoftradegoods",
        "purchasesoftradedgoods",
        "purchasesoftradegoods",
        "purchaseofgoods",
        "purchasegoods",
        "costoftradedgoods",
        "costoftradedgoodssold",
        "tradedgoodssold",
        "directexpense",
        "directexpenses",
        "changeininventor",
        "changesininventor",
        "marketinganddistribution",
        "rawmaterial",
        "powerandfuel",
        "manufacturingexpense",
        "interestexpended",
        "interestexpense",
        "interestexpenses",
        "operatingexpense",
        "operatingexpenses",
        "totalexpenditure",
        "provisionsandcontingencies",
        "provisionandcontingency",
        "provisionsotherthantax",
        "impairmentonfinancial",
        "feecost",
        "feesandcommissionexpense",
        "feesandcontrollingexpense",
        "feesandcontrollingexpenses",
        "commissionexpense",
    )
    return any(needle in key for needle in direct_needles)


def _looks_like_finance_pl(rows: list[dict[str, Any]]) -> bool:
    """Return true for banking/NBFC-style statements with finance-specific rows."""

    keys = [row_key(str(row.get("label") or "")) for row in rows]
    has_total_income = any(key == "totalincome" for key in keys)
    has_revenue = any(key in {"revenue", "revenuefromoperations", "revenuefromoperation"} for key in keys)
    cues = 0
    for key in keys:
        if any(
            needle in key
            for needle in (
                "interestearned",
                "interestincome",
                "interestexpended",
                "interestexpense",
                "provisionsandcontingencies",
                "provisionandcontingency",
                "impairmentonfinancial",
                "netgainonfairvalue",
                "feesandcommissionincome",
                "feesandcommissionexpense",
            )
        ):
            cues += 1
    return has_total_income and cues >= 2 and not has_revenue


def _source_order_pl_style(label: str) -> str:
    key = row_key(label)
    if key in {"totalincome", "revenue", "revenuefromoperations", "revenuefromoperation"}:
        return "key"
    if _is_pat_label(label):
        return "key"
    if (
        "profitbeforetax" in key
        or "profitlossbeforetax" in key
        or "totalexpense" in key
        or "totalexpenditure" in key
        or "taxexpense" in key
        or "taxexpenses" in key
        or key in {"epsbasic", "basic", "earningspersharebasic"}
    ):
        return "subtotal"
    return "normal"


def _source_order_pl_role(label: str) -> str:
    key = row_key(label)
    if key in {"totalincome", "revenue", "revenuefromoperations", "revenuefromoperation"}:
        return "revenue"
    if "taxexpense" in key or "taxexpenses" in key:
        return "tax"
    if any(
        needle in key
        for needle in (
            "interestexpended",
            "interestexpense",
            "operatingexpense",
            "provisionsandcontingencies",
            "provisionandcontingency",
            "impairmentonfinancial",
            "feesandcommissionexpense",
        )
    ):
        return "gross_component"
    return ""


def _is_pat_label(label: str) -> bool:
    key = row_key(label)
    return key in {"pat", "profitaftertax", "profitlossaftertax", "profitfortheperiod", "netprofitfortheperiod"}


def _is_post_pat_non_pnl_label(label: str) -> bool:
    """Drop OCI/equity/distribution rows that appear after PAT in source tables."""

    key = row_key(label)
    if "eps" in key or "earningspershare" in key or key in {"basic", "diluted", "epsbasic", "epsdiluted"}:
        return False
    non_pnl_needles = (
        "othercomprehensive",
        "totalcomprehensive",
        "comprehensiveincome",
        "paidupequity",
        "paidupshare",
        "equitysharecapital",
        "facevalue",
        "reserves",
        "noncontrollinginterest",
        "attributableto",
        "gainlossarising",
        "fairvaluationofinvestments",
        "incometaxexpensecreditontheabove",
    )
    return any(needle in key for needle in non_pnl_needles)


def _total_expenses_appears_to_include_below_ebitda_items(rows: list[dict[str, Any]]) -> bool:
    """Return true when Total Expenses is after depreciation/finance in source order."""

    total_index = _first_row_index(rows, _is_total_expenses_label)
    if total_index is None:
        return False
    depreciation_index = _first_row_index(rows, lambda label: "depreciation" in row_key(label) or "amortisation" in row_key(label) or "amortization" in row_key(label))
    finance_index = _first_row_index(rows, lambda label: row_key(label) in {"financecost", "financecosts"})
    known_below_ebitda = [index for index in (depreciation_index, finance_index) if index is not None]
    if not known_below_ebitda:
        return True
    return total_index > max(known_below_ebitda)


def _first_row_index(rows: list[dict[str, Any]], predicate: Any) -> int | None:
    for index, row in enumerate(rows):
        if predicate(str(row.get("label") or "")):
            return index
    return None


def render_table_png(
    *,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    row_styles: list[str],
    columns: list[dict[str, str]],
    path: Path,
    footer_left: str,
    footer_right: str,
    unit_note: str = "",
    first_col_fraction: float = 0.22,
    title_size: int = 19,
    cell_size: int = 10,
    header_size: int = 11,
) -> Path:
    """Render a styled table to PNG using matplotlib."""

    if not rows:
        raise ValueError("No table rows available for image rendering")
    display_rows = _wrap_first_column_rows(rows)
    display_headers = [headers[0]] + [compact_column_header(header) for header in headers[1:]]
    max_body_lines = max((str(row[0]).count("\n") + 1 for row in display_rows), default=1)
    frame = pd.DataFrame(display_rows, columns=display_headers)
    row_count = len(frame.index) + 1
    col_count = len(headers)
    width_px = max(MIN_WIDTH_PX, 360 + 190 * max(col_count - 1, 1))
    row_height_px = 34 + 14 * max(0, min(max_body_lines, 3) - 1)
    height_px = max(MIN_HEIGHT_PX, 190 + row_height_px * row_count)
    figure = plt.figure(figsize=(width_px / DPI, height_px / DPI), dpi=DPI)
    figure.patch.set_facecolor(WHITE)
    figure.patch.set_alpha(1.0)
    ax = figure.add_axes([0, 0, 1, 1], facecolor=WHITE)
    ax.set_facecolor(WHITE)
    ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ax.transAxes, color=WHITE, ec="none", zorder=-20))
    ax.add_patch(plt.Rectangle((0, 0.91), 1, 0.09, transform=ax.transAxes, color=TITLE_BLUE, ec=GRID, lw=0.8))
    display_title = _wrap_title(title)
    ax.text(
        0.5,
        0.955,
        display_title,
        ha="center",
        va="center",
        fontsize=_title_font_size(display_title, title_size),
        fontweight="bold",
        color=BLACK,
        linespacing=1.05,
    )
    if unit_note:
        ax.text(0.012, 0.928, f"Unit: {unit_note}", ha="left", va="center", fontsize=9, fontweight="bold", color=BLACK)
    ax.text(0.012, 0.025, footer_left, ha="left", va="center", fontsize=7, color=BLACK)
    ax.text(0.988, 0.025, footer_right, ha="right", va="center", fontsize=7, color=BLACK)

    first_col_fraction = min(max(first_col_fraction, 0.16), 0.48)
    data_weights = [1.05 if column.get("kind") == "change" else 1.0 for column in columns]
    total_weight = sum(data_weights) or 1.0
    remaining_fraction = 1.0 - first_col_fraction
    col_widths = [first_col_fraction] + [
        remaining_fraction * weight / total_weight for weight in data_weights
    ]
    table = ax.table(
        cellText=frame.values.tolist(),
        colLabels=display_headers,
        colWidths=col_widths,
        cellLoc="center",
        bbox=[0.0, 0.065, 1.0, 0.845],
        loc="center",
    )
    table.auto_set_font_size(False)
    change_column_indexes = {
        index + 1 for index, column in enumerate(columns) if column.get("kind") == "change"
    }
    for (row_index, col_index), cell in table.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(0.75)
        cell.PAD = 0.012
        text = cell.get_text()
        text.set_wrap(True)
        if row_index == 0:
            is_current = col_index == 1 and col_count > 1
            cell.set_facecolor(LIGHT_GREEN if is_current else HEADER_BLUE)
            text.set_color(BLACK if is_current else WHITE)
            text.set_fontsize(header_size)
            text.set_fontweight("bold")
            text.set_ha("left" if col_index == 0 else "center")
            continue

        style = row_styles[row_index - 1] if row_index - 1 < len(row_styles) else "normal"
        _style_body_cell(cell, text, style, col_index, cell_size)
        if col_index in change_column_indexes and is_negative_percent(text.get_text()):
            text.set_color(RED)
    path.unlink(missing_ok=True)
    figure.savefig(path, dpi=DPI, facecolor=WHITE, edgecolor=WHITE, transparent=False)
    plt.close(figure)
    return path


def _wrap_first_column_rows(rows: list[list[str]]) -> list[list[str]]:
    """Wrap long row labels so they do not spill into numeric columns."""

    wrapped: list[list[str]] = []
    for row in rows:
        if not row:
            wrapped.append(row)
            continue
        first = _wrap_table_label(str(row[0] or ""))
        wrapped.append([first] + list(row[1:]))
    return wrapped


def _wrap_title(title: str) -> str:
    """Wrap very long image titles into at most two centered lines."""

    text = re.sub(r"\s+", " ", str(title or "").strip())
    if len(text) <= 76:
        return text
    lines = textwrap.wrap(text, width=68, break_long_words=False, break_on_hyphens=False)
    if len(lines) <= 2:
        return "\n".join(lines)
    return "\n".join([lines[0], lines[1].rstrip(". ") + "..."])


def _title_font_size(title: str, base_size: int) -> int:
    """Return a title font size that fits within the header band."""

    longest = max((len(line) for line in str(title or "").splitlines()), default=0)
    if longest > 70:
        return max(13, base_size - 5)
    if "\n" in str(title):
        return max(14, base_size - 4)
    if longest > 58:
        return max(15, base_size - 3)
    return base_size


def _wrap_table_label(label: str, *, width: int = 30, max_lines: int = 3) -> str:
    """Wrap a table label to bounded lines with an ellipsis on overflow."""

    text = re.sub(r"\s+", " ", str(label or "").strip())
    if len(text) <= width:
        return text
    lines = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    kept[-1] = kept[-1].rstrip(". ") + "..."
    return "\n".join(kept)


def rows_to_table(rows: list[dict[str, Any]], columns: list[dict[str, str]], *, skip_margin_changes: bool = False) -> list[list[str]]:
    """Convert row dictionaries into display cells."""

    table_rows: list[list[str]] = []
    for row in rows:
        label = str(row.get("label") or "")
        cells = [short_display_label(label)]
        for column in columns:
            if column["kind"] == "value":
                cells.append(format_display_cell(label, (row.get("values") or {}).get(column["period"], "")))
            else:
                change_key = f"{column['from']}->{column['to']}"
                changes = row.get("changes") if isinstance(row.get("changes"), dict) else {}
                if row.get("_approved_for_render") is True:
                    change_value = "" if skip_margin_changes and "margin" in label.lower() else str(changes.get(change_key, ""))
                    cells.append(truncate_decimal_display(change_value))
                else:
                    change_value = "" if skip_margin_changes and "margin" in label.lower() else change_for_row(row, column["from"], column["to"])
                    cells.append(truncate_decimal_display(change_value))
        table_rows.append(cells)
    return table_rows


def short_display_label(label: str) -> str:
    """Shorten verbose row labels for compact Telegram image tables."""

    text = " ".join(str(label or "").replace("\xa0", " ").split())
    if _is_unclear_value(text):
        return "N/A"
    replacements = {
        "Profit before exceptional items, Other Income": "Profit before exceptional items",
        "Profit before exceptional items and Other Income": "Profit before exceptional items",
        "Profit before other income and exceptional items": "Profit before exceptional items",
        "Total Expenses excluding Depreciation and Finance Costs": "Total Expenses excluding",
        "Total Expenses excluding Depreciation and Finance Cost": "Total Expenses excluding",
        "Total Expenses excluding depreciation and finance costs": "Total Expenses excluding",
        "Changes in inventories of finished goods and work-in-progress": "Changes in inventories",
        "Changes in inventories of finished goods, work-in-progress and stock-in-trade": "Changes in inventories",
        "Changes in inventories of finished goods, stock-in-trade and work-in-progress": "Changes in inventories",
        "Changes in inventories of finished goods work in progress and Stock in Trade": "Changes in inventories",
        "Changes in Inventories of Finished Goods, Stock -in- Trade and Work -in- Progress": "Changes in inventories",
        "Depreciation and amortisation expense": "Depreciation",
        "Depreciation and amortization expense": "Depreciation",
        "Depreciation and Amortization Expenses": "Depreciation",
        "Other income / write-back included in Total Income": "Other income/write-back",
    }
    for old, new in replacements.items():
        text = re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def result_display_columns(rows: list[dict[str, Any]], result_period: str) -> list[dict[str, str]]:
    """Build result columns following Q1/Q2/Q3/Q4 display rules."""

    periods = available_periods(rows)
    current = pick_current_period(periods, result_period)
    if not current:
        return [{"kind": "value", "label": period, "period": period} for period in periods]
    parsed = parse_period(current)
    if not parsed:
        return [{"kind": "value", "label": period, "period": period} for period in periods]
    kind, year = parsed
    columns: list[dict[str, str]] = [{"kind": "value", "label": current, "period": current}]
    if kind.startswith("Q"):
        quarter = int(kind[1])
        previous_quarter = find_period(periods, previous_quarter_kind(quarter), previous_quarter_year(quarter, year))
        if previous_quarter:
            columns.append({"kind": "value", "label": previous_quarter, "period": previous_quarter})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous_quarter})
        yoy_quarter = find_period(periods, kind, year - 1)
        if yoy_quarter:
            columns.append({"kind": "value", "label": yoy_quarter, "period": yoy_quarter})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": yoy_quarter})
        if quarter == 2:
            add_aggregate_columns(columns, periods, "H1", year)
            add_aggregate_columns(columns, periods, "FY", year)
        elif quarter == 4:
            add_aggregate_columns(columns, periods, "FY", year)
    elif kind.startswith("H"):
        half = int(kind[1])
        previous_half_kind = "H1" if half == 2 else "H2"
        previous_half_year = year if half == 2 else year - 1
        previous_half = find_period(periods, previous_half_kind, previous_half_year)
        if previous_half:
            columns.append({"kind": "value", "label": previous_half, "period": previous_half})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous_half})
        yoy_half = find_period(periods, kind, year - 1)
        if yoy_half:
            columns.append({"kind": "value", "label": yoy_half, "period": yoy_half})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": yoy_half})
        if half == 2:
            add_aggregate_columns(columns, periods, "FY", year)
    elif kind == "FY":
        previous_fy = find_period(periods, "FY", year - 1)
        if previous_fy:
            columns.append({"kind": "value", "label": previous_fy, "period": previous_fy})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous_fy})
    return [column for column in columns if column_has_data(rows, column)]


def variable_display_columns(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build FY current/FY previous/change columns for variables."""

    fy_periods = [period for period in available_periods(rows) if (parse_period(period) or ("", 0))[0] == "FY"]
    if len(fy_periods) >= 2:
        current, previous = fy_periods[0], fy_periods[1]
        columns = [
            {"kind": "value", "label": current, "period": current},
            {"kind": "value", "label": previous, "period": previous},
            {"kind": "change", "label": "Change (in %)", "from": current, "to": previous},
        ]
        return [column for column in columns if column_has_data(rows, column)]
    return [{"kind": "value", "label": period, "period": period} for period in available_periods(rows)]


def add_aggregate_columns(columns: list[dict[str, str]], periods: list[str], kind: str, year: int) -> None:
    """Append aggregate current/previous/change columns if present."""

    current = find_period(periods, kind, year)
    previous = find_period(periods, kind, year - 1)
    if current:
        columns.append({"kind": "value", "label": current, "period": current})
        if previous:
            columns.append({"kind": "value", "label": previous, "period": previous})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous})


def normalize_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize row payloads into display dictionaries."""

    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or item.get("metric") or "").strip()
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        if not label:
            continue
        normalized = dict(item)
        normalized.update(
            {
                "label": label,
                "type": str(item.get("type") or ("data" if values else "section")).lower(),
                "values": {str(key): str(val) for key, val in values.items() if str(val).strip()},
                "changes": item.get("changes") if isinstance(item.get("changes"), dict) else {},
            }
        )
        output.append(normalized)
    return output


def available_periods(rows: list[dict[str, Any]]) -> list[str]:
    """Return available periods sorted for display."""

    found: set[str] = set()
    for row in rows:
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        found.update(str(period) for period, value in values.items() if str(value).strip())
    return sorted(found, key=period_sort_key)


def pick_current_period(periods: list[str], result_period: str) -> str:
    """Pick the current result period from metadata or values."""

    if result_period in periods:
        return result_period
    parsed = parse_period(result_period)
    if parsed:
        found = find_period(periods, parsed[0], parsed[1])
        if found:
            return found
    quarter_periods = [period for period in periods if (parse_period(period) or ("", 0))[0].startswith("Q")]
    return quarter_periods[0] if quarter_periods else (periods[0] if periods else "")


def quarter_fy_from_columns(columns: list[dict[str, str]], result_period: str) -> tuple[str, str]:
    """Return filename-friendly quarter and FY labels."""

    parsed_result = parse_period(result_period)
    if parsed_result:
        kind, year = parsed_result
        return kind, f"FY{year:02d}"
    period = columns[0].get("period", "") if columns else result_period
    parsed = parse_period(period) or parse_period(compact_column_header(period))
    if not parsed:
        return "Period", "FY"
    kind, year = parsed
    return kind, f"FY{year:02d}"


def parse_period(value: str) -> tuple[str, int] | None:
    """Parse labels such as Q4 FY26, 9M FY26, H1 FY26, and FY26."""

    match = re.search(r"\b(?:(Q[1-4]|H[12]|9M)\s*FY?|FY)\s*(\d{2,4})\b", value or "", flags=re.IGNORECASE)
    if not match:
        return None
    full = match.group(0).upper().replace(" ", "")
    year = int(match.group(2))
    if year >= 2000:
        year %= 100
    if full.startswith("Q"):
        return full[:2], year
    if full.startswith("H"):
        return full[:2], year
    if full.startswith("9M"):
        return "9M", year
    return "FY", year


def period_sort_key(period: str) -> tuple[int, int, str]:
    """Sort newest quarters/FY first."""

    parsed = parse_period(period)
    if not parsed:
        return (999, 999, period)
    kind, year = parsed
    rank = {"Q4": 0, "Q3": 1, "Q2": 2, "Q1": 3, "9M": 4, "H2": 5, "H1": 6, "FY": 7}.get(kind, 99)
    return (-year, rank, period)


def previous_quarter_kind(quarter: int) -> str:
    """Return previous quarter label."""

    return "Q4" if quarter == 1 else f"Q{quarter - 1}"


def previous_quarter_year(quarter: int, year: int) -> int:
    """Return fiscal year for previous quarter."""

    return year - 1 if quarter == 1 else year


def find_period(periods: list[str], kind: str, year: int) -> str:
    """Find a period by kind and FY year."""

    for period in periods:
        if parse_period(period) == (kind, year):
            return period
    return ""


def pick_values(rows: list[dict[str, Any]], aliases: list[str]) -> dict[str, str]:
    """Return values for the first matching row alias."""

    alias_keys = [row_key(alias) for alias in aliases]
    exact_matches: list[dict[str, Any]] = []
    fuzzy_matches: list[dict[str, Any]] = []
    for row in rows:
        label_key = row_key(str(row.get("label") or ""))
        if not label_key:
            continue
        if label_key in alias_keys:
            exact_matches.append(row)
        elif any(alias_key and (alias_key in label_key or label_key in alias_key) for alias_key in alias_keys):
            fuzzy_matches.append(row)
    for row in exact_matches + fuzzy_matches:
        values = row.get("values")
        if isinstance(values, dict) and values:
            return {str(key): str(value) for key, value in values.items() if str(value).strip()}
    return {}


def pick_values_strict(rows: list[dict[str, Any]], aliases: list[str]) -> dict[str, str]:
    """Return values where the alias is present in the row label.

    Unlike pick_values(), this does not allow a short row label to match a
    longer alias. That prevents rows like Exceptional items from matching
    aliases such as Profit before tax but after exceptional items.
    """

    alias_keys = [row_key(alias) for alias in aliases]
    exact_matches: list[dict[str, Any]] = []
    contains_matches: list[dict[str, Any]] = []
    for row in rows:
        label_key = row_key(str(row.get("label") or ""))
        if not label_key:
            continue
        if label_key in alias_keys:
            exact_matches.append(row)
        elif any(alias_key and alias_key in label_key for alias_key in alias_keys):
            contains_matches.append(row)
    for row in exact_matches + contains_matches:
        values = row.get("values")
        if isinstance(values, dict) and values:
            return {str(key): str(value) for key, value in values.items() if str(value).strip()}
    return {}


def pick_values_exact(rows: list[dict[str, Any]], aliases: list[str]) -> dict[str, str]:
    """Return values only when a row label exactly matches an alias key."""

    alias_keys = {row_key(alias) for alias in aliases}
    for row in rows:
        label_key = row_key(str(row.get("label") or ""))
        if label_key not in alias_keys:
            continue
        values = row.get("values")
        if isinstance(values, dict) and values:
            return {str(key): str(value) for key, value in values.items() if str(value).strip()}
    return {}


def sum_matching_rows(rows: list[dict[str, Any]], needles: list[str]) -> dict[str, str]:
    """Sum rows whose labels contain the provided needles."""

    needle_keys = [row_key(item) for item in needles]
    periods = available_periods(rows)
    result: dict[str, str] = {}
    for period in periods:
        total = 0.0
        matched = False
        for row in rows:
            key = row_key(str(row.get("label") or ""))
            if not any(needle in key for needle in needle_keys):
                continue
            number = to_number((row.get("values") or {}).get(period))
            if number is None:
                continue
            matched = True
            total += number
        if matched:
            result[period] = format_number(total)
    return result


def calc_sum(periods: list[str], components: list[dict[str, str]]) -> dict[str, str]:
    """Sum component rows period-by-period, treating missing cells as zero."""

    active_components = [component for component in components if component]
    if not active_components:
        return {}
    result: dict[str, str] = {}
    for period in periods:
        matched = False
        total = 0.0
        for component in active_components:
            number = to_number(component.get(period))
            if number is None:
                continue
            matched = True
            total += number
        if matched:
            result[period] = format_number(total)
    return result


def calc_subtract(
    periods: list[str],
    base: dict[str, str],
    components: list[dict[str, str]],
    fallback: dict[str, str],
    *,
    require_any_component: bool,
) -> dict[str, str]:
    """Calculate base minus components, preferring direct PDF values.

    Client rule: once a formula is applicable, missing component rows/cells are
    treated as zero. This handles companies that omit zero-value expense lines.
    """

    result: dict[str, str] = dict(fallback)
    active_components = [component for component in components if component]
    has_any_component = bool(active_components)
    for period in periods:
        if result.get(period):
            continue
        base_value = to_number(base.get(period))
        if base_value is None or (require_any_component and not has_any_component):
            continue
        total = base_value
        for component_value in component_numbers_for_period(active_components, period):
            total -= component_value
        result[period] = format_number(total)
    return result


def calc_add(
    periods: list[str],
    base: dict[str, str],
    components: list[dict[str, str]],
    fallback: dict[str, str],
    *,
    require_any_component: bool,
) -> dict[str, str]:
    """Calculate base plus components, preferring direct PDF values."""

    result: dict[str, str] = dict(fallback)
    active_components = [component for component in components if component]
    has_any_component = bool(active_components)
    for period in periods:
        if result.get(period):
            continue
        base_value = to_number(base.get(period))
        if base_value is None or (require_any_component and not has_any_component):
            continue
        total = base_value
        for component_value in component_numbers_for_period(active_components, period):
            total += component_value
        result[period] = format_number(total)
    return result


def component_numbers_for_period(components: list[dict[str, str]], period: str) -> list[float]:
    """Return component numbers, treating missing cells as zero."""

    numbers: list[float] = []
    for component in components:
        number = to_number(component.get(period))
        numbers.append(number if number is not None else 0.0)
    return numbers


def calc_margin(periods: list[str], numerator: dict[str, str], denominator: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    """Calculate a percentage margin from numerator and denominator."""

    result: dict[str, str] = {}
    for period in periods:
        num = to_number(numerator.get(period))
        den = to_number(denominator.get(period))
        if num is None or den in (None, 0):
            if fallback.get(period):
                result[period] = ensure_percent(fallback[period])
            continue
        result[period] = f"{(num / den) * 100:.2f}%"
    return result or {period: ensure_percent(value) for period, value in fallback.items()}


def change_for_row(row: dict[str, Any], current_period: str, previous_period: str) -> str:
    """Return percentage change using current and previous values."""

    current = to_number((row.get("values") or {}).get(current_period))
    previous = to_number((row.get("values") or {}).get(previous_period))
    if current is None or previous in (None, 0):
        return ""
    return f"{((current - previous) / abs(previous)) * 100:.2f}%"


def column_has_data(rows: list[dict[str, Any]], column: dict[str, str]) -> bool:
    """Return whether a value/change column has any visible data."""

    for row in rows:
        if column["kind"] == "value" and str((row.get("values") or {}).get(column["period"], "")).strip():
            return True
        if column["kind"] == "change" and change_for_row(row, column["from"], column["to"]):
            return True
    return False


def row_has_value(row: dict[str, Any]) -> bool:
    """Return whether a row has at least one value."""

    return any(str(value).strip() for value in (row.get("values") or {}).values())


def data_row(label: str, values: dict[str, str], style: str, *, role: str = "", basis: str = "") -> dict[str, Any]:
    """Create a display data row."""

    row = {"label": label, "type": "data", "values": values, "style": style}
    if role:
        row["formula_role"] = role
    if basis:
        row["formula_basis"] = basis
    return row


def section_row(label: str) -> dict[str, Any]:
    """Create a display section row."""

    return {"label": label, "type": "section", "values": {}, "style": "section"}


def to_number(value: Any) -> float | None:
    """Parse display values into floats."""

    text = str(value or "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    negative_parentheses = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", "."}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -abs(number) if negative_parentheses else number


def format_number(value: float) -> str:
    """Format calculated monetary values with production-safe precision."""

    if not math.isfinite(value):
        return ""
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".")


def format_display_cell(label: str, value: Any) -> str:
    """Format visible table values without decimal points."""

    text = _compact_numeric_spacing("" if value is None else str(value).strip())
    if _is_unclear_value(text):
        return "N/A"
    if not text:
        return text
    if "eps" in row_key(label):
        return _format_numeric_display(text, force_two_decimals=False, percent=False)
    if "%" in text or "margin" in row_key(label):
        return _format_numeric_display(text, force_two_decimals=True, percent=True)
    cleaned = text.replace(",", "")
    negative_parentheses = cleaned.startswith("(") and cleaned.endswith(")")
    numeric_text = cleaned.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", numeric_text):
        return text
    number = float(numeric_text)
    if negative_parentheses:
        number = -abs(number)
    return _format_whole_display_number(number)


def truncate_decimal_display(value: Any) -> str:
    """Backward-compatible helper: hide decimal fractions in visible cells."""

    text = _compact_numeric_spacing("" if value is None else str(value).strip())
    if not text:
        return text
    if _is_unclear_value(text):
        return "N/A"

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        suffix = "%" if token.endswith("%") else ""
        core = token[:-1] if suffix else token
        wrapped = core.startswith("(") and core.endswith(")")
        numeric = core.strip("()").replace(",", "")
        try:
            number = float(numeric)
        except ValueError:
            return token
        if wrapped:
            number = -abs(number)
        return _format_whole_display_number(number) + suffix

    return re.sub(r"\(?-?\d[\d,]*\.\d+\)?%?", _replace, text)


def _format_numeric_display(text: str, *, force_two_decimals: bool, percent: bool) -> str:
    suffix = "%" if percent and "%" in text else ""
    cleaned = _compact_numeric_spacing(text).strip().removesuffix("%").strip().replace(",", "")
    negative_parentheses = cleaned.startswith("(") and cleaned.endswith(")")
    numeric_text = cleaned.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", numeric_text):
        return text
    number = float(numeric_text)
    if negative_parentheses:
        number = -abs(number)
    return _format_whole_display_number(number) + suffix


def _format_parenthesized_number(value: float, *, decimals: int) -> str:
    if not math.isfinite(value):
        return ""
    decimals = max(0, min(6, int(decimals)))
    text = f"{abs(value):.{decimals}f}"
    return f"({text})" if value < 0 else text


def _format_whole_display_number(value: float) -> str:
    """Display-only integer formatting that truncates decimal fractions."""

    if not math.isfinite(value):
        return ""
    number = math.trunc(abs(value))
    text = str(number)
    return f"({text})" if value < 0 else text


def _compact_numeric_spacing(value: str) -> str:
    """Remove spaces accidentally inserted inside numeric cells."""

    text = str(value or "").strip()
    compact = re.sub(r"\s+", "", text)
    candidate = compact.removesuffix("%").strip()
    if candidate.startswith("(") and candidate.endswith(")"):
        candidate = candidate[1:-1]
    candidate = candidate.replace(",", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", candidate):
        return compact
    return text


def _is_unclear_value(value: str) -> bool:
    """Return true for model/OCR uncertainty markers that should display as N/A."""

    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return normalized in {
        "unclear",
        "notclearlyvisible",
        "notclear",
        "notlegible",
        "cannotread",
        "cantread",
        "unreadable",
        "unreadble",
        "notreadable",
        "illegible",
        "notvisible",
        "notidentified",
        "unknown",
        "na",
        "notavailable",
        "notapplicable",
        "blank",
        "missing",
    }


def ensure_percent(value: str) -> str:
    """Return a percent-formatted value."""

    text = str(value or "").strip()
    return text if not text or text.endswith("%") else f"{text}%"


def is_negative_percent(value: str) -> bool:
    """Return whether text is a negative percentage."""

    return str(value or "").strip().startswith("-") and "%" in str(value or "")


def row_key(label: str) -> str:
    """Normalize row labels for alias matching."""

    return re.sub(r"[^a-z0-9]", "", label.lower())


def safe_filename(value: str, *, max_length: int = 72) -> str:
    """Return a filesystem-safe filename stem."""

    text = re.sub(r"[^A-Za-z0-9]+", "_", clean_display_company_name(value)).strip("_")
    max_length = max(16, int(max_length or 72))
    return text[:max_length].strip("_") or "financial_output"


def clean_display_company_name(value: Any) -> str:
    """Remove filename prefixes from visible company names."""

    text = str(value or "").strip()
    if not text:
        return "Company"
    text = re.sub(r"(?i)^source[_\s-]*pdf[_\s-]*", "", text)
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:0\d{1,2}|\d{2,3})[\s.-]+(?=[A-Za-z])", "", text).strip()
    return text or "Company"


def company_name(extraction: dict[str, Any], announcement: Announcement | None) -> str:
    """Return display company name."""

    return clean_display_company_name(extraction.get("company_name") or (announcement.company_name if announcement else "") or "Company")


def source_name(extraction: dict[str, Any], announcement: Announcement | None) -> str:
    """Return exchange source label."""

    return str(extraction.get("source") or (announcement.source.upper() if announcement else "") or "PDF")


def footer_basis_label(extraction: dict[str, Any], standalone_tag: bool = False) -> str:
    """Return the compact statement-basis footer label."""

    if standalone_tag:
        return ""
    basis = str(extraction.get("statement_basis") or "").strip().lower()
    if basis == "consolidated":
        return "CONSOLIDATED"
    if basis == "single_statement":
        return ""
    return ""


def title_with_basis(company: str, extraction: dict[str, Any], standalone_tag: bool = False) -> str:
    """Return title with visible basis where it improves auditability."""

    if standalone_tag:
        return f"{company} (STANDALONE)"
    basis = str(extraction.get("statement_basis") or "").strip().lower()
    if basis == "consolidated":
        return f"{company} (CONSOLIDATED)"
    return company


def _style_body_cell(cell: Any, text: Any, style: str, col_index: int, cell_size: int) -> None:
    """Apply row/cell colors and fonts."""

    text.set_fontsize(cell_size)
    text.set_ha("left" if col_index == 0 else "center")
    text.set_color(BLACK)
    if style == "section":
        cell.set_facecolor(HEADER_BLUE)
        text.set_color(WHITE)
        text.set_fontweight("bold")
    elif style == "section_green":
        cell.set_facecolor(DARK_GREEN)
        text.set_color(WHITE)
        text.set_fontweight("bold")
    elif style == "pnl_section":
        cell.set_facecolor(DARK_GREEN if col_index == 0 else WHITE)
        text.set_color(WHITE if col_index == 0 else BLACK)
        text.set_fontweight("bold" if col_index == 0 else "normal")
    elif style == "segment_blue":
        cell.set_facecolor(HEADER_BLUE)
        text.set_color(WHITE)
        text.set_fontweight("bold")
    elif style == "segment_green":
        cell.set_facecolor(DARK_GREEN)
        text.set_color(WHITE)
        text.set_fontweight("bold")
    elif style == "key":
        cell.set_facecolor(DARK_GREEN if col_index == 0 else LIGHT_GREEN)
        text.set_color(WHITE if col_index == 0 else BLACK)
        text.set_fontweight("bold")
    elif style == "subtotal":
        cell.set_facecolor(LIGHT_GREEN)
        text.set_fontweight("bold")
    elif style == "margin":
        cell.set_facecolor(WHITE)
        text.set_fontweight("bold")
    elif style == "important":
        cell.set_facecolor(LIGHT_GREEN)
        text.set_fontweight("bold")
    elif style == "alternate":
        cell.set_facecolor(SUBTLE)
    else:
        cell.set_facecolor(VERY_LIGHT_GREEN if col_index == 0 else WHITE)
