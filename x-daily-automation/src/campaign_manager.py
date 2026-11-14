"""Promotion planning records, order placement, and status tracking.

A planning record holds *targets* (what you hope a post achieves).  When
SMM_AUTO_ORDER=true the bot orders for each newly discovered post:

  * likes   - one order, straight away
  * views   - split into several uneven orders (the plan's "view_schedule"),
              placed one or two per run until the total is reached

Every placed order is recorded in the plan's "authorized_campaigns" list.

Auto-placed orders carry {"authorized_by": "auto"}.  Orders you place yourself
outside the bot can also be tracked: edit data/campaigns.json and add an entry
to the post's "authorized_campaigns" list, e.g.
  {"provider": "x_ads", "reference": "<campaign id from X Ads Manager>",
   "authorized_by": "you", "authorized_at": "2026-01-31T12:00:00Z"}
Providers with a registered read-only status checker are refreshed each run.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .config import Config, format_iso, parse_iso_datetime
from .scanner import Post
from .order_planner import plan_quantities, plan_times
from .smm_client import SMMClient, SMMError, SMMRejected, OrderResult  # noqa: F401 (OrderResult re-exported for type hints)

log = logging.getLogger(__name__)

# Planning record status
PLAN_PLANNED = "PLANNED"

# Post processing status
POST_DISCOVERED = "DISCOVERED"
POST_PLANNED = "PLANNED"

# Promotion status (derived; never implies the bot bought anything)
PROMO_NOT_STARTED = "NOT_STARTED"
PROMO_CAMPAIGN_LINKED = "CAMPAIGN_LINKED"
PROMO_TARGETS_REACHED = "TARGETS_REACHED"

# Panel order states that never change again; no point asking about them every run.
FINAL_ORDER_STATUSES = {"completed", "partial", "canceled", "cancelled", "refunded"}

MAX_SPLIT_ATTEMPTS = 3  # rejected view orders are retried on later runs this many times
MAX_SPLITS_PER_RUN = 2  # per post: normally one is due; two lets a late run catch up
DEFAULT_VIEW_LIMITS = (100, 10**9)  # used when the panel's service list is unavailable


class CampaignStatusProvider(Protocol):
    """Read-only status lookup for an externally created, authorized campaign."""

    name: str

    def get_status(self, reference: str) -> dict[str, Any]: ...


class SMMStatusProvider:
    def __init__(self, client: SMMClient, name: str):
        self._client = client
        self.name = name

    def get_status(self, reference: str) -> dict[str, Any]:
        status = self._client.get_order_status(reference)
        return {
            "status": status.status,
            "charge": status.charge,
            "currency": status.currency,
            "start_count": status.start_count,
            "remains": status.remains,
        }


class XAdsStatusProvider:
    """Placeholder for the official X Ads API (https://developer.x.com/en/docs/x-ads-api).

    The Ads API requires a separately approved Ads API application, OAuth 1.0a
    user-context credentials for an ads account, and is not covered by a
    bearer token. Implement get_status() against the documented campaign
    endpoints only after that access is granted. It must stay read-only.
    """

    name = "x_ads"

    def get_status(self, reference: str) -> dict[str, Any]:
        raise NotImplementedError("X Ads API access is not configured for this bot")


def new_post_record(post: Post, now: datetime) -> dict[str, Any]:
    return {
        "post_id": post.post_id,
        "post_url": post.url,
        "text": post.text,
        "kind": post.kind,
        "created_at": post.created_at,
        "discovered_at": format_iso(now),
        "status": POST_DISCOVERED,
        "public_metrics": post.public_metrics,
        "metrics_updated_at": format_iso(now) if post.public_metrics else None,
    }


def new_plan(post_record: dict[str, Any], config: Config, now: datetime) -> dict[str, Any]:
    budget = None
    if config.promotion_budget is not None:
        budget = {"amount": config.promotion_budget, "currency": config.promotion_budget_currency}
    stamp = format_iso(now)
    return {
        "post_id": post_record["post_id"],
        "post_url": post_record["post_url"],
        "status": PLAN_PLANNED,
        "promotion_status": PROMO_NOT_STARTED,
        "targets": {"impressions": config.impression_target, "likes": config.like_target},
        "budget": budget,
        "authorized_campaigns": [],
        "performance": None,
        "created_at": stamp,
        "updated_at": stamp,
        "errors": [],
    }


def ensure_plans(posts: dict[str, Any], plans: dict[str, Any], config: Config, now: datetime) -> list[str]:
    """Create a plan for every discovered post that lacks one. Idempotent."""
    created = []
    for post_id, record in posts.items():
        if post_id not in plans:
            plans[post_id] = new_plan(record, config, now)
            created.append(post_id)
        if record.get("status") == POST_DISCOVERED:
            record["status"] = POST_PLANNED
    return created


def apply_metrics(posts: dict[str, Any], plans: dict[str, Any], metrics_by_id: dict[str, dict[str, int]], now: datetime) -> int:
    """Record public metrics returned by the X API and update progress."""
    stamp = format_iso(now)
    updated = 0
    for post_id, metrics in metrics_by_id.items():
        if post_id in posts:
            posts[post_id]["public_metrics"] = metrics
            posts[post_id]["metrics_updated_at"] = stamp
        plan = plans.get(post_id)
        if plan is None:
            continue
        plan["performance"] = {
            "impressions": metrics.get("impression_count"),
            "likes": metrics.get("like_count"),
            "source": "x_api_public_metrics",
            "updated_at": stamp,
        }
        plan["updated_at"] = stamp
        updated += 1
    for plan in plans.values():
        plan["promotion_status"] = derive_promotion_status(plan)
    return updated


def derive_promotion_status(plan: dict[str, Any]) -> str:
    perf = plan.get("performance") or {}
    targets = plan.get("targets") or {}
    impressions, likes = perf.get("impressions"), perf.get("likes")
    if (
        isinstance(impressions, int)
        and isinstance(likes, int)
        and impressions >= targets.get("impressions", 0)
        and likes >= targets.get("likes", 0)
    ):
        return PROMO_TARGETS_REACHED
    if plan.get("authorized_campaigns"):
        return PROMO_CAMPAIGN_LINKED
    return PROMO_NOT_STARTED


def refresh_campaign_statuses(
    plans: dict[str, Any],
    providers: dict[str, CampaignStatusProvider],
    now: datetime,
) -> dict[str, Any]:
    """Query read-only status for linked campaigns whose provider is configured."""
    summary: dict[str, Any] = {"checked": 0, "errors": [], "verified_spend": {}}
    stamp = format_iso(now)
    for post_id, plan in plans.items():
        for campaign in plan.get("authorized_campaigns", []):
            if not isinstance(campaign, dict):
                continue
            provider = providers.get(str(campaign.get("provider", "")))
            reference = campaign.get("reference")
            if provider is None or not reference:
                continue
            last = campaign.get("last_status") or {}
            if str(last.get("status", "")).lower() in FINAL_ORDER_STATUSES:
                if isinstance(last.get("charge"), (int, float)):
                    currency = last.get("currency") or "unknown"
                    summary["verified_spend"][currency] = summary["verified_spend"].get(currency, 0.0) + last["charge"]
                continue
            try:
                status = provider.get_status(str(reference))
            except (SMMError, NotImplementedError, ValueError) as exc:
                message = f"{campaign.get('provider')} {reference}: {exc}"
                campaign["last_error"] = str(exc)
                campaign["last_checked_at"] = stamp
                summary["errors"].append(message)
                log.warning("Campaign status check failed for post %s: %s", post_id, exc)
                continue
            campaign["last_status"] = status
            campaign["last_error"] = None
            campaign["last_checked_at"] = stamp
            plan["updated_at"] = stamp
            summary["checked"] += 1
            charge, currency = status.get("charge"), status.get("currency") or "unknown"
            if isinstance(charge, (int, float)):
                summary["verified_spend"][currency] = summary["verified_spend"].get(currency, 0.0) + charge
    for plan in plans.values():
        plan["promotion_status"] = derive_promotion_status(plan)
    return summary


def auto_place_orders(
    new_post_ids: list[str],
    posts: dict[str, Any],
    plans: dict[str, Any],
    config: "Config",
    smm_client: SMMClient,
    now: datetime,
    view_limits: tuple[int, int] = DEFAULT_VIEW_LIMITS,
    rng: Optional[random.Random] = None,
) -> list[dict[str, Any]]:
    """Start ordering for newly discovered posts (SMM_AUTO_ORDER=true).

    Likes are placed now as a single order. Views are only *scheduled* here, as
    a set of split orders; place_due_view_orders() places them as they come due.
    Types already ordered or scheduled are skipped, so reruns are idempotent.
    Failures are stored in the plan's 'errors' list and never propagate.

    Returns the like orders placed (post_id, type, order_id, quantity).
    """
    stamp = format_iso(now)
    results: list[dict[str, Any]] = []

    for post_id in new_post_ids:
        plan = plans.get(post_id)
        post = posts.get(post_id)
        if plan is None or post is None:
            continue
        targets = plan.get("targets", {})
        campaigns = plan.get("authorized_campaigns", [])

        views = int(targets.get("impressions", 0))
        already_views = "view_schedule" in plan or any(c.get("type") == "impressions" for c in campaigns)
        if config.smm_impression_service_id and views > 0 and not already_views:
            try:
                schedule_view_orders(plan, config, now, view_limits, rng or random.Random())
            except ValueError as exc:
                msg = f"Views not scheduled for post {post_id}: {exc}"
                plan.setdefault("errors", []).append(msg)
                log.warning("%s", msg)

        likes = int(targets.get("likes", 0))
        if config.smm_like_service_id and likes > 0 and not any(c.get("type") == "likes" for c in campaigns):
            service_id = config.smm_like_service_id
            try:
                result = smm_client.place_order(service_id, post.get("post_url", ""), likes)
            except (SMMError, ValueError) as exc:
                msg = f"Auto-order failed (likes for post {post_id}): {exc}"
                plan.setdefault("errors", []).append(msg)
                log.warning("%s", msg)
            else:
                plan.setdefault("authorized_campaigns", []).append(
                    _campaign_entry(config, result.order_id, stamp, service_id, "likes", likes)
                )
                results.append({"post_id": post_id, "type": "likes", "order_id": result.order_id, "quantity": likes})
                log.info("Placed likes order %s for post %s (qty=%d)", result.order_id, post_id, likes)
        plan["updated_at"] = stamp

    return results


def _campaign_entry(config: Config, reference: str, stamp: str, service_id: str, kind: str, quantity: int) -> dict[str, Any]:
    return {
        "provider": config.smm_provider,
        "reference": reference,
        "authorized_by": "auto",
        "authorized_at": stamp,
        "service_id": service_id,
        "type": kind,
        "quantity": quantity,
    }


def schedule_view_orders(
    plan: dict[str, Any], config: Config, now: datetime, limits: tuple[int, int], rng: random.Random
) -> dict[str, Any]:
    """Split the plan's view target into uneven orders due at spread-out times. Places nothing."""
    total = int(plan["targets"]["impressions"])
    quantities = plan_quantities(total, limits[0], limits[1], rng, (config.view_split_min, config.view_split_max))
    times = plan_times(len(quantities), now.timestamp(), config.view_spread_hours * 3600, rng)
    plan["view_schedule"] = {
        "total": total,
        "service_id": config.smm_impression_service_id,
        "created_at": format_iso(now),
        "orders": [
            {
                "quantity": quantity,
                "due_at": format_iso(datetime.fromtimestamp(due, timezone.utc)),
                "status": "pending",  # pending | placed | failed | unknown
                "attempts": 0,
            }
            for quantity, due in zip(quantities, times)
        ],
    }
    return plan["view_schedule"]


def place_due_view_orders(
    plans: dict[str, Any], config: Config, smm_client: SMMClient, now: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    """Place the scheduled view orders that have come due. Returns (placed, problems).

    A rejected order stays pending and is tried again on later runs, up to
    MAX_SPLIT_ATTEMPTS. An order with no clear answer is marked 'unknown' and
    never retried, because it may already exist on the panel.
    """
    stamp = format_iso(now)
    placed: list[dict[str, Any]] = []
    problems: list[str] = []
    for post_id, plan in plans.items():
        schedule = plan.get("view_schedule")
        if not isinstance(schedule, dict):
            continue
        orders = schedule.get("orders", [])
        due = [
            (index, order)
            for index, order in enumerate(orders, start=1)
            if order.get("status") == "pending" and parse_iso_datetime(order["due_at"]) <= now
        ]
        for index, order in due[:MAX_SPLITS_PER_RUN]:
            label = f"views {index}/{len(orders)}"
            quantity, service_id = int(order["quantity"]), str(schedule["service_id"])
            plan["updated_at"] = stamp
            try:
                result = smm_client.place_order(service_id, plan["post_url"], quantity)
            except SMMRejected as exc:
                order["attempts"] = int(order.get("attempts", 0)) + 1
                order["error"] = str(exc)
                gave_up = order["attempts"] >= MAX_SPLIT_ATTEMPTS
                if gave_up:
                    order["status"] = "failed"
                problem = (
                    f"View order rejected ({label}, {quantity:,} for post {post_id}): {exc}"
                    + (" - giving up on this one" if gave_up else " - will retry next run")
                )
                problems.append(problem)
                log.warning("%s", problem)
                break  # the next split for this post would be rejected for the same reason
            except (SMMError, ValueError) as exc:
                order["status"] = "unknown"
                order["error"] = str(exc)
                problem = (
                    f"View order outcome unknown ({label}, {quantity:,} for post {post_id}): {exc}"
                    " - not retried; check the panel's order list"
                )
                problems.append(problem)
                plan.setdefault("errors", []).append(problem)
                log.warning("%s", problem)
            else:
                order.update(status="placed", reference=result.order_id, placed_at=stamp, error=None)
                entry = _campaign_entry(config, result.order_id, stamp, service_id, "impressions", quantity)
                entry["split"] = f"{index}/{len(orders)}"
                plan.setdefault("authorized_campaigns", []).append(entry)
                placed.append({"post_id": post_id, "type": label, "order_id": result.order_id, "quantity": quantity})
                log.info("Placed %s order %s for post %s (qty=%d)", label, result.order_id, post_id, quantity)
    return placed, problems


def view_schedule_progress(plan: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Summary of a plan's split view orders for reports, or None if it has none."""
    schedule = plan.get("view_schedule")
    if not isinstance(schedule, dict) or not schedule.get("orders"):
        return None
    orders = schedule["orders"]
    done = [o for o in orders if o.get("status") == "placed"]
    pending = [o for o in orders if o.get("status") == "pending"]
    return {
        "total": schedule.get("total", 0),
        "orders": len(orders),
        "placed_orders": len(done),
        "placed_quantity": sum(int(o["quantity"]) for o in done),
        "pending_orders": len(pending),
        "problem_orders": len(orders) - len(done) - len(pending),
        "next_due": min((o["due_at"] for o in pending), default=None),
    }


def tracked_post_ids(posts: dict[str, Any], now: datetime, lookback_days: int, exclude: Optional[set[str]] = None) -> list[str]:
    """Posts recent enough that their metrics are still worth refreshing."""
    if lookback_days <= 0:
        return []
    exclude = exclude or set()
    ids = []
    for post_id, record in posts.items():
        if post_id in exclude:
            continue
        try:
            created = parse_iso_datetime(record["created_at"])
        except (KeyError, ValueError):
            continue
        if (now - created).days < lookback_days:
            ids.append(post_id)
    return sorted(ids, key=int)
