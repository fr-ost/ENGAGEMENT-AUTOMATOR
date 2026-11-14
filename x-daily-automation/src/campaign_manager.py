"""Promotion planning records, order placement, and status tracking.

A planning record holds *targets* (what you hope a post achieves).  When
SMM_AUTO_ORDER=true the bot places SMM panel orders for each newly discovered
post and records the order reference in the plan's "authorized_campaigns" list.

Auto-placed orders carry {"authorized_by": "auto"}.  Orders you place yourself
outside the bot can also be tracked: edit data/campaigns.json and add an entry
to the post's "authorized_campaigns" list, e.g.
  {"provider": "x_ads", "reference": "<campaign id from X Ads Manager>",
   "authorized_by": "you", "authorized_at": "2026-01-31T12:00:00Z"}
Providers with a registered read-only status checker are refreshed each run.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Protocol

from .config import Config, format_iso, parse_iso_datetime
from .scanner import Post
from .smm_client import SMMClient, SMMError, OrderResult  # noqa: F401 (OrderResult re-exported for type hints)

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
) -> list[dict[str, Any]]:
    """Place SMM panel orders for newly discovered posts (SMM_AUTO_ORDER=true).

    For each new post, places one impression order and/or one like order
    according to the configured service IDs and target quantities.  Already-
    ordered types are skipped so reruns are idempotent.  Failures are logged
    and stored in the plan's 'errors' list but never propagate — a failed order
    never aborts the run.

    Returns a list of placement result dicts (post_id, type, order_id, quantity).
    """
    stamp = format_iso(now)
    results: list[dict[str, Any]] = []

    for post_id in new_post_ids:
        plan = plans.get(post_id)
        post = posts.get(post_id)
        if plan is None or post is None:
            continue

        link = post.get("post_url", "")
        targets = plan.get("targets", {})

        # Build the list of (type_label, service_id, quantity) to try.
        orders_to_place: list[tuple[str, str, int]] = []
        if config.smm_impression_service_id:
            qty = int(targets.get("impressions", 0))
            if qty > 0:
                orders_to_place.append(("impressions", config.smm_impression_service_id, qty))
        if config.smm_like_service_id:
            qty = int(targets.get("likes", 0))
            if qty > 0:
                orders_to_place.append(("likes", config.smm_like_service_id, qty))

        for order_type, service_id, quantity in orders_to_place:
            # Idempotency: skip if an order of this type was already recorded.
            already = any(c.get("type") == order_type for c in plan.get("authorized_campaigns", []))
            if already:
                log.debug("Skipping %s order for post %s — already recorded", order_type, post_id)
                continue

            try:
                result = smm_client.place_order(service_id, link, quantity)
                campaign_entry: dict[str, Any] = {
                    "provider": config.smm_provider,
                    "reference": result.order_id,
                    "authorized_by": "auto",
                    "authorized_at": stamp,
                    "service_id": service_id,
                    "type": order_type,
                    "quantity": quantity,
                }
                plan.setdefault("authorized_campaigns", []).append(campaign_entry)
                plan["updated_at"] = stamp
                results.append({"post_id": post_id, "type": order_type, "order_id": result.order_id, "quantity": quantity})
                log.info(
                    "Placed %s order %s for post %s (service=%s qty=%d)",
                    order_type, result.order_id, post_id, service_id, quantity,
                )
            except (SMMError, ValueError) as exc:
                msg = f"Auto-order failed ({order_type} for post {post_id}): {exc}"
                plan.setdefault("errors", []).append(msg)
                plan["updated_at"] = stamp
                log.warning("%s", msg)

    return results


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
