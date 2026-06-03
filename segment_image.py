"""Segment-wise financial image renderer."""

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
    calc_margin,
    company_name,
    data_row,
    normalize_rows,
    quarter_fy_from_columns,
    render_table_png,
    result_display_columns,
    row_has_value,
    row_key,
    rows_to_table,
    safe_filename,
    section_row,
    source_name,
    footer_basis_label,
    title_with_basis,
)


def render_segment_image(
    extraction: dict[str, Any],
    announcement: Announcement | None,
    output_dir: Path,
    unit_label: str,
    *,
    standalone_tag: bool = False,
    approved_rows: list[dict[str, Any]] | None = None,
    approved_columns: list[dict[str, str]] | None = None,
) -> Path:
    """Render already-approved segment performance rows."""

    assert_renderer_input_approved(extraction)
    output_dir.mkdir(parents=True, exist_ok=True)
    company = company_name(extraction, announcement)
    source = source_name(extraction, announcement)
    extracted_at = datetime.now()
    rows = approved_display_rows(approved_rows if approved_rows is not None else extraction.get("approved_segment_rows"))
    if not rows:
        raise RenderBlockedError("approved Segment rows missing")
    value_rows = [row for row in rows if row_has_value(row)]
    columns = approved_display_columns(
        approved_columns if approved_columns is not None else extraction.get("approved_segment_columns")
    )
    quarter, fy = quarter_fy_from_columns(columns, str(extraction.get("result_period") or ""))
    path = output_dir / f"{safe_filename(company, max_length=56)}_{quarter}_{fy}_Segments.png"
    footer_left = f"Data Source: {source} | {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}"
    footer_right = unit_label or ""
    title = title_with_basis(company, extraction, standalone_tag)
    footer_basis = footer_basis_label(extraction, standalone_tag)
    if footer_basis:
        footer_right = f"{footer_right} | {footer_basis}".strip(" |")
    if standalone_tag:
        footer_right = f"{footer_right} | ONLY STANDALONE FOUND".strip(" |")

    if not value_rows or not columns:
        raise ValueError("Segment data not available in this PDF")

    headers = ["Segment Wise"] + [column["label"] for column in columns]
    table_rows = rows_to_table(rows, columns)
    row_styles = [str(row.get("style") or "normal") for row in rows]
    return render_table_png(
        title=f"{title} - Segment Performance",
        headers=headers,
        rows=table_rows,
        row_styles=row_styles,
        columns=columns,
        path=path,
        footer_left=footer_left,
        footer_right=footer_right,
        unit_note=unit_label,
        first_col_fraction=0.30,
        title_size=18,
        cell_size=11,
        header_size=11,
    )


def build_segment_rows(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    """Build dynamic segment rows from every extracted segment table."""

    output: list[dict[str, Any]] = []
    tables: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    has_named_segments = False
    for table in extraction.get("segment_tables") or []:
        if not isinstance(table, dict):
            continue
        raw_rows = normalize_rows(table.get("rows"))
        rows = [row for row in raw_rows if row_has_value(row) or row.get("type") == "section"]
        if not any(row_has_value(row) for row in rows):
            continue
        tables.append((table, rows))
        has_named_segments = has_named_segments or _has_named_segment_rows(rows)

    structured_rows = _build_segment_metric_blocks(tables)
    if structured_rows:
        return structured_rows

    if not has_named_segments:
        return output

    for index, (table, rows) in enumerate(tables, start=1):
        if len(tables) > 1 and not _has_named_segment_rows(rows):
            continue
        title = str(table.get("title") or f"Segment {index}").strip() or f"Segment {index}"
        output.append(section_row(title) | {"style": "segment_blue" if index % 2 else "segment_green"})
        for row in rows:
            label = _clean_segment_label(str(row.get("label") or ""))
            if row.get("type") == "section" and not row_has_value(row):
                output.append(section_row(label) | {"style": "segment_green" if index % 2 else "segment_blue"})
            else:
                output.append(data_row(label, row.get("values") or {}, _segment_row_style(label)))
    return output


def _build_segment_metric_blocks(tables: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Build one block per business segment when rows carry metric-specific labels."""

    metrics_by_segment: dict[str, dict[str, dict[str, str]]] = {}
    reconciliation: list[dict[str, Any]] = []
    metric_names = {"Revenue", "Segment Profit", "Segment Assets", "Segment Liabilities"}
    for _table, rows in tables:
        for row in rows:
            label = str(row.get("label") or "").strip()
            values = row.get("values") or {}
            parsed = _parse_segment_metric_label(label, metric_names)
            if parsed:
                segment, metric = parsed
                metrics_by_segment.setdefault(segment, {})[metric] = values
            elif row_has_value(row):
                reconciliation.append(data_row(_clean_segment_label(label), values, _segment_row_style(label)))

    if not metrics_by_segment:
        return []

    output: list[dict[str, Any]] = []
    for index, (segment, metrics) in enumerate(metrics_by_segment.items(), start=1):
        output.append(section_row(segment) | {"style": "segment_blue" if index % 2 else "segment_green"})
        revenue = metrics.get("Revenue", {})
        profit = metrics.get("Segment Profit", {})
        if revenue:
            output.append(data_row("Revenue", revenue, "important"))
        if profit:
            output.append(data_row("Segment Profit", profit, "important"))
        margin = calc_margin(list(set(revenue) | set(profit)), profit, revenue, {})
        if margin:
            output.append(data_row("Segment Profit Margin %", margin, "margin"))
        for label in ("Segment Assets", "Segment Liabilities"):
            if metrics.get(label):
                output.append(data_row(label, metrics[label], "important"))

    if reconciliation:
        output.append(section_row("Reconciliation") | {"style": "segment_green"})
        output.extend(reconciliation)
    return output


def _parse_segment_metric_label(label: str, metric_names: set[str]) -> tuple[str, str] | None:
    """Parse labels like ``Cables - Revenue`` into segment and metric."""

    cleaned = _clean_segment_label(label)
    cleaned_lower = cleaned.lower()
    metric_aliases = {
        "Segment Revenue": "Revenue",
        "Segment Results": "Segment Profit",
        "Segment Result": "Segment Profit",
        "Profit (+)/ loss (-) before tax": "Segment Profit",
        "Profit/(loss) before tax": "Segment Profit",
        "Profit before tax": "Segment Profit",
        "Segment Profit/(Loss)": "Segment Profit",
    }
    for source_metric, target_metric in metric_aliases.items():
        prefix = f"{source_metric} - "
        suffix = f" - {source_metric}"
        prefix_lower = prefix.lower()
        suffix_lower = suffix.lower()
        if cleaned_lower.startswith(prefix_lower):
            segment = cleaned[len(prefix) :].strip()
            if segment:
                return segment, target_metric
        if cleaned_lower.endswith(suffix_lower):
            segment = cleaned[: -len(suffix)].strip()
            if segment:
                return segment, target_metric
    for metric in metric_names:
        suffix = f" - {metric}"
        prefix = f"{metric} - "
        suffix_lower = suffix.lower()
        prefix_lower = prefix.lower()
        if cleaned_lower.endswith(suffix_lower):
            segment = cleaned[: -len(suffix)].strip()
            if segment:
                return segment, metric
        if cleaned_lower.startswith(prefix_lower):
            segment = cleaned[len(prefix) :].strip()
            if segment:
                return segment, metric
    return None


def _has_named_segment_rows(rows: list[dict[str, Any]]) -> bool:
    """Return true when the table contains real named business segments."""

    count = 0
    for row in rows:
        label = str(row.get("label") or "").strip()
        if not re.match(r"^\(\s*[a-z]\s*\)\s+", label, flags=re.IGNORECASE):
            continue
        key = row_key(label)
        if "unallocated" in key or "corporate" in key:
            continue
        count += 1
    return count >= 1


def _segment_row_style(label: str) -> str:
    """Return row style for segment metrics."""

    key = row_key(label)
    important = [
        "revenue",
        "total",
        "ebit",
        "profit",
        "result",
        "margin",
    ]
    return "important" if any(item in key for item in important) else "normal"


def _clean_segment_label(label: str) -> str:
    """Shorten verbose OCR segment labels so table text remains readable."""

    cleaned = " ".join(str(label or "").split())
    replacements = {
        "Other Unallocable Income/(Net of Unallocable Expenditure)": "Other unallocable income/(expenditure)",
        "Profit/(Loss) before Share in Profit/(Loss) in Associates/ Joint Venture and Tax": "PBT before share in associates/JV and tax",
        "Add: Share in Profit/(Loss) in Associates/ Joint Venture": "Add: share in associates/JV",
        "Engineering, Procurement & Construction (EPC)": "Engineering, Procurement & Construction (EPC)",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    return cleaned
