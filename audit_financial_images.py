"""Audit generated financial-image eligibility against stored live debug payloads.

This is a read-only diagnostic for the Telegram image pipeline. It does not
regenerate images; it checks whether the current renderer would allow the same
sections from the latest stored extraction payloads and flags obvious data
quality problems before fresh PNGs are trusted.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from bs_cf_image import _cash_flow_only_rows, build_bs_cf_rows
from image_validation import image_file_issues
from image_generator import (
    _available_render_jobs,
    _period_caption_parts,
    _repair_extraction_from_embedded_ocr_tables,
    _standalone_conflicts_with_consolidated_source,
    _statement_basis,
)
from pl_image import (
    build_pl_rows,
    normalize_rows,
    result_display_columns,
    row_has_value,
    row_key,
    variable_display_columns,
)
from segment_image import build_segment_rows
from unit_detector import normalize_extraction_units


PNL_LABELS_IN_BS = {
    "revenue",
    "totalincome",
    "profitbeforetax",
    "profitaftertax",
    "epsbasic",
    "costofmaterialsconsumed",
    "otherexpenses",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit current financial image eligibility.")
    parser.add_argument(
        "--debug-log",
        default="latest",
        help="Live debug JSONL file to inspect, or 'latest' to auto-pick the newest live_debug_*.jsonl.",
    )
    parser.add_argument(
        "--since",
        default="2026-05-23T18:05:00",
        help="Only inspect processed records at or after this timestamp string.",
    )
    parser.add_argument(
        "--image-root",
        default="output/images",
        help="Image root to compare against renderer file timestamps.",
    )
    args = parser.parse_args()

    debug_log = _resolve_debug_log(args.debug_log)
    records = _load_records(debug_log, args.since)
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    formula_issues: list[dict[str, Any]] = []
    image_mtime_issues: list[str] = []
    image_issues = image_file_issues(Path(args.image_root))

    patched_after_images = _patched_after_newest_image(Path(args.image_root))
    if patched_after_images:
        image_mtime_issues.append(patched_after_images)

    for record in records:
        row = _audit_record(record)
        rows.append(row)
        if row["issues"]:
            issues.append(row)
        formula_issues.extend(_audit_pnl_formulas(record, row["current"]))

    counts = Counter(kind for row in rows for kind in row["current"])
    skipped = sum(1 for row in rows if row["standalone_conflict_skip"])

    print(f"Debug log: {debug_log}")
    print(f"Since: {args.since}")
    print(f"Image root: {args.image_root}")
    print(f"Audited records: {len(rows)}")
    print(f"Current eligible P&L: {counts.get('pnl', 0)}")
    print(f"Current eligible BS/CF: {counts.get('bs_cf', 0)}")
    print(f"Current eligible Segments: {counts.get('segments', 0)}")
    print(f"Standalone-conflict skips: {skipped}")
    print(f"Eligibility issues: {len(issues)}")
    print(f"P&L formula issues: {len(formula_issues)}")
    print(f"Image file issues: {len(image_issues)}")
    for message in image_mtime_issues:
        print(f"WARNING: {message}")

    if issues:
        print("\nEligibility issue details:")
        for row in issues:
            print(json.dumps(row, ensure_ascii=False))
    if formula_issues:
        print("\nFormula issue details:")
        for issue in formula_issues:
            print(json.dumps(issue, ensure_ascii=False))
    if image_issues:
        print("\nImage file issue details:")
        for issue in image_issues:
            print(json.dumps(issue, ensure_ascii=False))

    print("\nPer-record summary:")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))

    return 1 if issues or formula_issues or image_issues else 0


def _load_records(path: Path, since: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "processed":
            continue
        if str(record.get("timestamp") or "") < since:
            continue
        metrics = record.get("metrics") or {}
        if record.get("rendered_images") or metrics.get("total_value_count", 0):
            records.append(record)
    return records


def _resolve_debug_log(value: str) -> Path:
    """Return the requested debug log path, resolving 'latest' by mtime."""

    if value.lower() != "latest":
        return Path(value)
    candidates = [
        path
        for path in Path("logs/debug").glob("live_debug_*.jsonl")
        if path.name != "live_debug_latest.jsonl"
    ]
    if not candidates:
        raise FileNotFoundError("No logs/debug/live_debug_*.jsonl files found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _audit_record(record: dict[str, Any]) -> dict[str, Any]:
    extraction = _repair_extraction_from_embedded_ocr_tables(record.get("extraction_payload") or {})
    company = extraction.get("company_name") or (record.get("announcement") or {}).get("company_name") or ""
    normalized, _, display_unit, _ = normalize_extraction_units(
        extraction,
        company=company,
        announcement_date=extraction.get("board_meeting_date") or "",
    )
    basis = _statement_basis(normalized)
    standalone_conflict_skip = _standalone_conflicts_with_consolidated_source(normalized)

    if standalone_conflict_skip:
        current: list[str] = []
    else:
        quarter, fy = _period_caption_parts(normalized)
        jobs = _available_render_jobs(
            normalized=normalized,
            announcement=None,
            output_dir=Path("output/_audit_placeholder"),
            display_unit=display_unit,
            standalone_tag=basis == "standalone",
            company=company,
            quarter_label=quarter,
            fy_label=fy,
        )
        current = [str(job["kind"]) for job in jobs if job["available"]]

    bs_rows = build_bs_cf_rows(normalized)
    segment_rows = build_segment_rows(normalized)
    pl_rows = build_pl_rows(normalize_rows(normalized.get("financial_rows")))
    issues = _eligibility_issues(normalized, current, bs_rows, segment_rows, pl_rows)

    return {
        "timestamp": record.get("timestamp"),
        "company": company,
        "old_images": [Path(path).name for path in record.get("rendered_images") or []],
        "current": current,
        "basis": basis,
        "standalone_conflict_skip": standalone_conflict_skip,
        "bs_cash_flow_only": _cash_flow_only_rows(bs_rows),
        "bs_value_rows": _value_row_count(bs_rows),
        "segment_value_rows": _value_row_count(segment_rows),
        "pnl_value_rows": _value_row_count(pl_rows),
        "issues": issues,
    }


def _eligibility_issues(
    normalized: dict[str, Any],
    current: list[str],
    bs_rows: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
    pl_rows: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    if "bs_cf" in current:
        if not variable_display_columns(bs_rows):
            issues.append("bs_cf_without_columns")
        if not any(row_has_value(row) for row in bs_rows):
            issues.append("bs_cf_without_values")
        for row in bs_rows:
            if not row_has_value(row):
                continue
            label = str(row.get("label") or "").strip()
            key = row_key(label)
            if re.fullmatch(r"[ivxlcdm]+", label, flags=re.IGNORECASE):
                issues.append(f"bs_roman_index_label:{label}")
            if key in PNL_LABELS_IN_BS:
                issues.append(f"bs_pnl_label:{label}")
        if _cash_flow_only_rows(bs_rows) and _value_row_count(bs_rows) < 2:
            issues.append("cash_flow_only_too_sparse")

    if "segments" in current and not _has_segment_business_blocks(segment_rows):
        issues.append("segment_without_named_rows")

    if "pnl" in current:
        columns = result_display_columns(pl_rows, str(normalized.get("result_period") or ""))
        periods = [str(column.get("period") or "") for column in columns if column.get("kind") == "value"]
        revenue = next(
            (
                row
                for row in pl_rows
                if str(row.get("formula_role") or "") == "revenue"
                or row_key(row.get("label", "")) in {"revenue", "totalincome"}
            ),
            None,
        )
        if not revenue or not any(_numericish((revenue.get("values") or {}).get(period)) for period in periods):
            issues.append("pnl_missing_revenue")
    return issues


def _audit_pnl_formulas(record: dict[str, Any], current: list[str]) -> list[dict[str, Any]]:
    if "pnl" not in current:
        return []
    extraction = _repair_extraction_from_embedded_ocr_tables(record.get("extraction_payload") or {})
    company = extraction.get("company_name") or (record.get("announcement") or {}).get("company_name") or ""
    normalized, *_ = normalize_extraction_units(
        extraction,
        company=company,
        announcement_date=extraction.get("board_meeting_date") or "",
    )
    rows = build_pl_rows(normalize_rows(normalized.get("financial_rows")))
    mapped = _row_map(rows)
    columns = result_display_columns(rows, str(normalized.get("result_period") or ""))
    periods = [str(column.get("period") or "") for column in columns if column.get("kind") == "value"]
    issues: list[dict[str, Any]] = []
    for period in periods:
        issues.extend(_period_formula_issues(company, period, rows, mapped))
    return issues


def _period_formula_issues(
    company: str,
    period: str,
    rows: list[dict[str, Any]],
    mapped: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    revenue = _first_number(_role_number(rows, "revenue", period), _row_number(mapped, "revenue", period), _row_number(mapped, "totalincome", period))
    gross_component_sum, has_gross_components = _role_sum(rows, "gross_component", period)
    if has_gross_components:
        direct_components = gross_component_sum
    else:
        material = _row_number(mapped, "costofmaterialsconsumed", period) or 0.0
        purchases = _row_number(mapped, "purchasesofstockintrade", period) or 0.0
        inventory = (
            _row_number(mapped, "changesininventoriesoffinishedgoodswipandstockintrade", period)
            or _row_number(mapped, "changesininventories", period)
            or 0.0
        )
        direct_components = material + purchases + inventory
    gross_profit = _row_number(mapped, "grossprofit", period)
    total_expenses = _row_number(mapped, "totalexpenses", period)
    employee = _first_number(_role_number(rows, "employee", period), _row_number(mapped, "employeebenefitsexpense", period)) or 0.0
    other_expense = _first_number(_role_number(rows, "operating_expense", period), _row_number(mapped, "otherexpenses", period)) or 0.0
    ebitda = _row_number(mapped, "ebitda", period)
    depreciation = _first_number(_role_number(rows, "depreciation", period), _row_number(mapped, "depreciationandamortisationexpense", period), _row_number(mapped, "depreciation", period)) or 0.0
    finance = _first_number(_role_number(rows, "finance", period), _row_number(mapped, "financecosts", period), _row_number(mapped, "financecost", period)) or 0.0
    pbei = _row_number(mapped, "profitbeforeexceptionalitemsotherincome", period)
    other_income = _first_number(_role_number(rows, "other_income", period), _row_number(mapped, "otherincome", period)) or 0.0
    exceptional = _first_number(_role_number(rows, "exceptional", period), _row_number(mapped, "exceptionalitems", period)) or 0.0
    pbt = _row_number(mapped, "profitbeforetax", period)
    tax = _first_number(_role_number(rows, "tax", period), _row_number(mapped, "totaltaxexpense", period), _row_number(mapped, "totaltaxexpenses", period)) or 0.0
    pat = _row_number(mapped, "pat", period)

    pbei_basis = _row_basis(mapped, "profitbeforeexceptionalitemsotherincome")
    if pbei_basis == "revenue_minus_total_expenses":
        pbei_expected = None if revenue is None or total_expenses is None else revenue - total_expenses
    else:
        pbei_expected = None if ebitda is None else ebitda - depreciation - finance

    checks = [
        ("gross_profit", gross_profit, None if _row_basis(mapped, "grossprofit") == "direct" or revenue is None else revenue - direct_components),
        ("ebitda", ebitda, None if _row_basis(mapped, "ebitda") == "direct" or gross_profit is None else gross_profit - employee - other_expense),
        ("profit_before_exceptional", pbei, None if pbei_basis == "direct" else pbei_expected),
        ("pbt", pbt, None if _row_basis(mapped, "profitbeforetax") == "direct" or pbei is None else pbei + other_income + exceptional),
        ("pat", pat, None if _row_basis(mapped, "pat") == "direct" or pbt is None else pbt - tax),
    ]
    issues: list[dict[str, Any]] = []
    for label, actual, expected in checks:
        if actual is None or expected is None:
            continue
        if abs(actual - expected) > 0.06:
            issues.append(
                {
                    "company": company,
                    "period": period,
                    "formula": label,
                    "actual": actual,
                    "expected": round(expected, 4),
                }
            )
    return issues


def _patched_after_newest_image(image_root: Path) -> str:
    if not image_root.exists():
        return ""
    images = list(image_root.rglob("*.png"))
    if not images:
        return ""
    newest_image = max(path.stat().st_mtime for path in images)
    patched_files = [Path("image_generator.py"), Path("bs_cf_image.py"), Path("segment_image.py"), Path("pl_image.py")]
    newest_patch = max(path.stat().st_mtime for path in patched_files if path.exists())
    if newest_patch > newest_image:
        return f"renderer files are newer than saved PNGs under {image_root}; regenerate images before trusting visual output"
    return ""


def _row_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aliases = {
        "employeebenefitexpense": "employeebenefitsexpense",
        "financecost": "financecosts",
        "taxexpense": "totaltaxexpense",
        "taxexpenses": "totaltaxexpense",
        "profitaftertax": "pat",
        "depreciation": "depreciationandamortisationexpense",
    }
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row_key(row.get("label", ""))
        mapped[key] = row
        if key in aliases:
            mapped[aliases[key]] = row
    return mapped


def _row_number(mapped: dict[str, dict[str, Any]], key: str, period: str) -> float | None:
    row = mapped.get(key)
    if not row:
        return None
    return _number((row.get("values") or {}).get(period))


def _row_basis(mapped: dict[str, dict[str, Any]], key: str) -> str:
    row = mapped.get(key)
    return str((row or {}).get("formula_basis") or "")


def _role_number(rows: list[dict[str, Any]], role: str, period: str) -> float | None:
    for row in rows:
        if str(row.get("formula_role") or "") != role:
            continue
        number = _number((row.get("values") or {}).get(period))
        if number is not None:
            return number
    return None


def _role_sum(rows: list[dict[str, Any]], role: str, period: str) -> tuple[float, bool]:
    total = 0.0
    matched = False
    for row in rows:
        if str(row.get("formula_role") or "") != role:
            continue
        number = _number((row.get("values") or {}).get(period))
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


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "—", "–"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        try:
            return -float(inner)
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _numericish(value: Any) -> bool:
    return _number(value) is not None


def _value_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row_has_value(row))


def _is_named_segment(row: dict[str, Any]) -> bool:
    return bool(re.match(r"^\(\s*[a-z]\s*\)\s+", str(row.get("label") or ""), flags=re.IGNORECASE))


def _has_segment_business_blocks(rows: list[dict[str, Any]]) -> bool:
    """Return whether rendered segment rows contain at least one real segment block."""

    generic_sections = {
        "segment wise",
        "reconciliation",
        "total",
        "total revenue from operations",
        "net revenue from operations",
    }
    for index, row in enumerate(rows):
        if _is_named_segment(row):
            return True
        if str(row.get("type") or "") != "section":
            continue
        label = str(row.get("label") or "").strip()
        if not label or label.lower() in generic_sections:
            continue
        following: list[str] = []
        for next_row in rows[index + 1 :]:
            if str(next_row.get("type") or "") == "section":
                break
            following.append(str(next_row.get("label") or "").strip().lower())
        if "revenue" in following or "segment profit" in following:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
