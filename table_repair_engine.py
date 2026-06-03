"""Deterministic table repairs and validation flags for financial OCR tables."""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pl_image import row_key
from unit_detector import RS_CR, RS_LAKHS, RS_MILLIONS, RS_THOUSANDS, USD_MILLIONS, display_unit_for_source


REPAIR_LOG_PATH = Path("output") / "repair_logs" / "financial_table_repairs.jsonl"
MAJOR_ROW_ALIASES = {
    "revenue": ("revenue", "revenuefromoperations", "revenuefromoperation", "totalrevenue"),
    "other_income": ("otherincome",),
    "total_income": ("totalincome", "totalrevenueincome"),
    "total_expenses": ("totalexpenses", "totalexpense"),
    "pbt": ("profitbeforetax", "profitlossbeforetax"),
    "tax": ("totaltaxexpense", "totaltaxexpenses", "taxexpense", "taxexpenses"),
    "pat": ("pat", "profitaftertax", "profitfortheperiod", "profitfortheyear", "profitlossaftertax"),
    "eps_basic": ("epsbasic", "basiceps", "basic"),
}


def repair_financial_payload(
    payload: dict[str, Any],
    *,
    company: str = "",
    source_pdf: str = "",
) -> dict[str, Any]:
    """Return a repaired copy of a parsed payload plus local-only metadata."""

    data = copy.deepcopy(payload or {})
    metadata = _base_metadata(data, company=company, source_pdf=source_pdf)
    repairs: list[dict[str, Any]] = []
    warnings: list[str] = []
    critical: list[str] = []

    rows = _financial_rows(data)
    if rows:
        _repair_revenue(rows, metadata, repairs)
        _repair_pbt(rows, metadata, repairs)
        if not data.get("skip_deterministic_pat_repair"):
            _repair_pat(rows, metadata, repairs)
        critical.extend(_q4_equals_fy_issues(rows, str(data.get("result_period") or "")))
        critical.extend(_repeated_value_collision_issues(rows))
        critical.extend(_q4_layout_issues(rows, str(data.get("result_period") or "")))
        if _missing_major_values(rows):
            critical.append("missing_required_major_values")

    critical.extend(_unit_guard_issues(data))
    critical.extend(_statement_basis_issues(data))
    critical.extend(_balance_sheet_total_issues(data))
    critical.extend(_cash_flow_consistency_issues(data))

    identities = build_column_identities(data)
    if identities:
        data["column_identities"] = identities

    critical = _dedupe(critical)
    warnings = _dedupe(warnings)
    data["table_repair_metadata"] = {
        "repairs": repairs,
        "warnings": warnings,
        "critical_issues": critical,
        "column_identities": identities,
    }
    data["repair_critical_issues"] = critical
    data["repair_warning_categories"] = warnings
    if repairs or critical:
        _write_repair_log(metadata, repairs, warnings, critical)
    return data


def build_column_identities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build stable column identity objects from available canonical periods."""

    periods: list[str] = []
    for period in payload.get("period_columns") or []:
        text = str(period or "").strip()
        if text and text not in periods:
            periods.append(text)
    for row in _financial_rows(payload):
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        for period in values:
            if period not in periods:
                periods.append(str(period))
    output: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        parsed = _parse_period(period)
        if not parsed:
            period_type = "unknown"
            parent_header = ""
            status = "unmapped"
            confidence = 0.0
        elif parsed[0].startswith("Q"):
            period_type = "quarter"
            parent_header = "Quarter Ended"
            status = "mapped"
            confidence = 1.0
        elif parsed[0].startswith("H"):
            period_type = "half_year"
            parent_header = "Half Year Ended"
            status = "mapped"
            confidence = 1.0
        else:
            period_type = "year"
            parent_header = "Year Ended"
            status = "mapped"
            confidence = 1.0
        output.append(
            {
                "position_index": index,
                "parent_header": parent_header,
                "raw_column_label": period,
                "raw_date": _raw_date_from_period(period),
                "period_type": period_type,
                "audit_status": "",
                "canonical_period": period,
                "source_page": "",
                "source_table_type": "profit_and_loss",
                "column_mapping_status": status,
                "identity_confidence": confidence,
            }
        )
    return output


def _raw_date_from_period(period: str) -> str:
    """Return a human-readable implied date when only canonical period is available."""

    parsed = _parse_period(period)
    if not parsed:
        return ""
    kind, year = parsed
    full_year = 2000 + year
    if kind == "Q1":
        return f"30-Jun-{full_year}"
    if kind == "Q2" or kind == "H1":
        return f"30-Sep-{full_year}"
    if kind == "Q3":
        return f"31-Dec-{full_year - 1}"
    if kind == "Q4" or kind == "H2" or kind == "FY":
        return f"31-Mar-{full_year}"
    return ""


def _repair_revenue(rows: list[dict[str, Any]], metadata: dict[str, Any], repairs: list[dict[str, Any]]) -> None:
    revenue = _find_row(rows, MAJOR_ROW_ALIASES["revenue"])
    other_income = _find_row(rows, MAJOR_ROW_ALIASES["other_income"])
    total_income = _find_row(rows, MAJOR_ROW_ALIASES["total_income"])
    if not revenue or not other_income or not total_income:
        return
    for period, total in _numeric_values(total_income).items():
        other = _row_number(other_income, period)
        if other is None:
            continue
        expected = total - other
        _repair_row_value(
            revenue,
            period,
            expected,
            "revenue_from_total_income",
            metadata,
            repairs,
        )


def _repair_pbt(rows: list[dict[str, Any]], metadata: dict[str, Any], repairs: list[dict[str, Any]]) -> None:
    total_income = _find_row(rows, MAJOR_ROW_ALIASES["total_income"])
    total_expenses = _find_row(rows, MAJOR_ROW_ALIASES["total_expenses"])
    pbt = _find_row(rows, MAJOR_ROW_ALIASES["pbt"])
    if not total_income or not total_expenses or not pbt:
        return
    if _has_nonzero_exceptional_item(rows):
        return
    expenses_key = row_key(str(total_expenses.get("label") or ""))
    depreciation = _find_row(
        rows,
        (
            "depreciation",
            "depreciationandamortisationexpense",
            "depreciationandamortizationexpense",
        ),
    )
    finance = _find_row(rows, ("financecost", "financecosts"))
    for period, income in _numeric_values(total_income).items():
        expenses = _row_number(total_expenses, period)
        if expenses is None:
            continue
        if "excluding" in expenses_key:
            depreciation_value = _row_number(depreciation, period) if depreciation else None
            finance_value = _row_number(finance, period) if finance else None
            if depreciation_value is None and finance_value is None:
                continue
            expenses += (depreciation_value or 0.0) + (finance_value or 0.0)
        _repair_row_value(
            pbt,
            period,
            income - expenses,
            "pbt_from_total_income_minus_total_expenses",
            metadata,
            repairs,
        )


def _has_nonzero_exceptional_item(rows: list[dict[str, Any]]) -> bool:
    """Return true when PBT differs from total income-expenses due exceptional items."""

    for row in rows:
        key = row_key(str(row.get("label") or ""))
        if "exceptional" not in key or "beforeexceptional" in key:
            continue
        for value in _numeric_values(row).values():
            if abs(value) > 0.0001:
                return True
    return False


def _repair_pat(rows: list[dict[str, Any]], metadata: dict[str, Any], repairs: list[dict[str, Any]]) -> None:
    pbt = _find_row(rows, MAJOR_ROW_ALIASES["pbt"])
    tax = _find_row(rows, MAJOR_ROW_ALIASES["tax"])
    pat = _find_row(rows, MAJOR_ROW_ALIASES["pat"])
    if not pbt or not tax or not pat:
        return
    for period, pbt_value in _numeric_values(pbt).items():
        tax_value = _row_number(tax, period)
        if tax_value is None:
            continue
        _repair_row_value(
            pat,
            period,
            pbt_value - tax_value,
            "pat_from_pbt_minus_tax",
            metadata,
            repairs,
        )


def _repair_row_value(
    row: dict[str, Any],
    period: str,
    expected: float,
    reason: str,
    metadata: dict[str, Any],
    repairs: list[dict[str, Any]],
) -> None:
    values = row.setdefault("values", {})
    if not isinstance(values, dict):
        return
    current_text = values.get(period)
    current = _to_float(current_text)
    if current is not None and _close(current, expected):
        return
    old_value = "" if current_text is None else str(current_text)
    new_value = _format_number(expected)
    values[period] = new_value
    repairs.append(
        {
            "company": metadata.get("company", ""),
            "source_pdf": metadata.get("source_pdf", ""),
            "page": "",
            "table_type": "profit_and_loss",
            "row": str(row.get("label") or ""),
            "column": period,
            "old_value": old_value,
            "new_value": new_value,
            "repair_reason": reason,
            "validation_status": "repaired",
        }
    )


def _q4_equals_fy_issues(rows: list[dict[str, Any]], result_period: str) -> list[str]:
    parsed = _parse_period(result_period)
    if not parsed or parsed[0] != "Q4":
        return []
    q4 = f"Q4 FY{parsed[1]:02d}"
    fy = f"FY{parsed[1]:02d}"
    labels: list[str] = []
    for row in _major_rows(rows):
        q4_value = _row_number(row, q4)
        fy_value = _row_number(row, fy)
        if q4_value is None or fy_value is None or abs(fy_value) < 0.0001:
            continue
        if _close(q4_value, fy_value, strict=True):
            labels.append(str(row.get("label") or "row"))
    if len(labels) >= 2 or any(row_key(label) in {"revenue", "revenuefromoperations", "totalincome"} for label in labels):
        return [f"q4_equals_fy_column_collision:{','.join(labels[:6])}"]
    return []


def _q4_layout_issues(rows: list[dict[str, Any]], result_period: str) -> list[str]:
    parsed = _parse_period(result_period)
    if not parsed or parsed[0] != "Q4":
        return []
    periods = _periods(rows)
    if not any(period.startswith("FY") for period in periods):
        return []
    year = parsed[1]
    expected = {f"Q4 FY{year:02d}", f"Q3 FY{year:02d}", f"Q4 FY{year - 1:02d}", f"FY{year:02d}", f"FY{year - 1:02d}"}
    missing = sorted(expected - periods)
    if missing:
        return [f"column_mapping_failure:q4_expected_layout_missing:{','.join(missing)}"]
    return []


def _repeated_value_collision_issues(rows: list[dict[str, Any]]) -> list[str]:
    major = _major_rows(rows)
    vectors: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for row in major:
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        normalized = tuple(
            sorted(
                (str(period), _number_token(value))
                for period, value in values.items()
                if _number_token(value) not in {"", "0", "0.0", "0.00"}
            )
        )
        if len(normalized) < 2:
            continue
        vectors.setdefault(normalized, []).append(str(row.get("label") or "row"))
    collisions = [labels for labels in vectors.values() if len(labels) >= 3]
    if collisions:
        return [f"repeated_value_collision:{','.join(collisions[0][:6])}"]
    return []


def _unit_guard_issues(payload: dict[str, Any]) -> list[str]:
    source = str(payload.get("source_currency_unit") or "")
    display = str(payload.get("currency_unit") or "")
    if not source or not display:
        return ["unit_not_detected"]
    if source == RS_LAKHS and display != RS_CR:
        return ["unit_conversion_failure:lakhs_not_displayed_as_crores"]
    if source == RS_MILLIONS and display != RS_CR:
        return ["unit_conversion_failure:inr_millions_not_displayed_as_crores"]
    if source == RS_THOUSANDS and display != RS_CR:
        return ["unit_conversion_failure:thousands_not_displayed_as_crores"]
    if source == USD_MILLIONS and display != USD_MILLIONS:
        return ["unit_conversion_failure:usd_millions_wrong_display_unit"]
    if source == RS_CR and display != RS_CR:
        return ["unit_conversion_failure:crores_wrong_display_unit"]
    if source not in {RS_LAKHS, RS_CR, RS_MILLIONS, RS_THOUSANDS, USD_MILLIONS}:
        expected = display_unit_for_source(source)
        if not expected:
            return ["unit_not_detected"]
    return []


def _statement_basis_issues(payload: dict[str, Any]) -> list[str]:
    discovery = payload.get("discovery_metadata")
    text = str(payload.get("ocr_markdown") or "").lower()
    basis = str(payload.get("statement_basis") or "").lower()
    if isinstance(discovery, dict):
        consolidated_available = bool(discovery.get("consolidated_available"))
        selected_basis = str(discovery.get("selected_statement_basis") or basis).lower()
        if consolidated_available and selected_basis == "standalone":
            return ["consolidated_available_but_standalone_selected"]
        return []
    if _has_positive_consolidated_statement_marker(text) and basis == "standalone":
        return ["consolidated_available_but_standalone_selected"]
    return []


def _has_positive_consolidated_statement_marker(text: str) -> bool:
    """Return true for real consolidated statements, not boilerplate notes."""

    cleaned = re.sub(r"\s+", " ", str(text or "").lower())
    negative_patterns = (
        "no subsidiary",
        "does not have any subsidiary",
        "not required to prepare consolidated",
        "consolidated financial statements are not applicable",
        "no consolidated financial statements",
    )
    if any(pattern in cleaned for pattern in negative_patterns):
        return False
    return bool(re.search(r"\b(?:audited\s+)?consolidated\s+financial\s+results?\b|\bconsolidated\s+statement\b", cleaned))


def _balance_sheet_total_issues(payload: dict[str, Any]) -> list[str]:
    rows = _variable_rows(payload)
    total_assets = _find_row(rows, ("totalassets",))
    total_equity_liabilities = _find_row(rows, ("totalequityandliabilities", "totalliabilitiesandequity"))
    if not total_assets or not total_equity_liabilities:
        return []
    issues: list[str] = []
    for period, assets in _numeric_values(total_assets).items():
        liabilities = _row_number(total_equity_liabilities, period)
        if liabilities is not None and not _close(assets, liabilities):
            issues.append(f"balance_sheet_total_mismatch:{period}")
    return issues


def _cash_flow_consistency_issues(payload: dict[str, Any]) -> list[str]:
    cash_rows = payload.get("cash_flow_variables") if isinstance(payload.get("cash_flow_variables"), list) else []
    if not cash_rows:
        return []
    issues: list[str] = []
    core_rows = [row for row in cash_rows if _cash_flow_role(str(row.get("label") or ""))]
    expected_periods = _expected_cash_flow_periods(str(payload.get("result_period") or ""))
    if len(core_rows) >= 3 and expected_periods:
        for period in expected_periods:
            if any(not _cash_flow_value_present(row, period) for row in core_rows[:3]):
                issues.append(f"cash_flow_period_missing:{period}")
    opening = _find_row(cash_rows, ("openingcashandcashequivalents", "cashandcashequivalentsatthebeginning"))
    closing = _find_row(cash_rows, ("closingcashandcashequivalents", "cashandcashequivalentsattheend"))
    net_change = _find_row(cash_rows, ("netincreaseincashandcashequivalents", "netdecreaseincashandcashequivalents"))
    if opening and closing and net_change:
        for period, open_value in _numeric_values(opening).items():
            close_value = _row_number(closing, period)
            change_value = _row_number(net_change, period)
            if close_value is None or change_value is None:
                continue
            if not _close(open_value + change_value, close_value):
                issues.append(f"cash_flow_closing_cash_mismatch:{period}")
    return issues


def _cash_flow_role(label: str) -> str:
    key = row_key(label)
    if "operating" in key:
        return "operating"
    if "investing" in key:
        return "investing"
    if "financing" in key:
        return "financing"
    return ""


def _cash_flow_value_present(row: dict[str, Any], period: str) -> bool:
    """Return whether a cash-flow cell is visibly present.

    Cash-flow statements often use a dash for nil/zero activity. Treat that as
    a present value for period-completeness checks while still rejecting truly
    blank or absent columns.
    """

    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    if period not in values:
        return False
    text = str(values.get(period) or "").strip()
    if not text:
        return False
    if _to_float(text) is not None:
        return True
    return re.sub(r"\s+", "", text).lower() in {"-", "—", "–", "nil"}


def _expected_cash_flow_periods(result_period: str) -> list[str]:
    match = re.search(r"(?:Q4|H2|FY)\s*FY?(\d{2,4})", result_period, flags=re.IGNORECASE)
    if not match:
        return []
    year = int(match.group(1))
    if year >= 2000:
        year %= 100
    return [f"FY{year:02d}", f"FY{year - 1:02d}"]


def _missing_major_values(rows: list[dict[str, Any]]) -> bool:
    periods = _periods(rows)
    if not periods:
        return True
    has_revenue = bool(_find_row(rows, MAJOR_ROW_ALIASES["revenue"]) or _find_row(rows, MAJOR_ROW_ALIASES["total_income"]))
    return not has_revenue


def _financial_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload.get("financial_rows") or [] if isinstance(row, dict)]


def _variable_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in payload.get("balance_sheet_variables") or []:
        if isinstance(section, dict) and isinstance(section.get("rows"), list):
            rows.extend(row for row in section["rows"] if isinstance(row, dict))
    return rows


def _find_row(rows: list[dict[str, Any]], aliases: tuple[str, ...]) -> dict[str, Any] | None:
    alias_set = set(aliases)
    for row in rows:
        key = row_key(str(row.get("label") or ""))
        if key in alias_set:
            return row
    for row in rows:
        key = row_key(str(row.get("label") or ""))
        # Only allow the alias to be contained in the row label, never the
        # reverse. The reverse match made short labels such as "Revenue" match
        # longer aliases such as "totalrevenueincome", causing Revenue to be
        # repaired as Revenue - Other Income.
        if any(alias and alias in key for alias in alias_set):
            return row
    return None


def _major_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    major_aliases = set(sum((list(value) for value in MAJOR_ROW_ALIASES.values()), []))
    return [row for row in rows if any(alias in row_key(str(row.get("label") or "")) for alias in major_aliases)]


def _numeric_values(row: dict[str, Any]) -> dict[str, float]:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    output: dict[str, float] = {}
    for period, value in values.items():
        number = _to_float(value)
        if number is not None:
            output[str(period)] = number
    return output


def _row_number(row: dict[str, Any], period: str) -> float | None:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    return _to_float(values.get(period))


def _to_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or "%" in text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -abs(number) if negative else number


def _number_token(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _close(actual: float, expected: float, *, strict: bool = False) -> bool:
    tolerance = 0.005 if strict else max(0.05, abs(expected) * 0.05)
    return abs(actual - expected) <= tolerance


def _format_number(value: float) -> str:
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".")


def _periods(rows: list[dict[str, Any]]) -> set[str]:
    periods: set[str] = set()
    for row in rows:
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        periods.update(str(period) for period, value in values.items() if str(value).strip())
    return periods


def _parse_period(value: str) -> tuple[str, int] | None:
    match = re.search(r"\b(?:(Q[1-4]|H[12])\s*FY?|FY)\s*(\d{2,4})\b", str(value or ""), flags=re.IGNORECASE)
    if not match:
        return None
    full = match.group(0).upper().replace(" ", "")
    kind = match.group(1).upper() if match.group(1) else "FY"
    year = int(match.group(2))
    if year >= 2000:
        year %= 100
    if full.startswith("Q"):
        kind = full[:2]
    elif full.startswith("H"):
        kind = full[:2]
    return kind, year


def _base_metadata(payload: dict[str, Any], *, company: str, source_pdf: str) -> dict[str, Any]:
    return {
        "company": company or str(payload.get("company_name") or ""),
        "source_pdf": source_pdf or str(payload.get("pdf_path") or ""),
    }


def _write_repair_log(
    metadata: dict[str, Any],
    repairs: list[dict[str, Any]],
    warnings: list[str],
    critical: list[str],
) -> None:
    try:
        REPAIR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        status = "failed" if critical else "ok"
        with REPAIR_LOG_PATH.open("a", encoding="utf-8") as handle:
            if repairs:
                for repair in repairs:
                    entry = dict(repair)
                    entry["logged_at"] = datetime.now().isoformat(timespec="seconds")
                    entry["validation_status"] = status if critical else entry.get("validation_status", "repaired")
                    handle.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
            if critical and not repairs:
                handle.write(
                    json.dumps(
                        {
                            "logged_at": datetime.now().isoformat(timespec="seconds"),
                            "company": metadata.get("company", ""),
                            "source_pdf": metadata.get("source_pdf", ""),
                            "page": "",
                            "table_type": "validation",
                            "row": "",
                            "column": "",
                            "old_value": "",
                            "new_value": "",
                            "repair_reason": ",".join(warnings),
                            "validation_status": status,
                            "critical_issues": critical,
                        },
                        ensure_ascii=True,
                        default=str,
                    )
                    + "\n"
                )
    except Exception:
        logging.exception("Failed to write financial table repair log.")


def _dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    for item in items:
        if item and item not in output:
            output.append(item)
    return output
