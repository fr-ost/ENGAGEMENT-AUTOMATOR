"""SMM panel "API v2" client (default provider: BulkFollows).

Request format, checked against https://bulkfollows.com/api:

  * one endpoint, https://bulkfollows.com/api/v2 (override with SMM_API_URL to
    use another panel that speaks the same v2 protocol)
  * HTTP POST, form-encoded, with parameters `key` and `action`
  * actions used here: `balance`, `services`, `status`, `add`
  * `add` (Default type) takes `service`, `link`, `quantity` -> {"order": 23501}
  * errors returned as a JSON object with an `error` string

Every response is validated; anything that does not match is reported as an
error rather than guessed at. The client is disabled unless SMM_ENABLED=true.

Order placement (action=add) is gated behind SMM_AUTO_ORDER=true and is only
called from campaign_manager.auto_place_orders for newly discovered posts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

ACTIONS = {
    "balance": "balance",
    "services": "services",
    "status": "status",
    "add": "add",  # place a new order; only called when SMM_AUTO_ORDER=true
}

_AUTH_HINTS = ("key", "auth", "unauthor", "forbidden", "token")


class SMMError(Exception):
    """Base error. Messages never contain the API key."""


class SMMAuthError(SMMError):
    """The API key was rejected."""


class SMMResponseError(SMMError):
    """The response did not match the expected format."""


@dataclass(frozen=True)
class Balance:
    amount: float
    currency: Optional[str]


@dataclass(frozen=True)
class OrderResult:
    order_id: str  # the panel's numeric order reference, kept as a string


@dataclass(frozen=True)
class OrderStatus:
    reference: str
    status: str
    charge: Optional[float]
    currency: Optional[str]
    start_count: Optional[int]
    remains: Optional[int]


def _to_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SMMResponseError(f"'{field_name}' is not numeric") from None


def _opt_float(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> Optional[int]:
    try:
        return None if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return None


class SMMClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        parsed = urlparse(api_url or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("SMM API URL must use https://")
        if not api_key:
            raise ValueError("SMM API key is required")
        self._url = api_url
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep
        self.request_count = 0

    def _post(self, action: str, **params: Any) -> Any:
        data = {"key": self._key, "action": ACTIONS[action], **params}
        attempt = 0
        while True:
            attempt += 1
            self.request_count += 1
            try:
                resp = self._session.post(self._url, data=data, timeout=self._timeout)
            except requests.Timeout:
                if attempt > self._max_retries:
                    raise SMMError(f"SMM API timed out on '{action}' after {attempt} attempts") from None
                self._backoff(attempt, action)
                continue
            except requests.RequestException as exc:
                if attempt > self._max_retries:
                    raise SMMError(f"SMM API network error on '{action}': {type(exc).__name__}") from None
                self._backoff(attempt, action)
                continue

            if resp.status_code in (401, 403):
                raise SMMAuthError(f"SMM API rejected the API key (HTTP {resp.status_code}). Check SMM_API_KEY.")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise SMMError(f"SMM API HTTP {resp.status_code} on '{action}' after {attempt} attempts")
                self._backoff(attempt, action)
                continue
            if resp.status_code != 200:
                raise SMMError(f"SMM API returned HTTP {resp.status_code} on '{action}'")
            try:
                body = resp.json()
            except ValueError:
                raise SMMResponseError(f"SMM API returned non-JSON content for '{action}'") from None
            if isinstance(body, dict) and "error" in body:
                message = str(body.get("error"))[:200].replace(self._key, "***")
                if any(hint in message.lower() for hint in _AUTH_HINTS):
                    raise SMMAuthError(f"SMM API authentication failed: {message}")
                raise SMMError(f"SMM API error on '{action}': {message}")
            return body

    def _backoff(self, attempt: int, action: str) -> None:
        delay = min(2 ** (attempt - 1), 30)
        log.warning("Retrying SMM API '%s' (attempt %d) in %ss", action, attempt, delay)
        self._sleep(delay)

    # -- read-only operations -------------------------------------------

    def get_balance(self) -> Balance:
        body = self._post("balance")
        if not isinstance(body, dict) or "balance" not in body:
            raise SMMResponseError("Balance response has no 'balance' field")
        currency = body.get("currency")
        return Balance(_to_float(body["balance"], "balance"), currency if isinstance(currency, str) else None)

    def list_services(self) -> list[dict[str, Any]]:
        body = self._post("services")
        if not isinstance(body, list):
            raise SMMResponseError("Services response is not a list")
        services = []
        for item in body:
            if not isinstance(item, dict) or "service" not in item:
                raise SMMResponseError("Services list contains an entry without a 'service' ID")
            services.append(item)
        return services

    def get_service(self, service_id: str, services: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
        """Return metadata for one service ID, or None if the panel does not list it."""
        services = self.list_services() if services is None else services
        for item in services:
            if str(item.get("service")) == str(service_id):
                return item
        return None

    def get_order_status(self, reference: str) -> OrderStatus:
        """Status of an existing order/campaign that the account owner placed and authorized."""
        if not str(reference).strip():
            raise ValueError("reference is required")
        body = self._post("status", order=str(reference))
        if not isinstance(body, dict) or not isinstance(body.get("status"), str):
            raise SMMResponseError("Status response has no 'status' string")
        currency = body.get("currency")
        return OrderStatus(
            reference=str(reference),
            status=body["status"],
            charge=_opt_float(body.get("charge")),
            currency=currency if isinstance(currency, str) else None,
            start_count=_opt_int(body.get("start_count")),
            remains=_opt_int(body.get("remains")),
        )

    def place_order(self, service_id: str, link: str, quantity: int) -> OrderResult:
        """Submit a new order to the SMM panel (action=add).

        Only called when SMM_AUTO_ORDER=true. The panel's standard response is
        {"order": <integer>}. Raises SMMError on any failure.
        """
        if not str(service_id).strip():
            raise ValueError("service_id is required")
        if not link.startswith("https://"):
            raise ValueError("link must be an https:// URL")
        if quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        body = self._post("add", service=str(service_id), link=link, quantity=str(quantity))
        if not isinstance(body, dict) or "order" not in body:
            raise SMMResponseError("Order response has no 'order' field")
        return OrderResult(order_id=str(body["order"]))


def summarize_service(item: dict[str, Any]) -> dict[str, Any]:
    """Keep only descriptive metadata fields for reports."""
    keys = ("service", "name", "type", "category", "rate", "min", "max")
    return {k: item[k] for k in keys if k in item}
