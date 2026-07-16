"""Regression tests for NSE announcement discovery."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx

import nse_scraper
import utils


class _JsonResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _PdfResponse:
    headers = {"content-type": "application/pdf"}
    content = b"%PDF-1.4\n% test\n%%EOF\n"


class _IncompletePdfResponse:
    headers = {"content-type": "application/pdf"}
    content = b"%PDF-1.7\n1 0 obj<</Linearized 1/L 200>>\n%%EOF\n"


class NseApiDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_empty_api_result_uses_browser_fallback(self) -> None:
        api_fetch = AsyncMock(return_value=[])
        browser_result = [
            object(),
        ]
        browser_fetch = AsyncMock(return_value=browser_result)

        with (
            patch.object(nse_scraper, "_fetch_nse_via_api", api_fetch),
            patch.object(nse_scraper, "_fetch_nse_via_browser", browser_fetch),
        ):
            announcements = await nse_scraper.fetch_nse_announcements(date(2026, 7, 15))

        self.assertEqual(browser_result, announcements)
        browser_fetch.assert_awaited_once_with(date(2026, 7, 15))

    async def test_api_discovery_continues_when_homepage_warmup_is_forbidden(self) -> None:
        request = httpx.Request("GET", nse_scraper.NSE_BASE_URL)
        response = httpx.Response(403, request=request)
        warmup_error = httpx.HTTPStatusError(
            "NSE homepage denied by Akamai",
            request=request,
            response=response,
        )
        api_response = _JsonResponse(
            [
                {
                    "sm_name": "Example Limited",
                    "symbol": "EXAMPLE",
                    "desc": "Outcome of Board Meeting",
                    "attchmntFile": "corporate/example.pdf",
                    "an_dt": "14-Jul-2026 18:30:00",
                }
            ]
        )

        mocked_request = AsyncMock(side_effect=[warmup_error, api_response])
        with patch.object(nse_scraper, "request_with_retries", mocked_request):
            announcements = await nse_scraper._fetch_nse_via_api(date(2026, 7, 14))

        self.assertEqual(1, len(announcements))
        self.assertEqual("Example Limited", announcements[0].company_name)
        self.assertEqual(2, mocked_request.await_count)

    async def test_nse_pdf_download_continues_when_homepage_warmup_is_forbidden(self) -> None:
        request = httpx.Request("GET", nse_scraper.NSE_BASE_URL)
        response = httpx.Response(403, request=request)
        warmup_error = httpx.HTTPStatusError(
            "NSE homepage denied by Akamai",
            request=request,
            response=response,
        )
        mocked_request = AsyncMock(side_effect=[warmup_error, _PdfResponse()])

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(utils, "Path", side_effect=lambda value: root / value),
                patch.object(utils, "request_with_retries", mocked_request),
                patch.object(utils, "_log_failed_download"),
            ):
                downloaded = await utils.download_pdf(
                    "NSE",
                    "Example Limited",
                    date(2026, 7, 14),
                    "https://nsearchives.nseindia.com/corporate/example.pdf",
                    nse_scraper.NSE_HEADERS,
                )

            self.assertIsNotNone(downloaded)
            self.assertEqual(_PdfResponse.content, downloaded.read_bytes())
            self.assertEqual(2, mocked_request.await_count)

    async def test_nse_pdf_download_retries_incomplete_linearized_response(self) -> None:
        request = httpx.Request("GET", nse_scraper.NSE_BASE_URL)
        response = httpx.Response(403, request=request)
        warmup_error = httpx.HTTPStatusError(
            "NSE homepage denied by Akamai",
            request=request,
            response=response,
        )
        mocked_request = AsyncMock(side_effect=[warmup_error, _IncompletePdfResponse(), _PdfResponse()])

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(utils, "Path", side_effect=lambda value: root / value),
                patch.object(utils, "request_with_retries", mocked_request),
                patch.object(utils, "_log_failed_download"),
            ):
                downloaded = await utils.download_pdf(
                    "NSE",
                    "Example Limited",
                    date(2026, 7, 15),
                    "https://nsearchives.nseindia.com/corporate/example.pdf",
                    nse_scraper.NSE_HEADERS,
                )

            self.assertIsNotNone(downloaded)
            self.assertEqual(_PdfResponse.content, downloaded.read_bytes())
            self.assertEqual(3, mocked_request.await_count)


if __name__ == "__main__":
    unittest.main()
