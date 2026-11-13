"""Bot logic: turn a link into scheduled panel orders and place them when due."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .config import Config
from .panel import Panel, PanelError, PanelRejected
from .planner import plan_quantities, plan_times
from .store import Store
from .tg import Telegram, TelegramError

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I)
NUMBER_RE = re.compile(r"^(\d[\d,]*)(k?)$", re.I)
BD_TZ = timezone(timedelta(hours=6))

HELP = """Send an X post link and I order views and likes for it.

  https://x.com/user/status/123            → default views and likes
  https://x.com/user/status/123 5000 100   → 5,000 views, 100 likes
  https://x.com/user/status/123 5000 0     → views only

Views are split into several uneven orders spread over time. Likes go as one order straight away.

/views 5000 – default views per link
/likes 100 – default likes per link
/spread 12 – hours over which view orders are placed
/splits 10 12 – how many view orders per link (min max)
/settings – show current settings
/status – recent jobs · /status 3 – one job in detail
/cancel 3 – stop the orders of job 3 that are not placed yet
/retry 3 – re-queue rejected orders of job 3
/balance – panel balance"""


@dataclass(frozen=True)
class Settings:
    views: int
    likes: int
    spread_hours: float
    splits: tuple[int, int]


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, BD_TZ).strftime("%d %b %H:%M")


def _limits(service: dict[str, Any]) -> tuple[int, int]:
    def as_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    return as_int(service.get("min"), 1), as_int(service.get("max"), 10**9)


class App:
    ACTIVE_ORDER_RETRY_SECONDS = 600
    MAX_ATTEMPTS = 6
    SERVICES_CACHE_SECONDS = 600
    UPDATE_ID_MAX_AGE_SECONDS = 6 * 86400  # Telegram may restart update IDs after a week of silence

    def __init__(
        self,
        config: Config,
        store: Store,
        panel: Panel,
        tg: Telegram,
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self.panel = panel
        self.tg = tg
        self.rng = rng or random.Random()
        self.clock = clock
        self._services: tuple[float, list[dict[str, Any]]] = (0.0, [])

    # -- settings -----------------------------------------------------------

    def settings(self) -> Settings:
        get = self.store.get_setting
        return Settings(
            views=int(get("views", str(self.config.default_views))),
            likes=int(get("likes", str(self.config.default_likes))),
            spread_hours=float(get("spread_hours", str(self.config.spread_hours))),
            splits=(int(get("splits_min", "10")), int(get("splits_max", "12"))),
        )

    def _settings_text(self) -> str:
        s = self.settings()
        return (
            f"Views per link: {s.views:,}\nLikes per link: {s.likes:,}\n"
            f"View orders per link: {s.splits[0]}-{s.splits[1]}, placed over {s.spread_hours:g}h\n"
            f"Services: views {self.config.view_service_id}, likes {self.config.like_service_id}"
        )

    # -- incoming messages --------------------------------------------------

    def _last_update_id(self) -> int:
        stored_at = float(self.store.get_setting("last_update_at", "0"))
        if self.clock() - stored_at > self.UPDATE_ID_MAX_AGE_SECONDS:
            return 0
        return int(self.store.get_setting("last_update_id", "0"))

    def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            # Recorded before acting, so a crash can drop a message but never order twice.
            if update_id <= self._last_update_id():
                return
            self.store.set_setting("last_update_id", update_id)
            self.store.set_setting("last_update_at", self.clock())
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if chat_id is None or not text:
            return
        if user_id not in self.config.owner_ids:
            self.tg.send(chat_id, f"Not authorised. Your Telegram user ID is {user_id}.")
            return
        try:
            reply = self._dispatch(text, chat_id)
        except PanelError as exc:
            reply = f"Panel error: {exc}"
        if reply:
            self.tg.send(chat_id, reply)

    def _dispatch(self, text: str, chat_id: int) -> str:
        if not text.startswith("/"):
            return self._new_job(text, chat_id)
        parts = text.split()
        command = parts[0][1:].split("@")[0].lower()
        handler = getattr(self, f"_cmd_{command}", None) if command.isalpha() else None
        if handler is None:
            return "Unknown command. /help shows what I understand."
        return handler(parts[1:])

    # -- new job ------------------------------------------------------------

    def _panel_services(self) -> list[dict[str, Any]]:
        fetched_at, services = self._services
        if not services or self.clock() - fetched_at > self.SERVICES_CACHE_SECONDS:
            services = self.panel.services()
            self._services = (self.clock(), services)
        return services

    def _service(self, service_id: str) -> Optional[dict[str, Any]]:
        return next((s for s in self._panel_services() if str(s.get("service")) == str(service_id)), None)

    def _new_job(self, text: str, chat_id: int) -> str:
        match = LINK_RE.search(text)
        if not match:
            return "Send an X post link (https://x.com/user/status/123), optionally followed by views and likes. /help"
        link = f"https://x.com/{match[1]}/status/{match[2]}"
        numbers = []
        for token in (text[: match.start()] + " " + text[match.end() :]).split():
            found = NUMBER_RE.match(token)
            if found:
                numbers.append(int(found[1].replace(",", "")) * (1000 if found[2] else 1))
        if len(numbers) > 2:
            return "I understand at most two numbers after the link: views, then likes. Nothing was ordered."
        s = self.settings()
        views = numbers[0] if numbers else s.views
        likes = numbers[1] if len(numbers) == 2 else s.likes
        if views == 0 and likes == 0:
            return "Views and likes are both 0, so there is nothing to order."
        if views > self.config.max_views or likes > self.config.max_likes:
            return (
                f"That is above the safety limit ({self.config.max_views:,} views, {self.config.max_likes:,} likes "
                "per link). Nothing was ordered. Raise MAX_VIEWS / MAX_LIKES if you meant it."
            )
        running = self.store.active_job_for_link(link)
        if running:
            return f"Job #{running['id']} is still placing orders for this link. /cancel {running['id']} first, or wait."

        try:
            view_service = self._service(self.config.view_service_id) if views else None
            like_service = self._service(self.config.like_service_id) if likes else None
        except PanelError as exc:
            return f"Could not read the panel's service list ({exc}). Nothing was ordered."

        now = self.clock()
        orders: list[tuple[str, int, str, float]] = []
        cost, cost_known = 0.0, True
        lines = [f"{link}"]

        def add_cost(service: dict[str, Any], quantity: int) -> None:
            nonlocal cost, cost_known
            try:
                cost += float(service["rate"]) * quantity / 1000
            except (KeyError, TypeError, ValueError):
                cost_known = False

        if views:
            if view_service is None:
                return f"View service {self.config.view_service_id} is not in the panel's service list. Nothing was ordered."
            low, high = _limits(view_service)
            try:
                quantities = plan_quantities(views, low, high, self.rng, s.splits)
            except ValueError as exc:
                return f"Views: {exc}. Nothing was ordered."
            times = plan_times(len(quantities), now, s.spread_hours * 3600, self.rng)
            orders += [("views", q, self.config.view_service_id, t) for q, t in zip(quantities, times)]
            add_cost(view_service, views)
            if len(quantities) > 1:
                lines.append(f"Views: {views:,} in {len(quantities)} orders, first now, last around {_when(times[-1])}")
            else:
                lines.append(f"Views: {views:,} in one order, now")
        if likes:
            if like_service is None:
                return f"Like service {self.config.like_service_id} is not in the panel's service list. Nothing was ordered."
            low, high = _limits(like_service)
            if not low <= likes <= high:
                return f"Likes: {likes:,} is outside the service limits {low:,}-{high:,}. Nothing was ordered."
            orders.append(("likes", likes, self.config.like_service_id, now))
            add_cost(like_service, likes)
            lines.append(f"Likes: {likes:,} in one order, now")

        job_id = self.store.create_job(link, views, likes, chat_id, now, orders)
        log.info("Job %d created: views=%d likes=%d orders=%d", job_id, views, likes, len(orders))
        if cost_known:
            lines.append(f"Estimated cost: {cost:.4f} (panel currency)")
        lines.append(f"/status {job_id} · /cancel {job_id}")
        return f"Job #{job_id} started\n" + "\n".join(lines)

    # -- placing orders -----------------------------------------------------

    def process_due(self) -> None:
        for order in self.store.due_orders(self.clock()):
            job = self.store.job(order["job_id"])
            if job is not None and job["status"] == "active":
                self._place(order, job)

    def _place(self, order: Any, job: Any) -> None:
        order_id, job_id, kind, quantity = order["id"], job["id"], order["kind"], order["quantity"]
        attempts = order["attempts"] + 1
        # Marked before the call: if the process dies mid-request, recover() flags it instead of re-buying.
        self.store.update_order(order_id, status="placing", attempts=attempts)
        try:
            panel_order_id = self.panel.add(order["service_id"], job["link"], quantity)
        except PanelRejected as exc:
            message = str(exc)
            if "active order" in message.lower() and attempts < self.MAX_ATTEMPTS:
                # The panel refuses a second order on a link while one is running; try again shortly.
                self.store.update_order(
                    order_id, status="pending", due_at=self.clock() + self.ACTIVE_ORDER_RETRY_SECONDS, error=message
                )
                log.info("Job %d: %s order %d postponed (%s)", job_id, kind, order_id, message)
                return
            self.store.update_order(order_id, status="failed", error=message)
            log.warning("Job %d: %s order %d rejected: %s", job_id, kind, order_id, message)
            self.tg.send(
                job["chat_id"],
                f"Job #{job_id}: the panel rejected a {kind} order for {quantity:,}: {message}\n"
                f"It was not placed. /retry {job_id} to try it again.",
            )
        except PanelError as exc:
            self.store.update_order(order_id, status="unknown", error=str(exc))
            log.warning("Job %d: %s order %d outcome unknown: %s", job_id, kind, order_id, exc)
            self.tg.send(
                job["chat_id"],
                f"Job #{job_id}: no clear answer from the panel for a {kind} order of {quantity:,} ({exc}).\n"
                f"It may or may not have been placed. Check the panel's order list; if it is missing, "
                f"/retry {job_id} all re-queues it.",
            )
        else:
            self.store.update_order(
                order_id, status="placed", panel_order_id=panel_order_id, placed_at=self.clock(), error=None
            )
            log.info("Job %d: %s order placed, panel id %s, qty %d", job_id, kind, panel_order_id, quantity)
        self._finish_if_done(job_id)

    def _finish_if_done(self, job_id: int) -> None:
        job = self.store.job(job_id)
        if job is None or job["status"] != "active" or self.store.has_open_orders(job_id):
            return
        self.store.set_job_status(job_id, "done")
        self.tg.send(job["chat_id"], "All orders handled.\n" + self._job_summary(self.store.job(job_id)))

    def recover(self) -> None:
        """Orders caught mid-request by a restart: outcome unknown, so report instead of re-placing."""
        for order in self.store.orders_with_status("placing"):
            self.store.update_order(order["id"], status="unknown", error="bot restarted while placing this order")
            job = self.store.job(order["job_id"])
            self.tg.send(
                job["chat_id"],
                f"Job #{job['id']}: the bot restarted while placing a {order['kind']} order of "
                f"{order['quantity']:,}. Check the panel's order list; if it is missing, /retry {job['id']} all.",
            )
            self._finish_if_done(job["id"])

    # -- reporting ----------------------------------------------------------

    def _job_summary(self, job: Any) -> str:
        orders = self.store.job_orders(job["id"])
        lines = [f"Job #{job['id']} ({job['status']}) {job['link']}"]
        for kind, total in (("views", job["views"]), ("likes", job["likes"])):
            mine = [o for o in orders if o["kind"] == kind]
            if not mine:
                continue
            placed = [o for o in mine if o["status"] == "placed"]
            line = f"{kind.capitalize()}: {sum(o['quantity'] for o in placed):,} of {total:,} ordered ({len(placed)}/{len(mine)} orders)"
            pending = [o for o in mine if o["status"] == "pending"]
            if pending:
                line += f", next {_when(min(o['due_at'] for o in pending))}"
            problems = [o for o in mine if o["status"] in ("failed", "unknown")]
            if problems:
                line += f", {len(problems)} not placed — /retry {job['id']}"
            lines.append(line)
        return "\n".join(lines)

    def _job_detail(self, job: Any) -> str:
        orders = self.store.job_orders(job["id"])
        panel_ids = [o["panel_order_id"] for o in orders if o["panel_order_id"]]
        try:
            statuses = self.panel.statuses(panel_ids) if panel_ids else {}
        except PanelError:
            statuses = {}
        lines = [self._job_summary(job), ""]
        for o in orders:
            line = f"{o['kind']} {o['quantity']:,}: "
            if o["status"] == "placed":
                live = statuses.get(o["panel_order_id"], {})
                line += f"panel #{o['panel_order_id']} placed {_when(o['placed_at'])}"
                if live.get("status"):
                    line += f" — {live['status']}"
                    if live.get("remains") not in (None, "", "0", 0):
                        line += f", {live['remains']} remaining"
            elif o["status"] == "pending":
                line += f"due {_when(o['due_at'])}"
            else:
                line += o["status"] + (f" ({o['error']})" if o["error"] else "")
            lines.append(line)
        return "\n".join(lines)

    # -- commands -----------------------------------------------------------

    def _cmd_start(self, args: list[str]) -> str:
        return HELP

    _cmd_help = _cmd_start

    def _cmd_settings(self, args: list[str]) -> str:
        return self._settings_text()

    def _set_number(self, args: list[str], key: str, label: str, maximum: int) -> str:
        if not args:
            return self._settings_text()
        found = NUMBER_RE.match(args[0])
        if not found:
            return f"Usage: /{key} 5000"
        value = int(found[1].replace(",", "")) * (1000 if found[2] else 1)
        if value > maximum:
            return f"{value:,} is above the safety limit of {maximum:,}."
        self.store.set_setting(key, value)
        return f"Default {label} per link: {value:,}"

    def _cmd_views(self, args: list[str]) -> str:
        return self._set_number(args, "views", "views", self.config.max_views)

    def _cmd_likes(self, args: list[str]) -> str:
        return self._set_number(args, "likes", "likes", self.config.max_likes)

    def _cmd_spread(self, args: list[str]) -> str:
        try:
            hours = float(args[0])
        except (IndexError, ValueError):
            return "Usage: /spread 12  (hours over which view orders are placed)"
        if not 0 <= hours <= 72:
            return "Spread must be between 0 and 72 hours."
        self.store.set_setting("spread_hours", hours)
        return f"View orders will be placed over {hours:g}h."

    def _cmd_splits(self, args: list[str]) -> str:
        try:
            low, high = int(args[0]), int(args[1])
        except (IndexError, ValueError):
            return "Usage: /splits 10 12  (minimum and maximum view orders per link)"
        if not 1 <= low <= high <= 50:
            return "Use two numbers between 1 and 50, smaller first."
        self.store.set_setting("splits_min", low)
        self.store.set_setting("splits_max", high)
        return f"Views will be split into {low}-{high} orders."

    def _cmd_balance(self, args: list[str]) -> str:
        amount, currency = self.panel.balance()
        return f"Panel balance: {amount:.2f} {currency}".strip()

    def _job_from_args(self, args: list[str], usage: str) -> tuple[Optional[Any], str]:
        if not args or not args[0].lstrip("#").isdigit():
            return None, usage
        job = self.store.job(int(args[0].lstrip("#")))
        return job, "" if job else f"There is no job {args[0]}."

    def _cmd_status(self, args: list[str]) -> str:
        if not args:
            jobs = self.store.recent_jobs()
            return "\n\n".join(self._job_summary(j) for j in jobs) if jobs else "No jobs yet. Send me a link."
        job, problem = self._job_from_args(args, "Usage: /status 3")
        return self._job_detail(job) if job else problem

    def _cmd_cancel(self, args: list[str]) -> str:
        job, problem = self._job_from_args(args, "Usage: /cancel 3")
        if not job:
            return problem
        cancelled = self.store.cancel_pending(job["id"])
        if job["status"] == "active":
            self.store.set_job_status(job["id"], "cancelled")
        return (
            f"Job #{job['id']}: {cancelled} unplaced order(s) cancelled. "
            "Orders already placed on the panel keep running."
        )

    def _cmd_retry(self, args: list[str]) -> str:
        job, problem = self._job_from_args(args, "Usage: /retry 3   (or /retry 3 all to include unclear ones)")
        if not job:
            return problem
        wanted = ("failed", "unknown") if "all" in [a.lower() for a in args[1:]] else ("failed",)
        orders = [o for o in self.store.job_orders(job["id"]) if o["status"] in wanted]
        if not orders:
            return f"Job #{job['id']} has nothing to retry."
        now = self.clock()
        for index, order in enumerate(orders):
            # Staggered so the panel does not see several orders for one link at once.
            self.store.update_order(order["id"], status="pending", due_at=now + index * 300, attempts=0, error=None)
        self.store.set_job_status(job["id"], "active")
        return f"Job #{job['id']}: {len(orders)} order(s) re-queued."

    # -- main loop ----------------------------------------------------------

    def run_forever(self) -> None:
        self.recover()
        log.info("Bot running")
        while True:
            try:
                self.process_due()
                next_due = self.store.next_due()
                wait = 25 if next_due is None else int(max(0, min(25, next_due - self.clock())))
                last = self._last_update_id()
                for update in self.tg.get_updates(last + 1 if last else 0, wait):
                    try:
                        self.handle_update(update)
                    except Exception:
                        log.exception("Failed to handle an update")
            except TelegramError as exc:
                log.warning("Telegram polling failed: %s", exc)
                time.sleep(5)
            except Exception:
                log.exception("Unexpected error in main loop")
                time.sleep(5)
