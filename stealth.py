"""Playwright stealth helpers with optional dependency support."""

from __future__ import annotations

from typing import Any


async def apply_stealth(page: Any) -> None:
    """Apply stealth patches when playwright-stealth is installed."""

    try:
        from playwright_stealth import stealth_async  # type: ignore

        await stealth_async(page)
        return
    except Exception:
        pass

    await page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """
    )

