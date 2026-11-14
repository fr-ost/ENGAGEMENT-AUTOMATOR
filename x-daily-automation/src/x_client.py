"""Minimal client for the official X API v2 (read-only endpoints only).

Endpoints used:
  GET /2/users/by/username/:username   resolve a username to a user ID
  GET /2/users/:id/tweets               the user's post timeline (paginated)
  GET /2/tweets?ids=...                 batch lookup, used for public metrics

There is deliberately no fallback to scraping x.com pages.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import requests

from .config import format_iso

log = logging.getLogger(__name__)

API_BASE = "https://api.x.com/2"
TWEET_FIELDS = "created_at,referenced_tweets,public_metrics,author_id,in_reply_to_user_id"


class XAPIError(Exception):
    """Base error for X API failures. Messages never contain credentials."""


class XAuthError(XAPIError):
    """401/403: bad, revoked, or under-privileged credentials."""


class XRateLimitError(XAPIError):
    """429 that could not be waited out within the configured limit."""


class XNotFoundError(XAPIError):
    """The requested user or resource does not exist."""


class XResponseError(XAPIError):
    """The API answered with something we could not validate."""


@dataclass
class TimelinePage:
    posts: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    truncated: bool = False  # more pages existed than max_pages allowed


class XClient:
    def __init__(
        self,
        bearer_token: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        max_rate_limit_wait: int = 120,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        base_url: str = API_BASE,
    ):
        if not bearer_token:
            raise ValueError("bearer_token is required")
        self._token = bearer_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_wait = max_rate_limit_wait
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self.request_count = 0
        self.posts_read = 0

    # -- low level -----------------------------------------------------

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "x-daily-automation/1.0"}
        attempt = 0
        while True:
            attempt += 1
            self.request_count += 1
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            except requests.Timeout:
                if attempt > self._max_retries:
                    raise XAPIError(f"Timed out calling X API {path} after {attempt} attempts") from None
                self._backoff(attempt, f"timeout on {path}")
                continue
            except requests.RequestException as exc:
                if attempt > self._max_retries:
                    raise XAPIError(
                        f"Network error calling X API {path}: {type(exc).__name__}"
                    ) from None
                self._backoff(attempt, f"{type(exc).__name__} on {path}")
                continue

            status = resp.status_code
            if status == 200:
                return self._parse_json(resp, path)
            if status in (401, 403):
                detail = self._error_detail(resp)
                raise XAuthError(
                    f"X API rejected the credentials for {path} (HTTP {status}{detail}). "
                    "Check X_BEARER_TOKEN and that your X developer project has access to this endpoint."
                )
            if status == 404:
                raise XNotFoundError(f"X API returned 404 for {path}{self._error_detail(resp)}")
            if status == 429:
                wait = self._rate_limit_wait(resp)
                if attempt > self._max_retries or wait > self._max_rate_limit_wait:
                    raise XRateLimitError(
                        f"X API rate limit hit on {path}; reset in ~{int(wait)}s "
                        f"(limit for waiting is {self._max_rate_limit_wait}s). Try again later."
                    )
                log.warning("X API rate limited on %s; waiting %.0fs", path, wait)
                self._sleep(wait)
                continue
            if 500 <= status < 600:
                if attempt > self._max_retries:
                    raise XAPIError(f"X API server error HTTP {status} on {path} after {attempt} attempts")
                self._backoff(attempt, f"HTTP {status} on {path}")
                continue
            raise XAPIError(f"X API returned HTTP {status} for {path}{self._error_detail(resp)}")

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2 ** (attempt - 1), 30)
        log.warning("Retrying X API call (%s), attempt %d, sleeping %ss", reason, attempt, delay)
        self._sleep(delay)

    def _rate_limit_wait(self, resp: requests.Response) -> float:
        reset = resp.headers.get("x-rate-limit-reset")
        if reset and reset.isdigit():
            return max(0.0, float(reset) - self._clock()) + 1.0
        return 60.0

    @staticmethod
    def _error_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return ""
        if isinstance(body, dict):
            title = body.get("title") or body.get("detail")
            if isinstance(title, str):
                return f": {title[:200]}"
        return ""

    @staticmethod
    def _parse_json(resp: requests.Response, path: str) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            raise XResponseError(f"X API returned non-JSON content for {path}") from None
        if not isinstance(body, dict):
            raise XResponseError(f"X API returned an unexpected JSON shape for {path}")
        return body

    # -- endpoints -----------------------------------------------------

    def get_user_by_username(self, username: str) -> dict[str, str]:
        body = self._get(f"/users/by/username/{username}")
        data = body.get("data")
        if not isinstance(data, dict):
            if body.get("errors"):
                raise XNotFoundError(f"X user @{username} was not found or is unavailable")
            raise XResponseError("User lookup response has no 'data' object")
        user_id, name = data.get("id"), data.get("username")
        if not (isinstance(user_id, str) and user_id.isdigit() and isinstance(name, str)):
            raise XResponseError("User lookup response is missing a valid id/username")
        return {"id": user_id, "username": name}

    def get_user_posts(
        self,
        user_id: str,
        start_time: datetime,
        max_pages: int = 5,
        page_size: int = 100,
        exclude: tuple[str, ...] = ("replies", "retweets"),
    ) -> TimelinePage:
        """Fetch the user's posts created at or after start_time, newest first."""
        result = TimelinePage()
        params: dict[str, Any] = {
            "max_results": page_size,
            "start_time": format_iso(start_time),
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": "username",
        }
        if exclude:
            params["exclude"] = ",".join(exclude)

        next_token: Optional[str] = None
        while True:
            if next_token:
                params["pagination_token"] = next_token
            body = self._get(f"/users/{user_id}/tweets", params=dict(params))
            result.pages_fetched += 1

            meta = body.get("meta")
            data = body.get("data", [])
            if not isinstance(meta, dict):
                if body.get("errors"):
                    raise XResponseError(f"Timeline request returned errors: {_error_titles(body)}")
                raise XResponseError("Timeline response has no 'meta' object")
            if not isinstance(data, list):
                raise XResponseError("Timeline 'data' is not a list")
            result.posts.extend(p for p in data if isinstance(p, dict))
            self.posts_read += len(data)
            users = (body.get("includes") or {}).get("users", [])
            if isinstance(users, list):
                result.users.extend(u for u in users if isinstance(u, dict))

            next_token = meta.get("next_token")
            if not next_token:
                return result
            if result.pages_fetched >= max_pages:
                result.truncated = True
                log.warning("Stopped after %d pages; more posts remain in the scan window", max_pages)
                return result

    def get_posts_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Batch-lookup posts (100 per request). Deleted posts are silently absent."""
        found: list[dict[str, Any]] = []
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            body = self._get("/tweets", params={"ids": ",".join(chunk), "tweet.fields": "public_metrics,created_at"})
            data = body.get("data", [])
            if not isinstance(data, list):
                raise XResponseError("Post lookup 'data' is not a list")
            found.extend(p for p in data if isinstance(p, dict))
            self.posts_read += len(data)
        return found


def _error_titles(body: dict[str, Any]) -> str:
    titles = []
    for err in body.get("errors", [])[:3]:
        if isinstance(err, dict):
            titles.append(str(err.get("title") or err.get("detail") or "error")[:120])
    return "; ".join(titles) or "unknown error"
