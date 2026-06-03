"""PDF text extraction and financial result parsing."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from models import FinancialData
from utils import compact_text, normalize_date

FINANCIAL_ROWS = [
    "Income",
    "Revenue",
    "Revenue from operations",
    "Other Income",
    "Total Income",
    "Expenses",
    "Cost of materials consumed",
    "Purchases / Consumables",
    "Change in inventory",
    "Gross Profit",
    "Gross Profit Margin",
    "Employee Benefit Expense",
    "Other expenses",
    "Total Expenses",
    "EBITDA",
    "EBITDA Margin",
    "Depreciation",
    "Finance Cost",
    "Profit before Exceptional Items, Other Income",
    "Profit before tax and exceptional items",
    "Exceptional items (Discontinued Operations)",
    "Profit Before Tax",
    "Current Tax",
    "Deferred Tax",
    "Tax Expenses",
    "PAT",
    "Profit for the period/year",
    "PAT Margin",
    "Other comprehensive income",
    "Total comprehensive income",
    "Paid up equity share capital",
    "Other Equity",
    "EPS (Basic)",
    "EPS (Diluted)",
]

MAX_PDF_PAGES = 30
MAX_SCREENSHOT_PAGES = 12
MAX_CAMELOT_PAGES = 4
CAMELOT_PAGE_SCAN_LIMIT = 12
MAX_PDFPLUMBER_TABLE_PAGES = 6
MAX_PYMUPDF_TABLE_PAGES = 6

PERIOD_PATTERNS = [
    r"Q[1-4]\s*FY\d{2,4}",
    r"Q[1-4]\s*F\.?Y\.?\s*\d{2,4}",
    r"FY\d{2,4}",
    r"Year ended\s+\d{1,2}[-/ .][A-Za-z0-9]{2,9}[-/ .]\d{2,4}",
    r"Quarter ended\s+\d{1,2}[-/ .][A-Za-z0-9]{2,9}[-/ .]\d{2,4}",
]

FIELD_ALIASES = {
    "Revenue": [
        "revenue",
        "net revenue",
        "turnover",
        "sales",
        "net sales",
        "income from operations",
        "revenue from operations",
        "total revenue",
    ],
    "Revenue from operations": ["revenue from operations", "sales income from operations"],
    "Other Income": ["other income", "other operating income"],
    "Total Income": ["total income", "income total", "total revenue and other income"],
    "Total Expenses": ["total expenses", "total expenditure", "expenses total"],
    "Expenses": ["expenses", "expenditure"],
    "Cost of materials consumed": ["raw material consumed", "cost of material consumed", "materials consumed"],
    "Purchases / Consumables": ["purchases", "purchases of stock in trade", "consumables"],
    "Change in inventory": ["changes in inventories", "increase decrease in stock", "change in stock"],
    "Employee Benefit Expense": ["employee benefit expense", "employee cost", "employees cost"],
    "Other expenses": ["other expenses", "other expenditure"],
    "Depreciation": ["depreciation", "depreciation and amortisation", "depreciation and amortization"],
    "Finance Cost": ["finance cost", "finance costs", "interest expense"],
    "Profit Before Tax": ["profit before tax", "pbt", "profit loss before tax"],
    "PAT": ["profit after tax", "net profit", "profit for the period", "profit for the year", "pat"],
    "EPS (Basic)": ["eps", "basic eps", "earnings per share", "basic earnings per share"],
    "EPS (Diluted)": ["diluted eps", "diluted earnings per share"],
    "Dividend": ["dividend per share", "dps", "dividend/share", "div. per share"],
}

NUMBER_RE = re.compile(
    r"(?P<negative_paren>\()? (?P<sign>-)? (?P<number>(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?) "
    r"\s*(?P<unit>cr\.?|crores?|lakhs?|lacs?|million|rs\.?|inr|%)? (?P<negative_paren_close>\))?",
    re.I | re.X,
)

DATE_RE = re.compile(
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?[-/ .](?P<month>\d{1,2}|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[-/ .,]+(?P<year>\d{2,4})|"
    r"(?P<month_name>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(?P<day_name>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year_name>\d{2,4})",
    re.I,
)


def parse_pdf(pdf_path: Path | str | None) -> FinancialData:
    """Extract financial fields from a PDF using multiple text/table/OCR strategies."""

    data = FinancialData()
    if not pdf_path:
        data.parser_status = "missing_pdf"
        data.parser_message = "PDF was not downloaded."
        return data

    path = Path(pdf_path)
    extraction = _extract_pdf_waterfall(path, data)
    text = extraction["text"]
    tables = extraction["tables"]
    if not compact_text(text):
        data.parser_status = "scanned_or_empty"
        data.parser_message = "No extractable text found after pdfplumber, PyMuPDF, pdfminer, Camelot, and OCR."
        logging.warning("No extractable text found in %s", path)
        return data

    text = _preprocess_text(text, data)

    _parse_financial_text_into_data(text, data, tables)
    if _rule_confidence(data) < 90:
        _try_llm_assisted_extraction(text, data)
    if _extraction_is_suspect(data):
        data.rows = {}
        if not data.screenshots and _looks_financial_page(text):
            screenshots = _render_page_screenshots(path, reason="no_table_data")
            data.screenshots = [str(item) for item in screenshots]
        data.parser_status = "no_financial_data"
        data.parser_message = "Only incidental or suspect financial-looking numbers were found."
        return data
    if not _has_numeric_financial_data(data):
        if not data.screenshots and _looks_financial_page(text):
            screenshots = _render_page_screenshots(path, reason="no_table_data")
            data.screenshots = [str(item) for item in screenshots]
        data.parser_status = "no_financial_data"
        data.parser_message = "No numeric financial result table data found."
        return data
    data.currency_unit = "Rs in Cr"
    return data


def parse_financial_text(
    text: str,
    *,
    parser_status: str = "parsed_text",
    parser_message: str = "",
    screenshots: list[str] | None = None,
) -> FinancialData:
    """Parse financial result data from already-extracted text or OCR text."""

    data = FinancialData(
        parser_status=parser_status,
        parser_message=parser_message,
        screenshots=screenshots or [],
    )
    _parse_financial_text_into_data(text, data, [])
    if not _has_numeric_financial_data(data):
        data.parser_status = "no_financial_data"
        data.parser_message = parser_message or "No numeric financial result table data found."
    else:
        data.currency_unit = "Rs in Cr"
    return data


def parse_financial_screenshots(image_paths: list[Path | str]) -> FinancialData:
    """OCR page screenshots and parse financial result data from them."""

    screenshots = [Path(path) for path in image_paths]
    ocr_text = _extract_with_ocr(screenshots)
    if not ocr_text:
        return FinancialData(
            parser_status="ocr_unavailable_or_empty",
            parser_message="OCR returned no text. Confirm tesseract.exe is installed and on PATH.",
            screenshots=[str(path) for path in screenshots],
        )
    return parse_financial_text(
        ocr_text,
        parser_status="parsed_ocr_screenshots",
        parser_message="Parsed from OCR screenshots.",
        screenshots=[str(path) for path in screenshots],
    )


def _extract_pdf_waterfall(path: Path, data: FinancialData) -> dict[str, object]:
    """Extract a PDF through a deterministic parser waterfall."""

    tables: list[list[list[str]]] = []
    last_error = ""
    layer_extractors = [
        ("pdfplumber", _extract_with_pdfplumber),
        ("pymupdf", _extract_with_pymupdf),
        ("pdfminer", _extract_with_pdfminer),
    ]
    for layer_name, extractor in layer_extractors:
        try:
            text, layer_tables = extractor(path)
            text_len = len(compact_text(text))
            tables.extend(layer_tables)
            data.parser_layers.append(f"{layer_name}:chars={text_len}:tables={len(layer_tables)}")
            logging.info("PDF layer %s for %s extracted %s chars and %s tables.", layer_name, path, text_len, len(layer_tables))
            if text_len >= 50:
                camelot_tables: list[list[list[str]]] = []
                if len(layer_tables) < 2 or text_len < 2000 or os.environ.get("TR_ALERT_FORCE_CAMELOT") == "1":
                    camelot_tables = _extract_tables_with_camelot(path, data)
                else:
                    data.parser_layers.append("camelot:skipped_existing_tables")
                tables.extend(camelot_tables)
                data.extraction_layer = layer_name
                data.parser_status = f"parsed_{layer_name}"
                return {"text": text, "tables": _dedupe_tables(tables)}
            last_error = f"{layer_name} extracted only {text_len} chars"
        except Exception as exc:
            last_error = f"{layer_name}: {exc}"
            data.parser_layers.append(f"{layer_name}:error={exc}")
            logging.warning("%s failed for %s: %s", layer_name, path, exc)

    camelot_tables = _extract_tables_with_camelot(path, data)
    tables.extend(camelot_tables)
    screenshots = _render_page_screenshots(path, reason="ocr")
    data.screenshots = [str(item) for item in screenshots]
    ocr_text = _extract_with_ocr_pdf2image(path, data) or _extract_with_ocr(screenshots)
    if ocr_text:
        data.parser_layers.append(f"ocr:chars={len(compact_text(ocr_text))}")
        data.extraction_layer = "ocr"
        data.parser_status = "parsed_ocr"
        data.parser_message = "OCR was used as the final extraction layer."
        return {"text": ocr_text, "tables": _dedupe_tables(tables)}

    data.parser_layers.append(f"ocr:empty; last_error={last_error}")
    data.parser_status = "unreadable"
    data.parser_message = last_error or "All parser layers failed."
    return {"text": "", "tables": _dedupe_tables(tables)}


def _preprocess_text(text: str, data: FinancialData) -> str:
    """Clean extracted text before financial extraction."""

    pages = [page for page in text.split("\f") if compact_text(page)]
    if len(pages) <= 1:
        data.document_type = "single_page"
        working = text
    else:
        data.document_type = "multi_page"
        data.preprocessing_flags.append(f"pages={len(pages)}")
        working = "\f".join(_remove_repeated_headers_footers(pages))
    working = re.sub(r"(?m)^\s*(?:page\s*)?\d+\s*(?:of\s+\d+)?\s*$", " ", working, flags=re.I)
    working = _fix_common_ocr_text_errors(working)
    working = _add_sliding_page_context(working)
    working = re.sub(r"[ \t]+", " ", working)
    working = re.sub(r"\n{3,}", "\n\n", working)
    data.language = _detect_language(working)
    if data.language and data.language != "en":
        data.preprocessing_flags.append(f"language={data.language}")
    return working


def _remove_repeated_headers_footers(pages: list[str]) -> list[str]:
    """Remove lines repeated on many pages, usually headers or footers."""

    line_counts: dict[str, int] = {}
    page_lines: list[list[str]] = []
    for page in pages:
        lines = [compact_text(line) for line in page.splitlines() if compact_text(line)]
        page_lines.append(lines)
        for line in set(lines[:5] + lines[-5:]):
            if 5 <= len(line) <= 140:
                line_counts[line] = line_counts.get(line, 0) + 1
    repeated = {line for line, count in line_counts.items() if count >= max(2, len(pages) // 2)}
    cleaned_pages = ["\n".join(line for line in lines if line not in repeated) for lines in page_lines]
    return cleaned_pages


def _fix_common_ocr_text_errors(text: str) -> str:
    """Fix common OCR/text-layer errors without touching normal prose aggressively."""

    repaired = text.replace("\u00a0", " ")
    repaired = re.sub(r"(?<=\d)\s+(?=\d{2,3}(?:\.\d+)?\b)", "", repaired)
    repaired = re.sub(r"\bCRORES?\b", "Crores", repaired, flags=re.I)
    repaired = re.sub(r"\b(?:Rs|INR)\s*\.?\s*in\s*Cr\.?\b", "Rs in Cr", repaired, flags=re.I)
    repaired = re.sub(r"\bTota[Il1]\b", "Total", repaired)
    repaired = re.sub(r"\b0ther\b", "Other", repaired)
    repaired = re.sub(r"\brn(?=[a-z])", "m", repaired)
    return repaired


def _add_sliding_page_context(text: str) -> str:
    """Append adjacent page windows so page-break-spanning fields can be scanned."""

    pages = [page for page in text.split("\f") if compact_text(page)]
    if len(pages) < 2:
        return text
    windows = [f"{pages[idx]}\n{pages[idx + 1]}" for idx in range(len(pages) - 1)]
    return text + "\n\n" + "\n\n".join(windows)


def _detect_language(text: str) -> str:
    """Detect language when optional language detection is available."""

    sample = compact_text(text)[:3000]
    if not sample:
        return ""
    try:
        from langdetect import detect

        return detect(sample)
    except Exception:
        letters = re.findall(r"[A-Za-z]", sample)
        return "en" if len(letters) >= max(25, len(sample) // 8) else "unknown"


def _rule_confidence(data: FinancialData) -> int:
    """Calculate an internal parser confidence for LLM gating."""

    values_found = sum(len(values) for values in data.rows.values())
    if data.parser_status in {"scanned_or_empty", "unreadable", "parse_timeout", "parse_error"}:
        return 0
    score = 30 if data.parser_status.startswith("parsed") else 0
    score += min(len(data.periods) * 6, 25)
    score += min(len(data.rows) * 3, 25)
    score += min(values_found, 20)
    return max(0, min(score, 100))


def _try_llm_assisted_extraction(text: str, data: FinancialData) -> None:
    """Use Anthropic for low-confidence PDFs when credentials and SDK are available."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        data.llm_status = "skipped_no_api_key"
        return
    try:
        import anthropic
    except Exception as exc:
        data.llm_status = f"skipped_sdk_unavailable:{exc}"
        return
    prompt = {
        "instruction": (
            "Extract Indian listed-company financial result fields from the provided exchange PDF text. "
            "Return only JSON with keys periods, rows, meeting_date, dividend, dividend_per_share. "
            "Rows must map metric names to period/value objects. Leave uncertain values blank."
        ),
        "target_metrics": FINANCIAL_ROWS,
        "text": compact_text(text)[:45000],
    }
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4000,
            temperature=0,
            messages=[{"role": "user", "content": json.dumps(prompt)}],
        )
        content = "".join(getattr(block, "text", "") for block in response.content)
        payload = json.loads(content)
    except Exception as exc:
        data.llm_status = f"failed:{exc}"
        logging.warning("LLM-assisted extraction failed: %s", exc)
        return
    if not isinstance(payload, dict):
        data.llm_status = "failed_non_object_json"
        return
    rows = payload.get("rows", {})
    if isinstance(rows, dict):
        for metric, period_values in rows.items():
            label = _match_financial_row(str(metric))
            if not label or not isinstance(period_values, dict):
                continue
            for period, value in period_values.items():
                normalized = _normalize_financial_value(str(value), data.currency_unit, label)
                if normalized:
                    _set_financial_value(data, label, str(period), normalized, 0.7)
    periods = payload.get("periods", [])
    if isinstance(periods, list):
        for period in periods:
            period_text = str(period)
            if period_text and period_text not in data.periods:
                data.periods.append(period_text)
    if payload.get("meeting_date"):
        data.meeting_date = normalize_date(str(payload["meeting_date"]))
    if payload.get("dividend"):
        data.dividend = str(payload["dividend"])
    if payload.get("dividend_per_share"):
        data.dividend_per_share = str(payload["dividend_per_share"])
    data.llm_status = "used"


def _parse_financial_text_into_data(
    text: str,
    data: FinancialData,
    tables: list[list[list[str]]],
) -> None:
    """Populate a FinancialData object from extracted text and optional tables."""

    data.text_excerpt = compact_text(text)[:1500]
    _parse_currency_unit(text, data)
    _parse_meeting_dates_and_times(text, data)
    _parse_dividend(text, data)
    _parse_financial_tables(tables, data)
    _parse_financial_lines(text, data)


def _extract_with_pdfplumber(path: Path) -> tuple[str, list[list[list[str]]]]:
    """Extract text and tables using pdfplumber."""

    import pdfplumber

    text_parts: list[str] = []
    tables: list[list[list[str]]] = []
    table_pages_seen = 0
    with pdfplumber.open(path) as pdf:
        if getattr(pdf, "is_encrypted", False):
            raise ValueError("PDF is password protected or encrypted.")
        for page in pdf.pages[:MAX_PDF_PAGES]:
            page_parts: list[str] = []
            page_text = page.extract_text() or ""
            if page_text:
                page_parts.append(page_text)
            layout_text = page.extract_text(layout=True, x_tolerance=1, y_tolerance=3) or ""
            if layout_text and layout_text != page_text:
                page_parts.append(layout_text)
            if page_parts:
                text_parts.append("\n".join(page_parts))
            if not _looks_financial_page(page_text) or table_pages_seen >= MAX_PDFPLUMBER_TABLE_PAGES:
                continue
            table_pages_seen += 1
            default_tables = page.extract_tables() or []
            for table in default_tables:
                cleaned_table = _clean_table(table)
                if cleaned_table:
                    tables.append(cleaned_table)
            if default_tables or len(page_text) > 5000:
                continue
            for settings in ({"vertical_strategy": "text", "horizontal_strategy": "text"},):
                extracted = page.extract_tables(table_settings=settings)
                for table in extracted or []:
                    cleaned_table = _clean_table(table)
                    if cleaned_table:
                        tables.append(cleaned_table)
    return "\f".join(text_parts), _dedupe_tables(tables)


def _extract_with_pymupdf(path: Path) -> tuple[str, list[list[list[str]]]]:
    """Extract text and tables using PyMuPDF as a fallback parser."""

    import fitz

    text_parts: list[str] = []
    tables: list[list[list[str]]] = []
    document = fitz.open(path)
    if document.needs_pass:
        raise ValueError("PDF is password protected or encrypted.")
    table_pages_seen = 0
    for page_number in range(min(document.page_count, MAX_PDF_PAGES)):
        page = document[page_number]
        page_text = page.get_text("text")
        page_parts = [page_text]
        block_text = _pymupdf_blocks_to_layout_text(page)
        if block_text and block_text != page_text:
            page_parts.append(block_text)
        text_parts.append("\n".join(page_parts))
        if not _looks_financial_page(page_text) or table_pages_seen >= MAX_PYMUPDF_TABLE_PAGES:
            continue
        table_pages_seen += 1
        try:
            found_tables = page.find_tables()
            for table in found_tables.tables:
                cleaned_table = _clean_table(table.extract())
                if cleaned_table:
                    tables.append(cleaned_table)
        except Exception:
            logging.debug("PyMuPDF table extraction failed for %s page %s", path, page_number + 1)
    document.close()
    return "\f".join(text_parts), _dedupe_tables(tables)


def _extract_with_pdfminer(path: Path) -> tuple[str, list[list[list[str]]]]:
    """Extract deep text streams using pdfminer.six."""

    from pdfminer.high_level import extract_text

    text = extract_text(str(path), maxpages=MAX_PDF_PAGES) or ""
    return text, []


def _extract_tables_with_camelot(path: Path, data: FinancialData) -> list[list[list[str]]]:
    """Extract tables using Camelot lattice and stream modes when available."""

    try:
        import camelot
    except Exception as exc:
        data.parser_layers.append(f"camelot:unavailable={exc}")
        return []

    tables: list[list[list[str]]] = []
    pages = _camelot_page_spec(path, data)
    for flavor in ("lattice", "stream"):
        try:
            found = camelot.read_pdf(str(path), pages=pages, flavor=flavor, suppress_stdout=True)
            data.parser_layers.append(f"camelot_{flavor}:tables={len(found)}")
            for table in found:
                raw = table.df.fillna("").astype(str).values.tolist()
                cleaned = _clean_table(raw)
                if cleaned:
                    tables.append(cleaned)
        except Exception as exc:
            data.parser_layers.append(f"camelot_{flavor}:error={exc}")
            logging.info("Camelot %s extraction skipped/failed for %s: %s", flavor, path, exc)
    return _dedupe_tables(tables)


def _camelot_page_spec(path: Path, data: FinancialData) -> str:
    """Return a bounded Camelot page spec biased toward financial-result pages."""

    financial_pages: list[int] = []
    page_count = 0
    try:
        import fitz

        document = fitz.open(path)
        page_count = document.page_count
        for page_number in range(min(page_count, CAMELOT_PAGE_SCAN_LIMIT)):
            page_text = document[page_number].get_text("text")
            if _looks_financial_page(page_text):
                financial_pages.append(page_number + 1)
            if len(financial_pages) >= MAX_CAMELOT_PAGES:
                break
        document.close()
    except Exception as exc:
        data.parser_layers.append(f"camelot_pages:scan_error={exc}")

    if not financial_pages:
        fallback_count = min(MAX_CAMELOT_PAGES, page_count or MAX_CAMELOT_PAGES)
        financial_pages = list(range(1, fallback_count + 1))
    pages = ",".join(str(page) for page in financial_pages[:MAX_CAMELOT_PAGES])
    data.parser_layers.append(f"camelot_pages:{pages}")
    return pages


def _extract_with_ocr_pdf2image(path: Path, data: FinancialData) -> str:
    """OCR a PDF through pdf2image when available."""

    try:
        from pdf2image import convert_from_path
    except Exception as exc:
        data.parser_layers.append(f"pdf2image:unavailable={exc}")
        return ""

    output_dir = Path("screenshots") / path.stem / "pdf2image_ocr"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        images = convert_from_path(str(path), first_page=1, last_page=MAX_SCREENSHOT_PAGES, dpi=250)
    except Exception as exc:
        data.parser_layers.append(f"pdf2image:error={exc}")
        logging.info("pdf2image failed for %s: %s", path, exc)
        return ""

    image_paths: list[Path] = []
    for idx, image in enumerate(images, start=1):
        target = output_dir / f"page_{idx:03d}.png"
        image.save(target)
        image_paths.append(target)
    data.screenshots = [str(item) for item in image_paths]
    text = _extract_with_ocr(image_paths)
    data.parser_layers.append(f"pdf2image_ocr:chars={len(compact_text(text))}")
    return text


def _pymupdf_blocks_to_layout_text(page: object) -> str:
    """Build line-oriented text from PyMuPDF word coordinates."""

    try:
        words = page.get_text("words")
    except Exception:
        return ""
    if not words:
        return ""
    lines: list[list[tuple[float, str]]] = []
    for word in words:
        x0, y0, _, _, text = word[:5]
        placed = False
        for line in lines:
            if abs(line[0][0] - y0) <= 3:
                line.append((x0, text))
                placed = True
                break
        if not placed:
            lines.append([(y0, ""), (x0, text)])
    rendered: list[str] = []
    for line in lines:
        entries = [(x, text) for x, text in line if text]
        entries.sort(key=lambda item: item[0])
        rendered.append(" ".join(text for _, text in entries))
    return "\n".join(rendered)


def _clean_table(table: list[list[object]]) -> list[list[str]]:
    """Normalize a raw extracted table to a rectangular text matrix."""

    cleaned_table = [
        [compact_text(str(cell or "")) for cell in row]
        for row in table
        if any(compact_text(str(cell or "")) for cell in row)
    ]
    if not cleaned_table:
        return []
    width = max(len(row) for row in cleaned_table)
    return [row + [""] * (width - len(row)) for row in cleaned_table]


def _dedupe_tables(tables: list[list[list[str]]]) -> list[list[list[str]]]:
    """Remove duplicate table extractions produced by alternate strategies."""

    seen: set[str] = set()
    deduped: list[list[list[str]]] = []
    for table in tables:
        signature = "\n".join("|".join(row) for row in table)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(table)
    return deduped


def _render_page_screenshots(path: Path, reason: str) -> list[Path]:
    """Render the first relevant PDF pages to PNG screenshots for OCR/review."""

    output_dir = Path("screenshots") / path.stem / reason
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = _render_screenshots_with_pymupdf(path, output_dir)
    if rendered:
        return rendered
    return _render_screenshots_with_pdfium(path, output_dir)


def _render_screenshots_with_pymupdf(path: Path, output_dir: Path) -> list[Path]:
    """Render PDF screenshots using PyMuPDF."""

    try:
        import fitz
    except Exception:
        logging.warning("PyMuPDF is unavailable; cannot render screenshots for %s", path)
        return []

    rendered: list[Path] = []
    try:
        document = fitz.open(path)
        if document.needs_pass:
            raise ValueError("PDF is password protected or encrypted.")
        page_limit = min(document.page_count, MAX_SCREENSHOT_PAGES)
        for page_number in range(page_limit):
            page = document[page_number]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
            target = output_dir / f"page_{page_number + 1:03d}.png"
            pixmap.save(target)
            rendered.append(target)
        document.close()
    except Exception:
        logging.exception("Could not render screenshots for %s", path)
    return rendered


def _render_screenshots_with_pdfium(path: Path, output_dir: Path) -> list[Path]:
    """Render PDF screenshots using pypdfium2 as a second renderer."""

    try:
        import pypdfium2 as pdfium
    except Exception:
        logging.warning("pypdfium2 is unavailable; cannot render screenshots for %s", path)
        return []

    rendered: list[Path] = []
    try:
        document = pdfium.PdfDocument(str(path))
        page_limit = min(len(document), MAX_SCREENSHOT_PAGES)
        for page_number in range(page_limit):
            page = document[page_number]
            bitmap = page.render(scale=4)
            pil_image = bitmap.to_pil()
            target = output_dir / f"page_{page_number + 1:03d}.png"
            pil_image.save(target)
            rendered.append(target)
            page.close()
        document.close()
    except Exception:
        logging.exception("Could not render screenshots with pypdfium2 for %s", path)
    return rendered


def _extract_with_ocr(screenshots: list[Path]) -> str:
    """Extract text from rendered screenshots when pytesseract and Tesseract exist."""

    if not screenshots:
        return ""
    try:
        import pytesseract
    except Exception:
        logging.warning("pytesseract is not installed; OCR skipped.")
        return ""
    tesseract_cmd = _find_tesseract_cmd()
    if not tesseract_cmd:
        logging.warning("tesseract.exe is not installed/on PATH; OCR skipped.")
        return ""
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    text_parts: list[str] = []
    for screenshot in screenshots:
        try:
            page_text = ""
            for candidate in _ocr_image_candidates(screenshot):
                page_text = pytesseract.image_to_string(str(candidate), config="--oem 3 --psm 6")
                if _looks_financial_page(page_text) and len(re.findall(r"\d", page_text)) >= 10:
                    break
            text_parts.append(page_text)
        except Exception as exc:
            logging.warning("OCR failed for %s: %s", screenshot, exc)
            return ""
    return "\n".join(text_parts)


def _find_tesseract_cmd() -> str:
    """Find tesseract.exe from PATH or common Windows install locations."""

    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Users\sharm\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _ocr_image_candidates(screenshot: Path) -> list[Path]:
    """Create high-contrast OCR candidate images for tiny/scanned financial tables."""

    candidates = [screenshot]
    try:
        import cv2

        image = cv2.imread(str(screenshot), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return candidates
        scaled = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        denoised = cv2.fastNlMeansDenoising(scaled, None, 10, 7, 21)
        threshold = cv2.adaptiveThreshold(
            denoised,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            35,
            11,
        )
        target = screenshot.with_name(f"{screenshot.stem}_ocr.png")
        cv2.imwrite(str(target), threshold)
        candidates.insert(0, target)
    except Exception:
        logging.debug("OCR preprocessing failed for %s", screenshot)
    return candidates


def _looks_financial_page(text: str) -> bool:
    """Return whether a page likely contains financial result tables."""

    lowered = (text or "").lower()
    if any(row.lower() in lowered for row in FINANCIAL_ROWS):
        return True
    return bool(re.search(_financial_page_regex(), lowered, re.I))


def _financial_page_regex() -> str:
    """Return a broad regex for pages that likely contain financial statements."""

    return (
        r"q[1-4]\s*fy\d{2,4}|quarter ended|year ended|revenue|turnover|"
        r"total income|ebitda|profit before tax|profit after tax|net profit|"
        r"earnings per share|eps|statement of audited|financial results"
    )


def _parse_currency_unit(text: str, data: FinancialData) -> None:
    """Find the currency unit declared in a financial result table."""

    match = re.search(r"(Rs\.?|INR|Rupees)\s*(?:in)?\s*(Cr|Crore|Lakhs?|Millions?)", text, re.I)
    if match:
        unit = match.group(2).lower()
        data.currency_unit = "Rs in Cr" if unit.startswith(("cr", "crore")) else match.group(0)


def _parse_meeting_dates_and_times(text: str, data: FinancialData) -> None:
    """Extract board meeting date and start/end times when visible."""

    date_match = re.search(
        r"meeting\s+(?:held\s+)?(?:on\s+)?(?:today\s+i\.e\.\s*)?([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        text,
        re.I,
    )
    if date_match:
        data.meeting_date = normalize_date(date_match.group(1))

    start_match = re.search(r"(?:commenced|started)\s+at\s+([0-9:. ]+\s*(?:a\.?m\.?|p\.?m\.?|hrs|IST)?)", text, re.I)
    end_match = re.search(r"(?:concluded|ended)\s+at\s+([0-9:. ]+\s*(?:a\.?m\.?|p\.?m\.?|hrs|IST)?)", text, re.I)
    if start_match:
        data.board_meeting_start_time = compact_text(start_match.group(1))
    if end_match:
        data.board_meeting_end_time = compact_text(end_match.group(1))


def _parse_dividend(text: str, data: FinancialData) -> None:
    """Extract dividend text from the PDF when a dividend is mentioned."""

    match = re.search(
        r"(?P<dividend>(?:final|interim|special)?\s*dividend[^.\n;]*(?:Rs\.?|INR|₹)?\s*(?P<amount>[0-9]+(?:\.[0-9]+)?)[^.\n;]*)",
        text,
        re.I,
    )
    if match:
        data.dividend = compact_text(match.group("dividend"))
        data.dividend_declared = "Yes"
        data.dividend_per_share = match.group("amount")
    elif re.search(r"\bno\s+dividend\b|\bnot\s+recommend(?:ed)?\s+(?:any\s+)?dividend\b", text, re.I):
        data.dividend_declared = "No"


def _set_financial_value(data: FinancialData, label: str, period: str, value: str, confidence: float) -> None:
    """Set one extracted financial value and its confidence score."""

    data.rows.setdefault(label, {})[period] = value
    data.field_confidence.setdefault(label, {})[period] = max(
        confidence,
        data.field_confidence.get(label, {}).get(period, 0.0),
    )


def _parse_financial_tables(tables: list[list[list[str]]], data: FinancialData) -> None:
    """Parse financial rows from extracted PDF tables."""

    for table in tables:
        if len(table) < 2:
            continue
        header_idx = _find_period_header(table)
        if header_idx is None:
            _parse_adjacent_table_cells(table, data)
            continue
        headers = _build_period_headers(table, header_idx)
        periods = [header for header in headers[1:] if header]
        if not periods:
            continue
        for period in periods:
            if period not in data.periods:
                data.periods.append(period)
        for row in table[header_idx + 1 :]:
            if not row:
                continue
            label, confidence = _match_financial_row_with_confidence(" ".join(row[:4]))
            if not label:
                continue
            for idx, period in enumerate(headers[1:], start=1):
                if period and idx < len(row):
                    value = _normalize_financial_value(row[idx], data.currency_unit, label)
                    if value:
                        _set_financial_value(data, label, period, value, confidence)


def _parse_adjacent_table_cells(table: list[list[str]], data: FinancialData) -> None:
    """Parse simple key/value tables using adjacent right/below cells."""

    for row_idx, row in enumerate(table):
        for col_idx, cell in enumerate(row):
            label, confidence = _match_financial_row_with_confidence(cell)
            if not label:
                continue
            candidates: list[str] = []
            if col_idx + 1 < len(row):
                candidates.append(row[col_idx + 1])
            if row_idx + 1 < len(table) and col_idx < len(table[row_idx + 1]):
                candidates.append(table[row_idx + 1][col_idx])
            for candidate in candidates:
                value = _normalize_financial_value(candidate, data.currency_unit, label)
                if value and re.search(r"\d", value):
                    period = data.periods[0] if data.periods else "Extracted"
                    if period not in data.periods:
                        data.periods.append(period)
                    _set_financial_value(data, label, period, value, min(confidence, 0.7))
                    break


def _find_period_header(table: list[list[str]]) -> int | None:
    """Return the first table row that appears to contain period headers."""

    for idx, row in enumerate(table[:10]):
        joined = " ".join(row)
        period_cell_count = sum(1 for cell in row if _looks_like_period_cell(cell))
        if period_cell_count >= 2:
            return idx
        if re.search(r"quarter\s+ended|year\s+ended|three\s+months\s+ended|half\s+year\s+ended", joined, re.I):
            return idx
        if sum(1 for pattern in PERIOD_PATTERNS if re.search(pattern, joined, re.I)) >= 1:
            return idx
    return None


def _build_period_headers(table: list[list[str]], header_idx: int) -> list[str]:
    """Build period headers, including multi-line table headers and date rows."""

    width = max(len(row) for row in table)
    headers: list[str] = []
    for col_idx in range(width):
        parts: list[str] = []
        for row_idx in range(header_idx, min(header_idx + 4, len(table))):
            row = table[row_idx]
            cell = row[col_idx] if col_idx < len(row) else ""
            cell = _normalize_period(cell)
            if cell and cell.lower() not in {"particulars", "sr no", "s no", "no"}:
                parts.append(cell)
        joined = compact_text(" ".join(parts))
        headers.append(_normalize_period(joined))
    if headers and not headers[0]:
        headers[0] = "Particulars"
    return headers


def _looks_like_period_cell(value: str) -> bool:
    """Return whether a table cell looks like a period/date header."""

    cleaned = compact_text(value)
    if not cleaned:
        return False
    if any(re.search(pattern, cleaned, re.I) for pattern in PERIOD_PATTERNS):
        return True
    return bool(re.search(r"\b\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b", cleaned))


def _normalize_period(value: str) -> str:
    """Normalize a period header from PDF table text."""

    cleaned = compact_text(value).replace("F.Y.", "FY").replace("F Y", "FY")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _match_financial_row(value: str) -> str | None:
    """Map a PDF table row label to a configured financial output row."""

    label, _ = _match_financial_row_with_confidence(value)
    return label


def _match_financial_row_with_confidence(value: str) -> tuple[str | None, float]:
    """Map a PDF row label to an output row with match confidence."""

    cleaned = compact_text(value).lower()
    cleaned = re.sub(r"[^a-z0-9 ]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _repair_ocr_label_text(cleaned)
    for label in sorted(FINANCIAL_ROWS, key=len, reverse=True):
        comparable = re.sub(r"[^a-z0-9 ]", " ", label.lower())
        comparable = re.sub(r"\s+", " ", comparable).strip()
        if comparable and comparable in cleaned:
            return label, 1.0
    aliases = {
        "profit after tax": "PAT",
        "profit for the year": "PAT",
        "profit for the quarter": "PAT",
        "net profit": "PAT",
        "profit for the period": "PAT",
        "profit for the period year": "Profit for the period/year",
        "pat": "PAT",
        "basic eps": "EPS (Basic)",
        "earnings per share basic": "EPS (Basic)",
        "basic earnings per share": "EPS (Basic)",
        "diluted eps": "EPS (Diluted)",
        "diluted earnings per share": "EPS (Diluted)",
        "revenue from operations": "Revenue",
        "income revenue from operations": "Revenue from operations",
        "sale of products": "Revenue",
        "net sales": "Revenue",
        "net sales income": "Revenue",
        "sales income from operations": "Revenue",
        "income from operations net": "Revenue",
        "income from operations": "Revenue",
        "other income": "Other Income",
        "total income": "Revenue",
        "fotal income": "Total Income",
        "iotal income": "Total Income",
        "tolal income": "Total Income",
        "tnlal income": "Total Income",
        "total income from operations": "Total Income",
        "income from operations incl": "Total Income",
        "expenses purchase": "Purchases / Consumables",
        "purchase of medical consumables": "Purchases / Consumables",
        "purchases of medical consumables": "Purchases / Consumables",
        "employee benefits expense": "Employee Benefit Expense",
        "employees cost": "Employee Benefit Expense",
        "employee cost": "Employee Benefit Expense",
        "finance costs": "Finance Cost",
        "depreciation and amortisation": "Depreciation",
        "profit before tax and exceptional items": "Profit before tax and exceptional items",
        "profit before tax": "Profit Before Tax",
        "current tax": "Current Tax",
        "deferred tax": "Deferred Tax",
        "tax expense": "Tax Expenses",
        "total tax": "Tax Expenses",
        "total expenses": "Total Expenses",
        "total expenditure": "Total Expenses",
        "expenditure": "Total Expenses",
        "other expenses": "Other expenses",
        "cost of materials consumed": "Cost of materials consumed",
        "purchases of stock in trade": "Purchases / Consumables",
        "changes in inventories": "Change in inventory",
        "other comprehensive income": "Other comprehensive income",
        "total comprehensive income": "Total comprehensive income",
        "paid up equity share capital": "Paid up equity share capital",
        "other equity": "Other Equity",
    }
    for alias, label in aliases.items():
        if alias in cleaned:
            return label, 1.0
    fuzzy_label, fuzzy_score = _fuzzy_field_match(cleaned)
    if fuzzy_label:
        logging.info("Fuzzy field match: %r -> %s (%.1f)", value[:120], fuzzy_label, fuzzy_score)
        return fuzzy_label, 0.7
    return None, 0.0


def _fuzzy_field_match(cleaned: str) -> tuple[str | None, float]:
    """Match field aliases with rapidfuzz when installed, otherwise difflib."""

    if not cleaned or len(cleaned) < 3:
        return None, 0.0
    best_label = None
    best_score = 0.0
    try:
        from rapidfuzz import fuzz

        scorer = lambda a, b: float(fuzz.partial_ratio(a, b))
    except Exception:
        from difflib import SequenceMatcher

        scorer = lambda a, b: SequenceMatcher(None, a, b).ratio() * 100
    for label, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            comparable = re.sub(r"[^a-z0-9 ]", " ", alias.lower())
            comparable = re.sub(r"\s+", " ", comparable).strip()
            score = scorer(cleaned, comparable)
            if score > best_score:
                best_label = label
                best_score = score
    return (best_label, best_score) if best_label and best_score >= 85 else (None, best_score)


def _repair_ocr_label_text(cleaned: str) -> str:
    """Repair common OCR damage in financial row labels."""

    replacements = {
        "f rom": "from",
        "othl r": "other",
        "oth1 r": "other",
        "othlr": "other",
        "olher": "other",
        "tnlal": "total",
        "fotal": "total",
        "iotal": "total",
        "tolal": "total",
        "tolr i": "total",
        "tolr": "total",
        "tola i": "total",
        "otal lncome": "total income",
        "otal income": "total income",
        "otalexpenses": "total expenses",
        "totalexpenses": "total expenses",
        "lrom": "from",
        "opr rallons": "operations",
        "oprrallons": "operations",
        "operallons": "operations",
        "operatlons": "operations",
        "oper.tiont": "operations",
        "nrt": "net",
        "des": "sales",
        "sdes": "sales",
        "l1tr1111w": "income",
        "l1tr": "income",
        "lncome": "income",
        "lncome": "income",
        "ltncome": "income",
        "ex11enses": "expenses",
        "exnense": "expense",
        "exnenses": "expenses",
        "exf enses": "expenses",
        "exf,enses": "expenses",
        "oepreciation": "depreciation",
        "amortisation": "amortization",
        "finance costs": "finance cost",
        "profit before exceptional ltems": "profit before exceptional items",
        "profit before exceptional items nd tax": "profit before exceptional items and tax",
        "net profit loss": "net profit",
    }
    repaired = cleaned
    for bad, good in replacements.items():
        repaired = repaired.replace(bad, good)
    repaired = re.sub(r"\s+", " ", repaired).strip()
    return repaired


def _has_numeric_financial_data(data: FinancialData) -> bool:
    """Return whether extracted data contains enough numeric financial values."""

    numeric_values = 0
    for period_values in data.rows.values():
        for value in period_values.values():
            if re.search(r"-?\d+(?:\.\d+)?%?$", value):
                numeric_values += 1
    return numeric_values >= 2


def _normalize_financial_value(value: str, currency_unit: str = "Rs in Cr", metric: str = "") -> str:
    """Normalize a financial table value to a concise string."""

    cleaned = compact_text(value)
    if not cleaned or cleaned in {"-", "--"}:
        return ""
    cleaned = cleaned.replace("−", "-").replace("–", "-")
    percent_match = re.fullmatch(r"\(?\s*-?\s*\d+(?:,\d+)*(?:\.\d+)?\s*\)?\s*%", cleaned)
    if percent_match:
        return cleaned.replace(" ", "")
    number_match = NUMBER_RE.search(cleaned)
    if not number_match:
        return cleaned
    number_text = number_match.group("number")
    negative = bool(number_match.group("negative_paren") and number_match.group("negative_paren_close")) or bool(
        number_match.group("sign")
    )
    unit = number_match.group("unit") or ""
    number_text = number_text.replace(",", "")
    if number_text.count(".") > 1:
        parts = number_text.split(".")
        number_text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        value_float = float(number_text)
    except ValueError:
        return cleaned
    if negative:
        value_float = -value_float
    effective_unit = unit or currency_unit
    if unit.lower().startswith("%"):
        return f"{value_float:.2f}%"
    if _should_convert_to_crores(effective_unit, metric):
        value_float = _convert_to_crores(value_float, effective_unit)
    return f"{value_float:.2f}"


def _should_convert_to_crores(currency_unit: str, metric: str) -> bool:
    """Return whether a metric should be converted to Crores."""

    lowered_metric = metric.lower()
    if "margin" in lowered_metric or "eps" in lowered_metric:
        return False
    return any(unit in (currency_unit or "").lower() for unit in ("lakh", "lac", "million"))


def _convert_to_crores(value: float, currency_unit: str) -> float:
    """Convert a numeric value from the declared unit into Crores."""

    lowered = (currency_unit or "").lower()
    if "lakh" in lowered or "lac" in lowered:
        return value / 100
    if "million" in lowered:
        return value / 10
    return value


def _parse_financial_lines(text: str, data: FinancialData) -> None:
    """Fallback parser for line-based financial metrics outside table extraction."""

    periods = _find_periods_in_text(text)
    if periods and (_periods_are_suspect(data.periods) or (len(data.periods) < 3 and len(periods) >= 5)):
        data.periods = []
        data.rows = {}
    for period in periods:
        if period not in data.periods:
            data.periods.append(period)
    lines = [compact_text(line) for line in text.splitlines() if compact_text(line)]
    for idx, line in enumerate(lines):
        label, confidence = _match_financial_row_with_confidence(line)
        if not label:
            continue
        values = _extract_financial_numbers_from_line(line, data.currency_unit, label, len(data.periods))
        if not values and data.periods:
            values = _extract_following_numeric_values(lines, idx + 1, data.currency_unit, label, len(data.periods))
        if values:
            _assign_line_values(data, label, values, confidence)
    if data.rows:
        return
    default_period = data.periods[0] if data.periods else "Extracted"
    for label in FINANCIAL_ROWS:
        pattern = rf"{re.escape(label)}\s+(?:Rs\.?|INR|₹)?\s*(-?\(?\d+(?:,\d+)*(?:\.\d+)?\)?%?)"
        match = re.search(pattern, text, re.I)
        if match:
            _set_financial_value(
                data,
                label,
                default_period,
                _normalize_financial_value(match.group(1), data.currency_unit, label),
                1.0,
            )


def _periods_are_suspect(periods: list[str]) -> bool:
    """Return whether period headers look like prose, not table columns."""

    if not periods:
        return False
    bad_markers = ("board of directors", "prepared in accordance", "operating activities", "companies act")
    for period in periods:
        lowered = period.lower()
        if len(period) > 45 or any(marker in lowered for marker in bad_markers):
            return True
        date_match = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", period)
        if date_match and (int(date_match.group(1)) > 31 or int(date_match.group(2)) > 12):
            return True
    return False


def _extraction_is_suspect(data: FinancialData) -> bool:
    """Return whether extracted values look like incidental prose numbers."""

    values_found = sum(len(values) for values in data.rows.values())
    if values_found >= 5:
        return False
    if values_found and len(data.rows) <= 3:
        return True
    if _periods_are_suspect(data.periods):
        return True
    return bool(data.periods) and all(period.lower().startswith("value ") for period in data.periods)


def _extract_financial_numbers_from_line(
    line: str,
    currency_unit: str,
    metric: str,
    period_count: int = 0,
) -> list[str]:
    """Extract all numeric cells from a text-line financial row."""

    cleaned = compact_text(line)
    cleaned = re.sub(r"\(\s*\d+\s*[-+]\s*\d+\s*\)", " ", cleaned)
    cleaned = re.sub(r"\bRefer\s+note\s+\d+\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"^[A-Za-z]?\s*\d{1,2}\s+", "", cleaned)
    number_pattern = r"\(?-?\d+(?:[,.]\d+)*\)?%?"
    raw_values = re.findall(number_pattern, cleaned)
    if period_count:
        if len(raw_values) > period_count + 2:
            return []
    raw_values = _repair_missing_decimal_values(raw_values, metric)
    values: list[str] = []
    for raw_value in raw_values:
        if re.fullmatch(r"\d{1,2}", raw_value) and len(raw_values) > 1:
            continue
        value = _normalize_financial_value(raw_value, currency_unit, metric)
        if value:
            values.append(value)
    return values


def _extract_following_numeric_values(
    lines: list[str],
    start_idx: int,
    currency_unit: str,
    metric: str,
    period_count: int,
) -> list[str]:
    """Extract values from rows where the label and numeric cells are split over lines."""

    values: list[str] = []
    for line in lines[start_idx : start_idx + 12]:
        if len(values) >= period_count:
            break
        if values and _match_financial_row(line):
            break
        if not _looks_like_numeric_cell_line(line):
            continue
        line_values = _extract_financial_numbers_from_line(line, currency_unit, metric, 0)
        if line_values:
            values.extend(line_values)
    return values[:period_count]


def _looks_like_numeric_cell_line(line: str) -> bool:
    """Return whether a line is likely one or more table numeric cells."""

    cleaned = compact_text(line)
    if not cleaned:
        return False
    if re.search(r"[A-Za-z]{3,}", cleaned):
        return False
    return bool(re.search(r"\(?-?\d[\d,.\s]*\)?%?", cleaned))


def _repair_missing_decimal_values(raw_values: list[str], metric: str) -> list[str]:
    """Repair common OCR/text-layer decimal drops in small financial rows."""

    decimal_sensitive = {
        "Other Income",
        "Current Tax",
        "Deferred Tax",
        "Tax Expenses",
        "EPS (Basic)",
        "EPS (Diluted)",
    }
    if metric not in decimal_sensitive:
        return raw_values
    repaired: list[str] = []
    has_decimal_context = any("." in value for value in raw_values)
    for value in raw_values:
        stripped = value.strip("()")
        if has_decimal_context and re.fullmatch(r"-?\d{3}", stripped):
            sign = "-" if stripped.startswith("-") else ""
            digits = stripped.lstrip("-")
            value = f"{sign}{digits[:-2]}.{digits[-2:]}"
        repaired.append(value)
    return repaired


def _assign_line_values(data: FinancialData, label: str, values: list[str], confidence: float = 1.0) -> None:
    """Assign line-extracted values to known or generated period columns."""

    if data.periods and len(values) > len(data.periods):
        values = values[: len(data.periods)]
    periods = data.periods[: len(values)]
    if len(periods) < len(values):
        periods.extend(f"Value {idx}" for idx in range(len(periods) + 1, len(values) + 1))
    for period in periods:
        if period not in data.periods:
            data.periods.append(period)
    for period, value in zip(periods, values):
        _set_financial_value(data, label, period, value, confidence)


def _find_periods_in_text(text: str) -> list[str]:
    """Find likely financial periods mentioned in PDF text."""

    statement_periods = _extract_statement_periods_from_text(text)
    if statement_periods:
        return statement_periods

    periods: list[str] = []
    previous_line = ""
    for line in text.splitlines():
        line_periods = _extract_periods_from_header_line(line)
        if len(line_periods) >= 2:
            context = f"{previous_line} {line}".lower()
            if "three months ended" in context and "year ended" in context and len(line_periods) >= 5:
                return [f"Three months ended {period}" for period in line_periods[:3]] + [
                    f"Year ended {period}" for period in line_periods[3:5]
                ]
            return _dedupe_duplicate_period_labels(line_periods)[:8]
        if compact_text(line):
            previous_line = line
    for match in re.finditer(r"\bQ[1-4]\s*F\.?Y\.?\s*\d{2,4}\b|\bFY\d{2,4}\b", text, re.I):
        period = _normalize_period(match.group(0))
        if period not in periods:
            periods.append(period)
    if periods:
        return periods[:8]
    for pattern in PERIOD_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            period = _normalize_period(match.group(0))
            if period not in periods:
                periods.append(period)
    for pattern in (
        r"\b\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[- ]\d{2,4}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
    ):
        for match in re.finditer(pattern, text, re.I):
            period = _normalize_period(match.group(0))
            if period not in periods:
                periods.append(period)
    return periods[:8]


def _extract_statement_periods_from_text(text: str) -> list[str]:
    """Detect financial statement period headers from messy OCR/text layers."""

    lowered = text.lower()
    patterns = (
        r"3\s*months\s+ended",
        r"three\s+months\s+ended",
        r"quarter\s+ended",
        r"quarter.{0,120}year\s+ended",
        r"financial\s+results.{0,500}year\s+ended",
        r"preceding.{0,80}month",
        r"correspond.{0,120}month",
        r"particulars.{0,500}month",
        r"particulars.{0,500}(?:31|3l).{0,80}(?:mar|dec)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lowered, re.S):
            window = text[match.start() : match.start() + 2200]
            dates = _extract_flexible_dates(window)
            if len(dates) >= 5:
                return [
                    f"Three months ended {dates[0]}",
                    f"Three months ended {dates[1]}",
                    f"Three months ended {dates[2]}",
                    f"Year ended {dates[3]}",
                    f"Year ended {dates[4]}",
                ]
            year_window = re.sub(r"\b(20\d)[:;](\d)\b", r"\1\2", window)
            year_window = re.sub(r"\b2021([56])\b", r"202\1", year_window)
            year_window = re.sub(r"\b202[sS]\b", "2025", year_window)
            year_window = re.sub(r"\b2[Oo](2[56])\b", r"20\1", year_window)
            years = re.findall(r"20\d{2}", year_window)
            has_december_column = bool(re.search(r"(?:31\D{0,6}(?:12|1\D{0,2}2)|dec)", year_window, re.I))
            if has_december_column and years.count("2026") >= 1 and years.count("2025") >= 2:
                return [
                    "Three months ended 31 March 2026",
                    "Three months ended 31 December 2025",
                    "Three months ended 31 March 2025",
                    "Year ended 31 March 2026",
                    "Year ended 31 March 2025",
                ]
    return []


def _extract_flexible_dates(text: str) -> list[str]:
    """Extract date headers despite OCR artifacts such as J 1.12.2025."""

    normalized = text
    normalized = re.sub(r"\b(20\d)[:;](\d)\b", r"\1\2", normalized)
    normalized = normalized.replace("J", "3").replace("j", "3")
    normalized = normalized.replace("O", "0").replace("o", "0")
    normalized = normalized.replace(",", "").replace("'", "")
    normalized = normalized.replace(":", "3").replace("l", "1").replace("I", "1")
    normalized = re.sub(r"\b3\s*[r1i]\s*([.\-/])", r"31\1", normalized, flags=re.I)
    normalized = re.sub(r"\b31\s+([.\-/])", r"31\1", normalized)
    normalized = re.sub(r"\b([23])\s+([.\-/])", r"\1\2", normalized)
    normalized = re.sub(r"\b2021([56])\b", r"202\1", normalized)
    normalized = re.sub(r"\b202[sS]\b", "2025", normalized)
    normalized = re.sub(r"\b7025\b", "2025", normalized)
    normalized = re.sub(r"\b2[Oo]25\b", "2025", normalized)
    normalized = re.sub(r"\b2[Oo]26\b", "2026", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    candidates = re.findall(r"\b3?1[.\-/ ](?:0?3|Mar(?:ch)?|0?12|1\.?2|Dec(?:ember)?)[.\-/ ]20\d{2}\b", normalized, re.I)
    dates: list[str] = []
    for candidate in candidates:
        cleaned = candidate.replace(".", "-").replace("/", "-")
        if re.search(r"(?:12|dec)", cleaned, re.I):
            date_value = "31 December " + re.search(r"20\d{2}", cleaned).group(0)
        else:
            date_value = "31 March " + re.search(r"20\d{2}", cleaned).group(0)
        dates.append(date_value)
    return dates[:5]


def _extract_periods_from_header_line(line: str) -> list[str]:
    """Extract date-like period cells from a likely table header line."""

    cleaned = compact_text(line)
    if not cleaned:
        return []
    patterns = [
        r"\b\d{1,2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{2,4}\b",
        r"\b\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/]\d{2,4}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
        r"\bQ[1-4]\s*F\.?Y\.?\s*\d{2,4}\b",
        r"\bFY\d{2,4}\b",
    ]
    periods: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, re.I):
            period = _normalize_period(match.group(0))
            periods.append(period)
    return periods


def _dedupe_duplicate_period_labels(periods: list[str]) -> list[str]:
    """Keep duplicate period dates distinguishable in Excel headers."""

    counts: dict[str, int] = {}
    labels: list[str] = []
    for period in periods:
        counts[period] = counts.get(period, 0) + 1
        labels.append(period if counts[period] == 1 else f"{period} ({counts[period]})")
    return labels
