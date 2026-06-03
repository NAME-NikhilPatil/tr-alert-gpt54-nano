"""Currency-unit detection and value normalization for financial images."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Callable

RS_LAKHS = "Rs in Lakhs"
RS_CR = "Rs in Cr"
RS_MILLIONS = "Rs in Millions"
RS_THOUSANDS = "Rs in Thousands"
USD_MILLIONS = "USD in Millions"


def canonical_currency_unit(unit_text: str) -> str:
    """Normalize a raw model/PDF unit label to one of the supported source units."""

    text = re.sub(r"\s+", " ", str(unit_text or " ")).strip()
    if not text:
        return ""
    if re.search(r"(?i)(?:['‘’]\s*0{3}\b|rs\.?\s*(?:in\s*)?0{3}\b|rupees?\s*(?:in\s*)?0{3}\b|thousands?)", text):
        return RS_THOUSANDS
    if re.search(r"(?i)\b(?:rs\.?|inr|rupees?|₹)?\s*(?:in\s*)?la(?:kh|c)s?\b", text) or re.search(r"(?i)\blakhs?|lacs?\b", text):
        return RS_LAKHS
    if re.search(r"(?i)\b(?:rs\.?|inr|rupees?|₹)?\s*(?:in\s*)?(?:crores?|cr\.?|crs\.?)\b", text):
        return RS_CR
    if re.search(r"(?i)\busd\s*(?:in\s*)?millions?\b", text):
        return USD_MILLIONS
    if re.search(r"(?i)\b(?:inr|rs\.?|rupees?|₹)\s*(?:in\s*)?millions?\b", text):
        return RS_MILLIONS
    if re.search(r"(?i)\bin\s+millions?\b", text):
        return USD_MILLIONS
    return ""


def detect_currency_unit(
    ocr_markdown: str,
    company: str = "",
    announcement_date: str = "",
    alert_callback: Callable[[str], object] | None = None,
) -> str:
    """
    Detect whether figures are in Crores, Lakhs/Lacs, or Millions.

    The search order intentionally follows the user-specified priority. If no
    unit is found, the caller receives an empty string and no default is applied.
    """

    text = re.sub(r"\s+", " ", str(ocr_markdown or " ")).strip()
    if re.search(
        r"(?i)(?:amounts?\s*(?:are\s+)?in\s*(?:rs\.?|inr|rupees?|₹)?\s*['‘’]?\s*0{3}\b|"
        r"(?:rs\.?|inr|rupees?|₹)\s*(?:in\s*)?['‘’]?\s*0{3}\b|"
        r"\b(?:amounts?|figures?|values?)\s*(?:are\s+)?in\s+(?:rs\.?|inr|rupees?|₹)?\s*thousands?\b|"
        r"\b(?:rs\.?|inr|rupees?|₹)\s+in\s+thousands?\b|"
        r"['‘’]\s*000\b)",
        text,
    ):
        return RS_THOUSANDS
    if re.search(r"(?i)\b(amount\s+in\s+inr\s+lacs|rs\.?\s*in\s*lacs?|rs\.?\s*in\s*lakhs?|lakhs?|lacs?)\b", text):
        return RS_LAKHS
    if re.search(r"(?i)\b(amount\s+in\s+crores?|rs\.?\s*in\s*cr\.?|rs\.?\s*in\s*crores?|crores?|crs\.?)\b", text):
        return RS_CR
    if re.search(
        r"(?i)\b(?:all\s+)?amounts?\s+(?:are\s+)?in\s+(?!usd\b)(?:inr|rs\.?|rupees?|[^\s]{1,8})\s*millions?\b",
        text,
    ) or re.search(r"(?i)\b(?:inr|rs\.?|rupees?)\s+(?:in\s+)?millions?\b", text):
        return RS_MILLIONS
    if re.search(r"(?i)\b(amount\s+in\s+millions?|usd\s+(?:in\s+)?millions?|in\s+millions?)\b", text):
        return USD_MILLIONS

    suffix = " ".join(part for part in (company, announcement_date) if part).strip()
    logging.warning("Unit not detected in PDF: %s", suffix or "unknown company/date")
    if alert_callback is not None:
        alert_callback(f"\u26a0\ufe0f Unit not detected for {company or 'company'} -- please verify manually")
    return ""


def display_unit_for_source(source_unit: str) -> str:
    """Return the final display unit after any required conversion."""

    if source_unit == RS_LAKHS:
        return RS_CR
    if source_unit == RS_CR:
        return RS_CR
    if source_unit == RS_MILLIONS:
        return RS_CR
    if source_unit == RS_THOUSANDS:
        return RS_CR
    if source_unit == USD_MILLIONS:
        return USD_MILLIONS
    return ""


def monetary_scale_for_source(source_unit: str) -> float:
    """Return the multiplier required to put source figures in display units."""

    if source_unit == RS_LAKHS:
        return 0.01
    if source_unit == RS_MILLIONS:
        return 0.1
    if source_unit == RS_THOUSANDS:
        return 0.0001
    return 1.0


def normalize_extraction_units(
    extraction: dict[str, Any],
    *,
    company: str = "",
    announcement_date: str = "",
    alert_callback: Callable[[str], object] | None = None,
) -> tuple[dict[str, Any], str, str, list[str]]:
    """
    Return a copy of extraction with monetary values normalized for rendering.

    Existing Mistral table fallback values are already normalized to their final
    display unit and mark that with ``values_display_unit_applied``. Raw or
    external payloads without that flag are converted when the OCR unit is Lakhs.
    """

    normalized = copy.deepcopy(extraction or {})
    ocr_markdown = str(
        normalized.get("ocr_markdown")
        or normalized.get("raw_ocr_markdown")
        or normalized.get("markdown")
        or ""
    )
    source_unit = detect_currency_unit(ocr_markdown, company, announcement_date, alert_callback)
    warnings: list[str] = []
    if not source_unit:
        fallback_unit = str(normalized.get("source_currency_unit") or normalized.get("currency_unit") or "")
        source_unit = canonical_currency_unit(fallback_unit) if not ocr_markdown.strip() else ""
        if not source_unit:
            warnings.append(f"\u26a0\ufe0f Unit not detected for {company or 'company'} -- please verify manually")

    display_unit = display_unit_for_source(source_unit)
    already_applied = bool(normalized.get("values_display_unit_applied") or normalized.get("values_normalized_to_crores"))
    if display_unit:
        normalized["source_currency_unit"] = source_unit
        normalized["currency_unit"] = display_unit
    elif "currency_unit" not in normalized:
        normalized["currency_unit"] = ""

    scale = monetary_scale_for_source(source_unit)
    converted_cell_count = 0
    segment_converted_cell_count = 0
    if source_unit in {RS_LAKHS, RS_MILLIONS, RS_THOUSANDS} and already_applied:
        # Direct GPT-auditor responses include both raw PDF cells and display
        # cells. Trust the raw PDF cells for monetary values and rebuild the
        # display values exactly once; this catches cases where the model
        # converted one row incorrectly while preserving EPS/per-share rows.
        converted_cell_count += _rebuild_display_values_from_raw_values(normalized, scale)
    if source_unit in {RS_LAKHS, RS_MILLIONS, RS_THOUSANDS} and not already_applied:
        converted_cell_count = _scale_extraction_values(normalized, scale)
        normalized["values_display_unit_applied"] = True
        normalized["segment_values_display_unit_applied"] = True
    elif source_unit in {RS_LAKHS, RS_MILLIONS, RS_THOUSANDS} and already_applied and not normalized.get("segment_values_display_unit_applied"):
        # Older OCR-table payloads marked the main values as already converted
        # while leaving segment tables in the original lakh scale.
        segment_converted_cell_count = _scale_segment_values(normalized, scale)
        normalized["segment_values_display_unit_applied"] = True
    normalized["conversion_provenance"] = {
        "source_unit": source_unit,
        "display_unit": display_unit,
        "conversion_factor": scale,
        "values_were_already_display_unit": already_applied,
        "conversion_applied_this_pass": bool(converted_cell_count or segment_converted_cell_count),
        "converted_cell_count": converted_cell_count,
        "segment_converted_cell_count": segment_converted_cell_count,
        "conversion_applied_once": bool(
            display_unit
            and (
                source_unit in {RS_CR, USD_MILLIONS}
                or already_applied
                or converted_cell_count
                or segment_converted_cell_count
            )
        ),
    }
    return normalized, source_unit, display_unit, warnings


def _scale_extraction_values(extraction: dict[str, Any], scale: float) -> int:
    """Scale all monetary row values in-place, skipping EPS and percentage rows."""

    converted = 0
    for row in _all_rows(extraction):
        label = str(row.get("label") or "")
        if _is_non_monetary_row(label):
            continue
        values = row.get("values")
        if isinstance(values, dict):
            raw_values = row.setdefault("raw_values", {})
            if not isinstance(raw_values, dict):
                raw_values = {}
                row["raw_values"] = raw_values
            for period, value in list(values.items()):
                raw_values.setdefault(period, str(value or "").strip())
                scaled = _scale_value(value, scale)
                if scaled != str(value or "").strip():
                    converted += 1
                values[period] = scaled
    return converted


def _scale_segment_values(extraction: dict[str, Any], scale: float) -> int:
    """Scale only segment-table monetary values in-place."""

    converted = 0
    for table in extraction.get("segment_tables") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "")
            if _is_non_monetary_row(label):
                continue
            values = row.get("values")
            if isinstance(values, dict):
                raw_values = row.setdefault("raw_values", {})
                if not isinstance(raw_values, dict):
                    raw_values = {}
                    row["raw_values"] = raw_values
                for period, value in list(values.items()):
                    raw_values.setdefault(period, str(value or "").strip())
                    scaled = _scale_value(value, scale)
                    if scaled != str(value or "").strip():
                        converted += 1
                    values[period] = scaled
    return converted


def _rebuild_display_values_from_raw_values(extraction: dict[str, Any], scale: float) -> int:
    """Rebuild display-unit values from scalar raw PDF cells when available."""

    rebuilt = 0
    for row in _all_rows(extraction):
        label = str(row.get("label") or "")
        if _is_non_monetary_row(label):
            continue
        values = row.get("values")
        raw_values = row.get("raw_values")
        if not isinstance(values, dict) or not isinstance(raw_values, dict):
            continue
        if not raw_values or any(isinstance(value, (dict, list)) for value in raw_values.values()):
            continue
        periods = list(values.keys())
        if not periods:
            continue
        raw_items = list(raw_values.items())
        if len(raw_items) != len(periods):
            continue
        for index, (raw_key, raw_value) in enumerate(raw_items):
            period = str(raw_key) if str(raw_key) in values else periods[index]
            scaled = _scale_value(raw_value, scale)
            old = str(values.get(period) or "").strip()
            if scaled != old:
                values[period] = scaled
                rebuilt += 1
    return rebuilt


def _all_rows(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all row dictionaries in the supported extraction payload."""

    rows: list[dict[str, Any]] = []
    if isinstance(extraction.get("financial_rows"), list):
        rows.extend(row for row in extraction["financial_rows"] if isinstance(row, dict))
    for table in extraction.get("segment_tables") or []:
        if isinstance(table, dict) and isinstance(table.get("rows"), list):
            rows.extend(row for row in table["rows"] if isinstance(row, dict))
    for section in extraction.get("balance_sheet_variables") or []:
        if isinstance(section, dict) and isinstance(section.get("rows"), list):
            rows.extend(row for row in section["rows"] if isinstance(row, dict))
    for key in ("cash_flow_variables", "key_variables"):
        if isinstance(extraction.get(key), list):
            rows.extend(row for row in extraction[key] if isinstance(row, dict))
    return rows


def _is_non_monetary_row(label: str) -> bool:
    """Return whether a label should not be scaled as currency."""

    lowered = label.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return (
        "eps" in lowered
        or "earning per share" in lowered
        or "earnings per share" in lowered
        or "margin" in lowered
        or "%" in lowered
        or compact in {"basic", "diluted", "epsbasic", "epsdiluted", "basiceps", "dilutedeps"}
    )


def _scale_value(value: Any, scale: float) -> str:
    """Scale one display value while preserving blanks and percentages."""

    text = str(value or "").strip()
    if not text or "%" in text:
        return text
    negative_parentheses = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return text
    try:
        number = float(cleaned)
    except ValueError:
        return text
    if negative_parentheses:
        number = -abs(number)
    return _format_number(number * scale)


def _format_number(value: float) -> str:
    """Format a numeric value like the financial screenshots."""

    # Keep calculation precision after unit conversion. The renderers round
    # visible cells separately, so formula checks, margins, and change %
    # calculations can still use the unrounded source-derived value.
    text = f"{value:.4f}"
    return text.rstrip("0").rstrip(".")
