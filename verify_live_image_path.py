"""Verify that live Telegram delivery uses GPT-generated PNG photos.

This is a static guard for the current live mode. It avoids starting the bot
or sending Telegram messages while checking that the production path still
extracts one queued PDF with GPT, renders validated financial PNGs, and sends
them via Telegram sendPhoto instead of the old Excel/document path.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function_source(path: str, function_name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"{function_name} not found in {path}")


def _assert_contains(text: str, needle: str, context: str) -> None:
    if needle not in text:
        raise AssertionError(f"{context} does not contain {needle!r}")


def _assert_not_contains(text: str, needle: str, context: str) -> None:
    if needle in text:
        raise AssertionError(f"{context} unexpectedly contains {needle!r}")


def main() -> None:
    main_source = _source("main.py")
    sender_source = _source("telegram_sender.py")
    generator_source = _source("image_generator.py")

    live_poll = _function_source("main.py", "_poll_once")
    worker_process = _function_source("main.py", "_process_live_pdf_job")
    send_live_output = _function_source("main.py", "_send_live_extraction_output")
    local_replay = _function_source("main.py", "_send_local_mistral_result")
    send_generated = _function_source("main.py", "_send_generated_financial_images")
    photo_sender = _function_source("telegram_sender.py", "_send_photo_to_chat")
    generator = _function_source("image_generator.py", "generate_financial_images")

    _assert_contains(main_source, "from gpt54_extractor import extract_pdf_with_gpt54", "main.py")
    _assert_contains(main_source, "from pdf_job_worker import PdfJobWorkerPool", "main.py")
    _assert_contains(main_source, "from image_generator import generate_financial_images", "main.py")

    _assert_contains(live_poll, "enqueue_pdf_job", "_poll_once")
    _assert_not_contains(live_poll, "extract_pdf_with_gpt54", "_poll_once")
    _assert_not_contains(live_poll, "generate_financial_images", "_poll_once")

    for context, text in (
        ("_process_live_pdf_job", worker_process),
        ("_send_local_mistral_result", local_replay),
    ):
        _assert_contains(text, "extract_pdf_with_gpt54", context)
        _assert_contains(text, "generate_financial_images", context)
        _assert_not_contains(text, "write_alert_excel", context)
        _assert_not_contains(text, "send_document", context)
        _assert_not_contains(text, "send_result", context)

    _assert_contains(worker_process, "GPT_REQUEST_STARTED", "_process_live_pdf_job")
    _assert_contains(worker_process, "GPT_REQUEST_FINISHED", "_process_live_pdf_job")
    _assert_contains(worker_process, "_send_live_extraction_output", "_process_live_pdf_job")
    _assert_contains(send_live_output, "_send_generated_financial_images", "_send_live_extraction_output")
    _assert_not_contains(send_live_output, "send_document", "_send_live_extraction_output")
    _assert_contains(send_generated, "sender.send_photo", "_send_generated_financial_images")
    _assert_not_contains(send_generated, "send_document", "_send_generated_financial_images")
    _assert_contains(photo_sender, "/sendPhoto", "TelegramSender._send_photo_to_chat")
    _assert_contains(photo_sender, '"image/png"', "TelegramSender._send_photo_to_chat")

    _assert_contains(generator_source, "from image_validation import validate_financial_png", "image_generator.py")
    _assert_contains(generator, "validate_financial_png(path)", "generate_financial_images")
    _assert_contains(generator, "path.unlink", "generate_financial_images")

    print("live-image-path verification passed")


if __name__ == "__main__":
    main()
