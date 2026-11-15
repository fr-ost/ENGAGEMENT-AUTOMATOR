"""End-to-end tests of the daily run with every network call mocked."""

import json
from datetime import timedelta

import pytest

import main
from src import storage
from src.config import parse_iso_datetime
from src.keepalive import Hooks, run_loop
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
    # Second scan asks only for posts newer than the newest one already seen.
    second_timeline = [c for c in mocked.calls if "/tweets" in c.request.url][-1]
    assert second_timeline.request.params["since_id"] == "1866000000000000002"
    assert "start_time" not in second_timeline.request.params
    assert "posts newer than ID 1866000000000000002" in report(cfg)
    # Account was cached, so the username lookup happened only once.
    assert sum("by/username" in c.request.url for c in mocked.calls) == 1
    assert "New posts this run: 0" in report(cfg)


def test_frequent_scans_read_each_post_once_with_a_daily_window_rescan(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    x = client()
    assert main.run(cfg, now=NOW, x_client=x) == 0
    assert x.posts_read == 2

    # Every 2 hours with nothing new: no posts are read at all.
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
    for hours in (2, 4, 6):
        x = client()
        assert main.run(cfg, now=NOW + timedelta(hours=hours), x_client=x) == 0
        assert x.posts_read == 0
    assert read(cfg, "state.json")["last_seen_post_id"] == "1866000000000000002"
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T23:00:00Z"

    # A new post is picked up by the next scan and becomes the new marker.
    mocked.replace("GET", TIMELINE_URL, json=timeline_body([raw_post("1866000000000000009", "2026-10-27T00:30:00Z")]))
    assert main.run(cfg, now=NOW + timedelta(hours=8), x_client=client()) == 0
    assert read(cfg, "state.json")["last_seen_post_id"] == "1866000000000000009"
    assert len(read(cfg, "posts.json")["posts"]) == 3

    # Once 24h have passed since the last window scan, one scan re-checks the overlap window.
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
    assert main.run(cfg, now=NOW + timedelta(hours=24), x_client=client()) == 0
    params = [c for c in mocked.calls if "/tweets" in c.request.url][-1].request.params
    assert params["start_time"] == "2026-10-26T23:00:00Z" and "since_id" not in params  # last scan (01:00) minus 2h
    assert main.run(cfg, now=NOW + timedelta(hours=26), x_client=client()) == 0
    params = [c for c in mocked.calls if "/tweets" in c.request.url][-1].request.params
    assert params["since_id"] == "1866000000000000009"


def test_failed_scan_does_not_move_the_last_seen_marker(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    mocked.replace("GET", TIMELINE_URL, status=503)
    assert main.run(cfg, now=NOW + timedelta(hours=2), x_client=client()) == 1
    assert read(cfg, "state.json")["last_seen_post_id"] == "1866000000000000002"


def test_manual_recovery_scan_uses_the_time_window(mocked, make_config):
    cfg = make_config()
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    recovery = make_config(SCAN_SINCE="2026-10-20T00:00:00Z")
    assert main.run(recovery, now=NOW + timedelta(hours=2), x_client=client()) == 0
    params = [c for c in mocked.calls if "/tweets" in c.request.url][-1].request.params
    assert params["start_time"] == "2026-10-20T00:00:00Z" and "since_id" not in params


def test_telegram_is_silent_when_nothing_is_new(mocked, make_config):
    cfg = make_config(TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json={"meta": {"result_count": 0}})
    mocked.post(TG_URL, json={"ok": True})
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    assert not [c for c in mocked.calls if "telegram" in c.request.url]
    assert "Telegram | not sent (nothing new)" in report(cfg)

    mocked.replace("GET", TIMELINE_URL, status=503)   # a failed scan still notifies
    assert main.run(cfg, now=NOW + timedelta(hours=2), x_client=client()) == 1
    assert len([c for c in mocked.calls if "telegram" in c.request.url]) == 1


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


def _fake_panel(mocked, reject_add=None):
    """A panel that answers by action and records every order placed."""
    from urllib.parse import parse_qs

    placed = []

    def reply(request):
        form = {k: v[0] for k, v in parse_qs(request.body).items()}
        action = form["action"]
        if action == "balance":
            return 200, {}, json.dumps({"balance": "50.00", "currency": "USD"})
        if action == "services":
            return 200, {}, json.dumps([
                {"service": 5, "name": "Views", "rate": "0.002", "min": "100", "max": "1000000"},
                {"service": 7, "name": "Likes", "rate": "3.7", "min": "10", "max": "5000"},
            ])
        if action == "status":
            return 200, {}, json.dumps({"status": "Completed", "charge": "0.01", "currency": "USD"})
        if reject_add and reject_add[0]:
            return 200, {}, json.dumps({"error": reject_add[0]})
        placed.append((form["service"], form["link"], int(form["quantity"])))
        return 200, {}, json.dumps({"order": 70000 + len(placed)})

    mocked.add_callback("POST", SMM_URL, callback=reply)
    return placed


class BotClock:
    """Time for a simulated keep-alive run: it only moves when the loop sleeps."""

    def __init__(self, start):
        self.now = start

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def _keep_alive(cfg, clock, hooks=None, max_minutes=0, after_cycle=None):
    """Drive real cycles through the real keep-alive loop.

    Returns (exit code, [(start time, X API requests made)] for every cycle).
    """
    cycles = []

    def cycle(first):
        x = client()
        smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
        code = main.run(cfg, now=clock.clock(), x_client=x, smm_client=smm)
        cycles.append((clock.clock(), x.request_count))
        if after_cycle:
            after_cycle(len(cycles))
        return code

    code = run_loop(
        cycle, lambda now: main.next_wake(cfg, now), hooks or Hooks(), max_minutes, clock=clock.clock, sleep=clock.sleep
    )
    return code, cycles


# KEEP_ALIVE_AFTER_POST_HOURS=0: these runs end as soon as the last view order is placed.
ORDERING = dict(SMM_ENABLED="true", SMM_AUTO_ORDER="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key",
                SMM_IMPRESSION_SERVICE_ID="5", SCAN_INTERVAL_HOURS=2, KEEP_ALIVE_AFTER_POST_HOURS=0)
POST_ID = "1866000000000000001"
LINK = f"https://x.com/ExampleUser/status/{POST_ID}"


def _telegram_texts(mocked):
    return [json.loads(c.request.body)["text"] for c in mocked.calls if "telegram" in c.request.url]


def test_one_run_finds_a_post_then_stays_alive_and_places_every_view_order_on_time(mocked, make_config):
    cfg = make_config(SMM_LIKE_SERVICE_ID="7", IMPRESSION_TARGET=4000, LIKE_TARGET=50,
                      TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42", **ORDERING)
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID, "2026-10-26T16:30:00.000Z")]))
    mocked.post(TG_URL, json={"ok": True})
    placed = _fake_panel(mocked)

    def after_cycle(number):
        if number == 1:                                                   # later scans find nothing new
            mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})

    # No scheduler, no second run: a single process started once, at discovery time.
    code, cycles = _keep_alive(cfg, BotClock(NOW), after_cycle=after_cycle)

    assert code == 0
    orders = read(cfg, "campaigns.json")["plans"][POST_ID]["view_schedule"]["orders"]
    assert 10 <= len(orders) <= 12 and sum(o["quantity"] for o in orders) == 4000
    assert all(o["status"] == "placed" for o in orders)
    for order in orders:                                                  # each at its own planned time
        lateness = parse_iso_datetime(order["placed_at"]) - parse_iso_datetime(order["due_at"])
        assert timedelta(0) <= lateness <= timedelta(minutes=5)
    assert len({o["placed_at"] for o in orders}) == len(orders)           # never two at the same moment
    assert parse_iso_datetime(orders[-1]["placed_at"]) - NOW < timedelta(hours=12)   # 4,000 inside 12 hours

    assert [o for o in placed if o[0] == "7"] == [("7", LINK, 50)]        # likes: one order, never repeated
    views = [o[2] for o in placed if o[0] == "5"]
    assert sum(views) == 4000 and len(views) == len(orders)
    assert placed[0][0] == "7" or placed[1][0] == "7"                     # ...and in the very first cycle

    # X is scanned about every 2 hours; the cycles that only place an order never touch it.
    assert cycles[0] == (NOW, 2)                                          # user lookup + timeline
    assert all(requests in (0, 1) for _, requests in cycles[1:])
    scans = [when for when, requests in cycles if requests]
    gaps = [later - earlier for earlier, later in zip(scans, scans[1:])]
    assert gaps and all(timedelta(minutes=100) <= gap <= timedelta(minutes=125) for gap in gaps)
    assert sum(1 for _, requests in cycles if requests == 0) >= len(orders) - 6
    starts = [when for when, _ in cycles]
    assert all(later - earlier >= timedelta(minutes=5) for earlier, later in zip(starts, starts[1:]))

    # Two messages in all: the post was found, and its views are fully ordered.
    texts = _telegram_texts(mocked)
    assert len(texts) == 2
    assert LINK in texts[0] and "Views are ordered in 10-12 parts over 12h; likes at once." in texts[0]
    assert f"Views fully ordered: 4,000 in {len(orders)} orders — {LINK}" in texts[1]
    assert "Split view orders in progress" not in report(cfg, "2026-10-27")
    assert main.next_wake(cfg, starts[-1]) is None                        # nothing left: the run ends


def test_a_run_that_starts_late_catches_up_without_ever_placing_two_orders_together(mocked, make_config):
    # The situation this bot was in: posts discovered, then no run for many hours.
    cfg = make_config(IMPRESSION_TARGET=4000, **ORDERING)
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    placed = _fake_panel(mocked)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    assert len(placed) == 2                                               # first slice of each post
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})

    late = NOW + timedelta(hours=16)
    code, cycles = _keep_alive(cfg, BotClock(late))

    assert code == 0
    plans = read(cfg, "campaigns.json")["plans"]
    longest = max(len(plan["view_schedule"]["orders"]) for plan in plans.values())
    for plan in plans.values():
        orders = plan["view_schedule"]["orders"]
        assert all(o["status"] == "placed" for o in orders)
        assert sum(o["quantity"] for o in orders) == 4000
        times = [parse_iso_datetime(o["placed_at"]) for o in orders[1:]]
        assert times == [late + timedelta(minutes=10 * step) for step in range(len(times))]
    assert [when for when, _ in cycles] == [late + timedelta(minutes=10 * step) for step in range(longest - 1)]
    assert cycles[-1][0] - late <= timedelta(minutes=110)                 # the backlog is gone in under two hours


def test_bot_keeps_scanning_for_a_day_after_the_latest_post_then_stops(mocked, make_config):
    cfg = make_config(SCAN_INTERVAL_HOURS=2)                              # no ordering at all: scans only
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID, "2026-10-26T16:30:00.000Z")]))

    def after_cycle(number):
        if number == 1:
            mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
        if number == 4:                                                   # a second post six hours in...
            mocked.replace("GET", TIMELINE_URL, json=timeline_body([raw_post("1866000000000000009", "2026-10-26T22:30:00Z")]))
        if number == 5:
            mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})

    code, cycles = _keep_alive(cfg, BotClock(NOW), after_cycle=after_cycle)

    assert code == 0
    starts = [when for when, _ in cycles]
    assert all(later - earlier == timedelta(hours=2) for earlier, later in zip(starts, starts[1:]))
    assert len(read(cfg, "posts.json")["posts"]) == 2                     # found by an on-time scan
    assert starts[-1] == NOW + timedelta(hours=8 + 24)                    # ...keeps the bot up a day from then
    assert main.next_wake(cfg, starts[-1]) is None


def test_rejected_view_order_is_reported_and_retried_after_a_pause(mocked, make_config):
    cfg = make_config(VIEW_SPLIT_MIN=1, VIEW_SPLIT_MAX=1, **ORDERING)
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID)]))
    reject = ["Not enough funds on balance"]
    placed = _fake_panel(mocked, reject)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)

    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    assert placed == [] and "Not enough funds" in report(cfg) and "trying again in 30 min" in report(cfg)
    assert main.next_wake(cfg, NOW) == NOW + timedelta(minutes=30)        # the run stays alive for the retry

    reject[0] = None                                                      # balance topped up
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
    assert main.run(cfg, now=NOW + timedelta(minutes=30), x_client=client(), smm_client=smm) == 0
    assert placed == [("5", LINK, 1000)]
    assert main.next_wake(cfg, NOW + timedelta(minutes=31)) is None


def test_likes_failure_reaches_the_owner(mocked, make_config):
    cfg = make_config(SMM_ENABLED="true", SMM_AUTO_ORDER="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key",
                      SMM_LIKE_SERVICE_ID="7", TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN,
                      TELEGRAM_CHAT_ID="42")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID)]))
    mocked.post(TG_URL, json={"ok": True})
    _fake_panel(mocked, ["Not enough funds on balance"])
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    assert "Likes order failed" in _telegram_texts(mocked)[0] and "Not enough funds" in _telegram_texts(mocked)[0]


def test_when_the_bot_stays_alive(mocked, make_config):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID)]))
    _fake_panel(mocked)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    cfg = make_config(**ORDERING)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    second_due = parse_iso_datetime(read(cfg, "campaigns.json")["plans"][POST_ID]["view_schedule"]["orders"][1]["due_at"])
    soon = NOW + timedelta(minutes=1)

    assert main.next_wake(cfg, soon) == second_due                        # alive while view orders wait
    assert main.next_wake(make_config(KEEP_ALIVE="always", **ORDERING), soon) == second_due
    assert main.next_wake(make_config(KEEP_ALIVE="off", **ORDERING), soon) is None
    assert main.next_wake(make_config(DRY_RUN="true", **ORDERING), soon) is None
    # Auto-ordering switched off: the waiting orders will not be placed, so there is nothing to wait for...
    paused = {**ORDERING, "SMM_AUTO_ORDER": "false"}
    assert main.next_wake(make_config(**paused), soon) is None
    # ...unless the bot is told to stay up for the regular scans as well.
    next_scan = NOW + timedelta(hours=2)
    assert main.next_wake(make_config(KEEP_ALIVE="always", **paused), soon) == next_scan
    # By default it also stays up for a day after the latest post, so the next one is found on time.
    default = {k: v for k, v in paused.items() if k != "KEEP_ALIVE_AFTER_POST_HOURS"}
    assert main.next_wake(make_config(**default), soon) == next_scan
    assert main.next_wake(make_config(**default), NOW + timedelta(hours=23, minutes=59)) == next_scan
    assert main.next_wake(make_config(**default), NOW + timedelta(hours=24)) is None
    assert main.next_wake(make_config(KEEP_ALIVE_AFTER_POST_HOURS=48, **default), NOW + timedelta(hours=24)) == next_scan


def test_an_overdue_order_the_last_cycle_could_not_move_is_left_for_half_an_hour(mocked, make_config):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post(POST_ID)]))
    _fake_panel(mocked)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    cfg = make_config(**ORDERING)
    assert main.run(cfg, now=NOW, x_client=client(), smm_client=smm) == 0
    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})

    # A cycle runs while orders are overdue and places none of them (here: ordering was off for it).
    later = NOW + timedelta(hours=5)
    assert main.run(make_config(**{**ORDERING, "SMM_AUTO_ORDER": "false"}), now=later, x_client=client(), smm_client=smm) == 0
    # Asked again straight away, the loop must not spin on the same overdue order.
    assert main.next_wake(cfg, later + timedelta(seconds=5)) == later + timedelta(seconds=5) + timedelta(minutes=30)


def test_failing_scan_is_retried_every_half_hour_and_announced_once(mocked, make_config):
    cfg = make_config(SCAN_INTERVAL_HOURS=2, KEEP_ALIVE="always",
                      TELEGRAM_ENABLED="true", TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json={"meta": {"result_count": 0}})
    mocked.post(TG_URL, json={"ok": True})
    assert main.run(cfg, now=NOW, x_client=client()) == 0
    mocked.replace("GET", TIMELINE_URL, status=503)

    start = NOW + timedelta(hours=2)
    code, cycles = _keep_alive(cfg, BotClock(start), max_minutes=130)     # runs out of time: no hand-over hook here

    assert code == 1
    assert [when for when, _ in cycles] == [start + timedelta(minutes=30 * n) for n in range(5)]
    assert len(_telegram_texts(mocked)) == 1 and "X scan failed" in _telegram_texts(mocked)[0]
    assert read(cfg, "state.json")["scan_failures"] == 5
    assert read(cfg, "state.json")["last_successful_scan_at"] == "2026-10-26T17:00:00Z"

    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})   # X is back
    assert main.run(cfg, now=start + timedelta(hours=3), x_client=client()) == 0
    assert read(cfg, "state.json")["scan_failures"] == 0
    assert len(_telegram_texts(mocked)) == 1                              # recovery with nothing new is silent


def test_turning_auto_order_off_stops_pending_view_orders(mocked, make_config):
    common = dict(SMM_ENABLED="true", SMM_API_URL=SMM_URL, SMM_API_KEY="smm-key", SMM_IMPRESSION_SERVICE_ID="5")
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body([raw_post("1866000000000000001")]))
    placed = _fake_panel(mocked)
    smm = SMMClient(SMM_URL, "smm-key", max_retries=0, sleep=no_sleep)
    assert main.run(make_config(SMM_AUTO_ORDER="true", **common), now=NOW, x_client=client(), smm_client=smm) == 0
    assert len(placed) == 1

    mocked.replace("GET", TIMELINE_URL, json={"meta": {"result_count": 0}})
    off = make_config(SMM_AUTO_ORDER="false", **common)
    assert main.run(off, now=NOW + timedelta(hours=13), x_client=client(), smm_client=smm) == 0
    assert len(placed) == 1


def test_manual_run_scans_even_inside_the_interval(mocked, make_config):
    mocked.get(USER_URL, json=user_body())
    mocked.get(TIMELINE_URL, json=timeline_body(TWO_POSTS))
    assert main.run(make_config(SCAN_INTERVAL_HOURS=2), now=NOW, x_client=client()) == 0
    before = sum("/tweets" in c.request.url for c in mocked.calls)
    later = NOW + timedelta(minutes=30)
    assert main.run(make_config(SCAN_INTERVAL_HOURS=2), now=later, x_client=client()) == 0
    assert sum("/tweets" in c.request.url for c in mocked.calls) == before
    assert main.run(make_config(SCAN_INTERVAL_HOURS=2, FORCE_SCAN="true"), now=later, x_client=client()) == 0
    assert sum("/tweets" in c.request.url for c in mocked.calls) == before + 1


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


def _main_env(monkeypatch, tmp_path, **extra):
    """Environment for main.main() with the cycle itself replaced by a recorder."""
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: 0)   # never read a developer's real .env
    for name in ("DRY_RUN", "FORCE_SCAN", "SCAN_SINCE", "KEEP_ALIVE", "KEEP_ALIVE_MINUTES", "ALLOW_STATE_REINIT"):
        monkeypatch.delenv(name, raising=False)
    env = {"X_BEARER_TOKEN": "test-bearer-token", "X_USERNAME": "someone",
           "DATA_DIR": str(tmp_path / "data"), "REPORTS_DIR": str(tmp_path / "reports"), **extra}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    clock = BotClock(NOW)
    monkeypatch.setattr(main, "_now", clock.clock)
    monkeypatch.setattr(main.time, "sleep", clock.sleep)
    seen, handovers = [], []
    monkeypatch.setattr(main, "run", lambda config: seen.append(config) or 0)
    monkeypatch.setattr(main, "hooks_from_env", lambda env: Hooks(hand_over=lambda: handovers.append(1) or True))
    return clock, seen, handovers


def test_main_applies_manual_run_options_to_the_first_cycle_only(monkeypatch, tmp_path):
    clock, seen, handovers = _main_env(
        monkeypatch, tmp_path, FORCE_SCAN="true", SCAN_SINCE="2026-10-20T00:00:00Z", ALLOW_STATE_REINIT="true"
    )
    wakes = iter([NOW + timedelta(hours=1), NOW + timedelta(hours=2), None])
    monkeypatch.setattr(main, "next_wake", lambda config, now: next(wakes))

    assert main.main() == 0

    assert [(c.force_scan, c.scan_since_override is not None, c.allow_state_reinit) for c in seen] == [
        (True, True, True), (False, False, False), (False, False, False),
    ]
    assert clock.now == NOW + timedelta(hours=2) and handovers == []


def test_dry_run_is_followed_by_a_real_run_only_if_something_is_waiting(monkeypatch, tmp_path):
    _, seen, handovers = _main_env(monkeypatch, tmp_path, DRY_RUN="true")
    monkeypatch.setattr(main, "next_wake", lambda config, now: None if config.dry_run else NOW + timedelta(hours=1))
    assert main.main() == 0
    assert len(seen) == 1 and seen[0].dry_run and handovers == [1]

    monkeypatch.setattr(main, "next_wake", lambda config, now: None)
    assert main.main() == 0
    assert handovers == [1]


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
