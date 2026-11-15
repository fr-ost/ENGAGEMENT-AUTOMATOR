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
    next_view_order_time,
    next_view_order_wake,
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


def _scheduled(make_config, target=5000, seed=4, **overrides):
    """A post whose views were just scheduled (no likes service), with its config and a panel mock."""
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", IMPRESSION_TARGET=target, **overrides)
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock()
    assert auto_place_orders(["1"], posts, plans, cfg, client, NOW, rng=random.Random(seed)) == ([], [])
    return plans, cfg, client


def _orders(plans):
    return plans["1"]["view_schedule"]["orders"]


def test_new_post_gets_likes_at_once_and_a_split_view_schedule(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7", IMPRESSION_TARGET=5000)
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock("99001")

    results, problems = auto_place_orders(["1"], posts, plans, cfg, client, NOW, rng=random.Random(4))

    # Likes: one order, now. Views: nothing bought yet, only scheduled.
    assert results == [{"post_id": "1", "type": "likes", "order_id": "99001", "quantity": 30}]
    assert problems == []
    client.place_order.assert_called_once_with("7", "https://x.com/u/status/1", 30)
    campaigns = plans["1"]["authorized_campaigns"]
    assert [c["type"] for c in campaigns] == ["likes"]
    assert campaigns[0]["provider"] == "bulkfollows" and campaigns[0]["authorized_by"] == "auto"

    orders = _orders(plans)
    assert 10 <= len(orders) <= 12
    assert sum(o["quantity"] for o in orders) == 5000
    assert len({o["quantity"] for o in orders}) > 1                      # uneven
    assert all(o["status"] == "pending" for o in orders)
    due = [parse_iso_datetime(o["due_at"]) for o in orders]
    assert due[0] == NOW and due == sorted(due)
    assert due[-1] < NOW + timedelta(hours=12)


def test_view_orders_go_out_one_at_a_time_exactly_when_each_is_due(make_config):
    plans, cfg, client = _scheduled(make_config)
    orders = _orders(plans)
    total_orders = len(orders)

    first = place_due_view_orders(plans, cfg, client, NOW)
    assert [p["type"] for p in first.placed] == [f"views 1/{total_orders}"]
    assert first.problems == [] and first.completed == []
    assert place_due_view_orders(plans, cfg, client, NOW).placed == []    # the next one is not due yet

    placed = [first.placed[0]["quantity"]]
    for position, order in enumerate(orders[1:], start=2):
        due = parse_iso_datetime(order["due_at"])
        assert next_view_order_time(plans["1"]) == due == next_view_order_wake(plans)
        assert place_due_view_orders(plans, cfg, client, due - timedelta(seconds=1)).placed == []
        outcome = place_due_view_orders(plans, cfg, client, due)
        assert [p["type"] for p in outcome.placed] == [f"views {position}/{total_orders}"]
        assert outcome.placed[0]["quantity"] == order["quantity"]
        assert bool(outcome.completed) == (position == total_orders)
        placed.append(outcome.placed[0]["quantity"])

    assert sum(placed) == 5000
    assert outcome.completed[0]["post_url"] == "https://x.com/u/status/1"
    assert outcome.completed[0]["placed_quantity"] == 5000 and outcome.completed[0]["placed_orders"] == total_orders
    assert parse_iso_datetime(orders[-1]["placed_at"]) < NOW + timedelta(hours=12)   # all ordered inside 12h
    assert next_view_order_time(plans["1"]) is None and next_view_order_wake(plans) is None
    progress = view_schedule_progress(plans["1"])
    assert progress["placed_quantity"] == 5000 and progress["pending_orders"] == 0 and progress["next_due"] is None
    splits = [c for c in plans["1"]["authorized_campaigns"] if c["type"] == "impressions"]
    assert len(splits) == total_orders and splits[0]["split"] == f"1/{total_orders}"
    assert place_due_view_orders(plans, cfg, client, NOW + timedelta(days=2)).placed == []


def test_overdue_view_orders_are_caught_up_ten_minutes_apart_never_all_at_once(make_config):
    # What happens if nothing ran for hours: the backlog still goes out as separate orders.
    plans, cfg, client = _scheduled(make_config, target=4000)
    total_orders = len(_orders(plans))
    place_due_view_orders(plans, cfg, client, NOW)
    late = NOW + timedelta(hours=16)                                      # every remaining order is overdue

    when, placements = late, []
    while next_view_order_time(plans["1"]) is not None:
        assert next_view_order_time(plans["1"]) <= when
        if placements:                                                    # too soon after the previous one
            assert place_due_view_orders(plans, cfg, client, when - timedelta(seconds=1)).placed == []
        outcome = place_due_view_orders(plans, cfg, client, when)
        assert len(outcome.placed) == 1                                   # one per post per call
        placements.append(when)
        when += timedelta(minutes=10)

    assert len(placements) == total_orders - 1
    assert placements[-1] - placements[0] == timedelta(minutes=10 * (total_orders - 2))
    assert sum(o["quantity"] for o in _orders(plans) if o["status"] == "placed") == 4000


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
    plans, _, _ = _scheduled(make_config, target=350)
    assert len(_orders(plans)) == 3

    posts, plans = posts_with("2"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", IMPRESSION_TARGET=50)
    ensure_plans(posts, plans, cfg, NOW)
    results, problems = auto_place_orders(["2"], posts, plans, cfg, _smm_client_mock(), NOW, view_limits=(100, 10**9))
    assert results == [] and "view_schedule" not in plans["2"]
    assert "below the service minimum" in problems[0] and problems == plans["2"]["errors"]


def test_rejected_view_order_waits_half_an_hour_and_carries_on_once_the_panel_accepts(make_config):
    plans, cfg, client = _scheduled(make_config, VIEW_SPLIT_MIN=2, VIEW_SPLIT_MAX=2)
    client.place_order.side_effect = SMMRejected("Not enough funds on balance")

    first = place_due_view_orders(plans, cfg, client, NOW)
    assert first.placed == [] and first.notes == []
    assert "Not enough funds" in first.problems[0] and "trying again in 30 min" in first.problems[0]
    assert next_view_order_time(plans["1"]) == NOW + timedelta(minutes=30)
    assert view_schedule_progress(plans["1"])["retrying"] is True

    place_due_view_orders(plans, cfg, client, NOW + timedelta(minutes=29))
    assert client.place_order.call_count == 1                              # not hammered in between

    again = place_due_view_orders(plans, cfg, client, NOW + timedelta(minutes=30))
    assert client.place_order.call_count == 2
    assert again.problems == [] and "trying again" in again.notes[0]       # repeats are not re-announced

    client.place_order.side_effect = None                                  # balance topped up
    client.place_order.return_value = OrderResult(order_id="5")
    done = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=1))
    assert [p["type"] for p in done.placed] == ["views 1/2"]
    assert plans["1"]["view_schedule"]["failures"] == 0 and "retry_after" not in plans["1"]["view_schedule"]
    assert view_schedule_progress(plans["1"])["retrying"] is False

    # A later failure is a new problem again, not a silent repeat.
    client.place_order.side_effect = SMMRejected("Not enough funds on balance")
    later = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=12))
    assert len(later.problems) == 1 and later.notes == []


def test_view_orders_are_given_up_long_after_the_schedule_should_have_finished(make_config):
    plans, cfg, client = _scheduled(make_config, VIEW_SPLIT_MIN=3, VIEW_SPLIT_MAX=3)
    place_due_view_orders(plans, cfg, client, NOW)                         # 1 of 3 placed
    client.place_order.side_effect = SMMRejected("Not enough funds on balance")

    still_trying = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=23))
    assert "trying again" in still_trying.problems[0]
    assert [o["status"] for o in _orders(plans)] == ["placed", "pending", "pending"]

    final = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=24, minutes=1))
    assert "giving up on this post's remaining 2 view order(s)" in final.problems[0]
    assert [o["status"] for o in _orders(plans)] == ["placed", "failed", "failed"]
    assert next_view_order_time(plans["1"]) is None
    assert final.problems[0] in plans["1"]["errors"]
    assert view_schedule_progress(plans["1"])["problem_orders"] == 2


def test_order_with_no_clear_answer_is_tried_once_more_then_left_alone(make_config):
    plans, cfg, client = _scheduled(make_config, VIEW_SPLIT_MIN=2, VIEW_SPLIT_MAX=2)
    client.place_order.side_effect = SMMError("SMM API timed out on 'add' after 1 attempts")

    first = place_due_view_orders(plans, cfg, client, NOW)
    assert first.placed == [] and "got no clear answer" in first.problems[0]
    assert _orders(plans)[0]["status"] == "pending"

    second = place_due_view_orders(plans, cfg, client, NOW + timedelta(minutes=30))
    assert "outcome unknown" in second.problems[0] and "not tried again" in second.problems[0]
    assert _orders(plans)[0]["status"] == "unknown"
    assert client.place_order.call_count == 2

    # The same order is never sent a third time; the schedule moves on to the next one.
    client.place_order.side_effect = None
    client.place_order.return_value = OrderResult(order_id="6")
    rest = place_due_view_orders(plans, cfg, client, NOW + timedelta(hours=12))
    assert [p["type"] for p in rest.placed] == ["views 2/2"]
    assert client.place_order.call_count == 3
    assert rest.completed == [] or rest.completed[0]["problem_orders"] == 1


def test_failed_likes_order_is_reported_not_raised(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_LIKE_SERVICE_ID="7")
    ensure_plans(posts, plans, cfg, NOW)
    client = MagicMock()
    client.place_order.side_effect = SMMError("Insufficient balance")

    results, problems = auto_place_orders(["1"], posts, plans, cfg, client, NOW)
    assert results == []
    assert "Likes order failed" in problems[0] and "Insufficient balance" in problems[0]
    assert problems == plans["1"]["errors"]


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

    assert auto_place_orders(["999"], posts, plans, cfg, client, NOW) == ([], [])
    assert client.place_order.call_count == 0


def test_tracked_post_ids_respects_lookback():
    posts = {
        "10": new_post_record(make_post("10", "2026-10-25T10:00:00Z"), NOW),
        "5": new_post_record(make_post("5", "2026-10-01T10:00:00Z"), NOW),
    }
    assert tracked_post_ids(posts, NOW, 7) == ["10"]
    assert tracked_post_ids(posts, NOW, 7, exclude={"10"}) == []
    assert tracked_post_ids(posts, NOW, 0) == []
