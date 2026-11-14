"""Daily X post discovery and promotion-planning run.

Exit codes:
  0  scan succeeded and state was saved
  1  scan failed or was incomplete, or state could not be saved
     (the previous checkpoint is preserved)
  2  configuration error
  3  state is corrupt/invalid or another run holds the lock
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.campaign_manager import (
    SMMStatusProvider,
    apply_metrics,
    DEFAULT_VIEW_LIMITS,
    auto_place_orders,
    ensure_plans,
    new_post_record,
    place_due_view_orders,
    refresh_campaign_statuses,
    tracked_post_ids,
)
from src.config import (
    PROJECT_ROOT,
    Config,
    ConfigError,
    format_iso,
    load_config,
    load_dotenv,
    parse_iso_datetime,
    setup_logging,
)
from src.deduplication import deduplicate
from src.reporter import RunSummary, render_markdown, render_telegram, write_report
from src.scanner import Scanner, ScanError
from src.smm_client import SMMClient, SMMError, summarize_service
from src.storage import BotState, LockError, PersistenceError, StateError, StateStore, record_run
from src.telegram import TelegramError, TelegramNotifier
from src.x_client import XAPIError, XAuthError, XClient

log = logging.getLogger("x_daily")

# Normal scans ask only for posts newer than the last one seen. This often, a scan
# re-reads the overlap window instead, to catch a post X indexed late.
WINDOW_RESCAN_INTERVAL = timedelta(hours=24)


SCAN_INTERVAL_TOLERANCE = timedelta(minutes=20)  # scheduled runs start late and unevenly


def _scan_due(state: dict[str, Any], config: Config, now: datetime) -> bool:
    """Runs come more often than scans; in-between runs only place orders that are due."""
    last = state.get("last_successful_scan_at")
    if config.force_scan or config.scan_since_override or not config.scan_interval_hours or not last:
        return True
    try:
        return now - parse_iso_datetime(last) >= timedelta(hours=config.scan_interval_hours) - SCAN_INTERVAL_TOLERANCE
    except (TypeError, ValueError):
        return True


def _since_id(state: dict[str, Any], config: Config, now: datetime) -> Optional[str]:
    last_seen, last_window_scan = state.get("last_seen_post_id"), state.get("last_window_scan_at")
    if not (isinstance(last_seen, str) and last_seen.isdigit()) or config.scan_since_override:
        return None
    try:
        if not last_window_scan or now - parse_iso_datetime(last_window_scan) >= WINDOW_RESCAN_INTERVAL:
            return None
    except (TypeError, ValueError):
        return None
    return last_seen


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def run(
    config: Config,
    now: Optional[datetime] = None,
    x_client: Optional[XClient] = None,
    smm_client: Optional[SMMClient] = None,
    notifier: Optional[TelegramNotifier] = None,
) -> int:
    now = now or _now()
    store = StateStore(config.data_dir)
    try:
        with store.lock():
            bot_state = store.load(allow_reinit=config.allow_state_reinit)
            return _run_locked(config, now, store, bot_state, x_client, smm_client, notifier)
    except (LockError, StateError) as exc:
        log.error("%s", exc)
        return 3


def _run_locked(
    config: Config,
    now: datetime,
    store: StateStore,
    bot_state: BotState,
    x_client: Optional[XClient],
    smm_client: Optional[SMMClient],
    notifier: Optional[TelegramNotifier],
) -> int:
    state, posts, plans = bot_state.state, bot_state.posts["posts"], bot_state.campaigns["plans"]
    summary = RunSummary(started_at=now, dry_run=config.dry_run, first_run=bot_state.first_run)
    summary.previous_checkpoint = state.get("last_successful_scan_at")
    summary.username = (state.get("account") or {}).get("username") or config.x_username
    summary.warnings += bot_state.recovery_notes
    if bot_state.first_run:
        summary.warnings.append(
            f"First run: no saved state, scanned the last {config.initial_lookback_hours}h only"
        )
    if config.scan_since_override:
        summary.warnings.append(f"Manual recovery scan from SCAN_SINCE={format_iso(config.scan_since_override)}")

    x_client = x_client or XClient(
        config.x_bearer_token,
        timeout=config.http_timeout_seconds,
        max_retries=config.http_max_retries,
        max_rate_limit_wait=config.max_rate_limit_wait_seconds,
    )

    # 1. Discover posts. Any failure leaves the checkpoint untouched.
    scan_ok = False
    scan_ids: set[str] = set()
    new_posts: list = []
    scan_due = _scan_due(state, config, now)
    try:
        if not scan_due:
            summary.scan_status = "SKIPPED"
            raise _ScanSkipped
        since_id = _since_id(state, config, now)
        result = Scanner(x_client, config).scan(
            state.get("account"), state.get("last_successful_scan_at"), now, since_id=since_id
        )
        summary.since_id = since_id
        summary.username = result.username
        summary.window_start = format_iso(result.window_start)
        summary.pages_fetched = result.pages_fetched
        summary.skipped_replies = result.skipped_replies
        summary.skipped_reposts = result.skipped_reposts
        summary.skipped_quotes = result.skipped_quotes
        summary.invalid_items = result.invalid_items
        state["account"] = {"user_id": result.user_id, "username": result.username}

        # 2. Deduplicate by immutable post ID and record new posts + plans.
        new_posts, summary.duplicates_skipped = deduplicate(result.posts, posts.keys())
        for post in new_posts:
            posts[post.post_id] = new_post_record(post, now)
        summary.new_posts = [posts[p.post_id] for p in new_posts]
        summary.plans_created = len(ensure_plans(posts, plans, config, now))

        metrics = {p.post_id: p.public_metrics for p in result.posts if p.public_metrics}
        scan_ids = set(metrics)
        summary.metrics_updated += apply_metrics(posts, plans, metrics, now)

        if result.truncated:
            summary.scan_status = "INCOMPLETE"
            summary.errors.append(
                f"Scan stopped after X_MAX_PAGES={config.x_max_pages} pages with more posts remaining; "
                "checkpoint NOT advanced. Increase X_MAX_PAGES and re-run."
            )
        else:
            summary.scan_status = "SUCCESS"
            scan_ok = True
            newest = max(int(result.newest_post_id or 0), int(state.get("last_seen_post_id") or 0))
            if newest:
                state["last_seen_post_id"] = str(newest)
            if since_id is None:
                state["last_window_scan_at"] = format_iso(now)
        if result.invalid_items:
            summary.warnings.append(f"Ignored {result.invalid_items} malformed post object(s) from the X API")
    except _ScanSkipped:
        log.info("Scan skipped: the last one was less than %dh ago", config.scan_interval_hours)
    except XAuthError as exc:
        summary.scan_status = "FAILED"
        summary.errors.append(f"X authentication failed: {exc}")
    except (XAPIError, ScanError) as exc:
        summary.scan_status = "FAILED"
        summary.errors.append(f"X scan failed: {exc}")

    # 3. Refresh public metrics for recent tracked posts (best effort).
    if summary.scan_status not in ("FAILED", "SKIPPED"):
        ids = tracked_post_ids(posts, now, config.metrics_lookback_days, exclude=scan_ids)
        if ids:
            try:
                found = x_client.get_posts_by_ids(ids)
                metrics = {
                    p["id"]: p["public_metrics"]
                    for p in found
                    if isinstance(p.get("id"), str) and isinstance(p.get("public_metrics"), dict)
                }
                summary.metrics_updated += apply_metrics(posts, plans, metrics, now)
            except XAPIError as exc:
                summary.warnings.append(f"Metrics refresh skipped: {exc}")

    summary.x_requests = x_client.request_count
    summary.x_posts_read = x_client.posts_read

    # 4. SMM panel checks: optional balance/service reads + auto-order for new posts.
    _smm_checks(config, summary, posts, plans, [p.post_id for p in new_posts], now, smm_client)

    # 5. Persist. The checkpoint is written last and only after a complete scan.
    if scan_ok:
        state["last_successful_scan_at"] = format_iso(now)
        state["last_scan_window_start"] = summary.window_start
        summary.new_checkpoint = state["last_successful_scan_at"]
        summary.checkpoint_advanced = True
    summary.finished_at = _now()
    record_run(state, summary.to_run_record())
    exit_code = 0 if scan_ok or not scan_due else 1

    if config.dry_run:
        summary.checkpoint_advanced = False
        log.info("DRY_RUN: state not saved")
    else:
        try:
            store.save(bot_state)
            summary.persisted = True
        except (PersistenceError, StateError) as exc:
            summary.checkpoint_advanced = False
            summary.errors.append(f"Saving state failed; checkpoint not advanced: {exc}")
            exit_code = 1

    # 6. Report, then optional notification (its failure never changes the exit code).
    markdown = render_markdown(summary, config, posts, plans)
    if config.dry_run:
        print(markdown)
    else:
        path = write_report(config.reports_dir, summary, markdown)
        log.info("Report written to %s", path)

    worth_a_message = bool(summary.new_posts or summary.errors or summary.scan_status in ("FAILED", "INCOMPLETE"))
    if config.telegram_enabled and not config.dry_run and not worth_a_message:
        summary.telegram_status = "not sent (nothing new)"
        write_report(config.reports_dir, summary, render_markdown(summary, config, posts, plans))
    elif config.telegram_enabled and not config.dry_run:
        try:
            notifier = notifier or TelegramNotifier(
                config.telegram_bot_token,  # type: ignore[arg-type]
                config.telegram_chat_id,  # type: ignore[arg-type]
                timeout=config.http_timeout_seconds,
            )
            sent = notifier.send(render_telegram(summary, config))
            summary.telegram_status = f"sent ({sent} message{'s' if sent != 1 else ''})"
        except (TelegramError, ValueError) as exc:
            summary.telegram_status = f"FAILED: {exc}"
            log.warning("Telegram notification failed (scan result unaffected): %s", exc)
        write_report(config.reports_dir, summary, render_markdown(summary, config, posts, plans))

    log.info(
        "Done: scan=%s new=%d duplicates=%d x_requests=%d errors=%d",
        summary.scan_status,
        len(summary.new_posts),
        summary.duplicates_skipped,
        summary.x_requests,
        len(summary.errors),
    )
    for error in summary.errors:
        log.error("%s", error)
    return exit_code


def _smm_checks(
    config: Config,
    summary: RunSummary,
    posts: dict[str, Any],
    plans: dict[str, Any],
    new_post_ids: list[str],
    now: datetime,
    smm_client: Optional[SMMClient],
) -> None:
    if not config.smm_enabled:
        return
    try:
        smm_client = smm_client or SMMClient(
            config.smm_api_url,
            config.smm_api_key,  # type: ignore[arg-type]
            timeout=config.http_timeout_seconds,
            max_retries=config.http_max_retries,
        )
    except ValueError as exc:
        summary.warnings.append(f"SMM panel client not created: {exc}")
        return

    try:
        balance = smm_client.get_balance()
        summary.smm_balance = f"{balance.amount:.2f} {balance.currency or ''}".strip()
    except SMMError as exc:
        summary.warnings.append(f"SMM panel balance unavailable: {exc}")

    view_limits = DEFAULT_VIEW_LIMITS
    wanted = [s for s in (config.smm_impression_service_id, config.smm_like_service_id) if s]
    if wanted:
        try:
            services = smm_client.list_services()
            for service_id in wanted:
                item = smm_client.get_service(service_id, services)
                if item is None:
                    summary.warnings.append(f"SMM panel service {service_id} is not in the service list")
                else:
                    summary.smm_services.append(summarize_service(item))
                    if service_id == config.smm_impression_service_id:
                        view_limits = _service_limits(item)
        except SMMError as exc:
            summary.warnings.append(f"SMM panel service metadata unavailable: {exc}")

    # Auto-ordering: likes at once and a split view schedule for new posts, then whatever
    # view orders have come due (from this run's posts and earlier ones).
    if config.smm_auto_order and not config.dry_run:
        if new_post_ids:
            summary.orders_placed.extend(
                auto_place_orders(new_post_ids, posts, plans, config, smm_client, now, view_limits)
            )
        placed, problems = place_due_view_orders(plans, config, smm_client, now)
        summary.orders_placed.extend(placed)
        summary.errors.extend(problems)

    campaign_summary = refresh_campaign_statuses(plans, {config.smm_provider: SMMStatusProvider(smm_client, config.smm_provider)}, now)
    summary.campaigns_checked = campaign_summary["checked"]
    summary.verified_spend = campaign_summary["verified_spend"]
    summary.warnings += [f"Campaign status check failed: {e}" for e in campaign_summary["errors"]]
    summary.smm_requests = smm_client.request_count


class _ScanSkipped(Exception):
    pass


def _service_limits(item: dict[str, Any]) -> tuple[int, int]:
    def as_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    return as_int(item.get("min"), DEFAULT_VIEW_LIMITS[0]), as_int(item.get("max"), DEFAULT_VIEW_LIMITS[1])


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")  # local convenience; Actions uses Secrets/Variables
    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging()
        log.error("%s", exc)
        return 2
    setup_logging(config)
    try:
        return run(config)
    except PersistenceError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
