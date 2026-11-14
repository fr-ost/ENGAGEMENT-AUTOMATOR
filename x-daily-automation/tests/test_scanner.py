from datetime import timedelta

import pytest
import requests

from src.scanner import Scanner, ScanError, canonical_url, classify, compute_window_start
from src.x_client import XAPIError, XAuthError, XRateLimitError, XResponseError
from tests.conftest import (
    BEARER,
    LOOKUP_URL,
    NOW,
    TIMELINE_URL,
    USER_ID,
    USER_URL,
    USERNAME,
    raw_post,
    timeline_body,
    user_body,
)

CACHED = {"user_id": USER_ID, "username": USERNAME}


def test_multiple_new_posts_are_filtered_sorted_and_have_canonical_urls(mocked, x_client, make_config):
    posts = [
        raw_post("105", "2026-10-26T12:00:00.000Z", refs=["quoted"]),
        raw_post("104", "2026-10-26T11:00:00.000Z", refs=["replied_to"]),
        raw_post("103", "2026-10-26T10:30:00.000Z", refs=["retweeted"]),
        raw_post("102", "2026-10-26T10:00:00.000Z"),
        raw_post("101", "2026-10-26T09:00:00.000Z", metrics={"impression_count": 50, "like_count": 2}),
    ]
    mocked.get(TIMELINE_URL, json=timeline_body(posts))

    result = Scanner(x_client, make_config()).scan(CACHED, None, NOW)

    assert [p.post_id for p in result.posts] == ["101", "102", "105"]  # oldest first
    assert result.posts[0].url == f"https://x.com/{USERNAME}/status/101"
    assert result.posts[0].public_metrics == {"impression_count": 50, "like_count": 2}
    assert result.posts[2].kind == "quote"
    assert result.skipped_replies == 1 and result.skipped_reposts == 1


def test_timeline_request_excludes_replies_and_reposts(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json=timeline_body([]))
    Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    params = mocked.calls[0].request.params
    assert params["exclude"] == "replies,retweets"
    assert params["start_time"] == "2026-10-25T17:00:00Z"
    assert "public_metrics" in params["tweet.fields"]


def test_quote_posts_can_be_excluded(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("2", refs=["quoted"]), raw_post("1")]))
    result = Scanner(x_client, make_config(INCLUDE_QUOTE_POSTS="false")).scan(CACHED, None, NOW)
    assert [p.post_id for p in result.posts] == ["1"]
    assert result.skipped_quotes == 1


def test_empty_scan(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json={"meta": {"result_count": 0}})
    result = Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert result.posts == []
    assert result.pages_fetched == 1
    assert not result.truncated


def test_pagination_follows_next_token(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("3"), raw_post("2")], next_token="tok1"))
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("1")]))
    result = Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert sorted(p.post_id for p in result.posts) == ["1", "2", "3"]
    assert result.pages_fetched == 2
    assert "pagination_token" not in mocked.calls[0].request.params
    assert mocked.calls[1].request.params["pagination_token"] == "tok1"


def test_pagination_stops_at_max_pages_and_marks_truncated(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("2")], next_token="more"))
    result = Scanner(x_client, make_config(X_MAX_PAGES=1)).scan(CACHED, None, NOW)
    assert result.truncated
    assert len(mocked.calls) == 1


def test_window_start_rules(make_config):
    cfg = make_config(SCAN_OVERLAP_HOURS=2, INITIAL_LOOKBACK_HOURS=24)
    assert compute_window_start(None, NOW, cfg) == NOW - timedelta(hours=24)
    assert compute_window_start("2026-10-25T17:00:00Z", NOW, cfg) == NOW - timedelta(hours=26)
    override = make_config(SCAN_SINCE="2026-10-01T00:00:00Z")
    assert compute_window_start("2026-10-25T17:00:00Z", NOW, override).isoformat() == "2026-10-01T00:00:00+00:00"


def test_future_window_start_is_rejected(mocked, x_client, make_config):
    cfg = make_config(SCAN_SINCE="2030-01-01T00:00:00Z")
    with pytest.raises(ScanError):
        Scanner(x_client, cfg).scan(CACHED, None, NOW)


def test_account_lookup_used_once_then_cached(mocked, x_client, make_config):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([]))
    scanner = Scanner(x_client, make_config())
    assert scanner.resolve_account(None) == (USER_ID, USERNAME)
    assert scanner.resolve_account(CACHED) == (USER_ID, USERNAME)
    assert sum(1 for c in mocked.calls if "by/username" in c.request.url) == 1


def test_configured_user_id_skips_lookup(mocked, x_client, make_config):
    cfg = make_config(X_USER_ID=USER_ID)
    assert Scanner(x_client, cfg).resolve_account(None) == (USER_ID, USERNAME)
    assert len(mocked.calls) == 0


def test_authentication_failure_is_not_retried_and_hides_token(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, status=401, json={"title": "Unauthorized"})
    with pytest.raises(XAuthError) as excinfo:
        Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert len(mocked.calls) == 1
    assert BEARER not in str(excinfo.value)


def test_rate_limit_waits_then_succeeds(mocked, make_config):
    from src.x_client import XClient

    sleeps = []
    client = XClient(BEARER, max_retries=2, max_rate_limit_wait=60, sleep=sleeps.append, clock=lambda: 1000.0)
    mocked.get(TIMELINE_URL, status=429, headers={"x-rate-limit-reset": "1010"})
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("1")]))
    result = Scanner(client, make_config()).scan(CACHED, None, NOW)
    assert [p.post_id for p in result.posts] == ["1"]
    assert sleeps == [11.0]


def test_rate_limit_too_long_raises(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, status=429, headers={"x-rate-limit-reset": str(1_000_000 + 900)})
    with pytest.raises(XRateLimitError):
        Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert len(mocked.calls) == 1


def test_timeout_is_retried_then_succeeds(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, body=requests.Timeout("slow"))
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("1")]))
    result = Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert len(result.posts) == 1


def test_timeouts_exhaust_bounded_retries(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, body=requests.Timeout("slow"))
    with pytest.raises(XAPIError, match="Timed out"):
        Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert len(mocked.calls) == 3  # 1 attempt + 2 retries


def test_server_errors_are_retried(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, status=503)
    mocked.get(TIMELINE_URL, json=timeline_body([]))
    Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert len(mocked.calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"body": "<html>not json</html>"},
        {"json": ["not", "an", "object"]},
        {"json": {"data": []}},  # no meta
        {"json": {"meta": {"result_count": 1}, "data": "nope"}},
        {"json": {"errors": [{"title": "Not Found Error"}]}},
    ],
)
def test_malformed_timeline_responses(mocked, x_client, make_config, kwargs):
    mocked.get(TIMELINE_URL, status=200, **kwargs)
    with pytest.raises(XResponseError):
        Scanner(x_client, make_config()).scan(CACHED, None, NOW)


def test_all_items_malformed_is_an_error(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json={"meta": {"result_count": 1}, "data": [{"id": "abc", "text": "x"}]})
    with pytest.raises(XResponseError):
        Scanner(x_client, make_config()).scan(CACHED, None, NOW)


def test_some_malformed_items_are_skipped(mocked, x_client, make_config):
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("2"), {"id": "bad"}, raw_post("3", created_at="nope")]))
    result = Scanner(x_client, make_config()).scan(CACHED, None, NOW)
    assert [p.post_id for p in result.posts] == ["2"]
    assert result.invalid_items == 2


def test_classify_and_canonical_url():
    assert classify(raw_post("1")) == "original"
    assert classify(raw_post("1", refs=["quoted"])) == "quote"
    assert classify(raw_post("1", refs=["retweeted"])) == "repost"
    assert classify(raw_post("1", in_reply_to_user_id="9")) == "reply"
    assert canonical_url("a", "1") == "https://x.com/a/status/1"


def test_lookup_requests_chunk_by_100(mocked, x_client):
    mocked.get(LOOKUP_URL, json={"data": []})
    x_client.get_posts_by_ids([str(i) for i in range(1, 251)])
    assert len(mocked.calls) == 3
