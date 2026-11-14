"""Discovers the account's own original posts within an overlapping scan window."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import Config, format_iso, parse_iso_datetime
from .x_client import XClient, XResponseError

log = logging.getLogger(__name__)


class ScanError(Exception):
    """The scan could not be completed; the checkpoint must not advance."""


@dataclass(frozen=True)
class Post:
    post_id: str
    text: str
    created_at: str  # ISO-8601 UTC as returned by X
    url: str
    kind: str  # "original" or "quote"
    public_metrics: Optional[dict[str, int]] = None


@dataclass
class ScanResult:
    user_id: str
    username: str
    window_start: datetime
    posts: list[Post] = field(default_factory=list)  # oldest first
    pages_fetched: int = 0
    truncated: bool = False
    skipped_replies: int = 0
    skipped_reposts: int = 0
    skipped_quotes: int = 0
    invalid_items: int = 0


def canonical_url(username: str, post_id: str) -> str:
    return f"https://x.com/{username}/status/{post_id}"


def classify(raw: dict[str, Any]) -> str:
    """Return 'reply', 'repost', 'quote', or 'original'."""
    refs = raw.get("referenced_tweets") or []
    types = {r.get("type") for r in refs if isinstance(r, dict)}
    if "retweeted" in types:
        return "repost"
    if "replied_to" in types or raw.get("in_reply_to_user_id"):
        return "reply"
    if "quoted" in types:
        return "quote"
    return "original"


def _metrics(raw: dict[str, Any]) -> Optional[dict[str, int]]:
    metrics = raw.get("public_metrics")
    if not isinstance(metrics, dict):
        return None
    return {k: v for k, v in metrics.items() if isinstance(k, str) and isinstance(v, int)}


def compute_window_start(
    last_successful_scan: Optional[str],
    now: datetime,
    config: Config,
) -> datetime:
    """Choose where this scan starts.

    Manual recovery (SCAN_SINCE) wins; otherwise the previous checkpoint minus an
    overlap so posts that X indexed late are not missed; otherwise (first run) a
    bounded lookback. Duplicates from the overlap are removed by post ID.
    """
    if config.scan_since_override:
        return config.scan_since_override
    if last_successful_scan:
        return parse_iso_datetime(last_successful_scan) - timedelta(hours=config.scan_overlap_hours)
    return now - timedelta(hours=config.initial_lookback_hours)


class Scanner:
    def __init__(self, client: XClient, config: Config):
        self._client = client
        self._config = config

    def resolve_account(self, cached: Optional[dict[str, Any]]) -> tuple[str, Optional[str]]:
        """Return (user_id, username), using as few API calls as possible."""
        cfg = self._config
        cached = cached or {}
        if cfg.x_user_id:
            username = cfg.x_username or cached.get("username")
            return cfg.x_user_id, username
        if (
            cached.get("user_id")
            and cached.get("username")
            and cfg.x_username
            and cached["username"].lower() == cfg.x_username.lower()
        ):
            return cached["user_id"], cached["username"]
        user = self._client.get_user_by_username(cfg.x_username)  # type: ignore[arg-type]
        return user["id"], user["username"]

    def scan(self, account_cache: Optional[dict[str, Any]], last_successful_scan: Optional[str], now: datetime) -> ScanResult:
        user_id, username = self.resolve_account(account_cache)
        window_start = compute_window_start(last_successful_scan, now, self._config)
        if window_start >= now:
            raise ScanError(f"Scan window start {format_iso(window_start)} is not in the past")
        log.info("Scanning posts by user %s since %s", user_id, format_iso(window_start))

        page = self._client.get_user_posts(
            user_id,
            start_time=window_start,
            max_pages=self._config.x_max_pages,
            page_size=self._config.x_page_size,
        )

        # Prefer the canonical username returned by the API for URLs.
        for user in page.users:
            if user.get("id") == user_id and isinstance(user.get("username"), str):
                username = user["username"]
        if not username:
            raise ScanError("Could not determine the account username for post URLs; set X_USERNAME")

        result = ScanResult(
            user_id=user_id,
            username=username,
            window_start=window_start,
            pages_fetched=page.pages_fetched,
            truncated=page.truncated,
        )
        for raw in page.posts:
            post_id = raw.get("id")
            created = raw.get("created_at")
            if not (isinstance(post_id, str) and post_id.isdigit() and isinstance(created, str)):
                result.invalid_items += 1
                continue
            try:
                parse_iso_datetime(created)
            except ValueError:
                result.invalid_items += 1
                continue
            author = raw.get("author_id")
            if author is not None and author != user_id:
                result.invalid_items += 1
                continue
            kind = classify(raw)
            if kind == "reply":
                result.skipped_replies += 1
                continue
            if kind == "repost":
                result.skipped_reposts += 1
                continue
            if kind == "quote" and not self._config.include_quote_posts:
                result.skipped_quotes += 1
                continue
            result.posts.append(
                Post(
                    post_id=post_id,
                    text=str(raw.get("text", "")),
                    created_at=created,
                    url=canonical_url(username, post_id),
                    kind=kind,
                    public_metrics=_metrics(raw),
                )
            )

        if page.posts and not result.posts and result.invalid_items == len(page.posts):
            raise XResponseError("Every post in the timeline response was malformed")
        result.posts.sort(key=lambda p: (parse_iso_datetime(p.created_at), int(p.post_id)))
        return result
