"""Render Mistral financial extraction output as Excel-style PNG images."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from models import Announcement
from mistral_parser import _available_periods
from mistral_parser import _change_for_row
from mistral_parser import _normalize_rows
from mistral_parser import _normalize_segment_tables
from mistral_parser import _normalize_variable_sections
from mistral_parser import _result_display_columns
from mistral_parser import _row_has_value
from mistral_parser import _sort_main_rows
from mistral_parser import _valid_financial_rows
from mistral_parser import _variable_display_columns

TITLE_BLUE = (218, 235, 247)
HEADER_BLUE = (31, 78, 121)
CURRENT_HEADER = (198, 224, 180)
DARK_GREEN = (31, 78, 18)
LIGHT_GREEN = (198, 224, 180)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRID = (25, 25, 25)
SECTION_GAP = 18

MAJOR_ROWS = {
    "revenue",
    "gross profit",
    "gross profit margin",
    "total expenses",
    "ebitda",
    "ebitda margin",
    "profit before exceptional items, other income",
    "profit before exceptional items and tax",
    "profit before tax",
    "pat",
    "pat margin",
    "eps (basic)",
    "total segment revenue",
    "total",
    "profit before tax",
}


def render_mistral_images(
    extraction: dict[str, Any],
    announcement: Announcement | None = None,
    output_dir: str | Path = Path("output") / "images",
) -> list[Path]:
    """Render all available Mistral tables into one Telegram-ready PNG file."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    company = str(extraction.get("company_name") or (announcement.company_name if announcement else "") or "Company")
    currency_unit = str(extraction.get("currency_unit") or "Rs in Cr")
    result_period = str(extraction.get("result_period") or "")
    stem = _safe_filename(company)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    section_images: list[Path] = []
    financial_rows = _sort_main_rows(_valid_financial_rows(extraction.get("financial_rows")))
    financial_rows = _clean_display_rows(financial_rows)
    if any(_row_has_value(row) for row in financial_rows):
        columns = _result_display_columns(financial_rows, result_period)
        section_images.append(
            _render_table(
                title=company,
                unit="Particulars",
                rows=financial_rows,
                columns=columns,
                path=output_path / f"{stem}_part_result_summary_{timestamp}.png",
                skip_margin_changes=True,
            )
        )

    for index, table in enumerate(_normalize_segment_tables(extraction.get("segment_tables")), start=1):
        rows = _clean_display_rows(_normalize_rows(table.get("rows")))
        if not any(_row_has_value(row) for row in rows):
            continue
        columns = _result_display_columns(rows, result_period)
        title = str(table.get("title") or f"{company} - Segment Wise")
        if "segment" in title.lower() and company.lower() not in title.lower():
            title = f"{company} - {title}"
        section_images.append(
            _render_table(
                title=title,
                unit=_segment_first_header(rows),
                rows=rows,
                columns=columns,
                path=output_path / f"{stem}_part_segment_{index}_{timestamp}.png",
            )
        )

    variable_rows: list[dict[str, Any]] = []
    for section in _normalize_variable_sections(extraction.get("balance_sheet_variables")):
        variable_rows.append({"label": str(section.get("section") or "Variables"), "type": "section", "values": {}, "changes": {}})
        variable_rows.extend(_normalize_rows(section.get("rows")))
    cash_flow_rows = _normalize_rows(extraction.get("cash_flow_variables"))
    if cash_flow_rows:
        variable_rows.append({"label": "Cash Flow Variables", "type": "section", "values": {}, "changes": {}})
        variable_rows.extend(cash_flow_rows)
    key_rows = _normalize_rows(extraction.get("key_variables"))
    if key_rows:
        variable_rows.append({"label": "Key Variables", "type": "section", "values": {}, "changes": {}})
        variable_rows.extend(key_rows)
    variable_rows = _clean_display_rows(variable_rows)
    if any(_row_has_value(row) for row in variable_rows):
        columns = _variable_display_columns(variable_rows)
        unit = "Balance Sheet Variables" if _available_periods(variable_rows) else "Key Variables"
        section_images.append(
            _render_table(
                title=f"{company} - Key Changes in Variables",
                unit=unit,
                rows=variable_rows,
                columns=columns,
                path=output_path / f"{stem}_part_key_variables_{timestamp}.png",
                variable_style=True,
            )
        )

    if not section_images:
        return []
    combined_path = output_path / f"{stem}_financial_output_{timestamp}.png"
    combined = _combine_section_images(section_images, combined_path)
    for section_path in section_images:
        if section_path != combined:
            try:
                section_path.unlink()
            except OSError:
                pass
    return [combined]


def _render_table(
    *,
    title: str,
    unit: str,
    rows: list[dict[str, Any]],
    columns: list[dict[str, str]],
    path: Path,
    skip_margin_changes: bool = False,
    variable_style: bool = False,
) -> Path:
    """Render one styled table to a PNG file."""

    columns = [column for column in columns if _column_has_any_value(rows, column)]
    if not columns:
        columns = [{"kind": "value", "label": period, "period": period} for period in _available_periods(rows)]
    first_col = min(max(max(_text_width_hint(str(row.get("label", ""))) for row in rows) + 34, 500), 760)
    col_widths = [_column_width(column) for column in columns]
    table_width = first_col + sum(col_widths)
    title_h = 92
    header_h = 42
    row_h = 36
    pad = 0
    height = title_h + header_h + row_h * len(rows) + 2
    width = table_width + pad * 2

    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    fonts = _fonts()

    draw.rectangle([0, 0, width, title_h], fill=TITLE_BLUE)
    _draw_centered(draw, title, (0, 0, width, title_h), fonts["title"], BLACK)

    y = title_h
    draw.rectangle([0, y, first_col, y + header_h], fill=HEADER_BLUE, outline=GRID, width=2)
    draw.text((8, y + header_h / 2), unit, font=fonts["header"], fill=WHITE, anchor="lm")
    x = first_col
    for index, column in enumerate(columns):
        col_w = col_widths[index]
        fill = CURRENT_HEADER if index == 0 and column["kind"] == "value" else HEADER_BLUE
        text_fill = BLACK if fill == CURRENT_HEADER else WHITE
        draw.rectangle([x, y, x + col_w, y + header_h], fill=fill, outline=GRID, width=2)
        _draw_right(draw, column["label"], x, y, col_w, header_h, fonts["header"], text_fill)
        x += col_w

    y += header_h
    for row in rows:
        label = str(row.get("label") or "")
        is_section = row.get("type") == "section" and not _row_has_value(row)
        is_major = _row_key(label) in {_row_key(item) for item in MAJOR_ROWS}
        label_fill = DARK_GREEN if is_section or is_major else LIGHT_GREEN
        label_text = WHITE if label_fill == DARK_GREEN else BLACK
        value_fill = LIGHT_GREEN if is_major and not _is_margin_row(label) and not variable_style else WHITE
        value_font = fonts["bold"] if is_major or _is_margin_row(label) else fonts["cell"]

        draw.rectangle([0, y, first_col, y + row_h], fill=label_fill, outline=GRID, width=2)
        draw.text((8, y + row_h / 2), _ellipsize(label, first_col - 16, fonts["bold"]), font=fonts["bold"], fill=label_text, anchor="lm")
        x = first_col
        for index, column in enumerate(columns):
            col_w = col_widths[index]
            draw.rectangle([x, y, x + col_w, y + row_h], fill=value_fill, outline=GRID, width=2)
            value = _cell_text(row, column, skip_margin_changes)
            _draw_right(draw, value, x, y, col_w, row_h, value_font, BLACK)
            x += col_w
        y += row_h

    image.save(path)
    return path


def _segment_first_header(rows: list[dict[str, Any]]) -> str:
    """Return a non-currency first-column header for segment tables."""

    for row in rows:
        label = str(row.get("label") or "").strip()
        if row.get("type") == "section" and label:
            return _ellipsize_plain(label, 32)
    return "Segment Wise"


def _clean_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove rows whose first-column labels are OCR-shifted values."""

    cleaned: list[dict[str, Any]] = []
    for row in rows:
        label = str(row.get("label") or "").strip()
        is_section = row.get("type") == "section" and not _row_has_value(row)
        if is_section:
            cleaned.append(row)
            continue
        if not _row_has_value(row):
            continue
        if _is_bad_display_label(label):
            continue
        cleaned.append(row)
    return _drop_empty_sections(cleaned)


def _drop_empty_sections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep section rows only when they have data rows under them."""

    kept: list[dict[str, Any]] = []
    pending_section: dict[str, Any] | None = None
    for row in rows:
        is_section = row.get("type") == "section" and not _row_has_value(row)
        if is_section:
            pending_section = row
            continue
        if pending_section is not None:
            kept.append(pending_section)
            pending_section = None
        kept.append(row)
    return kept


def _is_bad_display_label(label: str) -> bool:
    """Return whether a row label is not meaningful enough to display."""

    text = label.strip()
    if not text:
        return True
    if _label_is_mostly_numeric(text):
        return True
    if _is_roman_only_label(text):
        return True
    return not _has_meaningful_label_text(text)


def _label_is_mostly_numeric(label: str) -> bool:
    """Return whether a label is likely a misplaced numeric table value."""

    compact = re.sub(r"[\s,().₹$%-]", "", label)
    if not compact:
        return True
    digit_count = sum(ch.isdigit() for ch in compact)
    alpha_count = sum(ch.isalpha() for ch in compact)
    return digit_count > 0 and alpha_count == 0


def _is_roman_only_label(label: str) -> bool:
    """Return whether the label is only a roman numeral marker."""

    compact = re.sub(r"[^A-Za-z]", "", label).upper()
    return bool(compact) and bool(re.fullmatch(r"[IVXLCDM]+", compact))


def _has_meaningful_label_text(label: str) -> bool:
    """Return whether a row label contains actual text, not only markers."""

    compact = re.sub(r"[^A-Za-z]", "", label)
    return len(compact) >= 2


def _combine_section_images(section_paths: list[Path], path: Path) -> Path:
    """Stack rendered table sections into one image for a single Telegram alert."""

    if len(section_paths) == 1:
        section_paths[0].replace(path)
        return path

    opened: list[Image.Image] = []
    try:
        for section_path in section_paths:
            opened.append(Image.open(section_path).convert("RGB"))

        width = max(image.width for image in opened)
        height = sum(image.height for image in opened) + SECTION_GAP * (len(opened) - 1)
        combined = Image.new("RGB", (width, height), WHITE)

        y = 0
        for image in opened:
            combined.paste(image, (0, y))
            y += image.height + SECTION_GAP

        combined.save(path)
        return path
    finally:
        for image in opened:
            image.close()


def _cell_text(row: dict[str, Any], column: dict[str, str], skip_margin_changes: bool) -> str:
    """Return display text for one table cell."""

    values = row.get("values", {}) if isinstance(row.get("values"), dict) else {}
    label = str(row.get("label") or "")
    if column["kind"] == "value":
        return str(values.get(column["period"], ""))
    if skip_margin_changes and "margin" in label.lower():
        return ""
    return _change_for_row(row, column["from"], column["to"])


def _column_has_any_value(rows: list[dict[str, Any]], column: dict[str, str]) -> bool:
    """Return whether a dynamic column has any visible data."""

    for row in rows:
        if row.get("type") == "section" and not _row_has_value(row):
            continue
        if _cell_text(row, column, skip_margin_changes=False):
            return True
    return False


def _column_width(column: dict[str, str]) -> int:
    """Return column width matching the wide screenshot style."""

    return 178 if column["kind"] == "change" else 145


def _fonts() -> dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    """Load Windows fonts with portable fallbacks."""

    return {
        "title": _font(["C:/Windows/Fonts/cambriab.ttf", "C:/Windows/Fonts/georgiab.ttf", "C:/Windows/Fonts/arialbd.ttf"], 34),
        "header": _font(["C:/Windows/Fonts/arialbd.ttf"], 24),
        "bold": _font(["C:/Windows/Fonts/arialbd.ttf"], 23),
        "cell": _font(["C:/Windows/Fonts/arial.ttf"], 24),
    }


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return the first available TrueType font."""

    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw centered text in a rectangle."""

    left, top, right, bottom = box
    draw.text(((left + right) / 2, (top + bottom) / 2), text, font=font, fill=fill, anchor="mm")


def _draw_right(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    width: int,
    height: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw right-aligned text in a cell."""

    draw.text((x + width - 8, y + height / 2), str(text), font=font, fill=fill, anchor="rm")


def _ellipsize(text: str, max_width: int, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> str:
    """Trim long labels to fit the first column."""

    if _measure(text, font) <= max_width:
        return text
    suffix = "..."
    while text and _measure(text + suffix, font) > max_width:
        text = text[:-1]
    return text + suffix if text else ""


def _ellipsize_plain(text: str, max_chars: int) -> str:
    """Trim text by character count before a font is available."""

    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."


def _measure(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    """Measure text width."""

    try:
        return int(font.getlength(text))
    except Exception:
        return len(text) * 12


def _text_width_hint(text: str) -> int:
    """Estimate text width before fonts are loaded."""

    return len(text) * 13


def _row_key(label: str) -> str:
    """Normalize a row label for style matching."""

    return re.sub(r"[^a-z0-9]", "", label.lower())


def _is_margin_row(label: str) -> bool:
    """Return whether a row is a margin row."""

    return "margin" in label.lower()


def _safe_filename(value: str) -> str:
    """Return a filesystem-safe stem."""

    stem = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return stem[:80] or "mistral_output"
