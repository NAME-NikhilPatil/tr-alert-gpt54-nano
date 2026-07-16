"""Local PDF filing classification and financial complexity routing signals."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import fitz
except Exception:  # pragma: no cover - optional dependency is declared in requirements.
    fitz = None  # type: ignore[assignment]


FINANCIAL_RESULTS = "FINANCIAL_RESULTS"
BOARD_MEETING_OUTCOME = "BOARD_MEETING_OUTCOME"
WARRANT_ALLOTMENT = "WARRANT_ALLOTMENT"
CAPITAL_ACTION = "CAPITAL_ACTION"
DIVIDEND_ONLY = "DIVIDEND_ONLY"
NON_FINANCIAL_DISCLOSURE = "NON_FINANCIAL_DISCLOSURE"
UNKNOWN = "UNKNOWN"

SKIPPED_NON_FINANCIAL_DISCLOSURE = "SKIPPED_NON_FINANCIAL_DISCLOSURE"


@dataclass(slots=True)
class FilingClassification:
    """Classification result produced before financial extraction."""

    filing_type: str
    company_name: str
    reason: str
    financial_images_required: bool
    key_disclosure: dict[str, str] = field(default_factory=dict)
    confidence: str = "medium"
    text_pages_scanned: int = 0
    page_count: int = 0
    text_char_count: int = 0

    @property
    def is_financial_results(self) -> bool:
        return self.filing_type == FINANCIAL_RESULTS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def skip_report(self) -> dict[str, Any]:
        return {
            "status": SKIPPED_NON_FINANCIAL_DISCLOSURE,
            "filing_type": self.filing_type,
            "company_name": self.company_name,
            "reason": self.reason,
            "images_generated": 0,
            "financial_images_required": False,
            "key_disclosure": self.key_disclosure,
        }


@dataclass(slots=True)
class FinancialComplexity:
    """Signals used to route model/reasoning effort for financial PDFs."""

    complex_pdf: bool
    complexity_score: int
    triggers: list[str] = field(default_factory=list)
    page_count: int = 0
    image_heavy_pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_pdf_filing(pdf_path: str | Path, announcement: Any | None = None) -> FilingClassification:
    """Classify a PDF without calling an LLM or using company-specific rules."""

    path = Path(pdf_path)
    text, page_count, pages_scanned, image_heavy_pages = extract_pdf_text(path, max_pages=0)
    compact = _normalize_text(text)
    company = _company_name(path, announcement, text)

    financial_score = _financial_result_score(compact)
    if financial_score >= 2:
        return FilingClassification(
            filing_type=FINANCIAL_RESULTS,
            company_name=company,
            reason="Visible financial result table indicators found in the PDF.",
            financial_images_required=True,
            confidence="high",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    key_disclosure = _warrant_disclosure(text)
    if key_disclosure or _has_any(compact, ("convertible warrant", "warrants", "preferential allotment")):
        return FilingClassification(
            filing_type=WARRANT_ALLOTMENT,
            company_name=company,
            reason="No financial result tables found. PDF contains board meeting outcome for allotment of warrants.",
            financial_images_required=False,
            key_disclosure=key_disclosure,
            confidence="high" if key_disclosure else "medium",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    if _has_any(compact, ("rights issue", "bonus issue", "preferential issue", "qualified institutional placement", "fund raising")):
        return FilingClassification(
            filing_type=CAPITAL_ACTION,
            company_name=company,
            reason="No financial result tables found. PDF contains a capital action disclosure.",
            financial_images_required=False,
            confidence="medium",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    if _has_any(compact, ("appointment of director", "resignation of director", "change in director", "re-appointment")):
        return FilingClassification(
            filing_type=NON_FINANCIAL_DISCLOSURE,
            company_name=company,
            reason="No financial result tables found. PDF contains a director or governance disclosure.",
            financial_images_required=False,
            confidence="medium",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    if image_heavy_pages:
        return FilingClassification(
            filing_type=FINANCIAL_RESULTS,
            company_name=company,
            reason=(
                "The PDF is scanned or image-only, so local text cannot safely rule out financial results; "
                "send it to GPT vision for final classification and extraction."
            ),
            financial_images_required=True,
            confidence="low",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    if "dividend" in compact and financial_score == 0:
        return FilingClassification(
            filing_type=DIVIDEND_ONLY,
            company_name=company,
            reason="No financial result tables found. PDF appears to be a dividend-only notice.",
            financial_images_required=False,
            confidence="medium",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    if _has_any(compact, ("outcome of board meeting", "outcome of the board meeting", "board meeting")):
        return FilingClassification(
            filing_type=BOARD_MEETING_OUTCOME,
            company_name=company,
            reason="No financial result tables found. PDF contains a board meeting outcome disclosure.",
            financial_images_required=False,
            confidence="medium",
            text_pages_scanned=pages_scanned,
            page_count=page_count,
            text_char_count=len(text),
        )

    return FilingClassification(
        filing_type=UNKNOWN,
        company_name=company,
        reason="No financial result table indicators were found in local PDF text.",
        financial_images_required=False,
        confidence="low",
        text_pages_scanned=pages_scanned,
        page_count=page_count,
        text_char_count=len(text),
    )


def analyze_financial_complexity(pdf_path: str | Path, classification: FilingClassification | None = None) -> FinancialComplexity:
    """Return generic complexity signals for model/reasoning routing."""

    path = Path(pdf_path)
    text, page_count, _pages_scanned, image_heavy_pages = extract_pdf_text(path, max_pages=0)
    compact = _normalize_text(text)
    triggers: list[str] = []

    if "standalone" in compact and "consolidated" in compact:
        triggers.append("both_standalone_and_consolidated")
    if _has_any(compact, ("segment revenue", "segment results", "segment assets", "segment liabilities", "reportable segment")):
        triggers.append("segment_table_exists")
    if "exceptional item" in compact or "exceptional items" in compact:
        triggers.append("exceptional_items_exist")
    if _has_any(compact, ("share of profit", "share of loss", "joint venture", "associate")):
        triggers.append("share_of_associate_or_jv_exists")
    if "discontinued operation" in compact:
        triggers.append("discontinued_operations_exist")
    if _has_any(compact, ("restated", "reclassified", "re-grouped", "regrouped")):
        triggers.append("restated_figures_exist")
    if page_count > 20:
        triggers.append("page_count_gt_20")
    if image_heavy_pages and (classification is None or classification.is_financial_results):
        triggers.append("scanned_or_image_heavy_financial_pages")

    unique_triggers = _dedupe(triggers)
    return FinancialComplexity(
        complex_pdf=bool(unique_triggers),
        complexity_score=len(unique_triggers),
        triggers=unique_triggers,
        page_count=page_count,
        image_heavy_pages=image_heavy_pages,
    )


def build_non_financial_skip_payload(
    classification: FilingClassification,
    pdf_path: str | Path,
    announcement: Any | None = None,
) -> dict[str, Any]:
    """Return a structured extraction-shaped skip payload."""

    report = classification.skip_report()
    return {
        **report,
        "company_name": classification.company_name,
        "board_meeting_date": str(getattr(announcement, "announcement_datetime", "") or ""),
        "source": str(getattr(announcement, "source", "") or ""),
        "pdf_path": str(pdf_path),
        "pdf_name": Path(pdf_path).name,
        "statement_basis": "not_applicable",
        "currency_unit": "",
        "source_currency_unit": "",
        "result_period": "",
        "period_columns": [],
        "financial_rows": [],
        "balance_sheet_variables": [],
        "cash_flow_variables": [],
        "segment_tables": [],
        "key_variables": [],
        "confidence": 0,
        "warnings": [report["reason"]],
        "parser_message": report["reason"],
        "parser_status": SKIPPED_NON_FINANCIAL_DISCLOSURE,
        "gpt_json_status": "skipped_non_financial",
        "ocr_status": "not_used",
        "extraction_layer": "local_filing_classifier",
        "extraction_mode": "skipped_non_financial_disclosure",
        "validation_status": SKIPPED_NON_FINANCIAL_DISCLOSURE,
        "validation_allows_images": False,
        "validation_errors": [],
        "validation_warnings": [report["reason"]],
        "validation_failure_categories": [],
        "render_gate": "SKIP_NON_FINANCIAL",
        "render_blocked_sections": [],
        "renderable_sections": [],
        "financial_images_required": False,
        "filing_classification": classification.to_dict(),
        "non_financial_skip_report": report,
    }


def non_financial_skip_message(payload: dict[str, Any]) -> str:
    """Return a short Telegram-safe skip message."""

    company = str(payload.get("company_name") or "Company")
    source = str(payload.get("source") or payload.get("exchange") or "Unknown").upper()
    return "\n".join(
        [
            company,
            f"Source: {source}",
            "Financial data is not available in the PDF.",
        ]
    )


def extract_pdf_text(pdf_path: Path, *, max_pages: int = 0, max_chars: int = 220_000) -> tuple[str, int, int, list[int]]:
    """Extract embedded text and image-heavy page hints from a PDF."""

    if fitz is None or not pdf_path.exists():
        return "", 0, 0, []
    chunks: list[str] = []
    image_heavy_pages: list[int] = []
    try:
        with fitz.open(pdf_path) as document:
            page_count = int(document.page_count)
            limit = page_count if max_pages <= 0 else min(page_count, max_pages)
            for index in range(limit):
                page = document.load_page(index)
                page_text = page.get_text("text") or ""
                if len(page_text.strip()) < 120 and len(page.get_images(full=True) or []) >= 1:
                    image_heavy_pages.append(index + 1)
                if page_text:
                    chunks.append(page_text)
                if sum(len(chunk) for chunk in chunks) >= max_chars:
                    break
            return "\n".join(chunks)[:max_chars], page_count, limit, image_heavy_pages
    except Exception:
        return "", 0, 0, []


def _financial_result_score(text: str) -> int:
    indicators = (
        "financial results",
        "audited financial results",
        "unaudited financial results",
        "statement of profit and loss",
        "revenue from operations",
        "profit before tax",
        "cash flow statement",
        "balance sheet",
        "segment revenue",
        "quarter ended",
        "year ended",
    )
    return sum(1 for item in indicators if item in text)


def _warrant_disclosure(text: str) -> dict[str, str]:
    output: dict[str, str] = {}
    normalized = re.sub(r"\s+", " ", text or " ")
    warrants = re.search(
        r"(\d[\d,]*)\s*(?:\([^)]+\)\s*)?(?:fully\s+)?(?:convertible\s+)?warrants?",
        normalized,
        re.IGNORECASE,
    )
    issue_sentence = _sentence_containing(normalized, ("issue price", "issue-price"))
    upfront_sentence = _sentence_containing(normalized, ("upfront", "subscription amount"))
    received_sentence = _sentence_containing(normalized, ("amount received", "total amount received", "aggregating"))
    issue_price = re.search(
        r"(?:rs\.?|inr|₹)\s*\.?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*(?:per\s+warrant|each)?",
        issue_sentence,
        re.IGNORECASE,
    )
    if not issue_price:
        issue_price = re.search(
            r"issue\s+price.{0,160}?(?:rs\.?|inr|₹)\s*\.?\s*([0-9]+(?:\.[0-9]+)?)",
            normalized,
            re.IGNORECASE,
        )
    upfront = re.search(
        r"(?:rs\.?|inr|₹)\s*\.?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*(?:per\s+warrant|each)",
        upfront_sentence,
        re.IGNORECASE,
    )
    if not upfront:
        upfront_window = _window_after(normalized, ("upfront", "subscription amount", "25%"), size=420)
        upfront_values = re.findall(r"(?:rs\.?|inr|₹)\s*\.?\s*([0-9]+(?:\.[0-9]+)?)", upfront_window, re.IGNORECASE)
        decimal_values = [value for value in upfront_values if "." in value]
        if decimal_values:
            upfront = re.match(r"(.+)", decimal_values[0])
    received_values = re.findall(r"(?:rs\.?|inr|₹)\s*\.?\s*([0-9][0-9,]+(?:\.[0-9]+)?)", received_sentence, re.IGNORECASE)
    if not received_values:
        received_values = re.findall(r"(?:rs\.?|inr|₹)\s*\.?\s*([0-9][0-9,]{4,}(?:\.[0-9]+)?)", normalized, re.IGNORECASE)
    if warrants:
        output["number_of_warrants"] = warrants.group(1)
    if issue_price:
        output["issue_price_per_warrant"] = f"Rs. {issue_price.group(1)}"
    if upfront:
        output["upfront_amount_per_warrant"] = f"Rs. {upfront.group(1)}"
    elif issue_price and re.search(r"(?:25\s*%|twenty\s*five\s*percent)", upfront_sentence + " " + normalized, re.IGNORECASE):
        output["upfront_amount_per_warrant"] = f"Rs. {_format_rupee_amount(float(issue_price.group(1)) * 0.25)}"
    if received_values:
        output["total_amount_received"] = f"Rs. {max(received_values, key=_indian_amount_value)}"
    return output


def _sentence_containing(text: str, needles: tuple[str, ...]) -> str:
    for sentence in re.split(r"(?<=[.;:])\s+", text):
        lowered = sentence.lower()
        if any(needle in lowered for needle in needles):
            return sentence
    return text


def _window_after(text: str, needles: tuple[str, ...], *, size: int) -> str:
    lowered = text.lower()
    positions = [lowered.find(needle) for needle in needles if lowered.find(needle) >= 0]
    if not positions:
        return text
    start = min(positions)
    return text[start : start + size]


def _indian_amount_value(value: str) -> float:
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0.0


def _format_rupee_amount(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _company_name(path: Path, announcement: Any | None, text: str = "") -> str:
    stem = re.sub(r"_\d+$", "", path.stem)
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", stem)
    company_stem = stem[: date_match.start()].rstrip("_") if date_match else stem
    stem_tokens = [token for token in re.split(r"[^a-z0-9]+", company_stem.lower()) if len(token) > 2]
    for line in (text or "").splitlines()[:80]:
        cleaned = re.sub(r"\s+", " ", line).strip(" :-")
        line_key = cleaned.lower()
        token_hits = sum(1 for token in stem_tokens[:4] if token in line_key)
        if token_hits >= min(2, len(stem_tokens)) and re.search(r"\b(?:limited|ltd)\.?$", cleaned, re.IGNORECASE) and 4 <= len(cleaned) <= 120:
            return re.sub(r"^(?:for|on behalf of)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    announced = str(getattr(announcement, "company_name", "") or "").strip()
    if announced:
        return announced
    return company_stem.replace("_", " ").strip() or path.stem


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower())


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output
