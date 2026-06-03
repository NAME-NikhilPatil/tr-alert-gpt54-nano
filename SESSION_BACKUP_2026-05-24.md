# Codex Session Backup - 2026-05-24

This file preserves the current project context before changing sign-in/account
mode. It intentionally does not copy `.env` values or API keys.

## If Chat History Disappears

If the Codex/ChatGPT conversation history is not visible after signing in with a
different account, open this project and ask the assistant to read:

1. `AGENTS.md`
2. `SESSION_BACKUP_2026-05-24.md`
3. Current source files in this folder

That should provide enough context to continue the NSE/BSE Telegram bot work.

## Current Project

Workspace:

`C:\Users\sharm\Downloads\tr_alert`

Purpose:

Poll latest NSE/BSE "Outcome of Board Meeting" announcements, download official
PDFs, extract financial-result data, and send Telegram alerts.

Current direction:

- Live Telegram output should use the Azure Foundry-hosted Mistral Document AI
  OCR/parser path.
- User does not want Excel attachments for the new live output mode.
- User wants Excel-style colored table screenshots in Telegram instead of
  monospace text tables.
- Existing one-shot scraper and older Excel/audit pipeline should stay available.

Important local files:

- `AGENTS.md` - full running handoff/history and project instructions.
- `main.py` - live bot loop and Telegram polling flow.
- `mistral_parser.py` - Mistral/Azure OCR extraction, normalization, formatting.
- `telegram_sender.py` - Telegram Bot API sender/subscriber helpers.
- `db_manager.py` - duplicate prevention and Telegram subscriber DB helpers.
- `image_generator.py`, `pl_image.py`, `segment_image.py`, `bs_cf_image.py` -
  image/table rendering helpers.
- `seen_announcements.db` - processed PDF/subscriber state.
- `.env` - local credentials/config, intentionally gitignored.

Recent verified state from the prior session:

- Azure Foundry OCR route works at `/providers/mistral/azure/ocr` with API
  version `2024-05-01-preview`.
- `MISTRAL_TABLE_FORMAT=html` is used.
- The 10-PDF Mistral smoke test parsed 10/10 PDFs, found usable financial or
  variable data in 7/10, and downgraded empty/no-value outputs instead of
  sending blank tables.
- Compile checks had passed for the edited parser/live-bot files.

Recommended next coding task:

Implement Telegram image alerts that render Mistral-extracted sections as
Excel-style colored table screenshots:

- Light-blue company/title band.
- Blue column headers.
- Green row-label bands.
- Grid lines.
- Only populated period columns.
- Dynamic sections for financial results, segments, balance sheet, cash flow,
  and key variables when present.

## Account/History Safety

Before changing sign-in:

1. Keep this folder backed up.
2. Keep `AGENTS.md` and this file.
3. Export ChatGPT/OpenAI account data from ChatGPT settings if the conversation
   exists in a ChatGPT account.
4. Do not delete `.env`, `seen_announcements.db`, `announcement_cache.db`,
   `downloads/`, `output/`, or `logs/` unless intentionally cleaning runtime
   state.
