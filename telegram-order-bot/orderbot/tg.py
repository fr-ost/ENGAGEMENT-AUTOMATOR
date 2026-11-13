"""Minimal Telegram Bot API client (long polling)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)


class TelegramError(Exception):
    """Messages never include the bot token (it is part of the request URL)."""


class Telegram:
    def __init__(self, token: str, session: Optional[requests.Session] = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._session = session or requests.Session()

    def _call(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            resp = self._session.post(f"{self._base}/{method}", json=payload, timeout=timeout)
            body = resp.json()
        except requests.RequestException as exc:
            raise TelegramError(f"{method} failed ({type(exc).__name__})") from None
        except ValueError:
            raise TelegramError(f"{method} returned non-JSON (HTTP {resp.status_code})") from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else None
            raise TelegramError(f"{method} rejected: {description or resp.status_code}")
        return body.get("result")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 15,
        )
        return result if isinstance(result, list) else []

    def delete(self, chat_id: int, message_id: int) -> bool:
        """Remove a message (used for messages that contain an API key)."""
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)
            return True
        except TelegramError as exc:
            log.warning("Could not delete a Telegram message: %s", exc)
            return False

    def send(self, chat_id: int, text: str) -> None:
        """Best effort: a failed notification must never break order processing."""
        try:
            self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
                timeout=15,
            )
        except TelegramError as exc:
            log.warning("Could not send Telegram message: %s", exc)
