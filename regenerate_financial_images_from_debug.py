"""Regenerate financial PNGs from stored live debug extraction payloads.

Use this after renderer changes to produce fresh images from already-captured
Mistral extraction payloads, without waiting for the live bot to see new
announcements. The default dry-run mode is read-only; pass ``--write`` to save
PNG files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from audit_financial_images import _resolve_debug_log
from image_generator import generate_financial_images


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate financial images from debug payloads.")
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
        "--output-root",
        default="output/regenerated_image_audit",
        help="Output root used when --write is supplied.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually write regenerated PNG files. Without this flag the script is read-only.",
    )
    args = parser.parse_args()

    debug_log = _resolve_debug_log(args.debug_log)
    records = _load_records(debug_log, args.since)
    print(f"Debug log: {debug_log}")
    print(f"Since: {args.since}")
    print(f"Mode: {'write' if args.write else 'dry-run'}")
    print(f"Output root: {args.output_root}")
    print(f"Candidate records: {len(records)}")

    generated = 0
    warnings = 0
    errors = 0
    for record in records:
        extraction = record.get("extraction_payload") or {}
        company = extraction.get("company_name") or (record.get("announcement") or {}).get("company_name") or "Company"
        dry_result = generate_financial_images_dry_run(extraction)
        if args.write:
            if not dry_result["kinds"]:
                result: dict[str, Any] | Any = dry_result
                paths = []
            else:
                try:
                    result = generate_financial_images(extraction, None, args.output_root)
                    paths = [str(image.path) for image in result.images]
                except Exception as exc:
                    result = dry_result
                    paths = []
                    errors += 1
                    summary = {
                        "timestamp": record.get("timestamp"),
                        "company": company,
                        "images": paths,
                        "warnings": dry_result["warnings"],
                        "missing_sections": dry_result["missing_sections"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    _print_json(summary)
                    continue
        else:
            result = dry_result
            paths = [f"<dry-run:{kind}>" for kind in result["kinds"]]

        generated += len(paths)
        warnings += len(result["warnings"] if isinstance(result, dict) else result.warnings)
        summary = {
            "timestamp": record.get("timestamp"),
            "company": company,
            "images": paths,
            "warnings": result["warnings"] if isinstance(result, dict) else result.warnings,
            "missing_sections": result["missing_sections"] if isinstance(result, dict) else result.missing_sections,
        }
        _print_json(summary)

    print(f"Generated image count: {generated}")
    print(f"Warning count: {warnings}")
    print(f"Error count: {errors}")
    return 1 if errors else 0


def generate_financial_images_dry_run(extraction: dict[str, Any]) -> dict[str, Any]:
    """Compute image availability without writing files."""

    from image_generator import (
        _available_render_jobs,
        _announcement_date,
        _dedupe_warnings,
        _period_caption_parts,
        _repair_extraction_from_embedded_ocr_tables,
        _standalone_conflicts_with_consolidated_source,
        _statement_basis,
    )
    from pl_image import safe_filename
    from unit_detector import normalize_extraction_units

    extraction = _repair_extraction_from_embedded_ocr_tables(extraction)
    company = str(extraction.get("company_name") or "Company")
    announcement_date = _announcement_date(extraction, None)
    normalized, _, display_unit, warnings = normalize_extraction_units(
        extraction,
        company=company,
        announcement_date=announcement_date,
    )
    statement_basis = _statement_basis(normalized)
    if _standalone_conflicts_with_consolidated_source(normalized):
        warnings.append(
            f"⚠️ Consolidated section detected but extracted data is standalone for {company}; "
            "financial images skipped for manual verification"
        )
        return {
            "kinds": [],
            "warnings": _dedupe_warnings(warnings),
            "missing_sections": ["P&L Statement", "Balance Sheet + Cash Flow", "Segment Performance"],
        }
    if statement_basis == "standalone":
        warnings.append(f"⚠️ Only standalone data found for {company}")

    quarter_label, fy_label = _period_caption_parts(normalized)
    jobs = _available_render_jobs(
        normalized=normalized,
        announcement=None,
        output_dir=Path("output") / "_dry_run" / safe_filename(company, max_length=56),
        display_unit=display_unit,
        standalone_tag=statement_basis == "standalone",
        company=company,
        quarter_label=quarter_label,
        fy_label=fy_label,
    )
    return {
        "kinds": [str(job["kind"]) for job in jobs if job["available"]],
        "warnings": _dedupe_warnings(warnings),
        "missing_sections": [str(job["section"]) for job in jobs if not job["available"]],
    }


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


def _print_json(value: dict[str, Any]) -> None:
    """Print JSON safely on Windows consoles that are not UTF-8."""

    text = json.dumps(value, ensure_ascii=False)
    print(text.encode("ascii", errors="backslashreplace").decode("ascii"))


if __name__ == "__main__":
    raise SystemExit(main())
