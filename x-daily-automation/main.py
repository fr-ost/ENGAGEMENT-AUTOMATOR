"""X post discovery and promotion ordering.

One *cycle* scans X when a scan is due, orders for new posts, and places the
split view orders that have come due. The process then stays alive and repeats
the cycle whenever the next order or scan is due (see src/keepalive.py), because
GitHub cannot be relied on to start a fresh run at those moments.

Exit codes (of the last cycle):
  0  scan succeeded (or was not due) and state was saved
  1  scan failed or was incomplete, or state could not be saved
     (the previous checkpoint is preserved)
  2  configuration error
  3  state is corrupt/invalid or another run holds the lock
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.campaign_manager import (
    SMMStatusProvider,
    apply_metrics,
    DEFAULT_VIEW_LIMITS,
    auto_place_orders,
    ensure_plans,
    new_post_record,
    next_view_order_wake,
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
from src.keepalive import hooks_from_env, run_loop
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


SCAN_INTERVAL_TOLERANCE = timedelta(minutes=20)  # a cycle this close to the next scan does the scan too
SCAN_RETRY_DELAY = timedelta(minutes=30)  # pause before repeating a scan that failed
SCAN_FAILURE_REMINDER_EVERY = 8  # of a row of failed scans, only the 1st, 8th, 16th... send a message
STUCK_RETRY_DELAY = timedelta(minutes=30)  # how long to leave an overdue item the last cycle could not move


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return parse_iso_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _last_scan_failed(state: dict[str, Any]) -> bool:
    last_ok = _parse_time(state.get("last_successful_scan_at"))
    last_try = _parse_time(state.get("last_scan_attempt_at"))
    return last_try is not None and (last_ok is None or last_try > last_ok)


def _next_scan_time(state: dict[str, Any], config: Config) -> Optional[datetime]:
    """When the next X scan is planned, or None if it should happen straight away."""
    if _last_scan_failed(state):
        return _parse_time(state.get("last_scan_attempt_at")) + SCAN_RETRY_DELAY  # type: ignore[operator]
    last_ok = _parse_time(state.get("last_successful_scan_at"))
    if last_ok is None:
        return None
    return last_ok + timedelta(hours=max(config.scan_interval_hours, 1))


def _scan_due(state: dict[str, Any], config: Config, now: datetime) -> bool:
    """Cycles come more often than scans; in-between cycles only place orders that are due."""
    if config.force_scan or config.scan_since_override or not config.scan_interval_hours:
        return True
    planned = _next_scan_time(state, config)
    if planned is None:
        return True
    return now >= (planned if _last_scan_failed(state) else planned - SCAN_INTERVAL_TOLERANCE)


def _posted_recently(posts: dict[str, Any], config: Config, now: datetime) -> bool:
    cutoff = now - timedelta(hours=config.keep_alive_after_post_hours)
    found = (_parse_time(post.get("discovered_at")) for post in posts.values())
    return any(when is not None and when > cutoff for when in found)


def next_wake(config: Config, now: datetime) -> Optional[datetime]:
    """When the bot next has something to do, or None if the process can stop.

    KEEP_ALIVE=auto keeps it running while split view orders are waiting, and
    for KEEP_ALIVE_AFTER_POST_HOURS after the latest new post so that the next
    one is found by a scan on time rather than whenever GitHub starts a run.
    KEEP_ALIVE=always keeps it running regardless.
    """
    if config.keep_alive == "off" or config.dry_run:
        return None
    try:
        bot_state = StateStore(config.data_dir).load()
    except StateError as exc:
        log.error("%s", exc)
        return None
    state = bot_state.state
    ordering = config.smm_enabled and config.smm_auto_order
    order_wake = next_view_order_wake(bot_state.campaigns["plans"]) if ordering else None
    if order_wake is None and config.keep_alive != "always":
        if not _posted_recently(bot_state.posts["posts"], config, now):
            return None
    scan_wake = _next_scan_time(state, config) or now
    wake = scan_wake if order_wake is None else min(order_wake, scan_wake)

    last_run = state.get("last_run") or {}
    last_started = _parse_time(last_run.get("started_at"))
    if last_started and wake <= last_started and not last_run.get("orders_placed") and not last_run.get("new_posts"):
        # The cycle that just ran could have dealt with this and did not; do not spin on it.
        wake = now + STUCK_RETRY_DELAY
    return wake


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
        state["last_scan_attempt_at"] = format_iso(now)
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
    if scan_due:
        state["scan_failures"] = 0 if scan_ok else int(state.get("scan_failures") or 0) + 1
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

    # Messages go out for news, not for every cycle: new posts, a post's views all ordered,
    # and problems. A scan that keeps failing is repeated only now and then.
    scan_failed = summary.scan_status in ("FAILED", "INCOMPLETE")  # each adds exactly one error above
    failures = int(state.get("scan_failures") or 0)
    repeat_failure = scan_failed and failures > 1 and failures % SCAN_FAILURE_REMINDER_EVERY != 0
    other_errors = len(summary.errors) - (1 if scan_failed else 0)
    worth_a_message = bool(
        summary.new_posts or summary.views_completed or other_errors > 0 or (scan_failed and not repeat_failure)
    )
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

    # Auto-ordering: likes at once and a split view schedule for new posts, then the view
    # orders whose turn has come (from this cycle's posts and earlier ones).
    if config.smm_auto_order and not config.dry_run:
        if new_post_ids:
            placed, problems = auto_place_orders(new_post_ids, posts, plans, config, smm_client, now, view_limits)
            summary.orders_placed.extend(placed)
            summary.errors.extend(problems)
        outcome = place_due_view_orders(plans, config, smm_client, now)
        summary.orders_placed.extend(outcome.placed)
        summary.errors.extend(outcome.problems)
        summary.warnings.extend(outcome.notes)
        summary.views_completed = outcome.completed

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
    hooks = hooks_from_env(os.environ)

    def cycle(first: bool) -> int:
        # What a manual run asks for (forced scan, recovery scan, state re-init) applies once.
        later = replace(config, force_scan=False, scan_since_override=None, allow_state_reinit=False)
        return run(config if first else later)

    try:
        if config.dry_run:
            code = run(config)
            # A dry run saves nothing, so anything still scheduled needs a real run to follow it.
            if next_wake(replace(config, dry_run=False), _now()) is not None:
                hooks.hand_over()
            return code
        return run_loop(
            cycle, lambda now: next_wake(config, now), hooks, config.keep_alive_minutes, clock=_now, sleep=time.sleep
        )
    except PersistenceError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
