"""Telegram delivery with retry and local queue fallback."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx

QUEUE_PATH = Path("logs") / "telegram_queue.jsonl"
QUEUE_LOCK = threading.Lock()


class TelegramSender:
    """Small Telegram Bot HTTP API client with retry and queue fallback."""

    def __init__(self, bot_token: str, chat_ids: str | list[str]) -> None:
        """Store Telegram credentials loaded from environment."""

        self.bot_token = bot_token
        self.chat_ids = self._parse_chat_ids(chat_ids)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)

    def set_chat_ids(self, chat_ids: str | list[str]) -> None:
        """Replace the active recipient list."""

        self.chat_ids = self._parse_chat_ids(chat_ids)

    def send_startup(self, poll_interval: int) -> bool:
        """Send the startup notification."""

        return self.send_text(f"Scraper is LIVE. Monitoring NSE + BSE every {poll_interval} seconds.")

    def send_error_alert(self, error_message: str) -> bool:
        """Send a live-loop crash notification."""

        return self.send_text(f"Scraper ERROR: {error_message}. Restarting...")

    def send_text(self, text: str, *, queue_on_failure: bool = True) -> bool:
        """Send one Telegram text message."""

        if not self.chat_ids:
            logging.warning("Telegram text skipped because there are no active subscribers.")
            return False
        all_ok = True
        for chat_id in self.chat_ids:
            payload = {"chat_id": chat_id, "text": text}
            ok = self._post_with_retry("sendMessage", data=payload)
            if not ok:
                all_ok = False
                if queue_on_failure:
                    self._queue({"kind": "text", "chat_id": chat_id, "text": text})
        return all_ok

    def send_document(self, file_path: Path, caption: str, *, queue_on_failure: bool = True) -> bool:
        """Send one Telegram document attachment."""

        if not self.chat_ids:
            logging.warning("Telegram document skipped because there are no active subscribers.")
            return False
        if not file_path.exists():
            logging.error("Telegram document does not exist: %s", file_path)
            return False
        all_ok = True
        for chat_id in self.chat_ids:
            ok = self._send_document_to_chat(chat_id, file_path, caption)
            if not ok:
                all_ok = False
                if queue_on_failure:
                    self._queue({"kind": "document", "chat_id": chat_id, "file_path": str(file_path), "caption": caption})
        return all_ok

    def send_photo(self, file_path: Path, caption: str = "", *, queue_on_failure: bool = True) -> bool:
        """Send one Telegram photo attachment."""

        if not self.chat_ids:
            logging.warning("Telegram photo skipped because there are no active subscribers.")
            return False
        if not file_path.exists():
            logging.error("Telegram photo does not exist: %s", file_path)
            return False
        all_ok = True
        for chat_id in self.chat_ids:
            ok = self._send_photo_to_chat(chat_id, file_path, caption)
            if not ok:
                all_ok = False
                if queue_on_failure:
                    self._queue({"kind": "photo", "chat_id": chat_id, "file_path": str(file_path), "caption": caption})
        return all_ok

    def send_result(self, summary_text: str, excel_path: Path, caption: str) -> int:
        """Send the required text summary and Excel attachment. Return sent count."""

        sent = 0
        if self.send_text(summary_text):
            sent += 1
        if self.send_document(excel_path, caption):
            sent += 1
        return sent

    def send_text_to_chat(self, chat_id: str, text: str) -> bool:
        """Send one text message to one chat, regardless of default subscribers."""

        return self._send_text_to_chats([str(chat_id).strip()], text)

    def get_updates(self, offset: int | None = None) -> list[dict[str, object]]:
        """Fetch pending Telegram bot updates without long polling."""

        params: dict[str, object] = {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = httpx.get(f"{self.base_url}/getUpdates", params=params, timeout=30)
            if response.status_code != 200:
                logging.warning("Telegram getUpdates failed HTTP %s: %s", response.status_code, response.text[:300])
                return []
            payload = response.json()
            if not payload.get("ok"):
                logging.warning("Telegram getUpdates returned not-ok payload: %s", str(payload)[:300])
                return []
            result = payload.get("result", [])
            return result if isinstance(result, list) else []
        except Exception as exc:
            logging.warning("Telegram getUpdates failed: %s", exc)
            return []

    def drain_queue(self) -> int:
        """Retry queued Telegram messages and keep failures queued."""

        if not QUEUE_PATH.exists():
            return 0
        sent = 0
        remaining: list[dict[str, str]] = []
        with QUEUE_LOCK:
            with QUEUE_PATH.open("r", encoding="utf-8") as handle:
                queued = [json.loads(line) for line in handle if line.strip()]
            for item in queued:
                kind = item.get("kind")
                chat_id = str(item.get("chat_id", "")).strip()
                target_chat_ids = [chat_id] if chat_id else self.chat_ids
                if kind == "text" and self._send_text_to_chats(target_chat_ids, str(item.get("text", ""))):
                    sent += 1
                elif kind == "document" and self._send_document_to_chats(
                    target_chat_ids,
                    Path(str(item.get("file_path", ""))),
                    str(item.get("caption", "")),
                ):
                    sent += 1
                elif kind == "photo" and self._send_photo_to_chats(
                    target_chat_ids,
                    Path(str(item.get("file_path", ""))),
                    str(item.get("caption", "")),
                ):
                    sent += 1
                else:
                    remaining.append(item)
            with QUEUE_PATH.open("w", encoding="utf-8") as handle:
                for item in remaining:
                    handle.write(json.dumps(item) + "\n")
        return sent

    def _send_text_to_chats(self, chat_ids: list[str], text: str) -> bool:
        """Send text to a specific set of chat IDs without queueing."""

        all_ok = True
        for chat_id in chat_ids:
            payload = {"chat_id": chat_id, "text": text}
            if not self._post_with_retry("sendMessage", data=payload):
                all_ok = False
        return all_ok

    def _send_document_to_chats(self, chat_ids: list[str], file_path: Path, caption: str) -> bool:
        """Send a document to a specific set of chat IDs without queueing."""

        all_ok = True
        for chat_id in chat_ids:
            if not self._send_document_to_chat(chat_id, file_path, caption):
                all_ok = False
        return all_ok

    def _send_document_to_chat(self, chat_id: str, file_path: Path, caption: str) -> bool:
        """Send one document to one chat ID."""

        for attempt in range(1, 4):
            try:
                with file_path.open("rb") as handle:
                    response = httpx.post(
                        f"{self.base_url}/sendDocument",
                        data={"chat_id": chat_id, "caption": caption},
                        files={"document": (file_path.name, handle)},
                        timeout=60,
                    )
                if response.status_code == 200:
                    return True
                logging.warning("Telegram document send failed HTTP %s for chat %s: %s", response.status_code, chat_id, response.text[:300])
            except Exception as exc:
                logging.warning("Telegram document send attempt %s failed for chat %s: %s", attempt, chat_id, exc)
            time.sleep(5)
        return False

    def _send_photo_to_chats(self, chat_ids: list[str], file_path: Path, caption: str) -> bool:
        """Send a photo to a specific set of chat IDs without queueing."""

        all_ok = True
        for chat_id in chat_ids:
            if not self._send_photo_to_chat(chat_id, file_path, caption):
                all_ok = False
        return all_ok

    def _send_photo_to_chat(self, chat_id: str, file_path: Path, caption: str) -> bool:
        """Send one photo to one chat ID."""

        for attempt in range(1, 4):
            try:
                with file_path.open("rb") as handle:
                    response = httpx.post(
                        f"{self.base_url}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption[:1024]},
                        files={"photo": (file_path.name, handle, "image/png")},
                        timeout=60,
                    )
                if response.status_code == 200:
                    return True
                logging.warning("Telegram photo send failed HTTP %s for chat %s: %s", response.status_code, chat_id, response.text[:300])
            except Exception as exc:
                logging.warning("Telegram photo send attempt %s failed for chat %s: %s", attempt, chat_id, exc)
            time.sleep(5)
        return False

    @staticmethod
    def _parse_chat_ids(chat_ids: str | list[str]) -> list[str]:
        """Normalize Telegram chat ID configuration."""

        if isinstance(chat_ids, str):
            return [part.strip() for part in chat_ids.split(",") if part.strip()]
        return [str(chat_id).strip() for chat_id in chat_ids if str(chat_id).strip()]

    def _post_with_retry(self, method: str, data: dict[str, str]) -> bool:
        """POST to Telegram with three attempts and 5-second backoff."""

        for attempt in range(1, 4):
            try:
                response = httpx.post(f"{self.base_url}/{method}", data=data, timeout=30)
                if response.status_code == 200:
                    return True
                logging.warning("Telegram %s failed HTTP %s: %s", method, response.status_code, response.text[:300])
            except Exception as exc:
                logging.warning("Telegram %s attempt %s failed: %s", method, attempt, exc)
            time.sleep(5)
        return False

    def _queue(self, payload: dict[str, str]) -> None:
        """Append a failed Telegram delivery to the local queue."""

        with QUEUE_LOCK:
            with QUEUE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
