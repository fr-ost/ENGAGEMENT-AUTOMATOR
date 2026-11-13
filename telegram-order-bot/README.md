# Telegram order bot

Send the bot an X post link; it orders **views** and **likes** for that post from your SMM panel
(BulkFollows by default, <https://bulkfollows.com/api>). Up to 10 people can use one bot, each
with their own panel account.

- **Views** are split into 10–12 uneven orders (e.g. 381, 523, 447 …) that add up exactly to the
  number you asked for. The first is placed immediately, the rest roughly evenly over the next
  12 hours, each at a slightly random time.
- **Likes** go as a single order, immediately.
- Every user enters **their own panel API key** in the chat; orders are paid from their own balance.
- Each user has their own defaults and only sees their own jobs.
- Only people the admin has added can use it, and only in a private chat with the bot.

## First-time setup for each user

1. Message the bot. If you are not added yet it replies with your Telegram ID — send that to the admin.
2. The admin runs `/adduser <that ID>`.
3. Send `/setkey`, then paste your API key (BulkFollows → Account Settings). The bot checks the
   key against the panel, shows your balance, and deletes your message containing the key.
4. Send a link.

## Using it

```
https://x.com/user/status/123            default views and likes
https://x.com/user/status/123 5000 100   5,000 views and 100 likes for this link
https://x.com/user/status/123 5000 0     views only
```

| Command | What it does |
| --- | --- |
| `/views 5000` | default views per link |
| `/likes 100` | default likes per link |
| `/spread 12` | hours over which the view orders are placed (`/spread 1` = all within an hour) |
| `/splits 10 12` | how many view orders per link (min max) |
| `/settings` | show the current settings |
| `/status` · `/status 3` | recent jobs · one job with each order's live panel status |
| `/cancel 3` | stop the orders of job 3 that have not been placed yet |
| `/retry 3` | re-queue orders the panel rejected (`/retry 3 all` also re-queues unclear ones) |
| `/balance` | your panel balance |
| `/setkey` | set or replace your panel API key |

Admin only:

| Command | What it does |
| --- | --- |
| `/adduser 123456789` | give a teammate access |
| `/removeuser 123456789` | remove access, delete their key, cancel their queued orders |
| `/users` | list who has access and whether they have set a key |

## Deploy on Railway

1. **Create the bot.** In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. **New Railway project** → *Deploy from GitHub repo* → pick this repository and the branch that
   contains this folder.
3. Service → **Settings → Root Directory**: `telegram-order-bot`. The start command
   (`python bot.py`) comes from `railway.json`.
4. Service → **Variables**: add

   | Variable | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | token from BotFather |
   | `TELEGRAM_OWNER_ID` | the admin's numeric Telegram user ID (see below); comma-separate several admins |
   | `SMM_VIEW_SERVICE_ID` | a "Default"-type views/impressions service ID |
   | `SMM_LIKE_SERVICE_ID` | a "Default"-type likes service ID |
   | `DATA_DIR` | `/data` |

5. **Add a volume** (right-click the service → *Attach volume*) mounted at `/data`.
   **Do not skip this.** Everyone's API key and the schedule of not-yet-placed view orders live in
   a SQLite file there; without a volume every redeploy or restart forgets them.
6. Keep the service at **one replica**. Two copies would fight over Telegram updates.

**Finding the admin's user ID:** deploy with any number in `TELEGRAM_OWNER_ID`, send the bot a
message, and it replies with your ID. Put that number in the variable. The admin then sets their
own key with `/setkey` like everyone else — there is no API key in the Railway variables.

**Service IDs are shared.** `SMM_VIEW_SERVICE_ID` and `SMM_LIKE_SERVICE_ID` apply to every user, so
all users need accounts on the same panel.

**Where keys are kept.** API keys are stored unencrypted in the SQLite file on the Railway volume.
Anyone with access to the Railway project can read them, so keep that project private.

Optional variables: `DEFAULT_VIEWS` (1000), `DEFAULT_LIKES` (30), `SPREAD_HOURS` (12),
`MAX_VIEWS` (100000) and `MAX_LIKES` (5000) as per-link typo guards, `MAX_USERS` (10), and
`SMM_API_URL` for another panel that speaks the same v2 API.

## What happens when something goes wrong

- **The panel rejects an order** (low balance, quantity outside the service limits): that order is
  marked failed, you get a message, and the rest of the job continues. `/retry <job>` re-queues it.
- **"You have active order with this link"**: many panels refuse a new order on a link while an
  earlier one for the same service is still running. The bot postpones that order by 10 minutes,
  up to 6 times, before giving up on it. If you see this often, choose a faster views service or
  a larger `/spread`.
- **No clear answer from the panel** (timeout, 5xx): the bot does **not** retry on its own,
  because the order may have gone through and a retry would pay twice. It tells you to check the
  panel; `/retry <job> all` re-queues it if it is missing.
- **Restart or redeploy mid-job**: pending orders resume from the volume. Orders that came due
  during the downtime are placed as soon as the bot is back.

How fast views are *delivered* after each order is placed depends on the panel service you pick.

## Run locally

```bash
cd telegram-order-bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                      # offline; needs no keys
set -a; source .env; set +a           # after copying .env.example to .env and filling it in
python bot.py
```
