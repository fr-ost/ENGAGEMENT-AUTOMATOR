"""Run summary, daily Markdown report, and Telegram text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import BD_TZ, Config, format_iso, parse_iso_datetime


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: Optional[datetime] = None
    scan_status: str = "NOT_RUN"  # SUCCESS, FAILED, INCOMPLETE, NOT_RUN
    dry_run: bool = False
    first_run: bool = False
    username: Optional[str] = None
    window_start: Optional[str] = None
    previous_checkpoint: Optional[str] = None
    new_checkpoint: Optional[str] = None
    checkpoint_advanced: bool = False
    new_posts: list[dict[str, Any]] = field(default_factory=list)
    duplicates_skipped: int = 0
    skipped_replies: int = 0
    skipped_reposts: int = 0
    skipped_quotes: int = 0
    invalid_items: int = 0
    plans_created: int = 0
    metrics_updated: int = 0
    x_requests: int = 0
    x_posts_read: int = 0
    pages_fetched: int = 0
    smm_requests: int = 0
    smm_balance: Optional[str] = None
    smm_services: list[dict[str, Any]] = field(default_factory=list)
    orders_placed: list[dict[str, Any]] = field(default_factory=list)
    campaigns_checked: int = 0
    verified_spend: dict[str, float] = field(default_factory=dict)
    telegram_status: str = "disabled"
    persisted: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bd_date(self) -> str:
        return self.started_at.astimezone(BD_TZ).strftime("%Y-%m-%d")

    def estimated_x_cost(self, config: Config) -> Optional[float]:
        if config.x_cost_per_request_usd is None and config.x_cost_per_post_read_usd is None:
            return None
        return self.x_requests * (config.x_cost_per_request_usd or 0.0) + self.x_posts_read * (
            config.x_cost_per_post_read_usd or 0.0
        )

    def to_run_record(self) -> dict[str, Any]:
        return {
            "started_at": format_iso(self.started_at),
            "finished_at": format_iso(self.finished_at) if self.finished_at else None,
            "scan_status": self.scan_status,
            "new_posts": len(self.new_posts),
            "duplicates_skipped": self.duplicates_skipped,
            "x_requests": self.x_requests,
            "errors": self.errors[:10],
        }


def _bd(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    return parse_iso_datetime(ts).astimezone(BD_TZ).strftime("%Y-%m-%d %H:%M BST")


def _progress(plan: dict[str, Any]) -> str:
    perf = plan.get("performance") or {}
    targets = plan.get("targets") or {}
    imp, likes = perf.get("impressions"), perf.get("likes")
    imp_s = f"{imp:,}" if isinstance(imp, int) else "n/a"
    likes_s = f"{likes:,}" if isinstance(likes, int) else "n/a"
    return f"{imp_s} / {targets.get('impressions', 0):,} impressions · {likes_s} / {targets.get('likes', 0):,} likes"


def posts_discovered_on(posts: dict[str, Any], bd_date: str) -> list[dict[str, Any]]:
    found = [
        p
        for p in posts.values()
        if parse_iso_datetime(p["discovered_at"]).astimezone(BD_TZ).strftime("%Y-%m-%d") == bd_date
    ]
    return sorted(found, key=lambda p: int(p["post_id"]))


def render_markdown(summary: RunSummary, config: Config, posts: dict[str, Any], plans: dict[str, Any]) -> str:
    started_bd = summary.started_at.astimezone(BD_TZ).strftime("%Y-%m-%d %H:%M BST")
    lines = [f"# X daily report — {summary.bd_date}", ""]
    if summary.dry_run:
        lines += ["> **DRY RUN** — nothing was saved and no notification was sent.", ""]
    lines += [
        "## Run",
        "",
        "| Item | Value |",
        "| --- | --- |",
        f"| Run started | {started_bd} |",
        f"| Account | @{summary.username or '?'} |",
        f"| Scan status | **{summary.scan_status}** |",
        f"| Scan window start | {_bd(summary.window_start)} |",
        f"| Previous checkpoint | {_bd(summary.previous_checkpoint)} |",
        f"| Checkpoint advanced | {'yes → ' + _bd(summary.new_checkpoint) if summary.checkpoint_advanced else 'no'} |",
        f"| State persisted | {'yes' if summary.persisted else 'no'} |",
        f"| Telegram | {summary.telegram_status} |",
        "",
        f"## New posts this run: {len(summary.new_posts)}",
        "",
    ]
    if summary.new_posts:
        for post in summary.new_posts:
            lines.append(f"- [{post['post_id']}]({post['post_url']}) — created {_bd(post['created_at'])} ({post['kind']})")
    else:
        lines.append("No new posts were found in this scan." if summary.scan_status == "SUCCESS" else "No new posts recorded.")
    lines.append("")

    if summary.orders_placed:
        lines += [f"## Auto-orders placed this run: {len(summary.orders_placed)}", ""]
        lines += ["| Post | Type | Order ID | Quantity |", "| --- | --- | --- | --- |"]
        for o in summary.orders_placed:
            pid = o["post_id"]
            plan = plans.get(pid, {})
            url = plan.get("post_url") or posts.get(pid, {}).get("post_url") or pid
            lines.append(f"| [{pid}]({url}) | {o['type']} | {o['order_id']} | {o['quantity']:,} |")
        lines.append("")

    today = posts_discovered_on(posts, summary.bd_date)
    lines += [f"## Promotion planning — posts discovered on {summary.bd_date}: {len(today)}", ""]
    lines.append(
        f"Targets per post: **{config.impression_target:,} impressions** and **{config.like_target:,} likes**"
        + (f" (auto-ordered via {config.smm_provider})" if config.smm_auto_order else " (planning goals only — no auto-orders)")
        + "."
    )
    lines.append("")
    if today:
        lines += ["| Post | Promotion status | Progress (X public metrics) |", "| --- | --- | --- |"]
        for post in today:
            plan = plans.get(post["post_id"], {})
            lines.append(f"| [{post['post_id']}]({post['post_url']}) | {plan.get('promotion_status', '—')} | {_progress(plan)} |")
        lines.append("")

    recent = [p for p in plans.values() if p.get("performance")]
    if recent:
        lines += ["## Tracked post performance", "", "| Post | Status | Progress |", "| --- | --- | --- |"]
        for plan in sorted(recent, key=lambda p: int(p["post_id"]), reverse=True)[:20]:
            lines.append(f"| [{plan['post_id']}]({plan['post_url']}) | {plan['promotion_status']} | {_progress(plan)} |")
        lines.append("")

    cost = summary.estimated_x_cost(config)
    lines += [
        "## API usage and cost",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| X API requests | {summary.x_requests} |",
        f"| X post objects read | {summary.x_posts_read} |",
        f"| Timeline pages fetched | {summary.pages_fetched} |",
        f"| Duplicates skipped | {summary.duplicates_skipped} |",
        f"| Replies / reposts / quotes filtered | {summary.skipped_replies} / {summary.skipped_reposts} / {summary.skipped_quotes} |",
        f"| Malformed items ignored | {summary.invalid_items} |",
        f"| Estimated X API cost | {f'${cost:.4f} (from your configured prices)' if cost is not None else 'unknown — set X_COST_PER_REQUEST_USD / X_COST_PER_POST_READ_USD from your X developer console'} |",
        f"| SMM panel ({config.smm_provider}) requests | {summary.smm_requests} |",
        f"| SMM panel balance | {summary.smm_balance or 'not retrieved'} |",
        f"| Linked campaigns checked | {summary.campaigns_checked} |",
        f"| Verified campaign spend | {_spend(summary.verified_spend)} |",
        "",
    ]
    if summary.smm_services:
        lines += ["### SMM panel service metadata", ""]
        for svc in summary.smm_services:
            lines.append("- " + ", ".join(f"{k}: {v}" for k, v in svc.items()))
        lines.append("")

    lines += ["## Errors and incomplete operations", ""]
    if summary.errors or summary.warnings:
        lines += [f"- ❌ {e}" for e in summary.errors]
        lines += [f"- ⚠️ {w}" for w in summary.warnings]
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _spend(spend: dict[str, float]) -> str:
    if not spend:
        return "none reported"
    return ", ".join(f"{amount:.2f} {currency}" for currency, amount in sorted(spend.items()))


def write_report(reports_dir: Path, summary: RunSummary, markdown: str) -> Path:
    """Write reports/YYYY-MM-DD.md (Bangladesh date). Reruns on the same day overwrite it."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{summary.bd_date}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(markdown, encoding="utf-8")
    tmp.replace(path)
    return path


def render_telegram(summary: RunSummary, config: Config) -> str:
    started_bd = summary.started_at.astimezone(BD_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"X daily report — {started_bd} (Bangladesh time)",
        f"Account: @{summary.username or '?'}",
        f"Scan: {summary.scan_status}",
        f"New posts: {len(summary.new_posts)}",
    ]
    for post in summary.new_posts:
        lines.append(f"• {post['post_url']}")
    lines.append(f"Planning targets per post: {config.impression_target:,} impressions, {config.like_target:,} likes")
    if summary.orders_placed:
        lines.append(f"Auto-orders placed: {len(summary.orders_placed)}")
        for o in summary.orders_placed:
            lines.append(f"  • {o['type']} order #{o['order_id']} — {o['quantity']:,} units")
    if summary.smm_balance:
        lines.append(f"SMM panel balance ({config.smm_provider}): {summary.smm_balance}")
    lines.append(f"X API requests: {summary.x_requests}; duplicates skipped: {summary.duplicates_skipped}")
    if summary.errors or summary.warnings:
        lines.append("Problems:")
        lines += [f"- {e}" for e in summary.errors]
        lines += [f"- {w}" for w in summary.warnings]
    return "\n".join(lines)
