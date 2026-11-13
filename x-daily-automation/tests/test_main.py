"""End-to-end tests of the daily run with every network call mocked."""

import json
from datetime import timedelta

import pytest

import main
from src import storage
from src.smm_client import SMMClient
from src.telegram import TelegramNotifier
from src.x_client import XClient
from tests.conftest import BEARER, NOW, TIMELINE_URL, USER_URL, no_sleep, raw_post, timeline_body, user_body

SMM_URL = "https://panel.example.test/api/v2"
TG_TOKEN = "999:tg-token"
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"


def client():
    return XClient(BEARER, max_retries=1, max_rate_limit_wait=5, sleep=no_sleep, clock=lambda: 0.0)


def read(cfg, name):
    return json.loads((cfg.data_dir / name).read_text())


def report(cfg, date="2026-10-26"):
    # 17:00 UTC is 23:00 in Bangladesh, the same calendar day.
    return (cfg.reports_dir / f"{date}.md").read_text()


TWO_POSTS = [
    raw_post("1866000000000000002", "2026-10-26T12:00:00.000Z", metrics={"impression_count": 10, "like_count": 1}),
    raw_post("1866000000000000001", "2026-10-26T09:00:00.000Z"),
]


def test_first_run_discovers_posts_and_persists(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))

    assert main.run(cfg, now=NOW, x_client=client()) == 0

    state = read(cfg, "state.json")
    assert state["last_successful_scan_at"] == "2026-10-26T17:00:00Z"
    assert state["account"] == {"user_id": "1234567890", "username": "ExampleUser"}
    posts = read(cfg, "posts.json")["posts"]
    assert set(posts) == {"1866000000000000001", "1866000000000000002"}
    assert posts["1866000000000000001"]["post_url"] == "https://x.com/ExampleUser/status/1866000000000000001"
    plans = read(cfg, "campaigns.json")["plans"]
    assert plans["1866000000000000002"]["targets"] == {"impressions": 1000, "likes": 30}
    text = report(cfg)
    assert "New posts this run: 2" in text
    assert "https://x.com/ExampleUser/status/1866000000000000002" in text
    assert "First run" in text
    assert not (cfg.data_dir / ".run.lock").exists()


def test_rerun_is_idempotent_with_overlapping_window(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    first_posts = read(cfg, "posts.json")

    # Same posts come back again (overlap); nothing new should be recorded.
    later = NOW + timedelta(minutes=30)  # 23:30 Bangladesh time, same report day
    assert main.run(cfg, now=later, x_client=client()) == 0
    second_posts = read(cfg, "posts.json")
    assert set(second_posts["posts"]) == set(first_posts["posts"])
    for post_id, record in first_posts["posts"].items():
        assert second_posts["posts"][post_id]["discovered_at"] == record["discovered_at"]
        assert second_posts["posts"][post_id]["status"] == record["status"]
    assert read(cfg, "campaigns.json")["plans"].keys() == first_posts["posts"].keys()
    state = read(cfg, "state.json")
    assert state["last_run"]["new_posts"] == 0
    assert state["last_run"]["duplicates_skipped"] == 2
    assert len(state["run_history"]) == 2
    # Second scan starts at the previous checkpoint minus the 2h overlap.
    second_timeline = [c for c in mocked.calls if "/tweets" in c.request.url][-1]
    assert second_timeline.request.params["start_time"] == "2026-10-26T15:00:00Z"
    # Account was cached, so the username lookup happened only once.
    assert sum("by/username" in c.request.url for c in mocked.calls) == 1
    assert "New posts this run: 0" in report(cfg)


def test_new_post_on_next_day_is_added(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    main.run(cfg, now=NOW, x_client=client())

    mocked.replace("GET", TIMELINE_URL, json=timeline_body([raw_post("1866000000000000003", "2026-10-27T08:00:00Z")]))
    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 0
    assert len(read(cfg, "posts.json")["posts"]) == 3
    assert "New posts this run: 1" in report(cfg, "2026-10-27")


def test_empty_scan_reports_zero(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json={"meta": {"result_count": 0}})
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    assert "New posts this run: 0" in report(cfg)
    assert "No new posts were found" in report(cfg)
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T17:00:00Z"


def _seed_successful_run(mocked, cfg):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(cfg, now=NOW, x_client=client()) == 0


def test_failed_scan_preserves_checkpoint(mocked, make_config):
    cfg = make_config()
    _seed_successful_run(mocked, cfg)
    mocked.replace("GET", TIMELINE_URL, status=503)

    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 1
    state = read(cfg, "state.json")
    assert state["last_successful_scan_at"] == "2026-10-26T17:00:00Z"
    assert state["last_run"]["scan_status"] == "FAILED"
    text = report(cfg, "2026-10-27")
    assert "FAILED" in text and "X scan failed" in text


def test_auth_failure_preserves_checkpoint_and_hides_token(mocked, make_config):
    cfg = make_config()
    _seed_successful_run(mocked, cfg)
    mocked.replace("GET", TIMELINE_URL, status=401, json={"title": "Unauthorized"})
    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 1
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T17:00:00Z"
    text = report(cfg, "2026-10-27")
    assert "X authentication failed" in text
    assert BEARER not in text


def test_truncated_scan_saves_posts_but_not_checkpoint(mocked, make_config):
    cfg = make_config(X_MAX_PAGES=1)
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS, next_token="more"))
    assert main.run(cfg, now=NOW, x_client=client()) == 1
    assert read(cfg, "state.json")["last_successful_scan_at"] is None
    assert len(read(cfg, "posts.json")["posts"]) == 2
    assert "INCOMPLETE" in report(cfg)


def test_persistence_failure_is_reported_and_checkpoint_not_advanced(mocked, make_config, monkeypatch):
    cfg = make_config()
    _seed_successful_run(mocked, cfg)
    real = storage.atomic_write_json

    def fail_state(path, data):
        if path.name == "state.json":
            raise storage.PersistenceError("simulated disk failure")
        real(path, data)

    monkeypatch.setattr(storage, "atomic_write_json", fail_state)
    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 1
    monkeypatch.undo()
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T17:00:00Z"
    assert "Saving state failed" in report(cfg, "2026-10-27")


def test_corrupt_state_stops_the_run_without_touching_files(mocked, make_config):
    cfg = make_config()
    _seed_successful_run(mocked, cfg)
    (cfg.data_dir / "state.json").write_text("{broken")
    calls_before = len(mocked.calls)
    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 3
    assert (cfg.data_dir / "state.json").read_text() == "{broken"
    assert len(mocked.calls) == calls_before  # no API calls made


def test_lock_held_returns_3(mocked, make_config):
    cfg = make_config()
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / ".run.lock").write_text("{}")
    assert main.run(cfg, now=NOW, x_client=client()) == 3
    assert len(mocked.calls) == 0


def test_telegram_failure_does_not_fail_successful_scan(mocked, make_config):
    cfg = make_config(TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    mocked.post(TG_URL, status=500)
    notifier = TelegramNotifier(TG_TOKEN, "42", max_retries=1, sleep=no_sleep)

    assert main.run(cfg, now=NOW, x_client=client(), notifier=notifier) == 0
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T17:00:00Z"
    text = report(cfg)
    assert "Telegram | FAILED" in text
    assert TG_TOKEN not in text


def test_telegram_success_message_content(mocked, make_config):
    cfg = make_config(TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    mocked.post(TG_URL, json={"ok": True})
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    sent = json.loads(mocked.calls[-1].request.body)["text"]
    assert "2026-10-26 23:00 (Bangladesh time)" in sent
    assert "New posts: 2" in sent
    assert "https://x.com/ExampleUser/status/1866000000000000001" in sent
    assert "1,000 impressions, 30 likes" in sent
    assert "Telegram | sent (1 message)" in report(cfg)


def test_smm_balance_and_service_metadata_in_report(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key", SMM_LIKE_SERVICE_ID="7")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([]))
    mocked.post(SMM_URL, json={"balance": "12.50", "currency": "USD"})
    mocked.post(SMM_URL, json=[{"service": 7, "name": "Svc", "rate": "1.0", "min": "10", "max": "100"}])
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)

    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    text = report(cfg)
    assert "12.50 USD" in text
    assert "name: Svc" in text
    assert "smm-key" not in text


def test_auto_order_placed_for_new_posts(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_AUTO_ORDER="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key",
                      SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    mocked.post(SMM_URL, json={"balance": "50.00", "currency": "USD"})   # balance
    mocked.post(SMM_URL, json={"order": 11111})  # impression order post 1
    mocked.post(SMM_URL, json={"order": 11112})  # like order post 1
    mocked.post(SMM_URL, json={"order": 11113})  # impression order post 2
    mocked.post(SMM_URL, json={"order": 11114})  # like order post 2
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)

    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    campaigns = read(cfg, "campaigns.json")["plans"]
    for pid in ("1866000000000000001", "1866000000000000002"):
        types_ordered = {c["type"] for c in campaigns[pid]["authorized_campaigns"]}
        assert types_ordered == {"impressions", "likes"}
        assert all(c["provider"] == "example" for c in campaigns[pid]["authorized_campaigns"])
    text = report(cfg)
    assert "Auto-orders placed this run: 4" in text
    assert "auto-ordered via example" in text


def test_auto_order_is_idempotent_on_rerun(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_AUTO_ORDER="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key",
                      SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    # First run: balance + 4 orders
    for body in [{"balance": "50.00", "currency": "USD"}, {"order": 1}, {"order": 2}, {"order": 3}, {"order": 4}]:
        mocked.post(SMM_URL, json=body)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    order_calls_first = smm.request_count

    # Second run: same posts come back; no new orders should be placed.
    mocked.replace("GET", TIMELINE_URL, json=timeline_body(TWO_POSTS))
    mocked.post(SMM_URL, json={"balance": "50.00", "currency": "USD"})
    smm2 = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW + timedelta(minutes=30), x_client=client(), smm_client=smm2) == 0

    # No additional place_order calls in second run (only a balance check at most).
    campaigns = read(cfg, "campaigns.json")["plans"]
    for pid in ("1866000000000000001", "1866000000000000002"):
        assert len(campaigns[pid]["authorized_campaigns"]) == 2  # unchanged from first run


def test_auto_order_failure_does_not_fail_scan(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_AUTO_ORDER="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key",
                      SMM_IMPRESSION_SERVICE_ID="5")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    mocked.post(SMM_URL, json={"balance": "0.00", "currency": "USD"})
    mocked.post(SMM_URL, json={"error": "Insufficient balance"})
    mocked.post(SMM_URL, json={"error": "Insufficient balance"})
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)

    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    text = report(cfg)
    assert "Auto-orders placed" not in text  # no orders in table


def test_auto_order_skipped_in_dry_run(mocked, make_config):
    from urllib.parse import parse_qs
    cfg = make_config(DRY_RUN="true", SMM_ENABLED="true", SMM_AUTO_ORDER="true",
                      SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key", SMM_IMPRESSION_SERVICE_ID="5")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    # No "add" order calls made (dry_run blocks ordering)
    add_calls = [
        c for c in mocked.calls
        if c.request.method == "POST" and
        parse_qs(c.request.body).get("action", [""])[0] == "add"
    ]
    assert len(add_calls) == 0


def test_smm_failure_is_not_fatal(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([]))
    mocked.post(SMM_URL, json={"error": "Invalid API key"})
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    assert "SMM panel balance unavailable" in report(cfg)


def test_dry_run_writes_nothing(mocked, make_config, capsys):
    cfg = make_config(DRY_RUN="true")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    assert not (cfg.data_dir / "state.json").exists()
    assert not cfg.reports_dir.exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_metrics_refresh_for_tracked_posts(mocked, make_config):
    cfg = make_config(METRICS_LOOKBACK_DAYS=7)
    _seed_successful_run(mocked, cfg)
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
    mocked.get(
        "https://api.x.com/2/tweets",
        json={"data": [{"id": "1866000000000000001", "public_metrics": {"impression_count": 2000, "like_count": 40}}]},
    )
    assert main.run(cfg, now=NOW + timedelta(days=1), x_client=client()) == 0
    plan = read(cfg, "campaigns.json")["plans"]["1866000000000000001"]
    assert plan["promotion_status"] == "TARGETS_REACHED"
    assert plan["performance"]["impressions"] == 2000


def test_estimated_cost_only_with_configured_prices(mocked, make_config):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    cfg = make_config()
    main.run(cfg, now=NOW, x_client=client())
    assert "unknown — set X_COST_PER_REQUEST_USD" in report(cfg)

    priced = make_config(X_COST_PER_POST_READ_USD="0.005", DATA_DIR=str(cfg.data_dir) + "2")
    main.run(priced, now=NOW, x_client=client())
    assert "$0.0100" in report(priced)  # 2 posts read x $0.005


def test_main_returns_2_on_config_error(monkeypatch):
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: 0)  # never read a developer's real .env
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_USERNAME", raising=False)
    monkeypatch.delenv("X_USER_ID", raising=False)
    assert main.main() == 2


@pytest.mark.parametrize("secret_env", ["X_BEARER_TOKEN"])
def test_logs_do_not_contain_secrets(mocked, make_config, caplog, secret_env):
    cfg = make_config()
    main.setup_logging(cfg)
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, status=401)
    main.run(cfg, now=NOW, x_client=client())
    assert BEARER not in caplog.text
