"""Validate current generated financial PNGs, excluding archives and tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from image_validation import validate_financial_png

DEFAULT_EXCLUDED_DIR_NAMES = {
    "_test",
    "images_archive_before_image_fix_20260524_1616",
    "pnl_visual_contact_sheets_2026-05-27",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate current generated financial PNG images.")
    parser.add_argument(
        "roots",
        nargs="*",
        default=["output/images", "output/regression_dynamic_pnl", "output/regression_long_pdf_selector"],
        help="Image roots to validate.",
    )
    args = parser.parse_args()

    paths = _collect_pngs([Path(root) for root in args.roots])
    issues: list[tuple[Path, str]] = []
    for path in paths:
        issue = validate_financial_png(path)
        if issue:
            issues.append((path, issue))

    print(f"PNG count: {len(paths)}")
    print(f"Issue count: {len(issues)}")
    for path, issue in issues:
        print(f"{issue} | {path}")
    return 1 if issues else 0


def _collect_pngs(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.png"):
            if _is_excluded(path):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                paths.append(path)
    return sorted(paths, key=lambda item: str(item).lower())


def _is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & DEFAULT_EXCLUDED_DIR_NAMES:
        return True
    if ".thumb" in path.name.lower():
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
