"""Regression tests for BSE announcement discovery."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import bse_scraper


class _JsonResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class BseApiDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_capture_recognizes_current_and_legacy_api_urls(self) -> None:
        self.assertTrue(
            bse_scraper._is_bse_announcement_response_url(
                "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1"
            )
        )
        self.assertTrue(
            bse_scraper._is_bse_announcement_response_url(
                "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?page_no=1"
            )
        )
        self.assertFalse(
            bse_scraper._is_bse_announcement_response_url(
                "https://api.bseindia.com/BseIndiaAPI/api/HeaderData/w"
            )
        )

    async def test_uses_current_subcategory_endpoint_and_parameter_names(self) -> None:
        payload = {
            "Table": [
                {
                    "NEWSID": "news-1",
                    "SCRIP_CD": "500314",
                    "NEWSSUB": "Oriental Hotels Ltd - 500314 - Outcome of Board Meeting",
                    "DT_TM": "2026-07-15T13:12:11.143",
                    "ATTACHMENTNAME": "example.pdf",
                    "SLONGNAME": "Oriental Hotels Ltd",
                }
            ],
            "Table1": [{"ROWCNT": 1}],
        }
        async def fake_request(*args: object, **kwargs: object) -> _JsonResponse:
            url = str(args[2])
            return _JsonResponse({}) if url == bse_scraper.BSE_BASE_URL else _JsonResponse(payload)

        request = AsyncMock(side_effect=fake_request)

        with patch.object(bse_scraper, "request_with_retries", request):
            announcements = await bse_scraper._fetch_bse_via_api(date(2026, 7, 15))

        self.assertEqual(1, len(announcements))
        api_call = request.await_args_list[1]
        self.assertTrue(api_call.args[2].endswith("/AnnSubCategoryGetData/w"))
        params = api_call.kwargs["params"]
        self.assertEqual("1", params["pageno"])
        self.assertIn("strscrip", params)
        self.assertNotIn("page_no", params)
        self.assertNotIn("strScrip", params)


if __name__ == "__main__":
    unittest.main()
