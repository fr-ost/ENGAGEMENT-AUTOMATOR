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
    refresh_campaign_statuses,
    tracked_post_ids,
)
from src.scanner import Post
from src.smm_client import OrderResult, SMMError
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


def test_auto_place_orders_places_impression_and_like(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5", SMM_LIKE_SERVICE_ID="7")
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock("99001")

    results = auto_place_orders(["1"], posts, plans, cfg, client, NOW)

    assert len(results) == 2
    types = {r["type"] for r in results}
    assert types == {"impressions", "likes"}
    campaigns = plans["1"]["authorized_campaigns"]
    assert len(campaigns) == 2
    assert all(c["authorized_by"] == "auto" for c in campaigns)
    assert all(c["provider"] == "bulkfollows" for c in campaigns)
    imp_order = next(c for c in campaigns if c["type"] == "impressions")
    assert imp_order["service_id"] == "5"
    assert imp_order["quantity"] == 1000
    like_order = next(c for c in campaigns if c["type"] == "likes")
    assert like_order["service_id"] == "7"
    assert like_order["quantity"] == 30
    assert client.place_order.call_count == 2


def test_auto_place_orders_only_impression_service(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5")
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock()

    results = auto_place_orders(["1"], posts, plans, cfg, client, NOW)

    assert len(results) == 1
    assert results[0]["type"] == "impressions"
    assert client.place_order.call_count == 1


def test_auto_place_orders_is_idempotent(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5")
    ensure_plans(posts, plans, cfg, NOW)
    client = _smm_client_mock()

    auto_place_orders(["1"], posts, plans, cfg, client, NOW)
    auto_place_orders(["1"], posts, plans, cfg, client, NOW)  # second call is a no-op

    assert client.place_order.call_count == 1
    assert len(plans["1"]["authorized_campaigns"]) == 1


def test_auto_place_orders_error_recorded_not_raised(make_config):
    posts, plans = posts_with("1"), {}
    cfg = make_config(SMM_IMPRESSION_SERVICE_ID="5")
    ensure_plans(posts, plans, cfg, NOW)
    client = MagicMock()
    client.place_order.side_effect = SMMError("Insufficient balance")

    results = auto_place_orders(["1"], posts, plans, cfg, client, NOW)

    assert results == []
    assert len(plans["1"]["errors"]) == 1
    assert "Insufficient balance" in plans["1"]["errors"][0]


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
