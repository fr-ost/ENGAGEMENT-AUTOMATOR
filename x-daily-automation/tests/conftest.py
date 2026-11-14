import re
from datetime import datetime, timezone

import pytest
import responses

from src.config import load_config
from src.x_client import XClient

USER_ID = "1234567890"
USERNAME = "ExampleUser"
BEARER = "test-bearer-token-value"
NOW = datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)

TIMELINE_URL = re.compile(rf"https://api\.x\.com/2/users/{USER_ID}/tweets.*")
LOOKUP_URL = re.compile(r"https://api\.x\.com/2/tweets\?.*")
USER_URL = f"https://api.x.com/2/users/by/username/{USERNAME}"


def no_sleep(_seconds):
    return None


@pytest.fixture
def env(tmp_path):
    return {
        "X_BEARER_TOKEN": BEARER,
        "X_USERNAME": USERNAME,
        "DATA_DIR": str(tmp_path / "data"),
        "REPORTS_DIR": str(tmp_path / "reports"),
        "METRICS_LOOKBACK_DAYS": "0",
    }


@pytest.fixture
def make_config(env):
    def _make(**overrides):
        merged = dict(env)
        merged.update({k: str(v) for k, v in overrides.items()})
        return load_config(merged)

    return _make


@pytest.fixture
def x_client():
    return XClient(BEARER, timeout=5, max_retries=2, max_rate_limit_wait=60, sleep=no_sleep, clock=lambda: 1_000_000.0)


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def raw_post(post_id, created_at="2026-10-26T10:00:00.000Z", text="hello", refs=None, metrics=None, **extra):
    post = {"id": str(post_id), "text": text, "created_at": created_at, "author_id": USER_ID}
    if refs:
        post["referenced_tweets"] = [{"type": t, "id": "1"} for t in refs]
    if metrics is not None:
        post["public_metrics"] = metrics
    post.update(extra)
    return post


def timeline_body(posts, next_token=None, username=USERNAME):
    meta = {"result_count": len(posts)}
    if posts:
        meta["newest_id"] = posts[0]["id"]
        meta["oldest_id"] = posts[-1]["id"]
    if next_token:
        meta["next_token"] = next_token
    body = {"meta": meta}
    if posts:
        body["data"] = posts
        body["includes"] = {"users": [{"id": USER_ID, "username": username, "name": "Example"}]}
    return body


def user_body():
    return {"data": {"id": USER_ID, "username": USERNAME, "name": "Example"}}
