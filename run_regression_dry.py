"""Dry-run local regression PDFs through the active financial extraction pipeline.

This script intentionally does not send Telegram messages. It disables legacy
company patches by default and reports whether any rendered output escaped the
validation gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from financial_pipeline import FinancialPipelineResult, process_financial_pdf


REGRESSION_TARGETS = (
    "Panacea Biotec",
    "Ahlada Engineers",
    "Talwalkars",
    "Titagarh Rail",
    "Fischer Medical",
    "Rajesh Exports",
    "Tenneco Clean Air",
    "Ambica Agarbathies",
    "Vaishali Pharma",
    "Gradiente Infotainment",
    "Kavveri Defence",
)


@dataclass(slots=True)
class DryRunRow:
    company: str
    pdf_file: str
    selected_basis: str
    selected_unit: str
    source_pages_used: str
    pnl_pages: str
    segment_pages: str
    balance_sheet_pages: str
    cash_flow_pages: str
    filing_type: str
    complexity_score: int
    gpt_model: str
    reasoning_effort: str
    reasoning_effort_requested: str
    reasoning_effort_used: str
    xhigh_supported: str
    fallback_reason: str
    model_used: str
    segment_required: str
    balance_sheet_required: str
    cash_flow_required: str
    raw_rows_count: int
    mapped_rows_count: int
    approved_pl_rows_count: int
    approved_segment_rows_count: int
    approved_bs_cf_rows_count: int
    target_pages: str
    ocr_or_vision_fallback_used: str
    validation_status: str
    failed_checks: str
    render_gate: str
    generated_images_count: int
    legacy_company_patch_used: str
    image_paths: str
    failure_report_path: str
    invariant_errors: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Run no-Telegram financial regression dry run.")
    parser.add_argument("--pdf", action="append", default=[], help="Explicit local PDF path. Can be repeated.")
    parser.add_argument("--pdf-root", default="downloads", help="Root folder to search for default regression PDFs.")
    parser.add_argument("--output-root", default="", help="Output root. Defaults to output/regression_dry_<timestamp>.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of PDFs after discovery.")
    parser.add_argument("--mock-apis", action="store_true", help="Use existing pipeline mock mode instead of GPT/API.")
    args = parser.parse_args()

    _load_dotenv(Path(".env"))
    _force_safe_env()
    output_root = Path(args.output_root) if args.output_root else Path("output") / f"regression_dry_{datetime.now():%Y%m%d_%H%M%S}"
    output_root.mkdir(parents=True, exist_ok=True)

    pdfs = _resolve_pdfs(args.pdf, Path(args.pdf_root))
    if args.limit > 0:
        pdfs = pdfs[: args.limit]
    rows: list[DryRunRow] = []

    print(
        "company\tpdf_file\tselected_basis\tselected_unit\tpnl_pages\tsegment_pages\tbalance_sheet_pages\t"
        "cash_flow_pages\tsource_pages_used\tfiling_type\tcomplexity_score\tgpt_model\treasoning_effort\t"
        "reasoning_effort_requested\treasoning_effort_used\txhigh_supported\tfallback_reason\tmodel_used\t"
        "segment_required\tbalance_sheet_required\tcash_flow_required\traw_rows_count\t"
        "mapped_rows_count\tapproved_pl_rows_count\tapproved_segment_rows_count\tapproved_bs_cf_rows_count\t"
        "validation_status\tfailed_checks\trender_gate\tgenerated_images_count\tlegacy_company_patch_used\tfailure_report_path"
    )
    for pdf in pdfs:
        result = process_financial_pdf(
            pdf,
            output_root=output_root,
            mock_apis=args.mock_apis,
            send_telegram=False,
            telegram_sender=None,
        )
        row = _row_from_result(result, output_root)
        rows.append(row)
        print(_tsv(row))

    _write_reports(output_root, rows)
    _print_summary(output_root, rows)
    return 0 if rows else 1


def _force_safe_env() -> None:
    os.environ["LEGACY_COMPANY_PATCH_MODE"] = "false"
    os.environ["LIVE_TELEGRAM_SEND"] = "false"
    os.environ["LLM_VALUES_FIRST_MODE"] = "true"
    os.environ["EXTRACTION_MODE"] = "llm_values_first_mode"
    os.environ["STRICT_VALIDATION"] = "false"
    os.environ["RENDER_WITH_WARNINGS"] = "false"
    os.environ["RENDER_ONLY_WHEN_SAFE"] = "false"
    # Dry runs should fail fast enough for manual feedback loops; production
    # timeout policy can stay in .env for the live bot.
    os.environ["GPT54_TIMEOUT_SECONDS"] = "600"
    os.environ["GPT54_RETRIES"] = "1"
    os.environ.setdefault("PRIMARY_MODEL", os.environ.get("GPT54_MODEL", "gpt-5.4-nano"))
    os.environ.setdefault("MODEL_REASONING_EFFORT", "high")
    os.environ.setdefault("GPT54_DEFAULT_REASONING_EFFORT", "high")
    os.environ.setdefault("GPT54_COMPLEX_REASONING_EFFORT", "xhigh")
    os.environ.setdefault("GPT54_OUTPUT_TOKEN_BUDGET", "128000")
    os.environ.setdefault("GPT54_MAX_OUTPUT_TOKENS", "128000")


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries from .env without printing secrets."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _resolve_pdfs(explicit: list[str], pdf_root: Path) -> list[Path]:
    if explicit:
        return [Path(item) for item in explicit if Path(item).exists()]
    all_pdfs = [path for path in pdf_root.rglob("*.pdf") if path.is_file()]
    selected: list[Path] = []
    seen: set[str] = set()
    for target in REGRESSION_TARGETS:
        match = _best_pdf_match(all_pdfs, target)
        if match and str(match).lower() not in seen:
            selected.append(match)
            seen.add(str(match).lower())
    return selected


def _best_pdf_match(paths: list[Path], target: str) -> Path | None:
    tokens = [token for token in _slug(target).split("_") if token]
    matches: list[Path] = []
    for path in paths:
        haystack = _slug(path.stem)
        if all(token in haystack for token in tokens):
            matches.append(path)
    if not matches:
        return None
    matches.sort(key=lambda path: (path.stat().st_mtime, len(path.name)), reverse=True)
    return matches[0]


def _slug(value: str) -> str:
    output = []
    previous_sep = False
    for char in value.lower():
        if char.isalnum():
            output.append(char)
            previous_sep = False
        elif not previous_sep:
            output.append("_")
            previous_sep = True
    return "".join(output).strip("_")


def _row_from_result(result: FinancialPipelineResult, output_root: Path) -> DryRunRow:
    extraction = result.extraction or {}
    generated = result.generated_images.images if result.generated_images else []
    image_paths = [str(image.path) for image in generated]
    failure_report = _failure_report_path(result, image_paths, output_root)
    invariant_errors = _invariant_errors(result, image_paths, failure_report)
    if result.final_status == "SKIPPED_NON_FINANCIAL_DISCLOSURE":
        validation_status = "SKIPPED_NON_FINANCIAL_DISCLOSURE"
    else:
        validation_status = "PASS" if not invariant_errors and result.final_status == "PASS" else "FAIL"
    failed_checks = list(extraction.get("validation_errors") or [])
    failed_checks.extend(invariant_errors)
    page_breakdown = _page_breakdown(extraction)
    return DryRunRow(
        company=str(extraction.get("company_name") or result.metadata.get("company_name") or ""),
        pdf_file=result.pdf_name,
        selected_basis=str(extraction.get("statement_basis") or ""),
        selected_unit=str(extraction.get("currency_unit") or extraction.get("source_currency_unit") or ""),
        source_pages_used=_source_pages_used(page_breakdown),
        pnl_pages=page_breakdown["pnl_pages"],
        segment_pages=page_breakdown["segment_pages"],
        balance_sheet_pages=page_breakdown["balance_sheet_pages"],
        cash_flow_pages=page_breakdown["cash_flow_pages"],
        filing_type=_filing_type(extraction),
        complexity_score=_complexity_score(extraction),
        gpt_model=_gpt_model(extraction),
        reasoning_effort=_reasoning_effort(extraction),
        reasoning_effort_requested=_routing_field(extraction, "reasoning_effort_requested"),
        reasoning_effort_used=_routing_field(extraction, "reasoning_effort_used"),
        xhigh_supported=_routing_field(extraction, "xhigh_supported"),
        fallback_reason=_routing_field(extraction, "fallback_reason"),
        model_used=_routing_field(extraction, "model_used"),
        segment_required=page_breakdown["segment_required"],
        balance_sheet_required=page_breakdown["balance_sheet_required"],
        cash_flow_required=page_breakdown["cash_flow_required"],
        raw_rows_count=_raw_rows_count(extraction),
        mapped_rows_count=_mapped_rows_count(extraction),
        approved_pl_rows_count=len(extraction.get("approved_pnl_rows") or []),
        approved_segment_rows_count=len(extraction.get("approved_segment_rows") or []),
        approved_bs_cf_rows_count=len(extraction.get("approved_bs_cf_rows") or []),
        target_pages=_target_pages(extraction),
        ocr_or_vision_fallback_used=_fallback_used(extraction),
        validation_status=validation_status,
        failed_checks="; ".join(str(item) for item in failed_checks if str(item).strip()),
        render_gate=str(extraction.get("render_gate") or ""),
        generated_images_count=len(image_paths),
        legacy_company_patch_used=str(bool(extraction.get("legacy_company_patch_mode"))).lower(),
        image_paths=" | ".join(image_paths),
        failure_report_path=str(failure_report),
        invariant_errors="; ".join(invariant_errors),
    )


def _target_pages(extraction: dict[str, Any]) -> str:
    candidates: list[Any] = []
    discovery = extraction.get("discovery_metadata") if isinstance(extraction.get("discovery_metadata"), dict) else {}
    for key in ("selected_pages", "target_pages", "page_numbers_used", "mistral_selected_pages"):
        value = extraction.get(key)
        if value:
            candidates.append(value)
    for key in ("selected_pages", "target_pages", "page_numbers_used"):
        value = discovery.get(key)
        if value:
            candidates.append(value)
    if not candidates:
        return ""
    value = candidates[0]
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _fallback_used(extraction: dict[str, Any]) -> str:
    markers = []
    for key in ("gpt54_fallback_metadata", "gpt54_vision_fallback_metadata", "ocr_fallback_triggered", "vision_fallback_triggered"):
        value = extraction.get(key)
        if value:
            markers.append(key)
    return ",".join(markers) if markers else "false"


def _page_breakdown(extraction: dict[str, Any]) -> dict[str, str]:
    pages = {
        "pnl_pages": set(),
        "segment_pages": set(),
        "balance_sheet_pages": set(),
        "cash_flow_pages": set(),
    }

    def add_page(key: str, row: dict[str, Any]) -> None:
        page = row.get("source_page") or row.get("page_no")
        if page not in (None, ""):
            pages[key].add(str(page))

    for row in extraction.get("financial_rows") or []:
        if isinstance(row, dict):
            add_page("pnl_pages", row)
    for table in extraction.get("segment_tables") or []:
        if isinstance(table, dict):
            for row in table.get("rows") or []:
                if isinstance(row, dict):
                    add_page("segment_pages", row)
    for section in extraction.get("balance_sheet_variables") or []:
        if isinstance(section, dict):
            section_page = section.get("source_page") or section.get("page_no")
            for row in section.get("rows") or []:
                if isinstance(row, dict):
                    if section_page not in (None, ""):
                        row = {**row, "source_page": row.get("source_page") or section_page}
                    add_page("balance_sheet_pages", row)
    for row in extraction.get("cash_flow_variables") or []:
        if isinstance(row, dict):
            add_page("cash_flow_pages", row)
    discovery = extraction.get("discovery_metadata") if isinstance(extraction.get("discovery_metadata"), dict) else {}
    for page in discovery.get("pages") or []:
        if not isinstance(page, dict):
            continue
        table_type = str(page.get("table_type") or "").lower()
        page_no = page.get("page_no")
        if page_no in (None, ""):
            continue
        if "profit" in table_type or "loss" in table_type:
            pages["pnl_pages"].add(str(page_no))
        elif "segment" in table_type:
            pages["segment_pages"].add(str(page_no))
        elif "balance" in table_type or "asset" in table_type:
            pages["balance_sheet_pages"].add(str(page_no))
        elif "cash" in table_type:
            pages["cash_flow_pages"].add(str(page_no))
    return {
        "pnl_pages": ",".join(sorted(pages["pnl_pages"], key=_page_sort_key)),
        "segment_pages": ",".join(sorted(pages["segment_pages"], key=_page_sort_key)),
        "balance_sheet_pages": ",".join(sorted(pages["balance_sheet_pages"], key=_page_sort_key)),
        "cash_flow_pages": ",".join(sorted(pages["cash_flow_pages"], key=_page_sort_key)),
        "segment_required": str(bool(extraction.get("segment_tables"))).lower(),
        "balance_sheet_required": str(bool(extraction.get("balance_sheet_variables"))).lower(),
        "cash_flow_required": str(bool(extraction.get("cash_flow_variables"))).lower(),
    }


def _page_sort_key(value: str) -> tuple[int, str]:
    try:
        return (int(float(value)), value)
    except Exception:
        return (999999, value)


def _source_pages_used(page_breakdown: dict[str, str]) -> str:
    pages: set[str] = set()
    for key in ("pnl_pages", "segment_pages", "balance_sheet_pages", "cash_flow_pages"):
        pages.update(part for part in str(page_breakdown.get(key) or "").split(",") if part)
    return ",".join(sorted(pages, key=_page_sort_key))


def _gpt_model(extraction: dict[str, Any]) -> str:
    metadata = extraction.get("gpt54_execution_metadata") if isinstance(extraction.get("gpt54_execution_metadata"), dict) else {}
    routing = extraction.get("model_routing") if isinstance(extraction.get("model_routing"), dict) else {}
    return str(metadata.get("model") or routing.get("model_used") or routing.get("model_requested") or "")


def _reasoning_effort(extraction: dict[str, Any]) -> str:
    metadata = extraction.get("gpt54_execution_metadata") if isinstance(extraction.get("gpt54_execution_metadata"), dict) else {}
    routing = extraction.get("model_routing") if isinstance(extraction.get("model_routing"), dict) else {}
    return str(metadata.get("reasoning_effort") or routing.get("reasoning_effort_used") or os.environ.get("MODEL_REASONING_EFFORT") or os.environ.get("GPT54_REASONING_EFFORT") or "")


def _filing_type(extraction: dict[str, Any]) -> str:
    classification = extraction.get("filing_classification") if isinstance(extraction.get("filing_classification"), dict) else {}
    return str(classification.get("filing_type") or extraction.get("filing_type") or "")


def _complexity_score(extraction: dict[str, Any]) -> int:
    complexity = extraction.get("financial_complexity") if isinstance(extraction.get("financial_complexity"), dict) else {}
    try:
        return int(complexity.get("complexity_score") or 0)
    except Exception:
        return 0


def _routing_field(extraction: dict[str, Any], field: str) -> str:
    routing = extraction.get("model_routing") if isinstance(extraction.get("model_routing"), dict) else {}
    metadata = extraction.get("gpt54_execution_metadata") if isinstance(extraction.get("gpt54_execution_metadata"), dict) else {}
    return str(routing.get(field) if field in routing else metadata.get(field, ""))


def _raw_rows_count(extraction: dict[str, Any]) -> int:
    return (
        len(extraction.get("financial_rows") or [])
        + sum(len(section.get("rows") or []) for section in extraction.get("balance_sheet_variables") or [] if isinstance(section, dict))
        + len(extraction.get("cash_flow_variables") or [])
        + sum(len(table.get("rows") or []) for table in extraction.get("segment_tables") or [] if isinstance(table, dict))
    )


def _mapped_rows_count(extraction: dict[str, Any]) -> int:
    summary = extraction.get("canonical_semantic_summary") if isinstance(extraction.get("canonical_semantic_summary"), dict) else {}
    if summary:
        return sum(len(values) for values in summary.values() if isinstance(values, list))
    return _raw_rows_count(extraction)


def _failure_report_path(result: FinancialPipelineResult, image_paths: list[str], output_root: Path) -> Path:
    if image_paths:
        folder = Path(image_paths[0]).parent
    else:
        company = _slug(str(result.metadata.get("company_name") or result.pdf_name or "unknown"))
        date = _slug(str(result.metadata.get("announcement_date") or "unknown_date"))
        folder = output_root / company / date
    report = Path(folder) / "VALIDATION_REPORT.json"
    if report.exists():
        return report
    return report


def _invariant_errors(result: FinancialPipelineResult, image_paths: list[str], failure_report: Path) -> list[str]:
    errors: list[str] = []
    extraction = result.extraction or {}
    validation_allows = bool(extraction.get("validation_allows_images"))
    render_gate = str(extraction.get("render_gate") or "").upper()
    if result.final_status == "PASS" and not image_paths:
        errors.append("pass_with_zero_images")
    if not validation_allows and image_paths:
        errors.append("validation_fail_with_images")
    if render_gate == "BLOCK_RENDER" and image_paths:
        errors.append("block_render_with_images")
    if extraction.get("legacy_company_patch_mode"):
        errors.append("legacy_company_patch_used")
    if result.final_status == "FAIL" and not failure_report.exists():
        errors.append("missing_failure_report")
    approved_keys = ("approved_pnl_rows", "approved_bs_cf_rows", "approved_segment_rows")
    if image_paths and not any(extraction.get(key) for key in approved_keys):
        errors.append("images_without_approved_rows")
    return errors


def _write_reports(output_root: Path, rows: list[DryRunRow]) -> None:
    json_path = output_root / "regression_dry_report.json"
    csv_path = output_root / "regression_dry_report.csv"
    json_path.write_text(
        json.dumps([asdict(row) for row in rows], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(DryRunRow.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _print_summary(output_root: Path, rows: list[DryRunRow]) -> None:
    passed = [row for row in rows if row.validation_status == "PASS"]
    skipped = [row for row in rows if row.validation_status == "SKIPPED_NON_FINANCIAL_DISCLOSURE"]
    failed = [row for row in rows if row.validation_status not in {"PASS", "SKIPPED_NON_FINANCIAL_DISCLOSURE"}]
    legacy_used = [row for row in rows if row.legacy_company_patch_used == "true"]
    leaked_images = [
        row for row in rows
        if "validation_fail_with_images" in row.invariant_errors or "block_render_with_images" in row.invariant_errors
    ]
    print("")
    print(f"output_root={output_root}")
    print(f"processed={len(rows)} passed={len(passed)} skipped={len(skipped)} failed={len(failed)}")
    print(f"legacy_company_patch_used={len(legacy_used)}")
    print(f"images_after_validation_failure={len(leaked_images)}")
    print(f"json_report={output_root / 'regression_dry_report.json'}")
    print(f"csv_report={output_root / 'regression_dry_report.csv'}")


def _tsv(row: DryRunRow) -> str:
    values = [
        row.company,
        row.pdf_file,
        row.selected_basis,
        row.selected_unit,
        row.pnl_pages,
        row.segment_pages,
        row.balance_sheet_pages,
        row.cash_flow_pages,
        row.source_pages_used,
        row.filing_type,
        str(row.complexity_score),
        row.gpt_model,
        row.reasoning_effort,
        row.reasoning_effort_requested,
        row.reasoning_effort_used,
        row.xhigh_supported,
        row.fallback_reason,
        row.model_used,
        row.segment_required,
        row.balance_sheet_required,
        row.cash_flow_required,
        str(row.raw_rows_count),
        str(row.mapped_rows_count),
        str(row.approved_pl_rows_count),
        str(row.approved_segment_rows_count),
        str(row.approved_bs_cf_rows_count),
        row.validation_status,
        row.failed_checks,
        row.render_gate,
        str(row.generated_images_count),
        row.legacy_company_patch_used,
        row.failure_report_path,
    ]
    return "\t".join(value.replace("\t", " ").replace("\n", " ") for value in values)


if __name__ == "__main__":
    raise SystemExit(main())
