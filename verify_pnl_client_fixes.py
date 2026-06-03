"""Verify the specific P&L fixes requested from client feedback.

This is a focused regression/audit script for the current live-debug payloads:

1. Balaji-style non-standard expense rows are rendered dynamically.
2. Siemens-style sparse consolidated P&L is fetched/renderable.
3. Repeated identical value-vector artifacts are rejected.
4. Saved generated PNGs are structurally valid.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit_financial_images import _resolve_debug_log
from financial_validation import validate_financial_payload
from image_generator import (
    _announcement_date,
    _has_pnl_image_data,
    _repair_extraction_from_embedded_ocr_tables,
    _standalone_conflicts_with_consolidated_source,
    _statement_basis,
)
from image_validation import image_file_issues
from pl_image import build_pl_rows, has_repeated_value_vector_artifact, normalize_rows, row_key
from unit_detector import normalize_extraction_units

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    fitz = None


@dataclass(slots=True)
class PreparedRecord:
    timestamp: str
    company: str
    result_period: str
    statement_basis: str
    normalized: dict[str, Any]
    pl_rows: list[dict[str, Any]]
    pdf_path: Path | None
    old_selected_pages: list[int]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify client-reported P&L fixes against live debug payloads.")
    parser.add_argument("--debug-log", default="latest", help="Debug JSONL path or 'latest'.")
    parser.add_argument("--since", default="2026-05-26T20:00:00", help="Only inspect records at or after this timestamp.")
    parser.add_argument("--image-root", default="output/regression_dynamic_pnl", help="Generated PNG root to validate.")
    args = parser.parse_args()

    debug_log = _resolve_debug_log(args.debug_log)
    records = [_prepare_record(record) for record in _load_processed_records(debug_log, args.since)]
    records = [record for record in records if record is not None]

    results: list[tuple[str, str]] = []
    try:
        results.append(("balaji_dynamic_rows", _check_balaji_dynamic_rows(records)))
        results.append(("siemens_consolidated_pnl", _check_siemens_consolidated_pnl(records)))
        results.append(("repeated_number_guard", _check_repeated_number_guard(records)))
        results.append(("standalone_consolidated_guard", _check_standalone_consolidated_guard(records)))
        results.append(("generated_png_integrity", _check_generated_png_integrity(Path(args.image_root))))
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"Debug log: {debug_log}")
    print(f"Since: {args.since}")
    print(f"Image root: {args.image_root}")
    for name, detail in results:
        print(f"OK {name}: {detail}")
    return 0


def _load_processed_records(path: Path, since: str) -> list[dict[str, Any]]:
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
        records.append(record)
    return records


def _prepare_record(record: dict[str, Any]) -> PreparedRecord | None:
    extraction = record.get("extraction_payload") or {}
    if not isinstance(extraction, dict):
        return None
    extraction = _repair_extraction_from_embedded_ocr_tables(extraction)
    company = str(extraction.get("company_name") or (record.get("announcement") or {}).get("company_name") or "")
    normalized, _, _, _ = normalize_extraction_units(
        extraction,
        company=company,
        announcement_date=_announcement_date(extraction, None),
    )
    pl_rows = build_pl_rows(normalize_rows(normalized.get("financial_rows")))
    return PreparedRecord(
        timestamp=str(record.get("timestamp") or ""),
        company=company,
        result_period=str(normalized.get("result_period") or ""),
        statement_basis=_statement_basis(normalized),
        normalized=normalized,
        pl_rows=pl_rows,
        pdf_path=_record_pdf_path(record),
        old_selected_pages=_int_list(normalized.get("mistral_selected_pages")),
    )


def _check_balaji_dynamic_rows(records: list[PreparedRecord]) -> str:
    record = _latest_company_record(records, "Balaji Telefilms")
    assert record is not None, "Balaji Telefilms record not found in debug log"
    labels = [str(row.get("label") or "") for row in record.pl_rows]

    assert record.result_period == "Q4 FY26", f"Balaji period was not repaired to Q4 FY26: {record.result_period}"
    assert _has_pnl_image_data(record.pl_rows, record.result_period), "Balaji P&L is not eligible for rendering"
    assert _find_row_containing(record.pl_rows, "cost", "production", "telecast"), (
        "Balaji dynamic row 'Cost of Production / Acquisition and Telecast Fees' not found"
    )
    assert _find_row_containing(record.pl_rows, "marketing", "distribution"), (
        "Balaji dynamic row 'Marketing and Distribution Expense' not found"
    )
    assert not any(row_key(label) == row_key("Cost of materials consumed") for label in labels), (
        "Balaji incorrectly rendered hardcoded Cost of materials consumed"
    )

    revenue = _value(_find_row_exact(record.pl_rows, "Revenue"), "Q4 FY26")
    cost_production = _value(_find_row_containing(record.pl_rows, "cost", "production", "telecast"), "Q4 FY26")
    inventory = _value(_find_row_containing(record.pl_rows, "changes", "inventories"), "Q4 FY26")
    marketing = _value(_find_row_containing(record.pl_rows, "marketing", "distribution"), "Q4 FY26")
    gross_profit = _value(_find_row_exact(record.pl_rows, "Gross Profit"), "Q4 FY26")
    calculated_gross = revenue - cost_production - inventory - marketing
    assert abs(calculated_gross - gross_profit) <= 0.02, (
        f"Balaji gross profit formula mismatch: calculated {calculated_gross:.2f}, rendered {gross_profit:.2f}"
    )
    return (
        "uses source expense rows and Q4 FY26 gross profit formula "
        f"{revenue:g}-{cost_production:g}-({inventory:g})-{marketing:g}={gross_profit:g}"
    )


def _check_siemens_consolidated_pnl(records: list[PreparedRecord]) -> str:
    record = _latest_company_record(records, "Siemens")
    assert record is not None, "Siemens record not found in debug log"
    assert record.statement_basis == "consolidated", f"Siemens basis is not consolidated: {record.statement_basis}"
    assert _has_pnl_image_data(record.pl_rows, record.result_period), "Siemens consolidated P&L is not eligible"

    revenue = _value(_find_row_exact(record.pl_rows, "Revenue"), "Q4 FY26")
    total_expenses = _value(_find_row_exact(record.pl_rows, "Total Expenses"), "Q4 FY26")
    pbe = _value(_find_row_exact(record.pl_rows, "Profit before exceptional items, Other Income"), "Q4 FY26")
    other_income = _value(_find_row_exact(record.pl_rows, "Other Income"), "Q4 FY26")
    pbt = _value(_find_row_exact(record.pl_rows, "Profit Before Tax"), "Q4 FY26")
    tax = _value(_find_row_exact(record.pl_rows, "Total tax expense"), "Q4 FY26")
    pat = _value(_find_row_exact(record.pl_rows, "PAT"), "Q4 FY26")
    assert abs((revenue - total_expenses) - pbe) <= 0.02, "Siemens Revenue - Total Expenses does not match PBE"
    assert abs((pbe + other_income) - pbt) <= 0.02, "Siemens PBE + Other Income does not match PBT"
    assert abs((pbt - tax) - pat) <= 0.02, "Siemens PBT - tax does not match PAT"
    return f"consolidated sparse P&L fetched: revenue {revenue:g}, PBE {pbe:g}, PBT {pbt:g}, PAT {pat:g}"


def _check_repeated_number_guard(records: list[PreparedRecord]) -> str:
    repeated_rows = [
        {"label": "Revenue", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
        {"label": "Cost of materials consumed", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
        {"label": "Employee benefits expense", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
        {"label": "Other expenses", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
        {"label": "Profit Before Tax", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
        {"label": "PAT", "values": {"Q4 FY26": "10", "Q3 FY26": "11", "FY26": "12"}},
    ]
    assert has_repeated_value_vector_artifact(repeated_rows), "Synthetic repeated-number artifact was not detected"
    assert build_pl_rows(repeated_rows) == [], "Synthetic repeated-number artifact still produced P&L rows"

    eligible_repeated: list[str] = []
    for record in records:
        source_rows = normalize_rows(record.normalized.get("financial_rows"))
        if has_repeated_value_vector_artifact(source_rows) and _has_pnl_image_data(record.pl_rows, record.result_period):
            eligible_repeated.append(record.company)
    assert not eligible_repeated, "Repeated-number artifact remained P&L-eligible: " + ", ".join(eligible_repeated)
    return "synthetic artifact rejected and no repeated-vector debug payload remains P&L-eligible"


def _check_standalone_consolidated_guard(records: list[PreparedRecord]) -> str:
    conflicts = [record for record in records if _standalone_conflicts_with_consolidated_source(record.normalized)]
    unresolved = [
        record.company
        for record in conflicts
        if not _current_selector_resolves_long_pdf_conflict(record) and not _current_validation_blocks_conflict(record)
    ]
    assert not unresolved, "Standalone data still conflicts with consolidated source: " + ", ".join(unresolved)
    consolidated_pnl = [
        record.company
        for record in records
        if record.statement_basis == "consolidated" and _has_pnl_image_data(record.pl_rows, record.result_period)
    ]
    assert consolidated_pnl, "No consolidated P&L records were renderable"
    resolved = len(conflicts) - len(unresolved)
    return f"{len(consolidated_pnl)} consolidated P&L records renderable; {resolved} old standalone/consolidated conflict(s) resolved by selector or validation gate"


def _current_validation_blocks_conflict(record: PreparedRecord) -> bool:
    """Return whether current validation prevents unsafe standalone rendering."""

    result = validate_financial_payload(record.normalized)
    return (not result.allows_images) and any(
        "consolidated_available_but_standalone_selected" in issue for issue in result.issues
    )


def _check_generated_png_integrity(image_root: Path) -> str:
    pngs = sorted(image_root.rglob("*.png"))
    assert pngs, f"No PNGs found under {image_root}"
    issues = image_file_issues(image_root)
    assert not issues, "Generated PNG integrity issues found: " + json.dumps(issues[:5], ensure_ascii=True)
    return f"{len(pngs)} PNG files passed structural image validation"


def _latest_company_record(records: list[PreparedRecord], company_fragment: str) -> PreparedRecord | None:
    matches = [record for record in records if company_fragment.lower() in record.company.lower()]
    if not matches:
        return None
    return sorted(matches, key=lambda record: record.timestamp)[-1]


def _current_selector_resolves_long_pdf_conflict(record: PreparedRecord) -> bool:
    """Return true when an old conflict is fixed by current long-PDF page selection."""

    if fitz is None or record.pdf_path is None or not record.pdf_path.exists():
        return False
    max_pages = max(1, len(record.old_selected_pages) or int(os.environ.get("MISTRAL_MAX_PAGES", "30")))
    document = None
    try:
        from mistral_parser import _select_financial_pages

        document = fitz.open(str(record.pdf_path))
        current_selected = [page + 1 for page in _select_financial_pages(document, max_pages=max_pages)]
    except Exception:
        return False
    finally:
        if document is not None:
            document.close()
    old_selected = record.old_selected_pages
    if not old_selected:
        return False
    selected_text = _selected_page_text(record.pdf_path, current_selected)
    has_statement_anchor = "standalone and consolidated financial statements" in selected_text.lower()
    return current_selected != old_selected and has_statement_anchor


def _find_row_exact(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    wanted = row_key(label)
    for row in rows:
        if row_key(str(row.get("label") or "")) == wanted:
            return row
    raise AssertionError(f"Required row not found: {label}")


def _find_row_containing(rows: list[dict[str, Any]], *terms: str) -> dict[str, Any]:
    for row in rows:
        label = str(row.get("label") or "").lower()
        if all(term.lower() in label for term in terms):
            return row
    raise AssertionError(f"Required row containing terms not found: {', '.join(terms)}")


def _value(row: dict[str, Any], period: str) -> float:
    values = row.get("values") or {}
    raw = str(values.get(period) or "").strip()
    assert raw, f"Missing value for {row.get('label')} / {period}"
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", raw)
    assert cleaned not in {"", "-", "."}, f"Non-numeric value for {row.get('label')} / {period}: {raw}"
    value = float(cleaned)
    return -abs(value) if negative else value


def _record_pdf_path(record: dict[str, Any]) -> Path | None:
    announcement = record.get("announcement") or {}
    extraction = record.get("extraction_payload") or {}
    for value in (
        extraction.get("pdf_path"),
        extraction.get("source_pdf_path"),
        announcement.get("pdf_path"),
        record.get("pdf_path"),
    ):
        if value:
            return Path(str(value))
    return None


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    output: list[int] = []
    for item in value:
        try:
            output.append(int(item))
        except Exception:
            continue
    return output


def _selected_page_text(path: Path, one_based_pages: list[int]) -> str:
    if fitz is None or not path.exists():
        return ""
    document = None
    try:
        document = fitz.open(str(path))
        chunks: list[str] = []
        for page_number in one_based_pages:
            index = page_number - 1
            if 0 <= index < int(document.page_count):
                try:
                    chunks.append(document[index].get_text("text") or "")
                except Exception:
                    continue
        return "\n".join(chunks)
    finally:
        if document is not None:
            document.close()


if __name__ == "__main__":
    raise SystemExit(main())
