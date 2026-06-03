"""Mistral document extraction and dynamic Telegram formatting."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from gpt54_extractor import extract_structured_with_gpt54
from models import Announcement
from unit_detector import RS_LAKHS
from unit_detector import RS_MILLIONS
from unit_detector import RS_THOUSANDS
from unit_detector import detect_currency_unit
from unit_detector import display_unit_for_source
from table_repair_engine import repair_financial_payload
from utils import normalize_date

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - optional but listed in requirements.
    fitz = None  # type: ignore[assignment]

try:
    from mistralai import Mistral
except Exception:  # pragma: no cover - SDK layout differs across versions.
    try:
        from mistralai.client import Mistral
    except Exception:  # pragma: no cover - optional dependency is checked at runtime.
        Mistral = None  # type: ignore[assignment]


EXTRACTION_PROMPT = """
You extract Indian listed-company financial result data from an official NSE/BSE PDF.

Return ONLY one valid JSON object. Do not include markdown, commentary, or citations.
Never guess. If a value is not clearly present, use null.
Convert all monetary values to Rs in Cr. Preserve percentages as percent strings.
Extract data ONLY from the Consolidated financial statements section.
Ignore any Standalone financial data completely.
If you see both Standalone and Consolidated sections, use only Consolidated figures.
If only Standalone financial statements are present, extract them and set parser_message
to mention that only standalone data was found.
Use labels exactly as found when they are company-specific, but map common rows to
the closest standard label.

Required JSON schema:
{
  "company_name": "string",
  "board_meeting_date": "DD-MM-YYYY or null",
  "currency_unit": "Rs in Cr",
  "result_period": "latest period such as Q4 FY26, Q3 FY26, Q2 FY26, Q1 FY26, FY26 or null",
  "confidence": 0.0,
  "parser_message": "short note",
  "financial_rows": [
    {
      "label": "row label exactly from PDF or closest standard label",
      "type": "data or section",
      "values": {
        "period label from PDF": "number string from PDF or null"
      }
    }
  ],
  "segment_tables": [
    {
      "title": "segment table title from PDF",
      "rows": [
        {"label": "segment or metric label from PDF", "type": "data or section", "values": {"period label from PDF": "number string from PDF or null"}}
      ]
    }
  ],
  "balance_sheet_variables": [
    {
      "section": "Assets or Liabilities as shown in PDF",
      "rows": [
        {"label": "line item from PDF", "values": {"FY period from PDF": "number string from PDF or null"}}
      ]
    }
  ],
  "cash_flow_variables": [
    {"label": "cash-flow line item from PDF", "values": {"FY period from PDF": "number string from PDF or null"}}
  ],
  "key_variables": [
    {"label": "any other important dynamic variable actually present in PDF", "values": {"period from PDF": "number string from PDF or null"}}
  ]
}

The schema above is structural only. Do not copy placeholder strings, do not
invent example values, and do not return rows unless their values are visible in
the PDF.

Financial row guidance:
- Include only rows that are clearly present in the PDF.
- Keep dynamic expense labels such as Cost of Goods Sold, Cost of materials consumed,
  Purchase of stock-in-trade, Operating cost, Changes in inventories, and other
  company-specific labels.
- Standard rows to look for include Revenue, Gross Profit, Gross Profit Margin,
  Employee Benefit Expense, Other expenses, Total Expenses, EBITDA, EBITDA Margin,
  Depreciation, Finance Cost, Profit before Exceptional Items, Other Income,
  Share of profit/loss in JV/Associate, Exceptional items, Profit Before Tax,
  Tax Expenses, PAT, PAT Margin, EPS (Basic), and EPS (Diluted).
- Include an "Expenses" section row when expense break-up rows are present.

Period logic:
- Identify the latest result quarter from the PDF headers.
- Q1 and Q3 output should contain quarterly periods only, not FY columns.
- Q2 and Q4 output may contain quarterly periods plus H1/FY columns if those columns
  are actually present in the PDF.
- Do not invent H1/FY values.
""".strip()

MAIN_ROW_ORDER = [
    "Revenue",
    "Expenses",
    "Cost of Goods Sold",
    "Cost of materials consumed",
    "Purchases of stock-in-trade",
    "Purchase of stock-in-trade",
    "Operating cost",
    "Change in inventory",
    "Changes in inventories",
    "Changes in inventories of finished goods, work-in-progress and stock-in-trade",
    "Gross Profit",
    "Gross Profit Margin",
    "Employee Benefit Expense",
    "Employee benefit expense",
    "Other Operating Expenses",
    "Other expenses",
    "Total Expenses",
    "EBITDA",
    "EBITDA Margin",
    "Depreciation",
    "Finance Cost",
    "Profit before Exceptional Items, Other Income",
    "Profit before exceptional items and tax",
    "Other Income",
    "Share of loss in JV/Associate",
    "Share of profit/loss in JV/Associate",
    "Exceptional items",
    "Exceptional items (Discontinued Operations)",
    "Profit Before Tax",
    "Tax Expenses",
    "PAT",
    "PAT Margin",
    "EPS (Basic)",
    "EPS (Diluted)",
]

PERIOD_RE = re.compile(
    r"\b(?:(?P<period_kind>Q[1-4]|H[12])\s*FY?|(?P<fy_kind>FY))\s*(?P<year>\d{2,4})\b",
    re.IGNORECASE,
)
TELEGRAM_LIMIT = 3900
DEFAULT_MISTRAL_MAX_PAGES = 30
COMPANY_STOPWORDS = {
    "and",
    "co",
    "company",
    "department",
    "exchange",
    "india",
    "indian",
    "limited",
    "listing",
    "ltd",
    "national",
    "of",
    "stock",
    "the",
}


class _PreparedPdf:
    """PDF path plus page-selection metadata sent to Mistral."""

    def __init__(
        self,
        path: Path,
        original_page_count: int | None = None,
        sent_page_count: int | None = None,
        selected_pages: list[int] | None = None,
        message: str = "",
    ) -> None:
        self.path = path
        self.original_page_count = original_page_count
        self.sent_page_count = sent_page_count
        self.selected_pages = selected_pages or []
        self.message = message


def extract_with_mistral(pdf_path: str | Path | None, announcement: Announcement | None = None) -> dict[str, Any]:
    """Extract structured financial result data from a PDF using Mistral."""

    if not pdf_path:
        return _error_result("mistral_no_pdf", "No PDF path was available for extraction.", announcement)
    path = Path(pdf_path)
    if not path.exists():
        return _error_result("mistral_no_pdf", f"PDF file does not exist: {path}", announcement)
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not api_key:
        return _error_result("mistral_config_error", "MISTRAL_API_KEY is not configured.", announcement)
    if Mistral is None and not _use_ocr_api() and not _is_azure_foundry_endpoint(_configured_base_url()):
        return _error_result("mistral_dependency_missing", "Python package 'mistralai' is not installed.", announcement)

    try:
        if _use_gpt54_after_ocr():
            return _extract_with_mistral_ocr_and_gpt54(path, announcement)
        prepared_pdf = _prepare_pdf_for_mistral(path)
        pdf_data = base64.b64encode(prepared_pdf.path.read_bytes()).decode("utf-8")
        response = _complete_with_document(api_key, pdf_data)
        extracted = _structured_payload_from_response(response)
        extracted["parser_status"] = extracted.get("parser_status") or "parsed_mistral"
        extracted.setdefault("parser_message", "Parsed by Mistral document model.")
        _attach_pdf_efficiency_metadata(extracted, prepared_pdf)
        normalized = normalize_mistral_extraction(extracted, announcement)
        try:
            from financial_validation import attach_validation, validate_financial_payload

            return attach_validation(normalized, validate_financial_payload(normalized, announcement))
        except Exception:
            logging.exception("Deterministic validation failed after Mistral extraction.")
            normalized["validation_status"] = "failed"
            normalized["validation_allows_images"] = False
            normalized["validation_errors"] = ["validation_exception"]
            return normalized
    except Exception as exc:
        logging.exception("Mistral extraction failed for %s", path)
        return _error_result("mistral_error", _friendly_mistral_error(str(exc)), announcement)


def extract_mistral_ocr(pdf_path: str | Path | None, announcement: Announcement | None = None) -> dict[str, Any]:
    """Run only Mistral OCR and return raw OCR/page/table payloads."""

    if not pdf_path:
        return _ocr_failure("mistral_no_pdf", "No PDF path was available for OCR.", announcement)
    path = Path(pdf_path)
    if not path.exists():
        return _ocr_failure("mistral_no_pdf", f"PDF file does not exist: {path}", announcement)
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not api_key:
        return _ocr_failure("mistral_config_error", "MISTRAL_API_KEY is not configured.", announcement)
    try:
        prepared_pdf = _prepare_pdf_for_mistral(path)
        pdf_data = base64.b64encode(prepared_pdf.path.read_bytes()).decode("utf-8")
        response = _complete_with_ocr_api(api_key, _configured_base_url(), pdf_data, include_annotation=False)
        pages = response.get("pages") if isinstance(response, dict) else []
        table_payload = _payload_from_ocr_tables(pages)
        ocr_markdown = str(table_payload.get("ocr_markdown") or _page_markdown(pages))
        result: dict[str, Any] = {
            "ocr_status": "ok",
            "parser_status": "mistral_ocr_completed",
            "parser_message": "Mistral OCR completed.",
            "pdf_path": str(path),
            "company_name": announcement.company_name if announcement else table_payload.get("company_name", ""),
            "source": announcement.source.upper() if announcement else "",
            "board_meeting_date": normalize_date(announcement.announcement_datetime) if announcement else "",
            "ocr_markdown": ocr_markdown,
            "ocr_tables": _ocr_tables_from_pages(pages),
            "table_payload": table_payload,
            "page_numbers_used": prepared_pdf.selected_pages,
            "source_page_count": prepared_pdf.original_page_count,
            "mistral_sent_page_count": prepared_pdf.sent_page_count,
            "mistral_selected_pages": prepared_pdf.selected_pages,
        }
        _attach_pdf_efficiency_metadata(result, prepared_pdf)
        result["raw_ocr_output_path"] = str(_store_raw_ocr_output(path, response, result, announcement))
        return result
    except Exception as exc:
        logging.exception("Mistral OCR failed for %s", path)
        return _ocr_failure("mistral_error", _friendly_mistral_error(str(exc)), announcement)


def _extract_with_mistral_ocr_and_gpt54(path: Path, announcement: Announcement | None) -> dict[str, Any]:
    """Run OCR first, then GPT-5.4 structured extraction, then validation metadata."""

    ocr_payload = extract_mistral_ocr(path, announcement)
    if ocr_payload.get("ocr_status") != "ok":
        return normalize_mistral_extraction(
            {
                "parser_status": ocr_payload.get("parser_status") or "mistral_ocr_failed",
                "parser_message": ocr_payload.get("parser_message") or "Mistral OCR failed.",
                "confidence": 0,
                "financial_rows": [],
                "segment_tables": [],
                "balance_sheet_variables": [],
                "cash_flow_variables": [],
                "key_variables": [],
                "ocr_markdown": ocr_payload.get("ocr_markdown", ""),
            },
            announcement,
        )

    gpt_payload = extract_structured_with_gpt54(
        ocr_payload,
        announcement,
        mock=_truthy_env("GPT54_MOCK", False),
    )
    if gpt_payload.get("gpt_json_status") in {"valid", "mock_valid_json"}:
        table_payload = ocr_payload.get("table_payload") if isinstance(ocr_payload.get("table_payload"), dict) else {}
        gpt_payload = _merge_ocr_table_payload(gpt_payload, table_payload)
    gpt_payload["ocr_status"] = ocr_payload.get("ocr_status")
    gpt_payload["raw_ocr_output_path"] = ocr_payload.get("raw_ocr_output_path", "")
    gpt_payload["ocr_markdown"] = gpt_payload.get("ocr_markdown") or ocr_payload.get("ocr_markdown", "")
    for key in ("source_page_count", "mistral_sent_page_count", "mistral_selected_pages", "page_numbers_used"):
        if ocr_payload.get(key) not in (None, ""):
            gpt_payload[key] = ocr_payload[key]
    normalized = normalize_mistral_extraction(gpt_payload, announcement)
    try:
        from financial_validation import attach_validation, validate_financial_payload

        normalized = attach_validation(normalized, validate_financial_payload(normalized, announcement))
    except Exception:
        logging.exception("Deterministic validation failed after GPT-5.4 extraction.")
        normalized["validation_status"] = "failed"
        normalized["validation_allows_images"] = False
        normalized["validation_errors"] = ["validation_exception"]
    return normalized


def _use_gpt54_after_ocr() -> bool:
    """Return whether the opt-in GPT-5.4 post-OCR path is enabled."""

    return _truthy_env("GPT54_EXTRACTION_ENABLED", False)


def _complete_with_document(api_key: str, pdf_data: str) -> Any:
    """Call either Azure Foundry or the public Mistral SDK for document extraction."""

    base_url = _configured_base_url()
    if _use_ocr_api():
        if _efficient_ocr_mode():
            response = _complete_with_ocr_api(api_key, base_url, pdf_data, include_annotation=False)
            table_payload = _payload_from_ocr_tables(response.get("pages") if isinstance(response, dict) else None)
            if _efficient_payload_is_strong(table_payload):
                response["_mistral_efficiency_mode"] = "ocr_tables_only"
                return response
            logging.info("Efficient OCR table pass was weak; retrying Mistral with document annotation.")
        response = _complete_with_ocr_api(api_key, base_url, pdf_data, include_annotation=True)
        response["_mistral_efficiency_mode"] = "ocr_with_annotation"
        return response
    if _is_azure_foundry_endpoint(base_url):
        return _complete_with_azure_foundry(api_key, base_url, pdf_data)
    client = _create_mistral_client(api_key, base_url)
    return client.chat.complete(
        model=os.environ.get("MISTRAL_MODEL", "mistral-large-latest"),
        temperature=0,
        response_format={"type": "json_object"},
        timeout_ms=int(os.environ.get("MISTRAL_TIMEOUT_MS", "120000")),
        messages=[{"role": "user", "content": _document_content(pdf_data, object_document_url=False)}],
    )


def _use_ocr_api() -> bool:
    """Return whether this deployment should be called through the OCR endpoint."""

    mode = os.environ.get("MISTRAL_API_MODE", "").strip().lower()
    model = os.environ.get("MISTRAL_MODEL", "").strip().lower()
    return mode == "ocr" or model.startswith("mistral-document-ai") or model.startswith("mistral-ocr")


def _truthy_env(name: str, default: bool = False) -> bool:
    """Return a boolean environment flag."""

    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _efficient_ocr_mode() -> bool:
    """Return whether the OCR call should use cheap table-first extraction."""

    return _truthy_env("MISTRAL_EFFICIENT_MODE", False)


def _configured_base_url() -> str:
    """Return the configured Mistral-compatible endpoint, if any."""

    return (
        os.environ.get("MISTRAL_BASE_URL", "").strip()
        or os.environ.get("MISTRAL_SERVER_URL", "").strip()
        or os.environ.get("MISTRAL_ENDPOINT", "").strip()
    )


def _is_azure_foundry_endpoint(base_url: str) -> bool:
    """Return whether the configured URL looks like an Azure Foundry AI Services endpoint."""

    lowered = base_url.lower()
    return "services.ai.azure.com" in lowered or "models.ai.azure.com" in lowered


def _create_mistral_client(api_key: str, base_url: str) -> Any:
    """Create a Mistral SDK client, optionally using a custom Foundry/base URL."""

    if not base_url:
        return Mistral(api_key=api_key)
    try:
        return Mistral(api_key=api_key, server_url=base_url)
    except TypeError:
        logging.warning("Installed mistralai SDK does not accept server_url; falling back to default Mistral API host.")
        return Mistral(api_key=api_key)


def _prepare_pdf_for_mistral(path: Path) -> _PreparedPdf:
    """Return the original PDF or a compact page-selected copy for Mistral."""

    max_pages = max(1, int(os.environ.get("MISTRAL_MAX_PAGES", str(DEFAULT_MISTRAL_MAX_PAGES))))
    if fitz is None:
        return _PreparedPdf(path=path)

    document = None
    compact = None
    try:
        document = fitz.open(str(path))
        page_count = int(document.page_count)
        if page_count <= max_pages:
            return _PreparedPdf(path=path, original_page_count=page_count, sent_page_count=page_count)
        if not _truthy_env("MISTRAL_PAGE_PRESELECT", False):
            logging.info(
                "PDF has %s pages, above Mistral/Azure limit %s; applying mandatory page preselection for %s",
                page_count,
                max_pages,
                path,
            )

        selected_pages = _select_financial_pages(document, max_pages)
        if not selected_pages:
            selected_pages = list(range(min(page_count, max_pages)))
        selected_pages = sorted(dict.fromkeys(page for page in selected_pages if 0 <= page < page_count))
        if len(selected_pages) > max_pages:
            selected_pages = sorted(selected_pages[:max_pages])

        work_dir = Path(os.environ.get("MISTRAL_WORK_DIR", "output/mistral_work"))
        work_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
        compact_path = work_dir / f"{path.stem}_mistral_{len(selected_pages)}of{page_count}_{digest}.pdf"

        compact = fitz.open()
        for page_index in selected_pages:
            compact.insert_pdf(document, from_page=page_index, to_page=page_index)
        compact.save(str(compact_path), garbage=4, deflate=True)
        message = (
            f"Original PDF had {page_count} pages; sent {len(selected_pages)} likely financial pages "
            f"to stay within the Mistral/Azure {max_pages}-page OCR limit."
        )
        logging.info("%s %s", message, compact_path)
        return _PreparedPdf(
            path=compact_path,
            original_page_count=page_count,
            sent_page_count=len(selected_pages),
            selected_pages=[page + 1 for page in selected_pages],
            message=message,
        )
    except Exception as exc:
        logging.warning("Could not preselect PDF pages for Mistral; using original PDF %s: %s", path, exc)
        return _PreparedPdf(path=path)
    finally:
        if compact is not None:
            compact.close()
        if document is not None:
            document.close()


def _select_financial_pages(document: Any, max_pages: int) -> list[int]:
    """Select pages most likely to contain result tables, preserving order."""

    scored: list[tuple[int, int]] = []
    anchor_pages: list[int] = []
    for page_index in range(int(document.page_count)):
        try:
            text = document[page_index].get_text("text")
        except Exception:
            text = ""
        score = _financial_page_score(text)
        if _is_financial_statement_anchor(text):
            anchor_pages.append(page_index)
            score += 40
        if score > 0:
            scored.append((score, page_index))

    if not scored:
        return list(range(min(int(document.page_count), max_pages)))

    selected: list[int] = []
    seen: set[int] = set()

    def add_page(page: int) -> None:
        if 0 <= page < int(document.page_count) and page not in seen and len(selected) < max_pages:
            seen.add(page)
            selected.append(page)

    for page_index in anchor_pages:
        for candidate in (page_index - 2, page_index - 1, page_index, page_index + 1, page_index + 2, page_index + 3, page_index + 4):
            add_page(candidate)

    for _, page_index in sorted(scored, reverse=True):
        for candidate in (page_index - 1, page_index, page_index + 1):
            add_page(candidate)
        if len(selected) >= max_pages:
            break

    if len(selected) < min(max_pages, int(document.page_count)):
        for page_index in range(int(document.page_count)):
            add_page(page_index)
            if len(selected) >= max_pages:
                break
    return sorted(selected)


def _financial_page_score(text: str) -> int:
    """Score how likely a page is to contain financial output data."""

    lowered = re.sub(r"\s+", " ", text.lower())
    if not lowered.strip():
        return 0
    weighted_keywords = {
        "financial results": 12,
        "statement of financial results": 14,
        "standalone financial results": 10,
        "consolidated financial results": 12,
        "standalone financial statements": 8,
        "consolidated financial statements": 10,
        "standalone and consolidated financial statements": 18,
        "audited financial results": 9,
        "unaudited financial results": 9,
        "quarter ended": 8,
        "year ended": 7,
        "three months ended": 7,
        "profit and loss": 8,
        "total income": 5,
        "revenue from operations": 5,
        "profit before tax": 5,
        "profit after tax": 5,
        "earnings per share": 5,
        "balance sheet": 7,
        "cash flow": 7,
        "segment": 5,
    }
    score = sum(weight for keyword, weight in weighted_keywords.items() if keyword in lowered)
    numeric_density = len(re.findall(r"\b\d{1,3}(?:,\d{2,3})*(?:\.\d+)?\b", lowered))
    return score + min(numeric_density // 8, 8)


def _is_financial_statement_anchor(text: str) -> bool:
    """Return true for pages that introduce standalone/consolidated statements."""

    lowered = re.sub(r"\s+", " ", text.lower())
    anchors = (
        "standalone and consolidated financial statements",
        "statement of audited consolidated financial results",
        "consolidated financial statements:",
        "consolidated financial results:",
    )
    return any(anchor in lowered for anchor in anchors)


def _attach_pdf_efficiency_metadata(extracted: dict[str, Any], prepared_pdf: _PreparedPdf) -> None:
    """Attach page-selection metadata to the extraction payload."""

    if prepared_pdf.original_page_count is not None:
        extracted["source_page_count"] = prepared_pdf.original_page_count
    if prepared_pdf.sent_page_count is not None:
        extracted["mistral_sent_page_count"] = prepared_pdf.sent_page_count
    if prepared_pdf.selected_pages:
        extracted["mistral_selected_pages"] = prepared_pdf.selected_pages
    if prepared_pdf.message:
        existing = str(extracted.get("parser_message") or "").strip()
        extracted["parser_message"] = f"{existing} {prepared_pdf.message}".strip()


def _complete_with_azure_foundry(api_key: str, base_url: str, pdf_data: str) -> dict[str, Any]:
    """Call Azure AI Foundry model inference chat completions for a document model."""

    url = _azure_chat_url(base_url)
    timeout_seconds = _mistral_timeout_seconds()
    headers = {
        "api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error = ""
    for object_document_url in (False, True):
        payload = {
            "model": os.environ.get("MISTRAL_MODEL", "mistral-document-ai-2512"),
            "messages": [{"role": "user", "content": _document_content(pdf_data, object_document_url=object_document_url)}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        if response.status_code < 400:
            return response.json()
        last_error = _redacted_http_error(response)
        if response.status_code not in {400, 415, 422}:
            break
    raise RuntimeError(last_error or "Azure Foundry Mistral request failed.")


def _complete_with_ocr_api(api_key: str, base_url: str, pdf_data: str, include_annotation: bool = True) -> dict[str, Any]:
    """Call the Mistral Document AI OCR endpoint."""

    if not base_url:
        raise RuntimeError("MISTRAL_BASE_URL must be set for OCR/document mode.")
    url = _ocr_url(base_url)
    timeout_seconds = _mistral_timeout_seconds()
    retry_count = _mistral_retry_count()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.environ.get("MISTRAL_MODEL", "mistral-document-ai-2512"),
        "document": {
            "type": "document_url",
            "document_name": "board_meeting_outcome.pdf",
            "document_url": f"data:application/pdf;base64,{pdf_data}",
        },
        "table_format": os.environ.get("MISTRAL_TABLE_FORMAT", "html"),
        "extract_header": True,
        "extract_footer": True,
    }
    if include_annotation:
        payload["document_annotation_format"] = _document_annotation_format()
        payload["document_annotation_prompt"] = EXTRACTION_PROMPT
    last_error = ""
    for attempt in range(retry_count):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt < retry_count - 1:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(last_error) from exc
        if response.status_code < 400:
            return response.json()
        last_error = _redacted_http_error(response)
        if response.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
            break
        if attempt < retry_count - 1:
            time.sleep(2**attempt)
    raise RuntimeError(last_error or "Mistral OCR request failed.")


def _mistral_timeout_seconds() -> int:
    """Return the per-request Mistral timeout in seconds."""

    return max(1, int(os.environ.get("MISTRAL_TIMEOUT_MS", "60000")) // 1000)


def _mistral_retry_count() -> int:
    """Return bounded Mistral retry count for live polling."""

    return max(1, min(3, int(os.environ.get("MISTRAL_RETRIES", "1"))))


def _ocr_url(base_url: str) -> str:
    """Build a Mistral Document AI OCR endpoint URL."""

    cleaned = base_url.rstrip("/")
    api_version = os.environ.get("MISTRAL_API_VERSION", "2024-05-01-preview")
    if "services.ai.azure.com" in cleaned.lower() and "/providers/mistral/azure/ocr" not in cleaned.lower():
        url = f"{cleaned}/providers/mistral/azure/ocr"
    elif cleaned.endswith("/v1/ocr"):
        url = cleaned
    elif cleaned.endswith("/v1"):
        url = f"{cleaned}/ocr"
    else:
        url = f"{cleaned}/v1/ocr"
    separator = "&" if "?" in url else "?"
    if "api-version=" not in url:
        url = f"{url}{separator}api-version={api_version}"
    return url


def _document_annotation_format() -> dict[str, Any]:
    """Return the JSON schema required by the Mistral OCR annotation API."""

    scalar_schema: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}]}
    values_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": scalar_schema,
    }
    row_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "type": {"type": ["string", "null"]},
            "values": values_schema,
        },
        "required": ["label", "values"],
        "additionalProperties": False,
    }
    segment_table_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": ["string", "null"]},
            "rows": {"type": "array", "items": row_schema},
        },
        "required": ["title", "rows"],
        "additionalProperties": False,
    }
    variable_section_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "section": {"type": "string"},
            "rows": {"type": "array", "items": row_schema},
        },
        "required": ["section", "rows"],
        "additionalProperties": False,
    }
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "company_name": {"type": ["string", "null"]},
            "board_meeting_date": {"type": ["string", "null"]},
            "currency_unit": {"type": ["string", "null"]},
            "result_period": {"type": ["string", "null"]},
            "confidence": {"type": ["number", "string", "null"]},
            "parser_message": {"type": ["string", "null"]},
            "statement_basis": {"type": ["string", "null"]},
            "financial_rows": {"type": "array", "items": row_schema},
            "segment_tables": {"type": "array", "items": segment_table_schema},
            "balance_sheet_variables": {"type": "array", "items": variable_section_schema},
            "cash_flow_variables": {"type": "array", "items": row_schema},
            "key_variables": {"type": "array", "items": row_schema},
        },
        "required": [
            "company_name",
            "board_meeting_date",
            "currency_unit",
            "result_period",
            "confidence",
            "parser_message",
            "financial_rows",
            "segment_tables",
            "balance_sheet_variables",
            "cash_flow_variables",
            "key_variables",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "board_meeting_financial_extraction",
            "schema": schema,
            "strict": False,
        },
    }


def _azure_chat_url(base_url: str) -> str:
    """Build the Azure Foundry chat-completions URL."""

    cleaned = base_url.rstrip("/")
    if re.search(r"/(?:v1/)?chat/completions(?:\?|$)", cleaned, flags=re.IGNORECASE):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"
    if ".inference.ai.azure.com" in cleaned.lower():
        return f"{cleaned}/v1/chat/completions"
    if not cleaned.endswith("/models"):
        cleaned = f"{cleaned}/models"
    api_version = os.environ.get("MISTRAL_API_VERSION", "2024-05-01-preview")
    return f"{cleaned}/chat/completions?api-version={api_version}"


def _document_content(pdf_data: str, *, object_document_url: bool) -> list[dict[str, Any]]:
    """Build a Mistral document content list."""

    data_url = f"data:application/pdf;base64,{pdf_data}"
    document_value: str | dict[str, str] = {"url": data_url} if object_document_url else data_url
    return [
        {"type": "document_url", "document_url": document_value},
        {"type": "text", "text": EXTRACTION_PROMPT},
    ]


def _redacted_http_error(response: httpx.Response) -> str:
    """Return a credential-safe HTTP error message."""

    body = response.text[:800].replace(os.environ.get("MISTRAL_API_KEY", ""), "[redacted]")
    return f"HTTP {response.status_code} from Azure Foundry Mistral endpoint: {body}"


def _friendly_mistral_error(message: str) -> str:
    """Return a short Telegram-safe parser message while logs keep the traceback."""

    lowered = message.lower()
    if "document_parser_too_many_pages" in lowered or "more than the maximum allowed" in lowered:
        return (
            "Mistral rejected the PDF because it exceeded the OCR page limit. "
            "The bot is configured to preselect likely financial pages; restart with the latest code and retry."
        )
    if "http 400" in lowered:
        return "Mistral returned HTTP 400 for this PDF. Full diagnostic details are available in the local logs/debug files."
    if "timed out" in lowered or "timeout" in lowered:
        return "Mistral timed out while processing this PDF. The bot kept the announcement and left values blank."
    return message[:500]


def parse_mistral_response(response: Any) -> dict[str, Any]:
    """Parse a Mistral chat response into a JSON dictionary."""

    text = _response_text(response)
    payload = _extract_json_payload(text)
    if not isinstance(payload, dict):
        raise ValueError("Mistral response did not contain a JSON object.")
    return payload


def _structured_payload_from_response(response: Any) -> dict[str, Any]:
    """Extract structured financial JSON from chat or OCR responses."""

    if isinstance(response, dict):
        efficiency_mode = str(response.get("_mistral_efficiency_mode") or "")
        payload: dict[str, Any] = {}
        annotation = response.get("document_annotation")
        if isinstance(annotation, dict):
            payload = annotation
        if isinstance(annotation, str) and annotation.strip():
            try:
                parsed_annotation = _extract_json_payload(annotation)
                if isinstance(parsed_annotation, dict):
                    payload = parsed_annotation
            except Exception:
                logging.warning("Could not parse Mistral document_annotation as JSON.")
        if "pages" in response:
            table_payload = _payload_from_ocr_tables(response.get("pages"))
            if payload:
                merged = _merge_ocr_table_payload(payload, table_payload)
                if efficiency_mode:
                    merged["mistral_efficiency_mode"] = efficiency_mode
                return merged
            if table_payload:
                if efficiency_mode:
                    table_payload["mistral_efficiency_mode"] = efficiency_mode
                return table_payload
            empty_payload = _empty_ocr_payload("OCR completed, but no structured financial table data was returned.", 0.2)
            if efficiency_mode:
                empty_payload["mistral_efficiency_mode"] = efficiency_mode
            return empty_payload
        if payload:
            return payload
    return parse_mistral_response(response)


def _efficient_payload_is_strong(payload: dict[str, Any]) -> bool:
    """Return whether local OCR-table parsing is strong enough to skip annotation."""

    if not payload or not has_mistral_financial_data(payload):
        return False
    rows = _normalize_rows(payload.get("financial_rows"))
    segment_rows = [
        row
        for table in _normalize_segment_tables(payload.get("segment_tables"))
        for row in _normalize_rows(table.get("rows"))
    ]
    variable_rows = []
    for section in _normalize_variable_sections(payload.get("balance_sheet_variables")):
        variable_rows.extend(_normalize_rows(section.get("rows")))
    variable_rows.extend(_normalize_rows(payload.get("cash_flow_variables")))
    value_count = sum(
        len(row.get("values") or {})
        for row in rows + segment_rows + variable_rows
        if _row_has_value(row)
    )
    return value_count >= int(os.environ.get("MISTRAL_EFFICIENT_MIN_VALUES", "4"))


def normalize_mistral_extraction(data: dict[str, Any], announcement: Announcement | None = None) -> dict[str, Any]:
    """Normalize a Mistral JSON payload into the formatter schema."""

    result = dict(data)
    extracted_company = str(result.get("company_name") or "").strip()
    if announcement:
        if extracted_company and _is_exchange_boilerplate_company(extracted_company):
            extracted_company = ""
            result["company_name"] = ""
        if extracted_company and not _company_names_match(extracted_company, announcement.company_name):
            logging.warning(
                "Dropping Mistral extraction because extracted company %r does not match announcement company %r.",
                extracted_company,
                announcement.company_name,
            )
            result["parser_status"] = "mistral_company_mismatch"
            result["parser_message"] = (
                f"Mistral extracted company '{extracted_company}', which does not match "
                f"announcement company '{announcement.company_name}'. Financial images skipped."
            )
            result["confidence"] = 0
            result["financial_rows"] = []
            result["segment_tables"] = []
            result["balance_sheet_variables"] = []
            result["cash_flow_variables"] = []
            result["key_variables"] = []
        result["company_name"] = str(result.get("company_name") or announcement.company_name)
        result["source"] = str(result.get("source") or announcement.source.upper())
        result["board_meeting_date"] = str(
            result.get("board_meeting_date") or normalize_date(announcement.announcement_datetime)
        )
        if result.get("parser_status") == "mistral_company_mismatch":
            result["company_name"] = announcement.company_name
    raw_markdown = str(result.get("ocr_markdown") or result.get("raw_ocr_markdown") or "")
    detected_unit = detect_currency_unit(
        raw_markdown,
        str(result.get("company_name") or (announcement.company_name if announcement else "") or ""),
        str(result.get("board_meeting_date") or ""),
    ) if raw_markdown.strip() else ""
    if detected_unit:
        result["source_currency_unit"] = detected_unit
        result["currency_unit"] = display_unit_for_source(detected_unit)
    elif raw_markdown.strip():
        result["currency_unit"] = ""
    else:
        result["currency_unit"] = str(result.get("currency_unit") or "")
    result["confidence"] = _normalize_confidence(result.get("confidence"))
    raw_financial_rows = result.get("financial_rows")
    result["financial_rows"] = _valid_financial_rows(raw_financial_rows)
    result["segment_tables"] = _normalize_segment_tables(result.get("segment_tables"))
    result["balance_sheet_variables"] = _normalize_variable_sections(result.get("balance_sheet_variables"))
    result["cash_flow_variables"] = _prune_empty_rows(_normalize_rows(result.get("cash_flow_variables")))
    result["key_variables"] = _prune_empty_rows(_normalize_rows(result.get("key_variables")))
    _drop_values_without_ocr_evidence(result, raw_markdown)
    if not result.get("result_period"):
        result["result_period"] = _infer_current_period(result["financial_rows"])
    result = repair_financial_payload(
        result,
        company=str(result.get("company_name") or (announcement.company_name if announcement else "") or ""),
        source_pdf=str(getattr(announcement, "pdf_path", "") or result.get("pdf_path") or ""),
    )
    has_data = has_mistral_financial_data(result)
    if has_data:
        result["confidence"] = max(float(result.get("confidence") or 0), _data_confidence(result))
    elif str(result.get("parser_status") or "").startswith("parsed_mistral") and int(
        float(result.get("ocr_financial_table_count") or 0)
    ) == 0:
        result["confidence"] = min(float(result.get("confidence") or 0), 0.4)
        result["parser_message"] = str(result.get("parser_message") or "No financial result table values found in OCR tables.")
    elif _had_label_rows(raw_financial_rows) and not has_data:
        result["confidence"] = min(float(result.get("confidence") or 0), 0.4)
        result["parser_message"] = (
            "Mistral returned row labels but no financial values; treated as no financial data."
        )
    return result


def _company_names_match(extracted_company: str, announcement_company: str) -> bool:
    """Return whether an extracted company name belongs to the announcement."""

    extracted_tokens = _company_tokens(extracted_company)
    announcement_tokens = _company_tokens(announcement_company)
    if not extracted_tokens or not announcement_tokens:
        return False
    overlap = extracted_tokens & announcement_tokens
    if overlap and len(overlap) / max(1, min(len(extracted_tokens), len(announcement_tokens))) >= 0.6:
        return True
    extracted_compact = "".join(sorted(extracted_tokens))
    announcement_compact = "".join(sorted(announcement_tokens))
    return bool(extracted_compact and announcement_compact) and (
        extracted_compact in announcement_compact or announcement_compact in extracted_compact
    )


def _is_exchange_boilerplate_company(company_name: str) -> bool:
    """Return whether a detected company name is actually exchange/address boilerplate."""

    text = re.sub(r"[^a-z0-9 ]+", " ", company_name.lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return False
    exchange_phrases = (
        "bse limited",
        "bse ltd",
        "the bse limited",
        "national stock exchange of india limited",
        "national stock exchange of india ltd",
        "listing department",
        "department of corporate services",
        "corporate services",
        "exchange plaza",
    )
    return any(phrase in text for phrase in exchange_phrases)


def _company_tokens(company_name: str) -> set[str]:
    """Tokenize a company name while removing exchange/address boilerplate."""

    cleaned = company_name.lower().replace("'", "")
    tokens = set(re.findall(r"[a-z0-9]+", cleaned))
    return {token for token in tokens if token not in COMPANY_STOPWORDS and len(token) > 1}


def _drop_values_without_ocr_evidence(result: dict[str, Any], raw_markdown: str) -> None:
    """Drop annotation values that are not visible in OCR text/tables."""

    if not raw_markdown.strip():
        return
    ocr_numbers = _ocr_number_values(raw_markdown)
    if len(ocr_numbers) < 3:
        return
    source_unit = str(result.get("source_currency_unit") or "")
    result["financial_rows"] = _filter_rows_by_ocr_evidence(
        _normalize_rows(result.get("financial_rows")),
        ocr_numbers,
        source_unit,
        "financial result",
    )
    result["segment_tables"] = _filter_segment_tables_by_ocr_evidence(
        result.get("segment_tables"),
        ocr_numbers,
        source_unit,
    )
    result["balance_sheet_variables"] = _filter_variable_sections_by_ocr_evidence(
        result.get("balance_sheet_variables"),
        ocr_numbers,
        source_unit,
    )
    result["cash_flow_variables"] = _filter_rows_by_ocr_evidence(
        _normalize_rows(result.get("cash_flow_variables")),
        ocr_numbers,
        source_unit,
        "cash flow",
    )
    result["key_variables"] = _filter_rows_by_ocr_evidence(
        _normalize_rows(result.get("key_variables")),
        ocr_numbers,
        source_unit,
        "key variable",
    )


def _filter_segment_tables_by_ocr_evidence(
    tables: Any,
    ocr_numbers: list[float],
    source_unit: str,
) -> list[dict[str, Any]]:
    """Keep only segment rows whose values can be found in OCR output."""

    output: list[dict[str, Any]] = []
    for table in _normalize_segment_tables(tables):
        rows = _filter_rows_by_ocr_evidence(
            _normalize_rows(table.get("rows")),
            ocr_numbers,
            source_unit,
            "segment",
        )
        rows = _valid_segment_rows(rows)
        if rows:
            output.append({"title": str(table.get("title") or "Segment Wise"), "rows": rows})
    return output


def _filter_variable_sections_by_ocr_evidence(
    sections: Any,
    ocr_numbers: list[float],
    source_unit: str,
) -> list[dict[str, Any]]:
    """Keep only variable rows whose values can be found in OCR output."""

    output: list[dict[str, Any]] = []
    for section in _normalize_variable_sections(sections):
        rows = _filter_rows_by_ocr_evidence(
            _normalize_rows(section.get("rows")),
            ocr_numbers,
            source_unit,
            "balance sheet variable",
        )
        if rows:
            output.append({"section": str(section.get("section") or "Variables"), "rows": rows})
    return output


def _filter_rows_by_ocr_evidence(
    rows: list[dict[str, Any]],
    ocr_numbers: list[float],
    source_unit: str,
    table_name: str,
) -> list[dict[str, Any]]:
    """Remove row values that are not present in OCR output."""

    filtered: list[dict[str, Any]] = []
    dropped_rows = 0
    dropped_values = 0
    original_value_count = 0
    supported_value_count = 0
    for row in rows:
        if row.get("type") == "section" and not _row_has_value(row):
            filtered.append(row)
            continue
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        original_value_count += len(values)
        supported_values: dict[str, str] = {}
        for period, value in values.items():
            if _value_has_ocr_evidence(value, ocr_numbers, source_unit):
                supported_values[str(period)] = str(value)
                supported_value_count += 1
            else:
                dropped_values += 1
        if supported_values:
            next_row = dict(row)
            next_row["values"] = supported_values
            filtered.append(next_row)
        elif values:
            dropped_rows += 1
    if original_value_count >= 4 and supported_value_count / max(original_value_count, 1) < 0.5:
        logging.warning(
            "Dropping %s table because only %s/%s values had OCR evidence.",
            table_name,
            supported_value_count,
            original_value_count,
        )
        return []
    if dropped_rows or dropped_values:
        logging.warning(
            "Dropped %s %s row(s) and %s value(s) without OCR evidence.",
            dropped_rows,
            table_name,
            dropped_values,
        )
    return _prune_empty_rows(filtered)


def _value_has_ocr_evidence(value: Any, ocr_numbers: list[float], source_unit: str) -> bool:
    """Return whether a normalized value appears in OCR numbers."""

    number = _to_number(value)
    if number is None:
        return False
    candidates = [number]
    if source_unit == RS_LAKHS:
        candidates.append(number * 100)
    if source_unit == RS_MILLIONS:
        candidates.append(number * 10)
    if source_unit == RS_THOUSANDS:
        candidates.append(number * 10000)
    for candidate in candidates:
        tolerance = max(0.05, abs(candidate) * 0.001)
        if any(abs(candidate - seen) <= tolerance for seen in ocr_numbers):
            return True
    return False


def _ocr_number_values(text: str) -> list[float]:
    """Return numeric values visible in OCR text."""

    numbers: list[float] = []
    for token in re.findall(r"\(?-?\d[\d,]*(?:\.\d+)?%?\)?", text):
        number = _to_number(token)
        if number is not None:
            numbers.append(number)
    return numbers


def format_mistral_output(
    extraction: dict[str, Any],
    announcement: Announcement | None = None,
    extracted_at: datetime | None = None,
) -> list[str]:
    """Render Mistral output into Telegram-safe formatted text messages."""

    extracted_at = extracted_at or datetime.now()
    company = str(extraction.get("company_name") or (announcement.company_name if announcement else "") or "Unknown Company")
    source = str(extraction.get("source") or (announcement.source.upper() if announcement else "") or "")
    meeting_date = str(
        extraction.get("board_meeting_date")
        or (normalize_date(announcement.announcement_datetime) if announcement else "")
        or ""
    )
    header = "\n".join(
        [
            company,
            f"Date: {meeting_date}",
            f"Source: {source}",
            f"Extracted At: {extracted_at.strftime('%d-%m-%Y %H:%M:%S')}",
        ]
    )

    messages: list[str] = []
    financial_rows = _sort_main_rows(_normalize_rows(extraction.get("financial_rows")))
    segment_tables = _normalize_segment_tables(extraction.get("segment_tables"))
    balance_sections = _normalize_variable_sections(extraction.get("balance_sheet_variables"))
    cash_flow_rows = _normalize_rows(extraction.get("cash_flow_variables"))
    key_variable_rows = _normalize_rows(extraction.get("key_variables"))

    if financial_rows:
        columns = _result_display_columns(financial_rows, str(extraction.get("result_period") or ""))
        messages.extend(
            _table_messages(
                header,
                str(extraction.get("currency_unit") or "Rs in Cr"),
                financial_rows,
                columns,
                skip_margin_changes=True,
            )
        )
    else:
        no_data_lines = [
            company,
            f"Source: {source}",
            "Financial data is not available in the PDF.",
        ]
        messages.append("\n".join(no_data_lines)[:TELEGRAM_LIMIT])

    for table in segment_tables:
        rows = _normalize_rows(table.get("rows"))
        if not rows:
            continue
        title = str(table.get("title") or f"{company} - Segment Wise")
        columns = _result_display_columns(rows, str(extraction.get("result_period") or ""))
        messages.extend(_table_messages(title, str(extraction.get("currency_unit") or "Rs in Cr"), rows, columns))

    variable_messages = _variable_messages(company, balance_sections, cash_flow_rows, key_variable_rows)
    messages.extend(variable_messages)
    return _dedupe_empty_messages(messages)


def has_mistral_financial_data(extraction: dict[str, Any]) -> bool:
    """Return whether the Mistral payload contains any financial table values."""

    rows = _normalize_rows(extraction.get("financial_rows"))
    segments = _normalize_segment_tables(extraction.get("segment_tables"))
    balance = _normalize_variable_sections(extraction.get("balance_sheet_variables"))
    cash = _normalize_rows(extraction.get("cash_flow_variables"))
    keys = _normalize_rows(extraction.get("key_variables"))
    return any(_row_has_value(row) for row in rows) or any(
        _row_has_value(row)
        for table in segments
        for row in _normalize_rows(table.get("rows"))
    ) or any(
        _row_has_value(row)
        for section in balance
        for row in _normalize_rows(section.get("rows"))
    ) or any(_row_has_value(row) for row in cash + keys)


def mistral_confidence(extraction: dict[str, Any]) -> int:
    """Return a 0-100 confidence score from a Mistral extraction payload."""

    return int(round(_normalize_confidence(extraction.get("confidence")) * 100))


def _error_result(status: str, message: str, announcement: Announcement | None) -> dict[str, Any]:
    """Build a normalized error result."""

    return normalize_mistral_extraction(
        {
            "parser_status": status,
            "parser_message": message,
            "confidence": 0,
            "financial_rows": [],
            "segment_tables": [],
            "balance_sheet_variables": [],
            "cash_flow_variables": [],
            "key_variables": [],
        },
        announcement,
    )


def _empty_ocr_payload(message: str, confidence: float) -> dict[str, Any]:
    """Build an OCR payload with no extracted financial values."""

    return {
        "parser_status": "parsed_mistral",
        "parser_message": message,
        "confidence": confidence,
        "financial_rows": [],
        "segment_tables": [],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "key_variables": [],
        "ocr_financial_table_count": 0,
    }


def _ocr_failure(status: str, message: str, announcement: Announcement | None) -> dict[str, Any]:
    """Build a structured OCR failure object."""

    return {
        "ocr_status": "failed",
        "parser_status": status,
        "parser_message": message,
        "company_name": announcement.company_name if announcement else "",
        "source": announcement.source.upper() if announcement else "",
        "board_meeting_date": normalize_date(announcement.announcement_datetime) if announcement else "",
        "ocr_markdown": "",
        "ocr_tables": [],
        "table_payload": {},
        "page_numbers_used": [],
        "source_page_count": None,
        "mistral_sent_page_count": None,
        "mistral_selected_pages": [],
    }


def _page_markdown(pages: Any) -> str:
    """Join page-level markdown from a Mistral OCR response."""

    if not isinstance(pages, list):
        return ""
    return "\n\n".join(str(page.get("markdown") or "") for page in pages if isinstance(page, dict))


def _ocr_tables_from_pages(pages: Any) -> list[dict[str, Any]]:
    """Return compact table content with page numbers for GPT input."""

    output: list[dict[str, Any]] = []
    if not isinstance(pages, list):
        return output
    for page_index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        page_number = page.get("index") or page.get("page_number") or page_index
        for table_index, table in enumerate(page.get("tables") or [], start=1):
            if not isinstance(table, dict):
                continue
            content = str(table.get("content") or "")
            if not content.strip():
                continue
            output.append({"page": page_number, "table_index": table_index, "content": content})
    return output


def _store_raw_ocr_output(
    pdf_path: Path,
    response: Any,
    metadata: dict[str, Any],
    announcement: Announcement | None,
) -> Path:
    """Persist raw OCR output for debugging without exposing credentials."""

    company = str(metadata.get("company_name") or (announcement.company_name if announcement else "") or pdf_path.stem)
    date = str(metadata.get("board_meeting_date") or datetime.now().strftime("%Y-%m-%d"))
    output_dir = Path(os.environ.get("OCR_DEBUG_DIR", "output/ocr")) / _safe_token(company) / _safe_token(date)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{_safe_token(pdf_path.stem)}_ocr.json"
    payload = {
        "pdf_path": str(pdf_path),
        "metadata": {key: value for key, value in metadata.items() if key not in {"ocr_markdown", "ocr_tables", "table_payload"}},
        "response": response,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, default=str), encoding="utf-8")
    return output_path


def _safe_token(value: str, max_length: int = 80) -> str:
    """Return a Windows-safe path token."""

    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return (token or "item")[:max_length]


def _payload_from_ocr_tables(pages: Any) -> dict[str, Any]:
    """Extract structured financial data from OCR page HTML tables."""

    if not isinstance(pages, list):
        return {}
    financial_candidates: list[dict[str, Any]] = []
    balance_candidates: list[dict[str, Any]] = []
    cash_candidates: list[dict[str, Any]] = []
    segment_tables: list[dict[str, Any]] = []
    company_name = ""
    ocr_parts: list[str] = []
    global_unit_parts: list[str] = []
    page_basis = _page_statement_basis_map(pages)
    for page in pages:
        if not isinstance(page, dict):
            continue
        global_unit_parts.append(str(page.get("markdown") or ""))
        for table in page.get("tables") if isinstance(page.get("tables"), list) else []:
            if isinstance(table, dict):
                global_unit_parts.append(str(table.get("content") or ""))
    global_unit_context = "\n".join(global_unit_parts)
    for page in pages:
        if not isinstance(page, dict):
            continue
        tables = page.get("tables") if isinstance(page.get("tables"), list) else []
        markdown = str(page.get("markdown") or "")
        page_no = _ocr_page_number(page, len(ocr_parts) + 1)
        inferred_basis = _inferred_page_basis(page_no, page_basis)
        if markdown.strip():
            ocr_parts.append(markdown)
        for table in tables:
            if not isinstance(table, dict):
                continue
            content = str(table.get("content") or "")
            rows = _html_table_rows(content)
            if not rows:
                continue
            text = _table_plain_text(rows)
            local_context = f"{text}\n{markdown}\nstatement basis: {inferred_basis}".strip()
            value_context = f"{local_context}\n{global_unit_context}"
            if content.strip():
                ocr_parts.append(content)
            company_name = company_name or _company_from_table(rows)
            if _looks_like_financial_result(text, rows):
                parsed = _parse_financial_result_table(rows, value_context)
                if parsed:
                    parsed["page_no"] = page_no
                    parsed["statement_basis"] = inferred_basis
                    parsed["context"] = local_context
                    financial_candidates.append(parsed)
            elif _looks_like_eps_continuation_table(text, rows):
                parsed = _parse_financial_result_table(rows, value_context)
                if parsed:
                    parsed["page_no"] = page_no
                    parsed["statement_basis"] = inferred_basis
                    parsed["context"] = local_context
                    parsed["continuation"] = "eps"
                    financial_candidates.append(parsed)
            elif _looks_like_segment_table(text, markdown):
                parsed_segment = _parse_segment_table(rows, value_context)
                if parsed_segment:
                    parsed_segment["page_no"] = page_no
                    parsed_segment["statement_basis"] = inferred_basis
                    parsed_segment["context"] = local_context
                    segment_tables.append(parsed_segment)
            elif _looks_like_cash_flow(text, markdown):
                parsed_cash = _parse_cash_flow_table(rows, value_context)
                if parsed_cash:
                    cash_candidates.append({"rows": parsed_cash, "context": local_context, "page_no": page_no, "statement_basis": inferred_basis})
            elif _looks_like_balance_sheet(text, markdown):
                parsed_balance = _parse_balance_sheet_table(rows, value_context)
                if parsed_balance:
                    balance_candidates.append({"sections": parsed_balance, "context": local_context, "page_no": page_no, "statement_basis": inferred_basis})

    selected_financial = _select_best_table_payload(financial_candidates)
    selected_balance = _select_best_variable_sections(balance_candidates)
    selected_cash_rows = _select_cash_flow_rows(cash_candidates)
    segment_tables = _select_segment_tables(segment_tables)
    ocr_markdown = "\n\n".join(ocr_parts)
    source_unit = detect_currency_unit(ocr_markdown, company_name, "") if ocr_markdown.strip() else ""
    display_unit = display_unit_for_source(source_unit)
    statement_basis = _statement_basis_from_contexts(
        [str(item.get("context") or "") for item in financial_candidates]
        + [str(item.get("context") or "") for item in balance_candidates]
        + [str(item.get("context") or "") for item in segment_tables]
        + [str(item.get("context") or "") for item in cash_candidates],
        financial_table_count=len(financial_candidates),
    )
    has_data = bool(
        selected_financial.get("financial_rows")
        or segment_tables
        or selected_balance
        or selected_cash_rows
    )
    discovery = _build_discovery_metadata(
        pages=pages,
        page_basis=page_basis,
        financial_candidates=financial_candidates,
        selected_financial=selected_financial,
        balance_candidates=balance_candidates,
        cash_candidates=cash_candidates,
        segment_tables=segment_tables,
        statement_basis=statement_basis,
        source_unit=source_unit,
        display_unit=display_unit,
    )
    if not has_data:
        empty = _empty_ocr_payload("No financial result table values found in OCR tables.", 0.2)
        empty.update(
            {
                "company_name": company_name,
                "currency_unit": display_unit,
                "source_currency_unit": source_unit,
                "ocr_markdown": ocr_markdown,
                "statement_basis": statement_basis,
                "values_display_unit_applied": True,
                "segment_values_display_unit_applied": True,
                "discovery_metadata": discovery,
            }
        )
        return empty

    payload = {
        "company_name": company_name,
        "currency_unit": display_unit,
        "source_currency_unit": source_unit,
        "result_period": selected_financial.get("result_period", ""),
        "confidence": 0.97,
        "parser_status": "parsed_mistral",
        "parser_message": "Parsed by Mistral OCR tables with deterministic HTML-table fallback.",
        "financial_rows": selected_financial.get("financial_rows", []),
        "segment_tables": segment_tables,
        "balance_sheet_variables": selected_balance,
        "cash_flow_variables": selected_cash_rows,
        "key_variables": [],
        "ocr_financial_table_count": len(financial_candidates),
        "ocr_markdown": ocr_markdown,
        "statement_basis": statement_basis,
        "values_display_unit_applied": True,
        "segment_values_display_unit_applied": True,
        "discovery_metadata": discovery,
    }
    if statement_basis == "standalone":
        payload["parser_message"] = f"{payload['parser_message']} Only standalone data found."
    return repair_financial_payload(payload, company=company_name)


def _build_discovery_metadata(
    *,
    pages: list[Any],
    page_basis: dict[int, str],
    financial_candidates: list[dict[str, Any]],
    selected_financial: dict[str, Any],
    balance_candidates: list[dict[str, Any]],
    cash_candidates: list[dict[str, Any]],
    segment_tables: list[dict[str, Any]],
    statement_basis: str,
    source_unit: str,
    display_unit: str,
) -> dict[str, Any]:
    """Build first-pass document discovery metadata for local audit reports."""

    selected_periods = _periods_from_rows_for_discovery(selected_financial.get("financial_rows"))
    result_period = str(selected_financial.get("result_period") or "")
    basis_values = {value for value in page_basis.values() if value}
    return {
        "standalone_available": "standalone" in basis_values or _contexts_contain(financial_candidates + balance_candidates + cash_candidates + segment_tables, "standalone"),
        "consolidated_available": "consolidated" in basis_values or _contexts_contain(financial_candidates + balance_candidates + cash_candidates + segment_tables, "consolidated"),
        "selected_statement_basis": statement_basis,
        "source_currency_unit": source_unit,
        "display_currency_unit": display_unit,
        "result_period": result_period,
        "period_layout": _period_layout_from_periods(selected_periods, result_period),
        "period_columns": selected_periods,
        "page_count_in_ocr_payload": len(pages),
        "basis_pages": {str(page): basis for page, basis in sorted(page_basis.items())},
        "section_pages": {
            "profit_and_loss": _candidate_pages([selected_financial] if selected_financial else []),
            "balance_sheet": _candidate_pages(balance_candidates),
            "cash_flow": _candidate_pages(cash_candidates),
            "segments": _candidate_pages(segment_tables),
        },
        "candidate_counts": {
            "profit_and_loss": len(financial_candidates),
            "balance_sheet": len(balance_candidates),
            "cash_flow": len(cash_candidates),
            "segments": len(segment_tables),
        },
    }


def _contexts_contain(candidates: list[dict[str, Any]], word: str) -> bool:
    needle = str(word or "").lower()
    return any(needle in str(candidate.get("context") or "").lower() for candidate in candidates if isinstance(candidate, dict))


def _candidate_pages(candidates: list[dict[str, Any]]) -> list[int]:
    pages: list[int] = []
    for candidate in candidates:
        page = candidate.get("page_no") if isinstance(candidate, dict) else None
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            continue
        if page_number not in pages:
            pages.append(page_number)
    return pages


def _periods_from_rows_for_discovery(rows: Any) -> list[str]:
    periods: list[str] = []
    if not isinstance(rows, list):
        return periods
    for row in rows:
        values = row.get("values") if isinstance(row, dict) else {}
        if not isinstance(values, dict):
            continue
        for period in values:
            text = str(period or "").strip()
            if text and text not in periods:
                periods.append(text)
    return periods


def _period_layout_from_periods(periods: list[str], result_period: str) -> str:
    labels = [str(period or "").upper() for period in periods]
    result = str(result_period or "").upper()
    has_quarter = any(re.fullmatch(r"Q[1-4]\s+FY\d{2}", label) for label in labels) or result.startswith("Q")
    has_half = any(re.fullmatch(r"H[12]\s+FY\d{2}", label) for label in labels) or result.startswith("H")
    has_year = any(re.fullmatch(r"FY\d{2}", label) for label in labels) or result.startswith("FY")
    if has_quarter and has_year:
        return "quarter_and_year"
    if has_half and has_year:
        return "half_year_and_year"
    if has_quarter:
        return "quarter"
    if has_half:
        return "half_year"
    if has_year:
        return "annual"
    return "unknown"


def _page_statement_basis_map(pages: list[Any]) -> dict[int, str]:
    """Return direct standalone/consolidated markers by OCR page number."""

    basis_by_page: dict[int, str] = {}
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        page_no = _ocr_page_number(page, index)
        parts = [str(page.get("markdown") or "")]
        for table in page.get("tables") if isinstance(page.get("tables"), list) else []:
            if isinstance(table, dict):
                parts.append(str(table.get("content") or ""))
        text = re.sub(r"\s+", " ", " ".join(parts).lower())
        has_consolidated = "consolidated" in text
        has_standalone = "standalone" in text
        if has_consolidated and not has_standalone:
            basis_by_page[page_no] = "consolidated"
        elif has_standalone and not has_consolidated:
            basis_by_page[page_no] = "standalone"
    return basis_by_page


def _ocr_page_number(page: dict[str, Any], default: int) -> int:
    """Return one-based OCR page number from Mistral page metadata."""

    page_number = page.get("page_number")
    if page_number not in (None, ""):
        try:
            return int(page_number)
        except (TypeError, ValueError):
            pass
    page_index = page.get("index")
    if page_index not in (None, ""):
        try:
            index = int(page_index)
        except (TypeError, ValueError):
            return default
        return index + 1 if index >= 0 else default
    return default


def _inferred_page_basis(page_no: int, basis_by_page: dict[int, str]) -> str:
    """Infer the statement basis for title-less table pages."""

    if basis_by_page.get(page_no):
        return basis_by_page[page_no]
    for offset in (1, 2, 3, 4, 5):
        if basis_by_page.get(page_no + offset):
            return basis_by_page[page_no + offset]
    for offset in (1, 2, 3, 4, 5):
        if basis_by_page.get(page_no - offset):
            return basis_by_page[page_no - offset]
    return ""


def payload_from_ocr_markdown_tables(ocr_markdown: str) -> dict[str, Any]:
    """Rebuild a deterministic table payload from OCR markdown with embedded HTML tables."""

    markdown = str(ocr_markdown or "")
    if "<table" not in markdown.lower():
        return {}
    pages: list[dict[str, Any]] = []
    for match in re.finditer(r"<table\b.*?</table>", markdown, flags=re.IGNORECASE | re.DOTALL):
        start, end = match.span()
        context = markdown[max(0, start - 1500) : min(len(markdown), end + 1500)]
        pages.append({"markdown": context, "tables": [{"content": match.group(0)}]})
    return _payload_from_ocr_tables(pages)


def _merge_ocr_table_payload(annotation: dict[str, Any], table_payload: dict[str, Any]) -> dict[str, Any]:
    """Prefer OCR table values when annotation data is sparse or missing."""

    merged = dict(annotation)
    if not table_payload:
        return merged
    existing_rows = _prune_empty_rows(_normalize_rows(merged.get("financial_rows")))
    table_rows = _prune_empty_rows(_normalize_rows(table_payload.get("financial_rows")))
    if table_rows and len(table_rows) >= len(existing_rows):
        merged["financial_rows"] = table_payload.get("financial_rows", [])
        merged["result_period"] = table_payload.get("result_period") or merged.get("result_period")
    for key in ("segment_tables", "balance_sheet_variables", "cash_flow_variables", "key_variables"):
        if table_payload.get(key):
            existing = merged.get(key)
            if key == "segment_tables" or not existing or not has_mistral_financial_data({key: existing}):
                merged[key] = table_payload[key]
    if table_payload.get("company_name") and (
        not merged.get("company_name") or _is_exchange_boilerplate_company(str(merged.get("company_name") or ""))
    ):
        merged["company_name"] = table_payload["company_name"]
    merged["currency_unit"] = table_payload.get("currency_unit") or merged.get("currency_unit")
    for key in (
        "source_currency_unit",
        "ocr_markdown",
        "statement_basis",
        "values_display_unit_applied",
        "table_repair_metadata",
        "repair_critical_issues",
        "repair_warning_categories",
        "column_identities",
        "discovery_metadata",
    ):
        if table_payload.get(key) not in (None, ""):
            merged[key] = table_payload[key]
    merged["ocr_financial_table_count"] = table_payload.get("ocr_financial_table_count", 0)
    if has_mistral_financial_data(table_payload):
        merged["confidence"] = max(_normalize_confidence(merged.get("confidence")), _normalize_confidence(table_payload.get("confidence")))
        merged["parser_message"] = table_payload.get("parser_message") or merged.get("parser_message")
    return merged


class _HTMLTableParser(HTMLParser):
    """Minimal HTML table parser for Mistral OCR table content."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start a table row/cell."""

        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        """Collect cell text."""

        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close a table row/cell."""

        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", " ".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell.strip() for cell in self._row):
                self.rows.append(self._row)
            self._row = None


def _html_table_rows(content: str) -> list[list[str]]:
    """Parse OCR HTML table content into rows/cells."""

    parser = _HTMLTableParser()
    try:
        parser.feed(content)
    except Exception:
        return []
    return parser.rows


def _table_plain_text(rows: list[list[str]]) -> str:
    """Return a searchable lower-case table text."""

    return " ".join(" ".join(row) for row in rows).lower()


def _company_from_table(rows: list[list[str]]) -> str:
    """Extract company name from a table title row when present."""

    for row in rows[:2]:
        if not row:
            continue
        first = row[0]
        match = re.search(r"(?P<name>[A-Z][A-Za-z0-9&.,'() \-]+?\b(?:Limited|Ltd\.?|LTD))\b", first)
        if match:
            name = re.sub(r"\s+", " ", match.group("name")).strip()
            if not _is_exchange_boilerplate_company(name):
                return name
    return ""


def _looks_like_financial_result(text: str, rows: list[list[str]]) -> bool:
    """Return whether a table is a financial-result table."""

    if any(term in text for term in ("segment revenue", "segment results", "segment assets", "segment liabilities")):
        return False
    has_particulars = any("particular" in " ".join(row).lower() for row in rows[:5])
    has_period_header = (
        ("quarter" in text and ("year ended" in text or "year ended on" in text))
        or bool(_financial_header(rows)[1])
    )
    pnl_terms = sum(
        1
        for term in (
            "revenue from operations",
            "revenue from operation",
            "total revenue",
            "total income",
            "expenses",
            "profit before tax",
            "profit for the period",
            "net profit",
        )
        if term in text
    )
    return (has_particulars or has_period_header) and has_period_header and (
        "financial results" in text or pnl_terms >= 2
    )


def _looks_like_eps_continuation_table(text: str, rows: list[list[str]]) -> bool:
    """Return whether a split/continuation table carries EPS rows for a P&L."""

    has_particulars = any("particular" in " ".join(row).lower() for row in rows[:5])
    has_periods = bool(_financial_header(rows)[1])
    eps_terms = "earnings per share" in text or " eps " in f" {text} "
    basic_diluted = "basic" in text and "diluted" in text
    return has_particulars and has_periods and (eps_terms or basic_diluted)


def _looks_like_balance_sheet(text: str, markdown: str) -> bool:
    """Return whether a table is a balance-sheet table."""

    text_lower = text.lower()
    context = f"{text_lower} {markdown.lower()}"
    if "cash flow" in text or "operating activities" in text:
        return False
    if not _has_balance_sheet_row_evidence(text_lower):
        return False
    return (
        "balance sheet" in context
        or "statement of assets and liabilities" in context
        or ("assets" in text_lower and ("equity and liabilities" in text_lower or "liabilities" in text_lower))
    )


def _has_balance_sheet_row_evidence(text: str) -> bool:
    """Require table-local BS rows so nearby notes are not misfiled as BS data."""

    compact = _row_key(text)
    asset_needles = (
        "totalassets",
        "currentassets",
        "noncurrentassets",
        "propertyplant",
        "capitalwork",
        "rightofuse",
        "inventories",
        "tradereceivables",
        "cashandcashequivalents",
        "bankbalances",
        "financialassets",
        "otherassets",
    )
    liability_needles = (
        "totalequityandliabilities",
        "equityandliabilities",
        "totalliabilities",
        "currentliabilities",
        "noncurrentliabilities",
        "equitysharecapital",
        "otherequity",
        "borrowings",
        "tradepayables",
        "financialliabilities",
        "otherliabilities",
        "deferredtaxliabilities",
    )
    asset_hits = sum(1 for needle in asset_needles if needle in compact)
    liability_hits = sum(1 for needle in liability_needles if needle in compact)
    if "statementofassetsandliabilities" in compact and asset_hits >= 1:
        return True
    return asset_hits >= 1 and liability_hits >= 1


def _looks_like_cash_flow(text: str, markdown: str) -> bool:
    """Return whether a table is a cash-flow table."""

    context = f"{text} {markdown.lower()}"
    return (
        "cash flow" in text
        or ("operating activities" in text and "investing activities" in text)
        or ("cash flow" in context and ("operating activities" in text or "financing activities" in text))
    )


def _looks_like_segment_table(text: str, markdown: str) -> bool:
    """Return whether a table is a segment table."""

    context = f"{text} {markdown.lower()}"
    return "segment" in context and ("revenue" in text or "results" in text)


def _parse_financial_result_table(rows: list[list[str]], text: str) -> dict[str, Any]:
    """Parse a financial-result HTML table into normalized rows."""

    header_index, periods, label_index, value_start = _financial_header(rows)
    if not periods:
        return {}
    scale = _money_scale(text)
    parsed_rows: list[dict[str, Any]] = []
    current_eps_context = ""
    for row in rows[header_index + 1 :]:
        row_label_index, row_value_start = _financial_row_label_and_value_start(row, label_index, value_start)
        if row_label_index < 0 or len(row) <= row_label_index:
            continue
        raw_label = row[row_label_index]
        label = _standard_financial_label(raw_label)
        if not label or _is_noise_financial_label(label):
            continue
        raw_lower = _clean_label(raw_label).lower()
        expanded_rows = _expanded_multi_metric_financial_rows(raw_label, row, row_value_start, periods, scale)
        if expanded_rows:
            parsed_rows.extend(expanded_rows)
            continue
        if label in {"EPS (Basic)", "EPS (Diluted)"} and current_eps_context:
            if "discontinuing" in current_eps_context and "continuing and discontinued" not in current_eps_context:
                continue
        values: dict[str, str] = {}
        for offset, period in enumerate(periods):
            cell_index = row_value_start + offset
            if cell_index >= len(row):
                continue
            value = _financial_value(row[cell_index], label, scale)
            if value is not None:
                values[period] = value
        if ("earnings per share" in raw_lower or "eps" in raw_lower) and not values:
            current_eps_context = raw_lower
            continue
        if _is_expense_section(label) and not values:
            parsed_rows.append({"label": "Expenses", "type": "section", "values": {}})
        elif values:
            parsed_rows.append({"label": label, "type": "data", "values": values})
    parsed_rows = _dedupe_rows_by_label(parsed_rows)
    _repair_revenue_from_total_income(parsed_rows)
    return {
        "financial_rows": parsed_rows,
        "result_period": periods[0] if periods else "",
    }


def _expanded_multi_metric_financial_rows(
    raw_label: str,
    row: list[str],
    value_start: int,
    periods: list[str],
    scale: float,
) -> list[dict[str, Any]]:
    """Split rows where OCR merged several P&L labels into one cell."""

    labels = _lettered_metric_labels(raw_label)
    if len(labels) < 2:
        return []
    values_by_label: list[dict[str, str]] = [{} for _label in labels]
    for offset, period in enumerate(periods):
        cell_index = value_start + offset
        if cell_index >= len(row):
            continue
        numbers = _numbers_from_segment_cell(row[cell_index])
        if len(numbers) < len(labels):
            continue
        for label_index, metric_label in enumerate(labels):
            standard = _standard_financial_label(metric_label) or metric_label
            value = _financial_value(numbers[label_index], standard, scale)
            if value is not None:
                values_by_label[label_index][period] = value
    output: list[dict[str, Any]] = []
    for metric_label, values in zip(labels, values_by_label):
        standard = _standard_financial_label(metric_label)
        if standard and values and not _is_noise_financial_label(standard):
            output.append({"label": standard, "type": "data", "values": values})
    return output


def _lettered_metric_labels(label: str) -> list[str]:
    """Return labels after ``a)``/``b)`` markers from a merged OCR cell."""

    text = _clean_label(label)
    marker_pattern = r"(?:\b[a-h]\)|\(\s*[a-h]\s*\))"
    if len(re.findall(marker_pattern, text, flags=re.IGNORECASE)) < 2:
        return []
    labels: list[str] = []
    for match in re.finditer(
        rf"{marker_pattern}\s*(?P<label>.*?)(?=\s+{marker_pattern}\s*|$)",
        text,
        flags=re.IGNORECASE,
    ):
        metric = _clean_label(match.group("label"))
        metric = re.sub(r"^(income|expenses?)\s+", "", metric, flags=re.IGNORECASE).strip()
        if metric:
            labels.append(metric)
    return labels


def _parse_balance_sheet_table(rows: list[list[str]], text: str) -> list[dict[str, Any]]:
    """Parse a balance-sheet HTML table into variable sections."""

    header_index, periods, label_index, value_start = _fy_header(rows)
    if not periods:
        return []
    scale = _money_scale(text)
    sections: list[dict[str, Any]] = []
    current_section = "Variables"
    for row in rows[header_index + 1 :]:
        if len(row) <= label_index:
            continue
        row_label_index = label_index
        if (
            label_index == 0
            and len(row) > 1
            and (not str(row[0] or "").strip() or _financial_value(row[0], "", 1.0) is not None)
            and _label_has_meaningful_text(_clean_label(row[1]))
        ):
            row_label_index = 1
        if (
            label_index > 0
            and len(row) > label_index
            and (
                not str(row[label_index] or "").strip()
                or _financial_value(row[label_index], "", 1.0) is not None
            )
            and _label_has_meaningful_text(_clean_label(row[0]))
        ):
            row_label_index = 0
        label = _clean_label(row[row_label_index])
        if not label or _is_noise_financial_label(label):
            continue
        row_value_start = _value_start_for_row(row, periods, row_label_index, value_start if row_label_index == label_index else row_label_index + 1)
        values = _row_values(row, periods, row_value_start, label, scale)
        if not values:
            if _is_statement_section(label):
                current_section = _section_title(label)
                if not any(section["section"] == current_section for section in sections):
                    sections.append({"section": current_section, "rows": []})
            continue
        if not sections:
            sections.append({"section": current_section, "rows": []})
        sections[-1]["rows"].append({"label": label, "type": "data", "values": values})
    return [section for section in sections if section.get("rows")]


def _parse_cash_flow_table(rows: list[list[str]], text: str) -> list[dict[str, Any]]:
    """Parse key cash-flow rows from an OCR HTML table."""

    header_index, periods, label_index, value_start = _fy_header(rows)
    if not periods:
        return []
    scale = _money_scale(text)
    output: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        if len(row) <= label_index:
            continue
        row_label_index = label_index
        if (
            label_index == 0
            and len(row) > 1
            and (not str(row[0] or "").strip() or _financial_value(row[0], "", 1.0) is not None)
            and _label_has_meaningful_text(_clean_label(row[1]))
        ):
            row_label_index = 1
        if (
            label_index > 0
            and len(row) > label_index
            and (
                not str(row[label_index] or "").strip()
                or _financial_value(row[label_index], "", 1.0) is not None
            )
            and _label_has_meaningful_text(_clean_label(row[0]))
        ):
            row_label_index = 0
        label = _standard_cash_flow_label(row[row_label_index])
        if not label:
            continue
        row_value_start = _value_start_for_row(row, periods, row_label_index, value_start if row_label_index == label_index else row_label_index + 1)
        values = _row_values(row, periods, row_value_start, label, scale)
        grouped_values = _grouped_period_row_values(row, periods, row_label_index, label, scale)
        if len(grouped_values) > len(values) or (grouped_values and any(not values.get(period) for period in grouped_values)):
            values.update(grouped_values)
        if values:
            output.append({"label": label, "type": "data", "values": values})
    return output


def _grouped_period_row_values(
    row: list[str],
    periods: list[str],
    label_index: int,
    label: str,
    scale: float,
) -> dict[str, str]:
    """Read rows where each FY has subtotal/detail cells from OCR colspans."""

    if len(periods) < 2:
        return {}
    first_value_index = label_index + 1
    value_cells = row[first_value_index:]
    if len(value_cells) < len(periods) * 2:
        return {}
    group_width = len(value_cells) // len(periods)
    if group_width < 2:
        return {}
    output: dict[str, str] = {}
    for index, period in enumerate(periods):
        group = value_cells[index * group_width : (index + 1) * group_width]
        for cell in reversed(group):
            value = _financial_value(cell, label, scale)
            if value is not None:
                output[period] = value
                break
    return output


def _parse_segment_table(rows: list[list[str]], text: str) -> dict[str, Any]:
    """Parse segment tables when OCR exposes them separately."""

    header_index, periods, _label_index, default_value_start = _financial_header(rows)
    if not periods:
        return {}
    scale = _money_scale(text)
    current_metric = ""
    parsed_rows: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        value_start = _segment_value_start(row, periods, default_value_start)
        label = _segment_label_from_row(row, value_start)
        if not label or _is_noise_financial_label(label):
            continue
        metric = _segment_metric_from_label(label)
        if metric:
            expanded = _expanded_segment_metric_rows(metric, label, row, value_start, periods, scale)
            if expanded:
                parsed_rows.extend(expanded)
            current_metric = metric
            continue
        if not current_metric:
            continue
        values = _row_values(row, periods, value_start, label, scale)
        if not values:
            continue
        output_label = _segment_output_label(current_metric, label)
        if output_label:
            parsed_rows.append({"label": output_label, "type": "data", "values": values})

    if parsed_rows:
        return {"title": "Segment Wise", "rows": _valid_segment_rows(parsed_rows), "context": text}
    return {}


def _expanded_segment_metric_rows(
    metric: str,
    label: str,
    row: list[str],
    value_start: int,
    periods: list[str],
    scale: float,
) -> list[dict[str, Any]]:
    """Split OCR rows like 'Segment Revenue (a) Pigments (b) API'."""

    names = _segment_names_from_combined_label(label)
    if not names:
        return []
    values_by_segment = {name: {} for name in names}
    for offset, period in enumerate(periods):
        cell_index = value_start + offset
        if cell_index >= len(row):
            continue
        numbers = _numbers_from_segment_cell(row[cell_index])
        if len(numbers) < len(names):
            continue
        for index, name in enumerate(names):
            value = _financial_value(numbers[index], f"{name} - {metric}", scale)
            if value is not None:
                values_by_segment[name][period] = value
    output: list[dict[str, Any]] = []
    for name, values in values_by_segment.items():
        if values:
            output.append({"label": f"{name} - {metric}", "type": "data", "values": values})
    return output


def _segment_names_from_combined_label(label: str) -> list[str]:
    """Return segment names from labels containing '(a) Name (b) Name'."""

    cleaned = _clean_label(label)
    if not re.search(r"\(\s*[a-z]\s*\)", cleaned, flags=re.IGNORECASE):
        return []
    names = []
    for match in re.finditer(
        r"\(\s*[a-z]\s*\)\s*(?P<name>.*?)(?=\s*\(\s*[a-z]\s*\)|$)",
        cleaned,
        flags=re.IGNORECASE,
    ):
        name = _clean_segment_name(match.group("name"))
        name = re.sub(r"^(segment\s+revenue|segment\s+results?|segment\s+assets?|segment\s+liabilities?)\s*", "", name, flags=re.IGNORECASE).strip()
        if name:
            names.append(name)
    return names


def _numbers_from_segment_cell(value: str) -> list[str]:
    """Extract one or more numeric values from a combined OCR segment cell."""

    text = str(value or "").replace(",", "")
    return re.findall(r"\(?-?\d+(?:\.\d+)?\)?|-", text)


def _segment_value_start(row: list[str], periods: list[str], default_value_start: int) -> int:
    """Return the first value column for a segment row."""

    if not periods:
        return default_value_start
    inferred = max(0, len(row) - len(periods))
    if inferred and (default_value_start <= 0 or inferred > default_value_start):
        return inferred
    return min(default_value_start, inferred) if inferred else default_value_start


def _segment_label_from_row(row: list[str], value_start: int) -> str:
    """Return the label cells before the first value column."""

    candidates = [_clean_label(cell) for cell in row[:value_start]]
    candidates = [cell for cell in candidates if cell and not re.fullmatch(r"\d+", cell)]
    return candidates[-1] if candidates else ""


def _segment_metric_from_label(label: str) -> str:
    """Map segment section labels to metric names."""

    key = _row_key(label)
    if "segmentrevenue" in key:
        return "Revenue"
    if "segmentresults" in key or "segmentprofit" in key:
        return "Segment Profit"
    if "segmentassets" in key:
        return "Segment Assets"
    if "segmentliabilities" in key:
        return "Segment Liabilities"
    return ""


def _segment_output_label(metric: str, label: str) -> str:
    """Return a non-ambiguous segment row label."""

    cleaned = _clean_label(label)
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if metric and re.match(r"^\(\s*[a-z]\s*\)\s+", cleaned, flags=re.IGNORECASE):
        return f"{_clean_segment_name(cleaned)} - {metric}"
    if metric and lower == "total":
        return f"Total - {metric}"
    if metric and "unallocated corporate" in lower:
        return f"{_clean_segment_name(cleaned)} - {metric}"
    if metric and ("total revenue" in lower or "inter-segment" in lower):
        return f"{cleaned} - {metric}"
    if metric and not _is_segment_reconciliation_label(cleaned):
        return f"{_clean_segment_name(cleaned)} - {metric}"
    return cleaned


def _clean_segment_name(label: str) -> str:
    """Remove segment lettering while preserving the business name."""

    return re.sub(r"^\(\s*[a-z]\s*\)\s+", "", _clean_label(label), flags=re.IGNORECASE)


def _is_segment_reconciliation_label(label: str) -> bool:
    """Return whether a segment row is a reconciliation metric, not a segment."""

    key = _row_key(label)
    reconciliation_terms = [
        "interest",
        "financecost",
        "otherunallocable",
        "profitbeforetax",
        "profitlossbefore",
        "shareinprofit",
        "tax",
        "intersegment",
        "totalrevenue",
    ]
    return any(term in key for term in reconciliation_terms)


def _financial_row_label_and_value_start(
    row: list[str],
    label_index: int,
    value_start: int,
) -> tuple[int, int]:
    """Recover per-row label/value positions when OCR rowspans shift cells."""

    candidates = [label_index]
    if label_index > 0:
        candidates.append(label_index - 1)
    candidates.extend([0, 1, 2])
    seen: set[int] = set()
    for index in candidates:
        if index in seen or index < 0 or index >= len(row):
            continue
        seen.add(index)
        raw = str(row[index] or "").strip()
        if not raw:
            continue
        label = _standard_financial_label(raw)
        if not label or _is_noise_financial_label(label):
            continue
        if _label_is_mostly_numeric(label):
            continue
        if not _label_has_meaningful_text(label):
            continue
        start = value_start if index == label_index else index + 1
        return index, start
    return -1, value_start


def _financial_header(rows: list[list[str]]) -> tuple[int, list[str], int, int]:
    """Find the financial result date header and infer label/value columns."""

    for index, row in enumerate(rows[:8]):
        row = _date_row_with_years(row, rows[index + 1 : index + 4])
        period_cells = [
            (cell_index, cell, _period_from_date_header(cell, "quarter"))
            for cell_index, cell in enumerate(row)
        ]
        period_cells = [(cell_index, cell, period) for cell_index, cell, period in period_cells if period]
        periods = [period for _cell_index, _cell, period in period_cells]
        period_count = len(periods)
        if period_count >= 3:
            label_index = _particulars_column_index(rows[: index + 1])
            if label_index >= 0:
                value_start = label_index + 1
            else:
                value_start = max(0, len(row) - len(periods))
                label_index = 1 if value_start >= 2 else 0
            periods, value_start = _select_financial_period_block(
                rows[: index + 1],
                period_cells,
                label_index,
                value_start,
            )
            periods = _repair_quarter_period_sequence(periods)
            return index, periods, label_index, value_start
    return 0, [], 0, 0


def _date_row_with_years(row: list[str], following_rows: list[list[str]]) -> list[str]:
    """Combine split date rows such as '31 March' plus a later '2026' row."""

    existing = [_period_from_date_header(cell, "quarter") for cell in row]
    if sum(1 for period in existing if period) >= 3:
        return row
    month_day_count = sum(1 for cell in row if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s]+\d{1,2}|\d{1,2}[-\s]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", str(cell), flags=re.IGNORECASE))
    if month_day_count < 3:
        return row
    for year_row in following_rows:
        years = [str(cell or "").strip() for cell in year_row]
        year_count = sum(1 for cell in years if re.fullmatch(r"\d{2,4}", cell))
        if year_count < 3:
            continue
        if len(years) == len(row):
            return [f"{cell} {years[index]}".strip() for index, cell in enumerate(row)]
        if len(row) == len(years) + 1:
            return [row[0]] + [f"{cell} {years[index]}".strip() for index, cell in enumerate(row[1:])]
    return row


def _select_financial_period_block(
    header_rows: list[list[str]],
    period_cells: list[tuple[int, str, str]],
    label_index: int,
    value_start: int,
) -> tuple[list[str], int]:
    """Choose consolidated columns from wide standalone/consolidated tables."""

    periods = _periods_from_cells(period_cells)
    if not periods:
        return [], value_start
    header_text = " ".join(" ".join(row) for row in header_rows).lower()
    if "half year" in header_text or "half-year" in header_text or "half yearly" in header_text:
        periods = _periods_from_cells(period_cells, half_year=True)
        return _dedupe_periods(periods), value_start
    if "standalone" in header_text and "consolidated" in header_text and len(period_cells) >= 6:
        block_size = len(period_cells) // 2
        consolidated_cells = period_cells[-block_size:]
        return _periods_from_cells(consolidated_cells), label_index + 1 + block_size
    if len(period_cells) == 3:
        periods.extend(_fy_periods_from_nearby_header(header_rows))
        return _dedupe_periods(periods), value_start
    return _repair_duplicate_fy_sequence(periods), value_start


def _periods_from_cells(period_cells: list[tuple[int, str, str]], *, half_year: bool = False) -> list[str]:
    """Return Q/FY period labels for a contiguous financial table block."""

    if not period_cells:
        return []
    if len(period_cells) >= 5:
        output: list[str] = []
        for offset, (_index, cell, quarter_period) in enumerate(period_cells):
            if offset >= len(period_cells) - 2:
                fy_period = _period_from_date_header(cell, "fy")
                output.append(fy_period or quarter_period)
            elif half_year:
                output.append(_period_from_date_header(cell, "half") or quarter_period)
            else:
                output.append(quarter_period)
        return [period for period in output if period]
    if half_year:
        output = []
        for _index, cell, quarter_period in period_cells:
            period = _period_from_date_header(cell, "half") or quarter_period
            if period:
                output.append(period)
        return output
    return [period for _index, _cell, period in period_cells if period]


def _repair_quarter_period_sequence(periods: list[str]) -> list[str]:
    """Repair obvious OCR year typos in quarter labels using the latest period."""

    if not periods:
        return periods
    periods = list(periods)
    parsed_quarters: list[tuple[int, int] | None] = []
    fy_years: list[int] = []
    for period in periods:
        quarter_match = re.match(r"Q(?P<quarter>[1-4])\s+FY(?P<year>\d{2})", period, flags=re.IGNORECASE)
        parsed_quarters.append((int(quarter_match.group("quarter")), int(quarter_match.group("year"))) if quarter_match else None)
        fy_match = re.match(r"FY(?P<year>\d{2})", period, flags=re.IGNORECASE)
        if fy_match:
            fy_years.append(int(fy_match.group("year")))

    # Common OCR issue in Q4 tables: the current quarter date is read as the
    # previous year's March date, or the prior-year quarter is read as current.
    # Use the adjacent Q3 and FY columns to repair only the obvious Q4 pattern.
    if len(parsed_quarters) >= 3:
        first, second, third = parsed_quarters[0], parsed_quarters[1], parsed_quarters[2]
        if first and second and third and first[0] == 4 and second[0] == 3 and third[0] == 4:
            current_year = max(fy_years) if fy_years else second[1]
            previous_year = current_year - 1
            if first[1] == previous_year and third[1] == previous_year:
                periods[0] = f"Q4 FY{current_year:02d}"
                parsed_quarters[0] = (4, current_year)
            elif first[1] == current_year and third[1] == current_year:
                periods[2] = f"Q4 FY{previous_year:02d}"
                parsed_quarters[2] = (4, previous_year)

    match = re.match(r"Q(?P<quarter>[1-4])\s+FY(?P<year>\d{2})", periods[0], flags=re.IGNORECASE)
    if not match:
        return periods
    latest_quarter = int(match.group("quarter"))
    latest_year = int(match.group("year"))
    repaired: list[str] = []
    for index, period in enumerate(periods):
        current = re.match(r"Q(?P<quarter>[1-4])\s+FY(?P<year>\d{2})", period, flags=re.IGNORECASE)
        if current and index <= 2:
            quarter = int(current.group("quarter"))
            year = int(current.group("year"))
            if quarter < latest_quarter and year > latest_year:
                repaired.append(f"Q{quarter} FY{latest_year:02d}")
                continue
        repaired.append(period)
    return _repair_duplicate_fy_sequence(repaired)


def _repair_duplicate_fy_sequence(periods: list[str]) -> list[str]:
    """Repair Q4/FY tables where OCR duplicated the prior FY year header."""

    if len(periods) == 2:
        first = _parse_simple_period(periods[0])
        second = _parse_simple_period(periods[1])
        if first and second and first == second and first[0] == "FY":
            if first[1] >= 26:
                return [periods[0], f"FY{first[1] - 1:02d}"]
            return [f"FY{first[1] + 1:02d}", periods[1]]
        return periods
    if len(periods) < 5:
        return periods
    periods = list(periods)
    parsed = [_parse_simple_period(period) for period in periods]
    if (
        parsed[0]
        and parsed[0][0] == "Q4"
        and parsed[-1]
        and parsed[-2]
        and parsed[-1][0] == parsed[-2][0] == "FY"
        and parsed[-1][1] == parsed[-2][1]
    ):
        periods[-2] = f"FY{parsed[0][1]:02d}"
    return periods


def _particulars_column_index(rows: list[list[str]]) -> int:
    """Return the header column that contains Particulars, if visible."""

    for row in rows:
        for index, cell in enumerate(row):
            if "particular" in str(cell or "").lower():
                return index
    return -1


def _fy_periods_from_nearby_header(rows: list[list[str]]) -> list[str]:
    """Infer FY columns from nearby year-ended header cells."""

    periods: list[str] = []
    for row in rows:
        for cell in row:
            text = str(cell or "")
            if "year" not in text.lower():
                continue
            period = _period_from_date_header(text, "fy")
            if period:
                periods.append(period)
    return periods


def _dedupe_periods(periods: list[str]) -> list[str]:
    """Return periods in order without duplicates."""

    output: list[str] = []
    seen: set[str] = set()
    for period in periods:
        if period and period not in seen:
            seen.add(period)
            output.append(period)
    return output


def _fy_header(rows: list[list[str]]) -> tuple[int, list[str], int, int]:
    """Find FY/as-at headers for balance sheet and cash-flow tables."""

    for index, row in enumerate(rows[:6]):
        period_cells = [
            (cell_index, _period_from_date_header(cell, "fy"))
            for cell_index, cell in enumerate(row)
        ]
        period_cells = [(cell_index, period) for cell_index, period in period_cells if period]
        periods = [period for _cell_index, period in period_cells]
        if len(periods) >= 2:
            label_index = _particulars_column_index(rows[: index + 1])
            if label_index < 0:
                label_index = 0
            value_start = period_cells[0][0]
            if value_start <= label_index:
                value_start = label_index + 1
            return index, _dedupe_periods(_repair_duplicate_fy_sequence(periods)), label_index, value_start
    return 0, [], 0, 0


def _period_from_date_header(value: str, kind_hint: str) -> str:
    """Map table date headers to Q/H/FY labels."""

    text = value.replace(".", "-").replace("/", "-").replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if kind_hint == "fy":
        range_match = re.fullmatch(r"(?:20)?\d{2}\s*[-–]\s*(?P<end>\d{2})", text)
        if range_match:
            return f"FY{int(range_match.group('end')):02d}"
        bare_year = re.fullmatch(r"(?:20)?(?P<year>\d{2})", text)
        if bare_year:
            return f"FY{int(bare_year.group('year')):02d}"
    match = re.search(
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s-]+(?P<month>[A-Za-z]{3,9}|\d{1,2})[\s-]+(?P<year>\d{2,4})",
        text,
        re.IGNORECASE,
    )
    if not match:
        month_first = re.search(
            r"(?P<month>[A-Za-z]{3,9})[\s-]+(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s-]+(?P<year>\d{2,4})",
            text,
            re.IGNORECASE,
        )
        if month_first:
            match = month_first
    if not match:
        month_year = re.search(
            r"\b(?P<month>[A-Za-z]{3,9})\s*-?\s*(?P<year>\d{2,4})\b",
            text,
            re.IGNORECASE,
        )
        if month_year:
            match = month_year
    if not match:
        return ""
    month_text = match.group("month")
    month_map = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    if month_text.isdigit():
        month = int(month_text)
    else:
        month = month_map.get(month_text[:3].lower(), 0)
    year = int(match.group("year"))
    if year >= 2000:
        year %= 100
    if kind_hint == "fy":
        return f"FY{year:02d}"
    fiscal_year = year if month == 3 else year + 1
    if kind_hint == "half":
        if month == 9:
            return f"H1 FY{fiscal_year:02d}"
        if month == 3:
            return f"H2 FY{fiscal_year:02d}"
    quarter = {3: "Q4", 12: "Q3", 9: "Q2", 6: "Q1"}.get(month)
    return f"{quarter} FY{fiscal_year:02d}" if quarter else f"FY{year:02d}"


def _parse_simple_period(period: str) -> tuple[str, int] | None:
    match = re.search(r"\b(?:(Q[1-4]|H[12])\s*FY?|FY)\s*(\d{2,4})\b", str(period or ""), flags=re.IGNORECASE)
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
    return "FY", year


def _money_scale(text: str) -> float:
    """Return multiplier to convert table values to Rs in Cr."""

    if re.search(
        r"(?:amounts?\s*(?:are\s+)?in\s*(?:rs\.?|inr|rupees?|₹)?\s*['‘’]?\s*0{3}\b|"
        r"(?:rs\.?|inr|rupees?|₹)\s*(?:in\s*)?['‘’]?\s*0{3}\b|"
        r"\b(?:amounts?|figures?|values?)\s*(?:are\s+)?in\s+(?:rs\.?|inr|rupees?|₹)?\s*thousands?\b|"
        r"\b(?:rs\.?|inr|rupees?|₹)\s+in\s+thousands?\b|"
        r"['‘’]\s*000\b)",
        text,
        flags=re.IGNORECASE,
    ):
        return 0.0001
    if re.search(r"\b(amount\s+in\s+inr\s+lacs|rs\.?\s*in\s*lacs?|rs\.?\s*in\s*lakhs?|lacs|lakhs|lakh)\b", text, flags=re.IGNORECASE):
        return 0.01
    if re.search(
        r"\b(?:all\s+)?amounts?\s+(?:are\s+)?in\s+(?!usd\b)(?:inr|rs\.?|rupees?|[^\s]{1,8})\s*millions?\b",
        text,
        flags=re.IGNORECASE,
    ) or re.search(r"\b(?:inr|rs\.?|rupees?)\s+(?:in\s+)?millions?\b", text, flags=re.IGNORECASE):
        return 0.1
    return 1.0


def _row_values(row: list[str], periods: list[str], value_start: int, label: str, scale: float) -> dict[str, str]:
    """Extract period/value pairs from a row."""

    values: dict[str, str] = {}
    for offset, period in enumerate(periods):
        cell_index = value_start + offset
        if cell_index >= len(row):
            continue
        value = _financial_value(row[cell_index], label, scale)
        if value is not None:
            values[period] = value
    return values


def _value_start_for_row(row: list[str], periods: list[str], label_index: int, default_value_start: int) -> int:
    """Infer value start for rows where OCR dropped repeated blank/Sr No cells."""

    if not periods:
        return default_value_start
    if default_value_start + len(periods) <= len(row):
        default_cells = row[default_value_start : default_value_start + len(periods)]
        if any(_financial_value(cell, "", 1.0) is not None for cell in default_cells):
            return default_value_start
    compact_start = max(label_index + 1, len(row) - len(periods))
    if compact_start + len(periods) <= len(row):
        return compact_start
    if default_value_start + len(periods) <= len(row):
        return default_value_start
    return max(label_index + 1, default_value_start)


def _financial_value(value: str, label: str, scale: float) -> str | None:
    """Normalize one table value, converting monetary values to crores."""

    text = str(value or "").strip()
    if not text or text.lower() in {"-", "nil", "na", "n/a"}:
        return None
    if "%" in text:
        return text
    negative = bool(re.match(r"^\(.*\)$", text))
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if negative:
        number = -abs(number)
    if "eps" not in label.lower() and "earning" not in label.lower():
        number *= scale
    return _format_number(number)


def _repair_revenue_from_total_income(rows: list[dict[str, Any]]) -> None:
    """Repair OCR-misaligned revenue using Total Income minus Other Income."""

    revenue = _row_by_key(rows, "revenue")
    total_income = _row_by_key(rows, "totalincome")
    other_income = _row_by_key(rows, "otherincome")
    if not revenue or not total_income or not other_income:
        return
    revenue_values = revenue.setdefault("values", {})
    total_values = total_income.get("values") if isinstance(total_income.get("values"), dict) else {}
    other_values = other_income.get("values") if isinstance(other_income.get("values"), dict) else {}
    for period, total_text in total_values.items():
        total = _to_float(total_text)
        other = _to_float(other_values.get(period))
        if total is None or other is None:
            continue
        calculated = total - other
        current = _to_float(revenue_values.get(period))
        if current is None or abs(current - calculated) > max(0.05, abs(calculated) * 0.05):
            revenue_values[period] = _format_number(calculated)


def _row_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for row in rows:
        if _row_key(str(row.get("label") or "")) == key:
            return row
    return None


def _to_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text.strip("()").replace(",", ""))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -abs(number) if negative else number


def _format_number(number: float) -> str:
    """Format numbers compactly for display."""

    text = f"{number:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _standard_financial_label(label: str) -> str:
    """Map common OCR table labels to target labels."""

    cleaned = _clean_label(label)
    key = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    lower = cleaned.lower()
    if (
        ("current tax" in lower or "deferred tax" in lower or "tax expense relating" in lower)
        and not re.fullmatch(r"(?:total\s+)?tax\s+expenses?:?", lower.strip())
    ):
        return cleaned
    if re.fullmatch(r"(?:total\s+)?tax\s+expenses?:?", lower.strip()):
        return "Total tax expense"
    if re.search(r"\bprofit\s*/?\s*\(?loss\)?\s*before\s+exceptional|\bprofit\s+before\s+exceptional", lower):
        return "Profit before exceptional items, Other Income"
    if re.search(r"\bprofit\s*/?\s*\(?loss\)?\s*after\s+tax\b|\bprofit\s+after\s+tax\b|\bnet\s+profit\s+for\s+the\s+period\b", lower):
        return "PAT"
    if re.search(r"\bprofit\s*/?\s*\(?loss\)?\s*before\s+tax\b|\bprofit\s+before\s+tax\b", lower) and "share of" not in lower:
        return "Profit Before Tax"
    mappings = [
        ("revenuefromoperation", "Revenue"),
        ("revenuefromoperations", "Revenue"),
        ("income from operations", "Revenue"),
        ("totalincome", "Total Income"),
        ("othernonoperationincomes", "Other Income"),
        ("otherincome", "Other Income"),
        ("costofmaterialconsumed", "Cost of materials consumed"),
        ("costofmaterialsconsumed", "Cost of materials consumed"),
        ("purchaseofstockintrade", "Purchase of stock-in-trade"),
        ("changeininventories", "Change in inventory"),
        ("employeebenefit", "Employee Benefit Expense"),
        ("otherexpenses", "Other expenses"),
        ("totalexpenses", "Total Expenses"),
        ("exceptionalitems", "Exceptional items"),
        ("taxexpense", "Total tax expense"),
        ("taxexpenses", "Total tax expense"),
        ("financecost", "Finance Cost"),
        ("financecosts", "Finance Cost"),
        ("depreciation", "Depreciation"),
        ("profitbeforetax", "Profit Before Tax"),
        ("profitlossbeforetax", "Profit Before Tax"),
        ("profitfortheperiod", "PAT"),
        ("basic", "EPS (Basic)"),
        ("diluted", "EPS (Diluted)"),
    ]
    for needle, target in mappings:
        if needle in key or needle in cleaned.lower():
            return target
    return cleaned


def _standard_cash_flow_label(label: str) -> str:
    """Map cash-flow table labels to key variable labels."""

    cleaned = _clean_label(label)
    lower = cleaned.lower()
    if "net cash" not in lower:
        return ""
    if "operating" in lower:
        return "Net cash inflow (outflow) from operating activities"
    if "investing" in lower:
        return "Net cash inflow (outflow) from investing activities"
    if "financing" in lower:
        return "Net cash inflow (outflow) from financing activities"
    return cleaned


def _clean_label(label: str) -> str:
    """Clean row labels extracted from OCR tables."""

    text = re.sub(r"\s+", " ", str(label or "").replace("\xa0", " ")).strip()
    text = re.sub(r"^[0-9]+[.)]?\s*", "", text)
    text = re.sub(r"^[a-z]\)\s*", "", text, flags=re.IGNORECASE)
    return text.strip(":- ")


def _is_expense_section(label: str) -> bool:
    """Return whether a row is the Expenses section header."""

    return _clean_label(label).lower() == "expenses"


def _is_noise_financial_label(label: str) -> bool:
    """Return whether a label is not a financial metric row."""

    lower = _clean_label(label).lower()
    compact = re.sub(r"[^a-z0-9]+", "", lower)
    return lower in {
        "",
        "s.no",
        "sr. no",
        "particulars",
        "quarter ended on",
        "year ended on",
        "audited",
        "unaudited",
    } or compact in {
        "audited",
        "unaudited",
        "i",
        "ii",
        "iii",
        "iv",
        "v",
        "vi",
        "vii",
        "viii",
        "ix",
        "x",
        "xi",
        "xii",
        "xiii",
        "xiv",
        "xv",
        "xvi",
        "xvii",
        "xviii",
        "xix",
        "xx",
        "xxi",
        "xxii",
    } or (
        "refer note" in lower
        and not any(term in compact for term in ("exceptional", "taxexpense", "taxexpenses", "profitbeforetax"))
    ) or lower.startswith("statement of ")


def _is_statement_section(label: str) -> bool:
    """Return whether a row should be rendered as a variable section."""

    lower = _clean_label(label).lower()
    return lower in {
        "assets",
        "liabilities",
        "equity and liabilities",
        "equity",
        "non-current assets",
        "current assets",
        "non-current liabilities",
        "current liabilities",
    }


def _section_title(label: str) -> str:
    """Normalize variable section names."""

    lower = _clean_label(label).lower()
    if "asset" in lower:
        return "Assets"
    if "liabilit" in lower or "equity" in lower:
        return "Liabilities"
    return _clean_label(label).title()


def _dedupe_rows_by_label(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate rows by label while preserving order."""

    output: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row.get("label") or "")
        key = _row_key(label)
        if key in by_label:
            by_label[key].setdefault("values", {}).update(row.get("values") or {})
            continue
        by_label[key] = row
        output.append(row)
    return output


def _select_best_table_payload(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer consolidated financial tables, otherwise the table with most values."""

    if not candidates:
        return {}
    selected = sorted(
        candidates,
        key=lambda item: (
            "consolidated" in str(item.get("context") or "").lower(),
            sum(len(row.get("values") or {}) for row in item.get("financial_rows", [])),
        ),
        reverse=True,
    )[0]
    return _merge_financial_candidate_rows(selected, candidates)


def _merge_financial_candidate_rows(
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge continuation rows, especially split EPS rows, into the selected P&L."""

    merged = dict(selected)
    rows = _normalize_rows(merged.get("financial_rows"))
    by_key: dict[str, dict[str, Any]] = {_row_key(str(row.get("label") or "")): row for row in rows}
    selected_context = str(selected.get("context") or "").lower()
    selected_is_consolidated = "consolidated" in selected_context
    for candidate in candidates:
        candidate_context = str(candidate.get("context") or "").lower()
        if candidate is selected:
            continue
        if selected_is_consolidated and "standalone" in candidate_context and "consolidated" not in candidate_context:
            continue
        if candidate.get("result_period") and selected.get("result_period"):
            if candidate.get("result_period") != selected.get("result_period"):
                continue
        for candidate_row in _normalize_rows(candidate.get("financial_rows")):
            label = str(candidate_row.get("label") or "")
            key = _row_key(label)
            if not key:
                continue
            if key in by_key:
                by_key[key].setdefault("values", {}).update(candidate_row.get("values") or {})
                continue
            if "eps" in key or "earning" in key or candidate.get("continuation") == "eps":
                by_key[key] = candidate_row
                rows.append(candidate_row)
    merged["financial_rows"] = _dedupe_rows_by_label(rows)
    return merged


def _select_best_variable_sections(candidates: list[Any]) -> list[dict[str, Any]]:
    """Prefer consolidated balance-sheet tables, otherwise richest sections."""

    if not candidates:
        return []
    normalized_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            sections = candidate.get("sections") if isinstance(candidate.get("sections"), list) else []
            context = str(candidate.get("context") or "")
        else:
            sections = candidate if isinstance(candidate, list) else []
            context = ""
        normalized_candidates.append({"sections": sections, "context": context})
    selected = sorted(
        normalized_candidates,
        key=lambda item: (
            "consolidated" in item["context"].lower(),
            sum(len(section.get("rows") or []) for section in item["sections"]),
        ),
        reverse=True,
    )[0]
    return selected["sections"]


def _select_cash_flow_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer consolidated cash-flow rows and avoid mixing statement bases."""

    if not candidates:
        return []
    consolidated = [
        candidate for candidate in candidates if "consolidated" in str(candidate.get("context") or "").lower()
    ]
    standalone = [
        candidate
        for candidate in candidates
        if "standalone" in str(candidate.get("context") or "").lower()
        and "consolidated" not in str(candidate.get("context") or "").lower()
    ]
    selected = consolidated or standalone or candidates
    rows: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}
    for candidate in selected:
        for row in _normalize_rows(candidate.get("rows")):
            key = _row_key(str(row.get("label") or ""))
            if not key:
                continue
            if key in by_label:
                by_label[key].setdefault("values", {}).update(row.get("values") or {})
                continue
            by_label[key] = row
            rows.append(row)
    return rows


def _select_segment_tables(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer consolidated segment tables and drop standalone duplicates."""

    if not candidates:
        return []
    consolidated = [
        table for table in candidates if "consolidated" in str(table.get("context") or "").lower()
    ]
    selected = consolidated or candidates
    return [
        {
            "title": table.get("title") or "Segment Wise",
            "rows": table.get("rows") or [],
            "context": table.get("context") or "",
        }
        for table in selected
        if table.get("rows")
    ]


def _statement_basis_from_contexts(contexts: list[str], financial_table_count: int = 0) -> str:
    """Return consolidated, standalone, or blank from OCR contexts."""

    joined = " ".join(contexts).lower()
    if "consolidated" in joined:
        return "consolidated"
    if "standalone" in joined or "audited standalone" in joined:
        return "standalone"
    return ""


def _data_confidence(result: dict[str, Any]) -> float:
    """Compute automated confidence from normalized extraction richness."""

    rows = _normalize_rows(result.get("financial_rows"))
    value_count = sum(len(row.get("values") or {}) for row in rows)
    if value_count >= 20:
        return 0.98
    if value_count >= 8:
        return 0.96
    if has_mistral_financial_data(result):
        return 0.95
    return 0.0


def _response_text(response: Any) -> str:
    """Extract text content from common Mistral SDK response shapes."""

    try:
        choices = getattr(response, "choices", None) or response.get("choices")  # type: ignore[union-attr]
        first = choices[0]
        message = getattr(first, "message", None) or first.get("message")
        content = getattr(message, "content", None) or message.get("content")
    except Exception as exc:
        raise ValueError(f"Could not read Mistral response content: {exc}") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or getattr(item, "content", "") or ""))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _extract_json_payload(text: str) -> Any:
    """Extract and decode the first JSON object from model text."""

    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(?P<body>\{.*?\})\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        cleaned = fence.group("body").strip()
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _normalize_confidence(value: Any) -> float:
    """Normalize confidence values to 0.0-1.0."""

    try:
        number = float(str(value).replace("%", "").strip())
    except Exception:
        return 0.0
    if number > 1:
        number = number / 100
    return max(0.0, min(1.0, number))


def _normalize_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize row-like payloads to [{'label', 'type', 'values', 'changes'}]."""

    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        iterable = [{"label": key, "values": val} for key, val in value.items()]
    elif isinstance(value, list):
        iterable = value
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or item.get("metric") or "").strip()
        if not label:
            continue
        raw_values = item.get("values") or item.get("periods") or {}
        values = _normalize_values(raw_values)
        row_type = str(item.get("type") or ("section" if not any(values.values()) else "data")).lower()
        rows.append(
            {
                "label": label,
                "type": "section" if row_type == "section" else "data",
                "values": values,
                "changes": _normalize_values(item.get("changes") or {}),
            }
        )
    return rows


def _normalize_segment_tables(value: Any) -> list[dict[str, Any]]:
    """Normalize segment table payloads."""

    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    tables: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rows = _prune_empty_rows(_normalize_rows(item.get("rows") or item.get("data")))
        rows = _valid_segment_rows(rows)
        if rows:
            tables.append({"title": str(item.get("title") or "Segment Wise"), "rows": rows})
    return tables


def _valid_financial_rows(rows: Any) -> list[dict[str, Any]]:
    """Return main financial rows only when row labels look trustworthy."""

    normalized = _prune_empty_rows(_normalize_rows(rows))
    trusted = _drop_bad_label_rows(normalized, "financial result")
    data_rows = [row for row in trusted if _row_has_value(row)]
    if not data_rows:
        return []
    if _has_repeated_value_vector_artifact(data_rows):
        logging.warning("Dropping financial result table because repeated identical value rows indicate OCR/prompt leakage.")
        return []
    numeric_label_count = sum(1 for row in data_rows if _label_is_mostly_numeric(str(row.get("label") or "")))
    if numeric_label_count / max(len(data_rows), 1) >= 0.35:
        logging.warning(
            "Dropping financial result table because %s/%s row labels are numeric, indicating OCR column misalignment.",
            numeric_label_count,
            len(data_rows),
        )
        return []
    meaningful_text_count = sum(1 for row in data_rows if _label_has_meaningful_text(str(row.get("label") or "")))
    if meaningful_text_count < max(1, min(2, len(data_rows))):
        logging.warning("Dropping financial result table because row labels do not contain enough textual metric names.")
        return []
    return trusted


def _has_repeated_value_vector_artifact(rows: list[dict[str, Any]]) -> bool:
    """Return true when many different labels carry the exact same values."""

    vector_counts: dict[tuple[tuple[str, str], ...], int] = {}
    for row in rows:
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        normalized_values = tuple(
            sorted(
                (str(period), re.sub(r"\s+", "", str(value)))
                for period, value in values.items()
                if str(value).strip() and str(value).strip() not in {"0", "0.0", "0.00", "-"}
            )
        )
        if len(normalized_values) < 2:
            continue
        vector_counts[normalized_values] = vector_counts.get(normalized_values, 0) + 1
    if not vector_counts:
        return False
    repeated = max(vector_counts.values())
    return repeated >= 4 and repeated / max(len(rows), 1) >= 0.35


def _valid_segment_rows(rows: Any) -> list[dict[str, Any]]:
    """Return segment rows only when row labels look textual enough to trust."""

    normalized = _prune_empty_rows(_normalize_rows(rows))
    trusted = _drop_bad_label_rows(normalized, "segment")
    data_rows = [row for row in trusted if _row_has_value(row)]
    if not data_rows:
        return []
    numeric_label_count = sum(1 for row in data_rows if _label_is_mostly_numeric(str(row.get("label") or "")))
    if numeric_label_count / max(len(data_rows), 1) >= 0.4:
        logging.warning(
            "Dropping segment table because %s/%s row labels are numeric, indicating OCR column misalignment.",
            numeric_label_count,
            len(data_rows),
        )
        return []
    meaningful_text_count = sum(1 for row in data_rows if _label_has_meaningful_text(str(row.get("label") or "")))
    if meaningful_text_count < max(1, min(2, len(data_rows))):
        logging.warning("Dropping segment table because row labels do not contain enough textual segment names.")
        return []
    return trusted


def _drop_bad_label_rows(rows: list[dict[str, Any]], table_name: str) -> list[dict[str, Any]]:
    """Drop individual rows whose labels are OCR-shifted numeric values or markers."""

    trusted: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if row.get("type") == "section" and not _row_has_value(row):
            trusted.append(row)
            continue
        label = str(row.get("label") or "")
        if _label_is_mostly_numeric(label) or _label_is_roman_only(label) or not _label_has_meaningful_text(label):
            dropped += 1
            continue
        trusted.append(row)
    if dropped:
        logging.warning("Dropped %s %s row(s) with unusable OCR labels.", dropped, table_name)
    return trusted


def _label_is_mostly_numeric(label: str) -> bool:
    """Return whether a row label is likely a misplaced numeric value."""

    text = label.strip()
    if not text:
        return True
    compact = re.sub(r"[\s,().₹$%-]", "", text)
    if not compact:
        return True
    digit_count = sum(ch.isdigit() for ch in compact)
    alpha_count = sum(ch.isalpha() for ch in compact)
    return digit_count > 0 and alpha_count == 0


def _label_has_meaningful_text(label: str) -> bool:
    """Return whether a label contains enough letters to be a real row name."""

    letters = re.findall(r"[A-Za-z]", label)
    return len(letters) >= 3


def _label_is_roman_only(label: str) -> bool:
    """Return whether a label is only a roman numeral marker."""

    compact = re.sub(r"[^A-Za-z]", "", label).upper()
    return bool(compact) and bool(re.fullmatch(r"[IVXLCDM]+", compact))


def _normalize_variable_sections(value: Any) -> list[dict[str, Any]]:
    """Normalize balance-sheet variable sections."""

    if isinstance(value, dict):
        value = [{"section": key, "rows": rows} for key, rows in value.items()]
    if not isinstance(value, list):
        return []
    sections: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rows = _prune_empty_rows(_normalize_rows(item.get("rows") or item.get("data")))
        if rows:
            sections.append({"section": str(item.get("section") or item.get("label") or "Variables"), "rows": rows})
    return sections


def _prune_empty_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop label-only rows unless the same table has at least one real value."""

    if not any(_row_has_value(row) for row in rows):
        return []
    pruned: list[dict[str, Any]] = []
    for row in rows:
        if _row_has_value(row) or row.get("type") == "section":
            pruned.append(row)
    return pruned


def _had_label_rows(value: Any) -> bool:
    """Return whether a raw Mistral payload included row-like labels."""

    if isinstance(value, dict):
        return bool(value)
    if not isinstance(value, list):
        return False
    for item in value:
        if isinstance(item, dict) and str(item.get("label") or item.get("name") or item.get("metric") or "").strip():
            return True
    return False


def _normalize_values(values: Any) -> dict[str, str]:
    """Normalize value maps and drop null-like entries."""

    if not isinstance(values, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "na", "n/a", "-"}:
            continue
        normalized[_normalize_period_label(str(key))] = _format_value(text)
    return normalized


def _normalize_period_label(value: str) -> str:
    """Clean common period/header strings."""

    text = re.sub(r"\s+", " ", value.replace("\n", " ")).strip()
    text = re.sub(r"(?i)\bquarter\s+ended\b", "Q", text)
    return text


def _format_value(value: str) -> str:
    """Clean value text while preserving percent signs."""

    text = value.replace("Rs.", "").replace("INR", "")
    text = re.sub(r"\b(crores|crore|crs|cr\.?|lakhs|lacs)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sort_main_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort standard financial rows into the target display order."""

    order = {_row_key(label): index for index, label in enumerate(MAIN_ROW_ORDER)}
    return sorted(rows, key=lambda row: (order.get(_row_key(str(row.get("label"))), 10_000), str(row.get("label"))))


def _row_key(label: str) -> str:
    """Normalize row labels for ordering."""

    return re.sub(r"[^a-z0-9]", "", label.lower())


def _result_display_columns(rows: list[dict[str, Any]], result_period: str) -> list[dict[str, str]]:
    """Build dynamic result table columns based on available periods."""

    periods = _available_periods(rows)
    current = _pick_current_period(periods, result_period)
    if not current:
        return [{"kind": "value", "label": period, "period": period} for period in periods]
    parsed = _parse_period(current)
    if not parsed:
        return [{"kind": "value", "label": period, "period": period} for period in periods]
    kind, year = parsed
    columns: list[dict[str, str]] = [{"kind": "value", "label": current, "period": current}]
    if kind.startswith("Q"):
        quarter = int(kind[1])
        previous_quarter = _find_period(periods, _previous_quarter_kind(quarter), _previous_quarter_year(quarter, year))
        if previous_quarter:
            columns.append({"kind": "value", "label": previous_quarter, "period": previous_quarter})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous_quarter})
        yoy_quarter = _find_period(periods, kind, year - 1)
        if yoy_quarter:
            columns.append({"kind": "value", "label": yoy_quarter, "period": yoy_quarter})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": yoy_quarter})
        if quarter in {2, 4}:
            for aggregate_kind in ("H1", "FY"):
                current_aggregate = _find_period(periods, aggregate_kind, year)
                previous_aggregate = _find_period(periods, aggregate_kind, year - 1)
                if current_aggregate:
                    columns.append({"kind": "value", "label": current_aggregate, "period": current_aggregate})
                    if previous_aggregate:
                        columns.append({"kind": "value", "label": previous_aggregate, "period": previous_aggregate})
                        columns.append({"kind": "change", "label": "Change (in %)", "from": current_aggregate, "to": previous_aggregate})
    elif kind == "FY":
        previous_fy = _find_period(periods, "FY", year - 1)
        if previous_fy:
            columns.append({"kind": "value", "label": previous_fy, "period": previous_fy})
            columns.append({"kind": "change", "label": "Change (in %)", "from": current, "to": previous_fy})
    return columns


def _variable_display_columns(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build FY-style display columns for key variables."""

    periods = _available_periods(rows)
    fy_periods = [period for period in periods if (_parse_period(period) or ("", 0))[0] == "FY"]
    if len(fy_periods) >= 2:
        current = fy_periods[0]
        previous = fy_periods[1]
        return [
            {"kind": "value", "label": current, "period": current},
            {"kind": "value", "label": previous, "period": previous},
            {"kind": "change", "label": "Change (in %)", "from": current, "to": previous},
        ]
    return [{"kind": "value", "label": period, "period": period} for period in periods]


def _available_periods(rows: list[dict[str, Any]]) -> list[str]:
    """Return available periods sorted in display order."""

    found: set[str] = set()
    for row in rows:
        found.update(str(period) for period in row.get("values", {}).keys())
    return sorted(found, key=_period_sort_key)


def _pick_current_period(periods: list[str], result_period: str) -> str:
    """Pick the latest current period from result_period or available headers."""

    normalized_result = _normalize_period_label(result_period)
    if normalized_result in periods:
        return normalized_result
    if normalized_result:
        parsed_result = _parse_period(normalized_result)
        if parsed_result:
            kind, year = parsed_result
            match = _find_period(periods, kind, year)
            if match:
                return match
    quarter_periods = [period for period in periods if (_parse_period(period) or ("", 0))[0].startswith("Q")]
    if quarter_periods:
        return quarter_periods[0]
    return periods[0] if periods else ""


def _infer_current_period(rows: list[dict[str, Any]]) -> str:
    """Infer the latest result period from rows."""

    return _pick_current_period(_available_periods(rows), "")


def _parse_period(value: str) -> tuple[str, int] | None:
    """Parse Q/H/FY period labels."""

    match = PERIOD_RE.search(value or "")
    if not match:
        return None
    kind = (match.group("period_kind") or match.group("fy_kind")).upper()
    year_text = match.group("year")
    year = int(year_text)
    if year >= 2000:
        year = year % 100
    return kind, year


def _period_sort_key(period: str) -> tuple[int, int, int, str]:
    """Sort current periods before comparative and annual periods."""

    parsed = _parse_period(period)
    if not parsed:
        return (999, 999, 999, period)
    kind, year = parsed
    kind_rank = {"Q4": 0, "Q3": 1, "Q2": 2, "Q1": 3, "H2": 4, "H1": 5, "FY": 6}.get(kind, 99)
    return (-year, kind_rank, 0, period)


def _previous_quarter_kind(quarter: int) -> str:
    """Return previous quarter kind."""

    return "Q4" if quarter == 1 else f"Q{quarter - 1}"


def _previous_quarter_year(quarter: int, year: int) -> int:
    """Return fiscal year for previous quarter."""

    return year - 1 if quarter == 1 else year


def _find_period(periods: list[str], kind: str, year: int) -> str:
    """Find a period label by kind and fiscal year."""

    for period in periods:
        parsed = _parse_period(period)
        if parsed == (kind, year):
            return period
    return ""


def _table_messages(
    heading: str,
    unit: str,
    rows: list[dict[str, Any]],
    columns: list[dict[str, str]],
    *,
    skip_margin_changes: bool = False,
) -> list[str]:
    """Render rows into one or more Telegram-sized table messages."""

    if not rows or not columns:
        return []
    row_width = min(max(max(len(str(row.get("label", ""))) for row in rows), len(unit), 18), 34)
    col_width = max(9, min(13, max(len(col["label"]) for col in columns)))
    header = _table_header(unit, columns, row_width, col_width)
    separator = "-" * min(len(header), 120)
    prefix = f"{heading}\n```text\n{header}\n{separator}\n"
    suffix = "```"
    messages: list[str] = []
    current_lines: list[str] = []
    current_len = len(prefix) + len(suffix)
    for row in rows:
        line = _table_row(row, columns, row_width, col_width, skip_margin_changes)
        if current_lines and current_len + len(line) + 1 > TELEGRAM_LIMIT:
            messages.append(prefix + "\n".join(current_lines) + "\n" + suffix)
            current_lines = []
            current_len = len(prefix) + len(suffix)
        current_lines.append(line)
        current_len += len(line) + 1
    if current_lines:
        messages.append(prefix + "\n".join(current_lines) + "\n" + suffix)
    return messages


def _table_header(unit: str, columns: list[dict[str, str]], row_width: int, col_width: int) -> str:
    """Build a fixed-width table header."""

    labels = [col["label"] for col in columns]
    return f"{unit[:row_width]:<{row_width}} " + " ".join(label[:col_width].rjust(col_width) for label in labels)


def _table_row(
    row: dict[str, Any],
    columns: list[dict[str, str]],
    row_width: int,
    col_width: int,
    skip_margin_changes: bool,
) -> str:
    """Build a fixed-width table row."""

    label = str(row.get("label", ""))
    values = row.get("values", {}) if isinstance(row.get("values"), dict) else {}
    if row.get("type") == "section" and not any(values.values()):
        return label[:row_width].ljust(row_width)
    cells: list[str] = []
    for column in columns:
        if column["kind"] == "value":
            cell = str(values.get(column["period"], ""))
        else:
            cell = "" if skip_margin_changes and "margin" in label.lower() else _change_for_row(row, column["from"], column["to"])
        cells.append(cell[:col_width].rjust(col_width))
    return f"{label[:row_width]:<{row_width}} " + " ".join(cells)


def _change_for_row(row: dict[str, Any], current_period: str, previous_period: str) -> str:
    """Return existing or computed percentage change for a row."""

    changes = row.get("changes", {}) if isinstance(row.get("changes"), dict) else {}
    for key in (
        f"{current_period}_vs_{previous_period}",
        f"{current_period} vs {previous_period}",
        f"{current_period}|{previous_period}",
    ):
        if key in changes:
            return str(changes[key])
    values = row.get("values", {}) if isinstance(row.get("values"), dict) else {}
    current = _to_number(values.get(current_period))
    previous = _to_number(values.get(previous_period))
    if current is None or previous in (None, 0):
        return ""
    return f"{((current - previous) / abs(previous)) * 100:.2f}%"


def _to_number(value: Any) -> float | None:
    """Convert a financial display value to float when possible."""

    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _variable_messages(
    company: str,
    balance_sections: list[dict[str, Any]],
    cash_flow_rows: list[dict[str, Any]],
    key_variable_rows: list[dict[str, Any]],
) -> list[str]:
    """Render balance sheet, cash flow, and other variable sections."""

    messages: list[str] = []
    if balance_sections:
        rows: list[dict[str, Any]] = []
        for section in balance_sections:
            rows.append({"label": str(section.get("section") or "Variables"), "type": "section", "values": {}, "changes": {}})
            rows.extend(_normalize_rows(section.get("rows")))
        columns = _variable_display_columns(rows)
        messages.extend(_table_messages(f"{company} - Key Changes in Variables", "Balance Sheet Variables", rows, columns))
    if cash_flow_rows:
        columns = _variable_display_columns(cash_flow_rows)
        messages.extend(_table_messages("Cash Flow Variables", "Cash Flow Variables", cash_flow_rows, columns))
    if key_variable_rows:
        columns = _variable_display_columns(key_variable_rows)
        messages.extend(_table_messages("Key Variables", "Key Variables", key_variable_rows, columns))
    return messages


def _row_has_value(row: dict[str, Any]) -> bool:
    """Return whether a row has at least one non-empty value."""

    values = row.get("values")
    return isinstance(values, dict) and any(str(value).strip() for value in values.values())


def _dedupe_empty_messages(messages: list[str]) -> list[str]:
    """Remove empty messages while preserving order."""

    cleaned: list[str] = []
    for message in messages:
        text = message.strip()
        if text:
            cleaned.append(text[:TELEGRAM_LIMIT])
    return cleaned


def _md_escape(value: object) -> str:
    """Escape minimal Telegram Markdown-sensitive characters."""

    text = str(value or "")
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")
