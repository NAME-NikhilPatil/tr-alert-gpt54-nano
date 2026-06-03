"""Compatibility entrypoint for `python scraper.py --source both`."""

from __future__ import annotations

from main import scraper_main as main


if __name__ == "__main__":
    main()
