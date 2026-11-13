import random

import pytest

from orderbot.app import App
from orderbot.config import ConfigError, load_config
from orderbot.panel import PanelRejected, PanelUncertain
from orderbot.store import Store

OWNER = 111
MEMBER = 222
OWNER_KEY = "owner-key-0001"
MEMBER_KEY = "member-key-0002"
LINK = "https://x.com/someone/status/1866000000000000001"
SERVICES = [
    {"service": 5, "name": "X views", "rate": "0.10", "min": "100", "max": "1000000"},
    {"service": 7, "name": "X likes", "rate": "2.00", "min": "10", "max": "5000"},
]


class FakePanel:
    def __init__(self, key="?"):
        self.key = key
        self.orders = []
        self.fail_with = None
        self.next_id = 9000

    def services(self):
        return SERVICES

    def balance(self):
        if self.key.startswith("bad"):
            raise PanelRejected("Invalid API key")
        return 12.5, "USD"

    def add(self, service_id, link, quantity):
        if self.fail_with:
            raise self.fail_with
        self.next_id += 1
        self.orders.append((service_id, link, quantity))
        return str(self.next_id)

    def statuses(self, ids):
        return {i: {"status": "In progress", "remains": "10"} for i in ids}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.to = []
        self.deleted = []

    def send(self, chat_id, text):
        self.sent.append(text)
        self.to.append(chat_id)

    def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def bot():
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "1:tok", "TELEGRAM_OWNER_ID": str(OWNER),
        "SMM_VIEW_SERVICE_ID": "5", "SMM_LIKE_SERVICE_ID": "7", "MAX_USERS": "3",
    })
    panels = {}

    def panel_for(key):
        return panels.setdefault(key, FakePanel(key))

    app = App(config, Store(":memory:"), panel_for, FakeTelegram(), rng=random.Random(7), clock=Clock())
    app.counter = 0
    app.panels = panels
    say(app, f"/setkey {OWNER_KEY}")
    app.panel = panels[OWNER_KEY]          # the owner's panel account
    return app


def update(app, text, user, chat_type="private", chat_id=None):
    app.counter += 1
    return {"update_id": app.counter, "message": {
        "message_id": 1000 + app.counter, "text": text,
        "chat": {"id": chat_id or user, "type": chat_type}, "from": {"id": user},
    }}


def say(app, text, user=OWNER):
    app.handle_update(update(app, text, user))
    return app.tg.sent[-1] if app.tg.sent else None


def views_placed(app):
    return [q for service, _, q in app.panel.orders if service == "5"]


def test_config_lists_missing_values():
    with pytest.raises(ConfigError) as excinfo:
        load_config({})
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value) and "SMM_VIEW_SERVICE_ID" in str(excinfo.value)


def test_strangers_cannot_order(bot):
    reply = say(bot, f"{LINK} 5000 100", user=999)
    assert "don't have access" in reply and "999" in reply
    assert bot.store.job(1) is None


def test_link_orders_likes_at_once_and_views_split_over_12_hours(bot):
    reply = say(bot, f"{LINK}?s=20&t=abc 5000 100")
    assert "Job #1 started" in reply and "5,000" in reply and "Estimated cost: 0.7000" in reply

    bot.process_due()
    assert ("7", LINK, 100) in bot.panel.orders          # likes: one shot, canonical link
    assert len(views_placed(bot)) == 1                    # only the first view split so far

    orders = bot.store.job_orders(1)
    view_orders = [o for o in orders if o["kind"] == "views"]
    assert 10 <= len(view_orders) <= 12
    assert sum(o["quantity"] for o in view_orders) == 5000
    assert max(o["due_at"] for o in view_orders) < bot.clock.now + 12 * 3600

    bot.clock.now += 6 * 3600
    bot.process_due()
    assert 1 < len(views_placed(bot)) < len(view_orders)

    bot.clock.now += 6 * 3600
    bot.process_due()
    assert sum(views_placed(bot)) == 5000
    assert len(bot.panel.orders) == len(view_orders) + 1
    assert bot.store.job(1)["status"] == "done"
    assert "All orders handled" in bot.tg.sent[-1]

    bot.process_due()                                      # nothing is ever placed twice
    assert len(bot.panel.orders) == len(view_orders) + 1


def test_defaults_and_settings_commands(bot):
    assert "5,000" in say(bot, "/views 5k")
    assert "200" in say(bot, "/likes 200")
    say(bot, "/spread 1")
    say(bot, "/splits 3 3")
    say(bot, LINK)
    orders = bot.store.job_orders(1)
    assert [o["quantity"] for o in orders if o["kind"] == "likes"] == [200]
    view_orders = [o for o in orders if o["kind"] == "views"]
    assert len(view_orders) == 3 and sum(o["quantity"] for o in view_orders) == 5000
    assert max(o["due_at"] for o in view_orders) < bot.clock.now + 3600


def test_views_only_and_bad_input(bot):
    assert "nothing to order" in say(bot, f"{LINK} 0 0")
    assert "Send an X post link" in say(bot, "hello")
    assert "safety limit" in say(bot, f"{LINK} 5000000")
    assert "below the service minimum" in say(bot, f"{LINK} 50 0")
    assert "outside the service limits" in say(bot, f"{LINK} 1000 5")
    assert bot.store.recent_jobs(OWNER) == []
    say(bot, f"{LINK} 1000 0")
    assert all(o["kind"] == "views" for o in bot.store.job_orders(1))


def test_same_link_twice_while_running_is_refused(bot):
    say(bot, f"{LINK} 5000 100")
    assert "still placing orders" in say(bot, f"https://twitter.com/someone/status/1866000000000000001 5000 100")
    assert len(bot.store.recent_jobs(OWNER)) == 1


def test_redelivered_update_does_not_order_twice(bot):
    again = update(bot, LINK, OWNER)
    bot.handle_update(again)
    bot.handle_update(again)
    assert len(bot.store.recent_jobs(OWNER)) == 1


def test_rejected_order_is_reported_and_can_be_retried(bot):
    bot.panel.fail_with = PanelRejected("Not enough funds on balance")
    say(bot, f"{LINK} 0 100")
    bot.process_due()
    assert bot.panel.orders == []
    assert any("rejected" in m and "Not enough funds" in m for m in bot.tg.sent)
    assert bot.store.job(1)["status"] == "done"

    bot.panel.fail_with = None
    assert "1 order(s) re-queued" in say(bot, "/retry 1")
    bot.process_due()
    assert bot.panel.orders == [("7", LINK, 100)]


def test_active_order_rejection_is_postponed_not_failed(bot):
    say(bot, f"{LINK} 0 100")
    bot.panel.fail_with = PanelRejected("You have active order with this link. Please wait until order being completed.")
    bot.process_due()
    order = bot.store.job_orders(1)[0]
    assert order["status"] == "pending" and order["due_at"] == bot.clock.now + 600
    bot.panel.fail_with = None
    bot.clock.now += 601
    bot.process_due()
    assert bot.panel.orders == [("7", LINK, 100)]


def test_unclear_outcome_is_not_retried_without_all(bot):
    bot.panel.fail_with = PanelUncertain("panel unreachable (Timeout)")
    say(bot, f"{LINK} 0 100")
    bot.process_due()
    assert bot.store.job_orders(1)[0]["status"] == "unknown"
    assert any("may or may not" in m for m in bot.tg.sent)
    assert "nothing to retry" in say(bot, "/retry 1")
    assert "re-queued" in say(bot, "/retry 1 all")


def test_restart_mid_order_is_flagged_not_replaced(bot):
    say(bot, f"{LINK} 0 100")
    order = bot.store.job_orders(1)[0]
    bot.store.update_order(order["id"], status="placing")
    bot.recover()
    bot.process_due()
    assert bot.panel.orders == []
    assert bot.store.job_orders(1)[0]["status"] == "unknown"
    assert any("restarted while placing" in m for m in bot.tg.sent)


def test_cancel_stops_remaining_splits(bot):
    say(bot, f"{LINK} 5000 100")
    bot.process_due()
    placed = len(bot.panel.orders)
    assert "cancelled" in say(bot, "/cancel 1")
    bot.clock.now += 24 * 3600
    bot.process_due()
    assert len(bot.panel.orders) == placed
    assert bot.store.job(1)["status"] == "cancelled"


def test_status_balance_help(bot):
    assert "No jobs yet" in say(bot, "/status")
    say(bot, f"{LINK} 5000 100")
    bot.process_due()
    assert "Likes: 100 of 100 ordered (1/1 orders)" in say(bot, "/status")
    detail = say(bot, "/status 1")
    assert "In progress" in detail and "due " in detail
    assert "12.50 USD" in say(bot, "/balance")
    assert "/spread" in say(bot, "/help")
    assert "Unknown command" in say(bot, "/nope")


# -- multi-user ---------------------------------------------------------------


def add_member(bot, user=MEMBER, key=MEMBER_KEY):
    say(bot, f"/adduser {user}")
    say(bot, f"/setkey {key}", user=user)
    return bot.panels[key]


def test_setup_asks_for_key_then_deletes_the_message(bot):
    say(bot, f"/adduser {MEMBER}")
    assert "connect your panel account" in say(bot, LINK, user=MEMBER)       # no key yet: nothing ordered
    assert "Send me your panel API key" in say(bot, "/setkey", user=MEMBER)
    bot.tg.deleted.clear()
    reply = say(bot, MEMBER_KEY, user=MEMBER)
    assert "Key saved" in reply and "12.50 USD" in reply and MEMBER_KEY not in reply
    assert bot.tg.deleted == [(MEMBER, 1000 + bot.counter)]
    assert bot.store.api_key(MEMBER) == MEMBER_KEY
    assert "Job #1 started" in say(bot, LINK, user=MEMBER)


def test_rejected_key_is_not_saved(bot):
    say(bot, f"/adduser {MEMBER}")
    assert "did not accept that key" in say(bot, "/setkey bad-key-12345", user=MEMBER)
    assert bot.store.api_key(MEMBER) is None
    assert "does not look like an API key" in say(bot, "/setkey abc", user=MEMBER)


def test_each_user_orders_with_their_own_key_and_settings(bot):
    member_panel = add_member(bot)
    say(bot, "/views 2000", user=MEMBER)
    say(bot, f"{LINK} 0 100")                    # owner: explicit
    say(bot, f"{LINK}", user=MEMBER)             # member: their defaults, same link is fine
    bot.process_due()
    assert bot.panel.orders == [("7", LINK, 100)]
    assert ("7", LINK, 30) in member_panel.orders
    member_views = [o for o in bot.store.job_orders(2) if o["kind"] == "views"]
    assert sum(o["quantity"] for o in member_views) == 2000
    assert bot.settings(OWNER).views == 1000


def test_users_cannot_see_or_control_each_others_jobs(bot):
    add_member(bot)
    say(bot, f"{LINK} 5000 100")
    assert "You have no job 1" in say(bot, "/status 1", user=MEMBER)
    assert "You have no job 1" in say(bot, "/cancel 1", user=MEMBER)
    assert "You have no job 1" in say(bot, "/retry 1", user=MEMBER)
    assert "No jobs yet" in say(bot, "/status", user=MEMBER)
    assert bot.store.job(1)["status"] == "active"


def test_notifications_go_to_the_job_owner(bot):
    member_panel = add_member(bot)
    member_panel.fail_with = PanelRejected("Not enough funds on balance")
    say(bot, f"{LINK} 0 100", user=MEMBER)
    bot.tg.to.clear()
    bot.process_due()
    assert bot.tg.to and set(bot.tg.to) == {MEMBER}


def test_only_admin_manages_users_and_limit_is_enforced(bot):
    add_member(bot)
    assert "Only the admin" in say(bot, "/adduser 333", user=MEMBER)
    assert "Only the admin" in say(bot, "/users", user=MEMBER)
    assert "333 added (3/3 users)" in say(bot, "/adduser 333")
    assert "limit of 3 users is reached" in say(bot, "/adduser 444")
    assert "already has access" in say(bot, "/adduser 333")
    listing = say(bot, "/users")
    assert "Users: 3/3" in listing and f"{OWNER} — admin, key set" in listing and "333 — member, no key yet" in listing
    assert MEMBER_KEY not in listing


def test_removing_a_user_deletes_key_and_cancels_their_queue(bot):
    member_panel = add_member(bot)
    say(bot, f"{LINK} 5000 100", user=MEMBER)
    bot.process_due()
    placed = len(member_panel.orders)
    reply = say(bot, f"/removeuser {MEMBER}")
    assert "removed" in reply and "API key deleted" in reply
    bot.clock.now += 24 * 3600
    bot.process_due()
    assert len(member_panel.orders) == placed
    assert bot.store.api_key(MEMBER) is None
    assert "don't have access" in say(bot, LINK, user=MEMBER)
    assert "cannot be removed" in say(bot, f"/removeuser {OWNER}")


def test_group_chats_are_refused(bot):
    bot.handle_update(update(bot, f"/setkey {OWNER_KEY}", OWNER, chat_type="group", chat_id=-500))
    assert "private chat" in bot.tg.sent[-1]
    bot.handle_update(update(bot, LINK, OWNER, chat_type="group", chat_id=-500))
    assert bot.store.recent_jobs(OWNER) == []
