import random
from datetime import timedelta

from unittest.mock import MagicMock

from src.campaign_manager import (
    PROMO_CAMPAIGN_LINKED,
    PROMO_NOT_STARTED,
    PROMO_TARGETS_REACHED,
    XAdsStatusProvider,
    apply_metrics,
    auto_place_orders,
    ensure_plans,
    new_post_record,
    place_due_view_orders,
    refresh_campaign_statuses,
    tracked_post_ids,
    view_schedule_progress,
)
from src.scanner import Post
from src.config import parse_iso_datetime
from src.smm_client import OrderResult, SMMError, SMMRejected
from tests.conftest import NOW


def make_post(post_id="1866000000000000001", created="2026-10-26T10:00:00.000Z"):
    return Post(post_id, "text", created, f"https://x.com/u/status/{post_id}", "original")


def posts_with(*post_ids):
    return {pid: new_post_record(make_post(pid), NOW) for pid in post_ids}


def test_plan_created_from_real_post_data(make_config):
    posts = posts_with("1866000000000000001")
    plans = {}
    created = ensure_plans(posts, plans, make_config(PROMOTION_BUDGET="25"), NOW)
    plan = plans["1866000000000000001"]
    assert created == ["1866000000000000001"]
    assert plan["post_url"] == "https://x.com/u/status/1866000000000000001"
    assert plan["targets"] == {"impressions": 1000, "likes": 30}
    assert plan["status"] == "PLANNED"
    assert plan["promotion_status"] == PROMO_NOT_STARTED
    assert plan["budget"] == {"amount": 25.0, "currency": "USD"}
    # Targets are kept separate from any campaign/order references.
    assert plan["authorized_campaigns"] == []
    assert posts["1866000000000000001"]["status"] == "PLANNED"


def test_ensure_plans_is_idempotent(make_config):
    posts = posts_with("1", "2")
    plans = {}
    cfg = make_config()
    assert len(ensure_plans(posts, plans, cfg, NOW)) == 2
    snapshot = {k: dict(v) for k, v in plans.items()}
    assert ensure_plans(posts, plans, cfg, NOW + timedelta(days=1)) == []
    assert plans == snapshot


def test_custom_targets(make_config):
    plans = {}
    ensure_plans(posts_with("1"), plans, make_config(IMPRESSION_TARGET=500, LIKE_TARGET=10), NOW)
    assert plans["1"]["targets"] == {"impressions": 500, "likes": 10}


def test_metrics_update_progress_and_status(make_config):
    posts, plans = posts_with("1", "2"), {}
    ensure_plans(posts, plans, make_config(), NOW)
    apply_metrics(posts, plans, {"1": {"impression_count": 1500, "like_count": 31}, "2": {"impression_count": 20, "like_count": 1}}, NOW)
    assert plans["1"]["promotion_status"] == PROMO_TARGETS_REACHED
    assert plans["1"]["performance"]["source"] == "x_api_public_metrics"
    assert plans["2"]["promotion_status"] == PROMO_NOT_STARTED
    assert posts["1"]["public_metrics"]["like_count"] == 31


class FakeProvider:
    name = "smmsoc"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def get_status(self, reference):
        self.calls.append(reference)
        if self.fail:
            raise SMMError("service unavailable")
        return {"status": "Completed", "charge": 1.5, "currency": "USD"}


def test_refresh_only_checks_linked_campaigns_for_known_providers(make_config):
    posts, plans = posts_with("1", "2"), {}
    ensure_plans(posts, plans, make_config(), NOW)
    plans["1"]["authorized_campaigns"].append({"provider": "smmsoc", "reference": "555", "authorized_by": "owner"})
    plans["2"]["authorized_campaigns"].append({"provider": "unknown", "reference": "9"})
    provider = FakeProvider()
    summary = refresh_campaign_statuses(plans, {"smmsoc": provider}, NOW)
    assert provider.calls == ["555"]
    assert summary["checked"] == 1
    assert summary["verified_spend"] == {"USD": 1.5}
    assert plans["1"]["authorized_campaigns"][0]["last_status"]["status"] == "Completed"
    assert plans["1"]["promotion_status"] == PROMO_CAMPAIGN_LINKED


def test_refresh_errors_are_recorded_not_raised(make_config):
    posts, plans = posts_with("1"), {}
    ensure_plans(posts, plans, make_config(), NOW)
    plans["1"]["authorized_campaigns"].append({"provider": "smmsoc", "reference": "555"})
    plans["1"]["authorized_campaigns"].append({"provider": "x_ads", "reference": "abc"})
    summary = refresh_campaign_statuses(plans, {"smmsoc": FakeProvider(fail=True), "x_ads": XAdsStatusProvider()}, NOW)
    assert summary["checked"] == 0
    assert len(summary["errors"]) == 2
    assert plans["1"]["authorized_campaigns"][0]["last_error"] == "service unavailable"


def _smm_client_mock(order_id="99001"):
    client = MagicMock()
    client.place_order.return_value = OrderResult(order_id=order_id)
    return client


def test_new_post_gets_likes_at_once_and_a_split_view_schedule(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7", IMPRESSION_TARGET=5000)
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock("99001")

    results = auto_place_orders(["1"], posts, plans, cfg, client, NOW, rng=random.Random(4))

    # Likes: one order, now. Views: nothing bought yet, only scheduled.
    assert results == [{"post_id": "1", "type": "likes", "order_id": "99001", "quantity": 30}]
    client.place_order.assert_called_once_with("7", "https://x.com/u/status/1", 30)
    campaigns = plans["1"]["authorized_campaigns"]
    assert [c["type"] for c in campaigns] == ["likes"]
    assert campaigns[0]["provider"] == "bulkfollows" and campaigns[0]["authorized_by"] == "auto"

    orders = plans["1"]["view_schedule"]["orders"]
    assert 10 <= len(orders) <= 12
    assert sum(o["quantity"] for o in orders) == 5000
    assert len({o["quantity"] for o in orders}) > 1                      # uneven
    assert all(o["status"] == "pending" for o in orders)
    due = [parse_iso_datetime(o["due_at"]) for o in orders]
    assert due[0] == NOW and due == sorted(due)
    assert due[-1] < NOW + timedelta(hours=12)


def test_view_orders_are_placed_as_they_come_due_until_the_total_is_reached(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", IMPRESSION_TARGET=5000)
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock()
    auto_place_orders(["1"], posts, plans, cfg, client, NOW, rng=random.Random(4))
    total_orders = len(plans["1"]["view_schedule"]["orders"])

    placed, problems = place_due_view_orders(plans, cfg, client, NOW)
    assert len(placed) == 1 and placed[0]["type"] == f"views 1/{total_orders}" and problems == []
    assert place_due_view_orders(plans, cfg, client, NOW) == ([], [])     # nothing else due yet

    quantities = [placed[0]["quantity"]]
    for hour in range(1, 14):                                             # hourly runs
        more, problems = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=hour))
        assert problems == [] and len(more) <= 2
        quantities += [m["quantity"] for m in more]
    assert sum(quantities) == 5000 and len(quantities) == total_orders
    progress = view_schedule_progress(plans["1"])
    assert progress["placed_quantity"] == 5000 and progress["pending_orders"] == 0
    splits = [c for c in plans["1"]["authorized_campaigns"] if c["type"] == "impressions"]
    assert len(splits) == total_orders and splits[0]["split"] == f"1/{total_orders}"
    assert place_due_view_orders(plans, cfg, client, NOW + timedelta(days=2)) == ([], [])


def test_scheduling_is_idempotent_and_respects_existing_orders(make_config):
    posts, plans = posts_with("1", "2"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7")
    ensure_plans(posts, plans, cfg, NOW)
    # Post 2 was already bought in one shot by an earlier version of the bot.
    plans["2"]["authorized_campaigns"] += [{"type": "impressions", "reference": "1"}, {"type": "likes", "reference": "2"}]
    client = _smm_client_mock()

    auto_place_orders(["1", "2"], posts, plans, cfg, client, NOW)
    schedule = [dict(o) for o in plans["1"]["view_schedule"]["orders"]]
    auto_place_orders(["1", "2"], posts, plans, cfg, client, NOW)          # second call is a no-op

    assert client.place_order.call_count == 1                              # likes for post 1 only
    assert plans["1"]["view_schedule"]["orders"] == schedule
    assert "view_schedule" not in plans["2"]


def test_small_view_target_uses_fewer_orders_and_tiny_target_is_reported(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", IMPRESSION_TARGET=350)
    ensure_plans(posts, plans, cfg, NOW)
    auto_place_orders(["1"], posts, plans, cfg, _smm_client_mock(), NOW, view_limits=(100, 10**9))
    assert len(plans["1"]["view_schedule"]["orders"]) == 3

    posts, plans = posts_with("2"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", IMPRESSION_TARGET=50)
    ensure_plans(posts, plans, cfg, NOW)
    auto_place_orders(["2"], posts, plans, cfg, _smm_client_mock(), NOW, view_limits=(100, 10**9))
    assert "view_schedule" not in plans["2"]
    assert "below the service minimum" in plans["2"]["errors"][0]


def test_rejected_view_order_is_retried_then_given_up(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", VIEW_SPLIT_MIN=2, VIEW_SPLIT_MAX=2)
    ensure_plans(posts, plans, cfg, NOW)
    client = MagicMock()
    client.place_order.side_effect = SMMRejected("Not enough funds on balance")
    auto_place_orders(["1"], posts, plans, cfg, client, NOW)

    later = NOW + timedelta(days=1)                                        # both splits are due
    for attempt in (1, 2):
        placed, problems = place_due_view_orders(plans, cfg, client, later)
        assert placed == [] and len(problems) == 1 and "will retry next run" in problems[0]
        assert client.place_order.call_count == attempt                    # the second split is not attempted
    placed, problems = place_due_view_orders(plans, cfg, client, later)
    assert "giving up" in problems[0]
    first, second = plans["1"]["view_schedule"]["orders"]
    assert first["status"] == "failed" and second["status"] == "pending"

    client.place_order.side_effect = None                                  # balance topped up
    client.place_order.return_value = OrderResult(order_id="5")
    placed, problems = place_due_view_orders(plans, cfg, client, later)
    assert len(placed) == 1 and second["status"] == "placed"


def test_unclear_view_order_outcome_is_never_retried(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", VIEW_SPLIT_MIN=1, VIEW_SPLIT_MAX=1)
    ensure_plans(posts, plans, cfg, NOW)
    client = MagicMock()
    client.place_order.side_effect = SMMError("SMM API timed out on 'add' after 1 attempts")
    auto_place_orders(["1"], posts, plans, cfg, client, NOW)

    placed, problems = place_due_view_orders(plans, cfg, client, NOW)
    assert placed == [] and "outcome unknown" in problems[0]
    assert plans["1"]["view_schedule"]["orders"][0]["status"] == "unknown"
    assert place_due_view_orders(plans, cfg, client, NOW + timedelta(days=1)) == ([], [])
    assert client.place_order.call_count == 1


def test_failed_likes_order_is_recorded_not_raised(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_LIKE_SERVICE_ID="7")
    ensure_plans(posts, plans, cfg, NOW)
    client = MagicMock()
    client.place_order.side_effect = SMMError("Insufficient balance")

    assert auto_place_orders(["1"], posts, plans, cfg, client, NOW) == []
    assert "Insufficient balance" in plans["1"]["errors"][0]


def test_finished_orders_are_not_asked_about_again(make_config):
    posts, plans = posts_with("1"), {}
    ensure_plans(posts, plans, make_config(), NOW)
    plans["1"]["authorized_campaigns"] += [
        {"provider": "smmsoc", "reference": "1", "last_status": {"status": "Completed", "charge": 0.5, "currency": "USD"}},
        {"provider": "smmsoc", "reference": "2", "last_status": {"status": "In progress", "charge": 0.1, "currency": "USD"}},
    ]
    provider = FakeProvider()
    summary = refresh_campaign_statuses(plans, {"smmsoc": provider}, NOW)
    assert provider.calls == ["2"]
    assert summary["verified_spend"] == {"USD": 2.0}                       # 0.5 remembered + 1.5 fresh


def test_auto_place_orders_skips_unknown_post_ids(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5")
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock()

    results = auto_place_orders(["999"], posts, plans, cfg, client, NOW)

    assert results == []
    assert client.place_order.call_count == 0


def test_tracked_post_ids_respects_lookback():
    posts = {
        "10": new_post_record(make_post("10", "2026-10-25T10:00:00Z"), NOW),
        "5": new_post_record(make_post("5", "2026-10-01T10:00:00Z"), NOW),
    }
    assert tracked_post_ids(posts, NOW, 7) == ["10"]
    assert tracked_post_ids(posts, NOW, 7, exclude={"10"}) == []
    assert tracked_post_ids(posts, NOW, 0) == []
