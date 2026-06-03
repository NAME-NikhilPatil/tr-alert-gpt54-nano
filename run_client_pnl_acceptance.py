"""Run the acceptance gate for the current client P&L image fixes.

The client feedback covered four concrete failure modes:

1. Dynamic P&L line items, e.g. Balaji/media or finance rows.
2. Malformed/improper generated images.
3. Repeated identical number artifacts.
4. Consolidated data missed even when the PDF contains consolidated numbers.

This runner executes the focused verifiers that cover those requirements and
writes one dated report under ``output/``. It does not start the live bot and it
does not contact Telegram.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
DEBUG_LOG = "logs/debug/live_debug_2026-05-26.jsonl"
SINCE = "2026-05-26T20:00:00"
IMAGE_ROOT = "output/regression_dynamic_pnl"


CHECKS: list[tuple[str, list[str]]] = [
    (
        "compile",
        [
            sys.executable,
            "-m",
            "compileall",
            "verify_live_image_runtime_dryrun.py",
            "verify_live_image_path.py",
            "validate_current_financial_images.py",
            "test_financial_image_guards.py",
            "verify_pnl_client_fixes.py",
            "audit_financial_images.py",
            "image_generator.py",
            "image_validation.py",
            "mistral_parser.py",
            "pl_image.py",
            "bs_cf_image.py",
            "segment_image.py",
            "main.py",
            "telegram_sender.py",
        ],
    ),
    ("live image runtime dry-run", [sys.executable, "-B", "verify_live_image_runtime_dryrun.py"]),
    ("live image static path", [sys.executable, "-B", "verify_live_image_path.py"]),
    ("current image validation", [sys.executable, "-B", "validate_current_financial_images.py"]),
    ("financial image guard tests", [sys.executable, "-B", "test_financial_image_guards.py"]),
    (
        "focused client P&L fixes",
        [
            sys.executable,
            "-B",
            "verify_pnl_client_fixes.py",
            "--debug-log",
            DEBUG_LOG,
            "--since",
            SINCE,
            "--image-root",
            IMAGE_ROOT,
        ],
    ),
    (
        "financial image audit",
        [
            sys.executable,
            "-B",
            "audit_financial_images.py",
            "--debug-log",
            DEBUG_LOG,
            "--since",
            SINCE,
            "--image-root",
            IMAGE_ROOT,
        ],
    ),
]


def run_check(name: str, command: list[str]) -> tuple[int, str]:
    """Run one acceptance command and return its exit code and combined output."""

    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=240,
    )
    parts = [
        f"## {name}",
        f"command: {' '.join(command)}",
        f"exit_code: {completed.returncode}",
        "",
    ]
    if completed.stdout.strip():
        parts.extend(["stdout:", completed.stdout.rstrip(), ""])
    if completed.stderr.strip():
        parts.extend(["stderr:", completed.stderr.rstrip(), ""])
    return completed.returncode, "\n".join(parts)


def main() -> int:
    """Run all acceptance checks and write a report."""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    report_parts = [
        "# Client P&L Acceptance Report",
        f"started_at: {started.isoformat(timespec='seconds')}",
        "",
        "Requirements covered:",
        "- Dynamic P&L line items are preserved for Balaji/media-style and finance-style rows.",
        "- Consolidated P&L data is selected/fetched when consolidated figures exist.",
        "- Repeated identical number artifacts are rejected.",
        "- Current generated images are structurally valid and non-contaminated.",
        "- Live image delivery path sends PNG photos and does not send Excel/documents.",
        "",
    ]
    failed: list[str] = []
    for name, command in CHECKS:
        code, section = run_check(name, command)
        report_parts.append(section)
        if code != 0:
            failed.append(name)
            break

    finished = datetime.now()
    status = "PASS" if not failed else "FAIL"
    report_parts.extend(
        [
            "## Summary",
            f"status: {status}",
            f"finished_at: {finished.isoformat(timespec='seconds')}",
            f"duration_seconds: {(finished - started).total_seconds():.1f}",
        ]
    )
    if failed:
        report_parts.append("failed_checks: " + ", ".join(failed))
    else:
        report_parts.append("failed_checks: none")

    report_path = OUTPUT_DIR / f"client_pnl_acceptance_{finished.strftime('%Y-%m-%d')}.txt"
    report_path.write_text("\n".join(report_parts) + "\n", encoding="utf-8")
    print(f"status {status}")
    print(f"report {report_path}")
    if failed:
        print("failed_checks " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
