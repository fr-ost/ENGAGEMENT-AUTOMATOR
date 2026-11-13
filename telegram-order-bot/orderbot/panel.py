"""SMM panel "API v2" client (BulkFollows format, https://bulkfollows.com/api).

POST form fields `key` + `action` to one endpoint. Errors come back as
{"error": "..."}.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests


class PanelError(Exception):
    """Base error. Messages never contain the API key."""


class PanelRejected(PanelError):
    """The panel answered with an error: the request definitely had no effect."""


class PanelUncertain(PanelError):
    """No usable answer (timeout, network, 5xx): an order may or may not exist."""


class Panel:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        timeout: float = 20.0,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._url = api_url
        self._key = api_key
        self._timeout = timeout
        self._session = session or requests.Session()
        self._sleep = sleep

    def _post(self, action: str, retries: int, **params: str) -> Any:
        data = {"key": self._key, "action": action, **params}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.post(self._url, data=data, timeout=self._timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise PanelUncertain(f"panel returned HTTP {resp.status_code}")
                try:
                    body = resp.json()
                except ValueError:
                    raise PanelUncertain(f"panel returned non-JSON (HTTP {resp.status_code})") from None
            except requests.RequestException as exc:
                error: PanelError = PanelUncertain(f"panel unreachable ({type(exc).__name__})")
            except PanelUncertain as exc:
                error = exc
            else:
                if isinstance(body, dict) and "error" in body:
                    raise PanelRejected(str(body["error"])[:300].replace(self._key, "***"))
                return body
            if attempt > retries:
                raise error
            self._sleep(2 * attempt)

    def balance(self) -> tuple[float, str]:
        body = self._post("balance", retries=2)
        try:
            return float(body["balance"]), str(body.get("currency") or "")
        except (KeyError, TypeError, ValueError):
            raise PanelError("unexpected balance response") from None

    def services(self) -> list[dict[str, Any]]:
        body = self._post("services", retries=2)
        if not isinstance(body, list):
            raise PanelError("unexpected services response")
        return [s for s in body if isinstance(s, dict) and "service" in s]

    def add(self, service_id: str, link: str, quantity: int) -> str:
        """Place one order. Never retried here: a retry after a timeout could buy twice."""
        body = self._post("add", retries=0, service=str(service_id), link=link, quantity=str(quantity))
        if not isinstance(body, dict) or "order" not in body:
            raise PanelUncertain("panel response had no 'order' id")
        return str(body["order"])

    def statuses(self, order_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Status per order id (up to 100 per call, per the panel docs)."""
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(order_ids), 100):
            chunk = order_ids[start : start + 100]
            body = self._post("status", retries=2, orders=",".join(chunk))
            if isinstance(body, dict):
                result.update({str(k): v for k, v in body.items() if isinstance(v, dict)})
        return result
