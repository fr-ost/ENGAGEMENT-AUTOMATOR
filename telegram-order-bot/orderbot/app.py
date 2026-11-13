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

HELP = """Send an X post link and I order views and likes for it, using your own panel account.

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
/balance – your panel balance
/setkey – set or replace your panel API key"""

ADMIN_HELP = """

Admin:
/adduser 123456789 – give a teammate access (they get their ID by messaging me)
/removeuser 123456789 – remove access, delete their key, cancel their queued orders
/users – who has access"""

NO_KEY = "First connect your panel account: send /setkey and then your API key (BulkFollows → Account Settings)."


@dataclass(frozen=True)
class Ctx:
    user: int
    message_id: Optional[int]
    admin: bool


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
        panel_for: Callable[[str], Panel],
        tg: Telegram,
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self.panel_for = panel_for
        self.tg = tg
        self.rng = rng or random.Random()
        self.clock = clock
        self._panels: dict[int, tuple[str, Panel]] = {}
        self._services: dict[int, tuple[float, list[dict[str, Any]]]] = {}

    # -- users --------------------------------------------------------------

    def _authorised(self, user: Any) -> bool:
        return isinstance(user, int) and (user in self.config.owner_ids or self.store.has_user(user))

    def _member_ids(self) -> set[int]:
        return set(self.config.owner_ids) | {row["user_id"] for row in self.store.users()}

    def _panel(self, user: int) -> Optional[Panel]:
        """The panel client for this user's own API key, or None if they have not set one."""
        key = self.store.api_key(user)
        if not key:
            return None
        cached = self._panels.get(user)
        if cached is None or cached[0] != key:
            cached = (key, self.panel_for(key))
            self._panels[user] = cached
        return cached[1]

    def _forget(self, user: int) -> None:
        self._panels.pop(user, None)
        self._services.pop(user, None)

    # -- settings -----------------------------------------------------------

    def _set(self, user: int, key: str, value: Any) -> None:
        self.store.set_setting(f"u{user}:{key}", value)

    def settings(self, user: int) -> Settings:
        def get(key: str, default: Any) -> str:
            return self.store.get_setting(f"u{user}:{key}", str(default))

        return Settings(
            views=int(get("views", self.config.default_views)),
            likes=int(get("likes", self.config.default_likes)),
            spread_hours=float(get("spread_hours", self.config.spread_hours)),
            splits=(int(get("splits_min", 10)), int(get("splits_max", 12))),
        )

    def _settings_text(self, user: int) -> str:
        s = self.settings(user)
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
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = (message.get("from") or {}).get("id")
        if chat_id is None or not text:
            return
        if chat.get("type") != "private" or chat_id != user:
            # API keys are typed into this bot, so it never works where other people can read along.
            self.tg.send(chat_id, "I only work in a private chat. Message me directly.")
            return
        if not self._authorised(user):
            self.tg.send(chat_id, f"You don't have access yet. Send this ID to the admin: {user}")
            return
        ctx = Ctx(user=user, message_id=message.get("message_id"), admin=user in self.config.owner_ids)
        try:
            reply = self._dispatch(text, ctx)
        except PanelError as exc:
            reply = f"Panel error: {exc}"
        if reply:
            self.tg.send(chat_id, reply)

    def _dispatch(self, text: str, ctx: Ctx) -> str:
        if text.startswith("/"):
            self._set(ctx.user, "awaiting_key", 0)
            parts = text.split()
            command = parts[0][1:].split("@")[0].lower()
            handler = getattr(self, f"_cmd_{command}", None) if command.isalpha() else None
            if handler is None:
                return "Unknown command. /help shows what I understand."
            return handler(parts[1:], ctx)
        if self.store.get_setting(f"u{ctx.user}:awaiting_key") == "1" and not LINK_RE.search(text):
            return self._save_key(text, ctx)
        return self._new_job(text, ctx.user)

    # -- API key setup ------------------------------------------------------

    def _cmd_setkey(self, args: list[str], ctx: Ctx) -> str:
        if args:
            return self._save_key(args[0], ctx)
        self._set(ctx.user, "awaiting_key", 1)
        return "Send me your panel API key now (BulkFollows → Account Settings). I will delete the message after reading it."

    def _save_key(self, key: str, ctx: Ctx) -> str:
        key = key.strip()
        deleted = ctx.message_id is not None and self.tg.delete(ctx.user, ctx.message_id)
        cleanup = "" if deleted else "\nI could not delete your message with the key — please delete it yourself."
        if not 8 <= len(key) <= 200 or len(key.split()) != 1:
            return "That does not look like an API key. Send /setkey and try again." + cleanup
        try:
            amount, currency = self.panel_for(key).balance()
        except PanelRejected as exc:
            return f"The panel did not accept that key ({exc}). Nothing was saved. Send /setkey to try again." + cleanup
        except PanelError as exc:
            return f"Could not reach the panel to check the key ({exc}). Nothing was saved. Send /setkey to try again." + cleanup
        self.store.set_api_key(ctx.user, key, self.clock())
        self._set(ctx.user, "awaiting_key", 0)
        self._forget(ctx.user)
        log.info("User %d set an API key", ctx.user)
        return f"Key saved. Your panel balance: {amount:.2f} {currency}".strip() + "\nNow send me an X post link." + cleanup

    # -- new job ------------------------------------------------------------

    def _service(self, user: int, panel: Panel, service_id: str) -> Optional[dict[str, Any]]:
        fetched_at, services = self._services.get(user, (0.0, []))
        if not services or self.clock() - fetched_at > self.SERVICES_CACHE_SECONDS:
            services = panel.services()
            self._services[user] = (self.clock(), services)
        return next((s for s in services if str(s.get("service")) == str(service_id)), None)

    def _new_job(self, text: str, user: int) -> str:
        panel = self._panel(user)
        if panel is None:
            return NO_KEY
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
        s = self.settings(user)
        views = numbers[0] if numbers else s.views
        likes = numbers[1] if len(numbers) == 2 else s.likes
        if views == 0 and likes == 0:
            return "Views and likes are both 0, so there is nothing to order."
        if views > self.config.max_views or likes > self.config.max_likes:
            return (
                f"That is above the safety limit ({self.config.max_views:,} views, {self.config.max_likes:,} likes "
                "per link). Nothing was ordered. Raise MAX_VIEWS / MAX_LIKES if you meant it."
            )
        running = self.store.active_job_for_link(link, user)
        if running:
            return f"Job #{running['id']} is still placing orders for this link. /cancel {running['id']} first, or wait."

        try:
            view_service = self._service(user, panel, self.config.view_service_id) if views else None
            like_service = self._service(user, panel, self.config.like_service_id) if likes else None
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

        job_id = self.store.create_job(link, views, likes, user, now, orders)
        log.info("Job %d created for user %d: views=%d likes=%d orders=%d", job_id, user, views, likes, len(orders))
        if cost_known:
            lines.append(f"Estimated cost: {cost:.4f} (panel currency)")
        lines.append(f"/status {job_id} · /cancel {job_id}")
        return f"Job #{job_id} started\n" + "\n".join(lines)

    # -- placing orders -----------------------------------------------------

    def process_due(self) -> None:
        for order in self.store.due_orders(self.clock()):
            job = self.store.job(order["job_id"])
            if job is None or job["status"] != "active":
                continue
            panel = self._panel(job["chat_id"])
            if panel is None:
                self.store.update_order(order["id"], status="failed", error="no API key set")
                self._finish_if_done(job["id"])
                continue
            self._place(order, job, panel)

    def _place(self, order: Any, job: Any, panel: Panel) -> None:
        order_id, job_id, kind, quantity = order["id"], job["id"], order["kind"], order["quantity"]
        attempts = order["attempts"] + 1
        # Marked before the call: if the process dies mid-request, recover() flags it instead of re-buying.
        self.store.update_order(order_id, status="placing", attempts=attempts)
        try:
            panel_order_id = panel.add(order["service_id"], job["link"], quantity)
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
        panel = self._panel(job["chat_id"])
        try:
            statuses = panel.statuses(panel_ids) if panel and panel_ids else {}
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

    def _cmd_start(self, args: list[str], ctx: Ctx) -> str:
        text = HELP + (ADMIN_HELP if ctx.admin else "")
        return text if self.store.api_key(ctx.user) else NO_KEY + "\n\n" + text

    _cmd_help = _cmd_start

    def _cmd_settings(self, args: list[str], ctx: Ctx) -> str:
        return self._settings_text(ctx.user)

    def _set_number(self, args: list[str], ctx: Ctx, key: str, maximum: int) -> str:
        if not args:
            return self._settings_text(ctx.user)
        found = NUMBER_RE.match(args[0])
        if not found:
            return f"Usage: /{key} 5000"
        value = int(found[1].replace(",", "")) * (1000 if found[2] else 1)
        if value > maximum:
            return f"{value:,} is above the safety limit of {maximum:,}."
        self._set(ctx.user, key, value)
        return f"Default {key} per link: {value:,}"

    def _cmd_views(self, args: list[str], ctx: Ctx) -> str:
        return self._set_number(args, ctx, "views", self.config.max_views)

    def _cmd_likes(self, args: list[str], ctx: Ctx) -> str:
        return self._set_number(args, ctx, "likes", self.config.max_likes)

    def _cmd_spread(self, args: list[str], ctx: Ctx) -> str:
        try:
            hours = float(args[0])
        except (IndexError, ValueError):
            return "Usage: /spread 12  (hours over which view orders are placed)"
        if not 0 <= hours <= 72:
            return "Spread must be between 0 and 72 hours."
        self._set(ctx.user, "spread_hours", hours)
        return f"View orders will be placed over {hours:g}h."

    def _cmd_splits(self, args: list[str], ctx: Ctx) -> str:
        try:
            low, high = int(args[0]), int(args[1])
        except (IndexError, ValueError):
            return "Usage: /splits 10 12  (minimum and maximum view orders per link)"
        if not 1 <= low <= high <= 50:
            return "Use two numbers between 1 and 50, smaller first."
        self._set(ctx.user, "splits_min", low)
        self._set(ctx.user, "splits_max", high)
        return f"Views will be split into {low}-{high} orders."

    def _cmd_balance(self, args: list[str], ctx: Ctx) -> str:
        panel = self._panel(ctx.user)
        if panel is None:
            return NO_KEY
        amount, currency = panel.balance()
        return f"Panel balance: {amount:.2f} {currency}".strip()

    def _own_job(self, args: list[str], ctx: Ctx, usage: str) -> tuple[Optional[Any], str]:
        """Users only ever see and control their own jobs."""
        if not args or not args[0].lstrip("#").isdigit():
            return None, usage
        job = self.store.job(int(args[0].lstrip("#")))
        if job is None or job["chat_id"] != ctx.user:
            return None, f"You have no job {args[0]}."
        return job, ""

    def _cmd_status(self, args: list[str], ctx: Ctx) -> str:
        if not args:
            jobs = self.store.recent_jobs(ctx.user)
            return "\n\n".join(self._job_summary(j) for j in jobs) if jobs else "No jobs yet. Send me a link."
        job, problem = self._own_job(args, ctx, "Usage: /status 3")
        return self._job_detail(job) if job else problem

    def _cmd_cancel(self, args: list[str], ctx: Ctx) -> str:
        job, problem = self._own_job(args, ctx, "Usage: /cancel 3")
        if not job:
            return problem
        cancelled = self.store.cancel_pending(job["id"])
        if job["status"] == "active":
            self.store.set_job_status(job["id"], "cancelled")
        return (
            f"Job #{job['id']}: {cancelled} unplaced order(s) cancelled. "
            "Orders already placed on the panel keep running."
        )

    def _cmd_retry(self, args: list[str], ctx: Ctx) -> str:
        job, problem = self._own_job(args, ctx, "Usage: /retry 3   (or /retry 3 all to include unclear ones)")
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

    # -- admin commands -----------------------------------------------------

    @staticmethod
    def _user_id_arg(args: list[str]) -> Optional[int]:
        return int(args[0]) if args and args[0].isdigit() else None

    def _cmd_adduser(self, args: list[str], ctx: Ctx) -> str:
        if not ctx.admin:
            return "Only the admin can do that."
        new_user = self._user_id_arg(args)
        if new_user is None:
            return "Usage: /adduser 123456789  (the ID I show your teammate when they message me)"
        members = self._member_ids()
        if new_user in members:
            return f"{new_user} already has access."
        if len(members) >= self.config.max_users:
            return f"The limit of {self.config.max_users} users is reached. /removeuser someone first."
        self.store.add_user(new_user, self.clock())
        self.tg.send(new_user, "You now have access. Send /start to set up your panel API key.")
        return f"{new_user} added ({len(members) + 1}/{self.config.max_users} users). They must send /setkey before ordering."

    def _cmd_removeuser(self, args: list[str], ctx: Ctx) -> str:
        if not ctx.admin:
            return "Only the admin can do that."
        target = self._user_id_arg(args)
        if target is None:
            return "Usage: /removeuser 123456789"
        if target in self.config.owner_ids:
            return "Admins are set in TELEGRAM_OWNER_ID and cannot be removed from here."
        if not self.store.has_user(target):
            return f"{target} is not a user."
        cancelled = self.store.remove_user(target)
        self.store.delete_settings(f"u{target}:")
        self._forget(target)
        return f"{target} removed, their API key deleted, {cancelled} queued order(s) cancelled."

    def _cmd_users(self, args: list[str], ctx: Ctx) -> str:
        if not ctx.admin:
            return "Only the admin can do that."
        members = sorted(self._member_ids())
        lines = [f"Users: {len(members)}/{self.config.max_users}"]
        for member in members:
            role = "admin" if member in self.config.owner_ids else "member"
            lines.append(f"{member} — {role}, {'key set' if self.store.api_key(member) else 'no key yet'}")
        return "\n".join(lines)

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
