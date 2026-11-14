from urllib.parse import parse_qs

import pytest
import requests

from src import smm_client
from src.smm_client import SMMAuthError, SMMClient, SMMError, SMMResponseError, summarize_service, OrderResult

URL = "https://panel.example.test/api/v2"
KEY = "smm-secret-key-123"


def no_sleep(_):
    return None


@pytest.fixture
def client():
    return SMMClient(URL, KEY, timeout=5, max_retries=2, sleep=no_sleep)


def form(call):
    return {k: v[0] for k, v in parse_qs(call.request.body).items()}


def test_balance_request_and_parsing(mocked, client):
    mocked.post(URL, json={"balance": "100.84", "currency": "USD"})
    balance = client.get_balance()
    assert balance.amount == pytest.approx(100.84)
    assert balance.currency == "USD"
    sent = form(mocked.calls[0])
    assert sent == {"key": KEY, "action": "balance"}
    assert client.request_count == 1


def test_list_services_and_inspect_one(mocked, client):
    services = [
        {"service": 1, "name": "Service A", "type": "Default", "category": "X", "rate": "0.90", "min": "50", "max": "10000"},
        {"service": 2, "name": "Service B", "type": "Default", "category": "X", "rate": "1.20", "min": "10", "max": "5000", "extra": "x"},
    ]
    mocked.post(URL, json=services)
    listed = client.list_services()
    assert len(listed) == 2
    item = client.get_service("2", listed)
    assert item["name"] == "Service B"
    assert client.get_service("999", listed) is None
    assert summarize_service(item) == {k: item[k] for k in ("service", "name", "type", "category", "rate", "min", "max")}


def test_order_status_is_read_only_lookup(mocked, client):
    mocked.post(URL, json={"charge": "0.27819", "start_count": "3572", "status": "Partial", "remains": "157", "currency": "USD"})
    status = client.get_order_status("23501")
    assert status.status == "Partial"
    assert status.charge == pytest.approx(0.27819)
    assert status.remains == 157
    assert form(mocked.calls[0]) == {"key": KEY, "action": "status", "order": "23501"}


def test_place_order_success(mocked, client):
    mocked.post(URL, json={"order": 98765})
    result = client.place_order("7", "https://x.com/user/status/1234", 1000)
    assert isinstance(result, OrderResult)
    assert result.order_id == "98765"
    sent = form(mocked.calls[0])
    assert sent == {"key": KEY, "action": "add", "service": "7", "link": "https://x.com/user/status/1234", "quantity": "1000"}


def test_place_order_response_missing_order_field(mocked, client):
    mocked.post(URL, json={"status": "ok"})
    with pytest.raises(SMMResponseError, match="'order'"):
        client.place_order("7", "https://x.com/user/status/1234", 1000)


def test_place_order_validation():
    c = SMMClient(URL, KEY, max_retries=0, sleep=no_sleep)
    with pytest.raises(ValueError):
        c.place_order("", "https://x.com/u/status/1", 100)
    with pytest.raises(ValueError):
        c.place_order("7", "http://x.com/u/status/1", 100)  # not https
    with pytest.raises(ValueError):
        c.place_order("7", "https://x.com/u/status/1", 0)


def test_add_action_is_in_actions():
    assert "add" in smm_client.ACTIONS


@pytest.mark.parametrize("message", ["Invalid API key", "Incorrect key"])
def test_authentication_error_in_body(mocked, client, message):
    mocked.post(URL, json={"error": message})
    with pytest.raises(SMMAuthError) as excinfo:
        client.get_balance()
    assert KEY not in str(excinfo.value)


def test_authentication_error_http_status(mocked, client):
    mocked.post(URL, status=401)
    with pytest.raises(SMMAuthError):
        client.get_balance()
    assert len(mocked.calls) == 1


def test_api_error_does_not_leak_key(mocked, client):
    mocked.post(URL, json={"error": f"bad request for {KEY}"})
    with pytest.raises(SMMError) as excinfo:
        client.get_balance()
    assert KEY not in str(excinfo.value)


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("get_balance", {"json": {"currency": "USD"}}),
        ("get_balance", {"json": {"balance": "abc"}}),
        ("get_balance", {"body": "not json"}),
        ("list_services", {"json": {"services": []}}),
        ("list_services", {"json": [{"name": "no id"}]}),
    ],
)
def test_malformed_responses(mocked, client, method, kwargs):
    mocked.post(URL, **kwargs)
    with pytest.raises(SMMResponseError):
        getattr(client, method)()


def test_malformed_status(mocked, client):
    mocked.post(URL, json={"charge": "1"})
    with pytest.raises(SMMResponseError):
        client.get_order_status("1")


def test_timeout_retries_are_bounded(mocked, client):
    mocked.post(URL, body=requests.Timeout())
    with pytest.raises(SMMError, match="timed out"):
        client.get_balance()
    assert len(mocked.calls) == 3


def test_server_error_then_success(mocked, client):
    mocked.post(URL, status=502)
    mocked.post(URL, json={"balance": "5", "currency": "USD"})
    assert client.get_balance().amount == 5.0


def test_https_required():
    with pytest.raises(ValueError):
        SMMClient("http://panel.example.test/api/v2", KEY)
    with pytest.raises(ValueError):
        SMMClient(URL, "")
