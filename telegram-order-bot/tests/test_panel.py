from urllib.parse import parse_qs

import pytest
import requests
import responses

from orderbot.panel import Panel, PanelRejected, PanelUncertain

URL = "https://panel.example.test/api/v2"
KEY = "secret-key-123"


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def panel():
    return Panel(URL, KEY, sleep=lambda _: None)


def test_add_sends_documented_fields(mocked, panel):
    mocked.post(URL, json={"order": 23501})
    assert panel.add("7", "https://x.com/u/status/1", 412) == "23501"
    sent = {k: v[0] for k, v in parse_qs(mocked.calls[0].request.body).items()}
    assert sent == {"key": KEY, "action": "add", "service": "7", "link": "https://x.com/u/status/1", "quantity": "412"}


def test_add_rejection_is_definite_and_hides_key(mocked, panel):
    mocked.post(URL, json={"error": f"Not enough funds for {KEY}"})
    with pytest.raises(PanelRejected) as excinfo:
        panel.add("7", "https://x.com/u/status/1", 100)
    assert KEY not in str(excinfo.value)


@pytest.mark.parametrize("kwargs", [{"body": requests.Timeout()}, {"status": 502}, {"body": "<html>"}, {"json": {"ok": 1}}])
def test_add_is_never_retried_when_outcome_is_unclear(mocked, panel, kwargs):
    mocked.post(URL, **kwargs)
    with pytest.raises(PanelUncertain):
        panel.add("7", "https://x.com/u/status/1", 100)
    assert len(mocked.calls) == 1


def test_reads_are_retried(mocked, panel):
    mocked.post(URL, status=502)
    mocked.post(URL, json={"balance": "100.84292", "currency": "USD"})
    assert panel.balance() == (100.84292, "USD")


def test_multi_status(mocked, panel):
    mocked.post(URL, json={"1": {"status": "Partial", "remains": "157"}, "10": {"error": "Incorrect order ID"}})
    assert panel.statuses(["1", "10"])["1"]["status"] == "Partial"
    assert parse_qs(mocked.calls[0].request.body)["orders"] == ["1,10"]
