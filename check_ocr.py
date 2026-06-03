"""Check whether Tesseract OCR is available to the parser."""

from __future__ import annotations

from pdf_parser import _find_tesseract_cmd


def main() -> None:
    """Print Tesseract OCR readiness."""

    path = _find_tesseract_cmd()
    if path:
        print(f"OK: {path}")
    else:
        print("MISSING: tesseract.exe was not found on PATH or common install locations.")


if __name__ == "__main__":
    main()

