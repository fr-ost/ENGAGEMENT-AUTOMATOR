import pytest
import requests

from src.telegram import CHUNK_SIZE, TelegramError, TelegramNotifier, split_message

TOKEN = "123456:telegram-secret-token"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def notifier():
    return TelegramNotifier(TOKEN, "42", timeout=5, max_retries=2, sleep=lambda _: None)


def test_split_message_respects_limit_and_keeps_content():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_SIZE for c in chunks)
    assert "\n".join(chunks) == text


def test_split_message_hard_splits_very_long_line():
    chunks = split_message("a" * 9000, limit=4000)
    assert [len(c) for c in chunks] == [4000, 4000, 1000]


def test_send_success_multiple_chunks(mocked):
    mocked.post(URL, json={"ok": True, "result": {}})
    sent = notifier().send("\n".join("y" * 100 for _ in range(100)))
    assert sent == 3
    assert len(mocked.calls) == 3


def test_server_errors_retry_then_fail_without_leaking_token(mocked):
    mocked.post(URL, status=500)
    with pytest.raises(TelegramError) as excinfo:
        notifier().send("hi")
    assert len(mocked.calls) == 3
    assert TOKEN not in str(excinfo.value)


def test_timeout_raises_telegram_error(mocked):
    mocked.post(URL, body=requests.Timeout())
    with pytest.raises(TelegramError, match="timed out"):
        notifier().send("hi")


def test_connection_error_hides_url(mocked):
    mocked.post(URL, body=requests.ConnectionError(f"failed to reach {URL}"))
    with pytest.raises(TelegramError) as excinfo:
        notifier().send("hi")
    assert TOKEN not in str(excinfo.value)


def test_bad_token(mocked):
    mocked.post(URL, status=401, json={"ok": False, "description": "Unauthorized"})
    with pytest.raises(TelegramError, match="bot token"):
        notifier().send("hi")
    assert len(mocked.calls) == 1


def test_rate_limit_uses_retry_after(mocked):
    sleeps = []
    n = TelegramNotifier(TOKEN, "42", max_retries=2, sleep=sleeps.append)
    mocked.post(URL, status=429, json={"ok": False, "parameters": {"retry_after": 3}})
    mocked.post(URL, json={"ok": True})
    assert n.send("hi") == 1
    assert sleeps == [3.0]


def test_ok_false_is_failure(mocked):
    mocked.post(URL, json={"ok": False})
    with pytest.raises(TelegramError):
        notifier().send("hi")
