"""Probe possible Azure/Mistral chat-completions endpoint shapes without printing secrets."""

from __future__ import annotations

import os
import base64
from pathlib import Path
from urllib.parse import urlparse

import httpx

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def main() -> None:
    """Probe likely endpoint variants and print status codes plus redacted response snippets."""

    if load_dotenv is not None:
        load_dotenv()
    base = os.environ["MISTRAL_BASE_URL"].rstrip("/")
    key = os.environ["MISTRAL_API_KEY"]
    model = os.environ.get("MISTRAL_MODEL", "mistral-document-ai-2512")
    api_version = os.environ.get("MISTRAL_API_VERSION", "2024-05-01-preview")
    urls = _candidate_urls(base, model, api_version)
    headers = {
        "api-key": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    models = [model]
    if model != "azureai":
        models.append("azureai")
    for url in urls:
        for request_model in models:
            payload = {
                "model": request_model,
                "messages": [{"role": "user", "content": "Reply with the single word ok."}],
                "temperature": 0,
                "max_tokens": 8,
            }
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=30)
                body = response.text[:240].replace(key, "[redacted]")
                print(f"HTTP {response.status_code} model={request_model} url={_redact_url(url)} body={body!r}")
            except Exception as exc:
                print(f"ERROR model={request_model} url={_redact_url(url)} error={exc}")

    pdfs = sorted(Path("downloads").rglob("*.pdf"))
    if not pdfs:
        return
    pdf_data = base64.b64encode(pdfs[0].read_bytes()).decode("utf-8")
    ocr_urls = _candidate_ocr_urls(base, api_version)
    ocr_models = list(dict.fromkeys([model, "mistral-ocr-2512", "mistral-ocr-latest", "azureai"]))
    for url in ocr_urls:
        for request_model in ocr_models:
            payload = {
                "model": request_model,
                "document": {
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{pdf_data}",
                },
                "table_format": os.environ.get("MISTRAL_TABLE_FORMAT", "html"),
                "extract_header": True,
                "extract_footer": True,
            }
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=60)
                body = response.text[:240].replace(key, "[redacted]")
                print(f"OCR HTTP {response.status_code} model={request_model} url={_redact_url(url)} body={body!r}")
            except Exception as exc:
                print(f"OCR ERROR model={request_model} url={_redact_url(url)} error={exc}")


def _candidate_urls(base: str, model: str, api_version: str) -> list[str]:
    """Return plausible Azure/Mistral endpoint candidates."""

    urls = [base]
    if not base.endswith("/v1/chat/completions"):
        urls.append(f"{base}/v1/chat/completions")
    urls.extend(
        [
            f"{base}/models/chat/completions?api-version={api_version}",
            f"{base}/chat/completions?api-version={api_version}",
            f"{base}/deployments/{model}/chat/completions?api-version={api_version}",
            f"{base}/openai/deployments/{model}/chat/completions?api-version=2024-10-21",
            f"{base}/openai/deployments/{model}/chat/completions?api-version=2024-12-01-preview",
        ]
    )
    return list(dict.fromkeys(urls))


def _candidate_ocr_urls(base: str, api_version: str) -> list[str]:
    """Return plausible OCR endpoint candidates."""

    urls = [
        f"{base}/providers/mistral/azure/ocr?api-version={api_version}",
        f"{base}/providers/mistral/azure/ocr",
        f"{base}/v1/ocr?api-version={api_version}",
        f"{base}/v1/ocr",
        f"{base}/ocr",
        f"{base}/models/ocr?api-version={api_version}",
        f"{base}/ocr?api-version={api_version}",
        f"{base}/models/v1/ocr?api-version={api_version}",
    ]
    return list(dict.fromkeys(urls))


def _redact_url(url: str) -> str:
    """Print endpoint path without query details that may contain deployment names only."""

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


if __name__ == "__main__":
    main()
