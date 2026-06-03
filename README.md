# BSE + NSE Board Meeting Outcome Scraper

Scrapes `Outcome of Board Meeting` announcements from NSE and BSE, downloads attached PDFs, extracts financial result fields when present, and writes an Excel workbook.

## Setup

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

OCR is optional. To OCR scanned screenshots, install the Tesseract OCR executable for Windows and make sure `tesseract.exe` is on `PATH`. Without it, the scraper still saves screenshots for failed/scanned PDFs but skips OCR.

## Run

```powershell
python scraper.py --source both
python scraper.py --source nse --date 2026-05-15
python scraper.py --source bse --date 2026-05-15
```

By default, `--date` is today's local date.

## Outputs

- PDFs: `downloads/{SOURCE}/{COMPANY_NAME}_{DATE}.pdf`
- Problem PDF screenshots: `screenshots/{PDF_NAME}/{reason}/page_001.png`
- Excel: `output/board_meeting_outcomes_{DATE}.xlsx`
- Log: `logs/scraper_{DATE}.log`

The parser never guesses missing PDF values. Cells are left blank when a value is not found. PDFs with no numeric financial result data, unreadable/corrupt PDFs, scanned PDFs without OCR text, and parser timeouts are retained in the workbook with parser status and screenshot paths when screenshots can be rendered.
