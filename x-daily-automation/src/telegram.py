"""Optional Telegram notifications via the Bot API sendMessage method."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
CHUNK_SIZE = 4000  # headroom below Telegram's 4096-character limit


class TelegramError(Exception):
    """Delivery failed. Messages never include the bot token."""


def split_message(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Split on line boundaries; hard-split lines longer than the limit."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: float = 15.0,
        max_retries: int = 2,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id are required")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep

    def send(self, text: str) -> int:
        """Send text (split if long). Returns the number of messages sent."""
        chunks = split_message(text)
        for index, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk = f"({index}/{len(chunks)})\n{chunk}"
            self._send_one(chunk)
        return len(chunks)

    def _send_one(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.post(self._url, json=payload, timeout=self._timeout)
            except requests.Timeout:
                if attempt > self._max_retries:
                    raise TelegramError(f"Telegram timed out after {attempt} attempts") from None
                self._sleep(min(2**attempt, 10))
                continue
            except requests.RequestException as exc:
                # str(exc) can contain the URL (and so the token) — log the type only.
                if attempt > self._max_retries:
                    raise TelegramError(f"Telegram network error: {type(exc).__name__}") from None
                self._sleep(min(2**attempt, 10))
                continue

            if resp.status_code == 200:
                try:
                    ok = resp.json().get("ok") is True
                except (ValueError, AttributeError):
                    ok = False
                if ok:
                    return
                raise TelegramError("Telegram returned HTTP 200 without ok=true")
            if resp.status_code in (401, 404):
                raise TelegramError("Telegram rejected the bot token (check TELEGRAM_BOT_TOKEN)")
            if resp.status_code == 400:
                raise TelegramError(f"Telegram rejected the request (HTTP 400): {_description(resp)}")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise TelegramError(f"Telegram HTTP {resp.status_code} after {attempt} attempts")
                retry_after = _retry_after(resp)
                self._sleep(min(retry_after if retry_after is not None else 2**attempt, 30))
                continue
            raise TelegramError(f"Telegram HTTP {resp.status_code}: {_description(resp)}")


def _description(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("description", ""))[:200]
    except (ValueError, AttributeError):
        return ""


def _retry_after(resp: requests.Response) -> Optional[float]:
    try:
        value = resp.json().get("parameters", {}).get("retry_after")
        return float(value) if value is not None else None
    except (ValueError, AttributeError, TypeError):
        return None
