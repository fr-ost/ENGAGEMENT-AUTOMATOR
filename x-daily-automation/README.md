# X Daily Automation

A small Python bot that runs once a day on **GitHub Actions** at **11:00 PM Bangladesh time (17:00 UTC)**. It:

1. finds your new X posts with the **official X API v2** (no scraping),
2. deduplicates them by post ID and saves them to JSON files in this repo,
3. creates a **promotion-planning record** for each new post (default target: 1,000 impressions and 30 likes),
4. tracks progress using X's own public metrics,
5. optionally talks to an **SMM panel** (default: [BulkFollows](https://bulkfollows.com/api)): reads your balance and service metadata, and, if you turn on `SMM_AUTO_ORDER`, places an impressions order and a likes order for each new post and tracks their status,
6. writes a daily Markdown report to `reports/` and optionally sends it to **Telegram**.

> **Spending money is opt-in.** With the defaults the bot places no orders; targets are planning goals only. Orders are placed only when both `SMM_ENABLED=true` and `SMM_AUTO_ORDER=true`. See [step 5](#5-configure-the-smm-panel-optional).

---

## Contents

- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [1. Create or use a GitHub repository](#1-create-or-use-a-github-repository)
- [2. Set up Python on macOS](#2-set-up-python-on-macos)
- [3. Get X API credentials](#3-get-x-api-credentials)
- [4. Add GitHub Actions secrets and variables](#4-add-github-actions-secrets-and-variables)
- [5. Configure the SMM panel (optional)](#5-configure-the-smm-panel-optional)
- [6. Enable GitHub Actions](#6-enable-github-actions)
- [7. Run the workflow manually](#7-run-the-workflow-manually)
- [8. Verify the first report](#8-verify-the-first-report)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Recovering from failed scans](#10-recovering-from-failed-scans)
- [11. Disabling the automation](#11-disabling-the-automation)
- [12. API and infrastructure costs](#12-api-and-infrastructure-costs)
- [Telegram setup](#telegram-setup)
- [Configuration reference](#configuration-reference)

---

## How it works

```
GitHub Actions (17:00 UTC daily or manual)
  └─ pytest (offline, mocked)        ← the bot does not run if tests fail
  └─ python main.py
       1. load + validate config     (exit 2 on bad config)
       2. lock + load JSON state     (exit 3 if corrupt or locked; nothing is reset)
       3. X API: user timeline since (last checkpoint − 2h overlap)
       4. dedupe by post ID → save new posts + planning records
       5. refresh public metrics for posts from the last 7 days
       6. SMM panel checks + auto-orders (optional; failures are only warnings)
       7. save state; the checkpoint is written LAST and only after a complete scan
       8. write reports/YYYY-MM-DD.md, then send Telegram (optional; failures never fail the run)
  └─ commit data/ + reports/ (only if changed; rebase on conflict, never force-push)
```

**Checkpoint safety.** `data/state.json` → `last_successful_scan_at` moves forward only when the whole scan succeeded **and** the posts/plans were written to disk. If the scan fails, is cut short, or saving fails, the previous checkpoint stays and the next run covers the gap. Because every scan overlaps the previous one by `SCAN_OVERLAP_HOURS`, posts that X indexes late are still found, and the duplicates this causes are dropped by post ID. Running the bot twice in a row is therefore harmless.

**Overlapping runs.** The workflow uses a `concurrency` group, so only one run happens at a time. Local runs also take a lock file, `data/.run.lock`.

## Project layout

```
.github/workflows/daily.yml      ← at the REPO ROOT (GitHub only reads workflows there)
x-daily-automation/
├── main.py                      orchestrates one daily run
├── src/
│   ├── config.py                env config, validation, secret-redacting logs, .env loader
│   ├── x_client.py              X API v2 client (retries, timeouts, rate limits)
│   ├── scanner.py               scan window, filtering (replies/reposts/quotes), canonical URLs
│   ├── deduplication.py         dedupe by immutable post ID
│   ├── storage.py               atomic JSON writes, validation, lock, recovery
│   ├── campaign_manager.py      planning records, metrics progress, read-only campaign status
│   ├── smm_client.py            SMM panel API v2 client (balance/services/status/add)
│   ├── reporter.py              Markdown report + Telegram text
│   └── telegram.py              Telegram sender (splitting, retries)
├── tests/                       pytest suite; all network calls mocked
├── data/                        state.json, posts.json, campaigns.json (committed by the bot)
├── reports/                     YYYY-MM-DD.md daily reports (committed by the bot)
├── requirements.txt             runtime: requests
├── requirements-dev.txt         tests: pytest, responses
└── .env.example                 template for local runs
```

---

## 1. Create or use a GitHub repository

**Option A: use this repository** (`fr-ost/ENG-AUTOMATOR`). The workflow is already at `.github/workflows/daily.yml`. Merge this branch into your default branch (`main`). Scheduled workflows only run from the default branch.

```bash
git clone https://github.com/fr-ost/ENG-AUTOMATOR.git
cd ENG-AUTOMATOR
git checkout main
git pull origin main
```

**Option B: a new repository.** Create an empty repo on github.com (a **private** repo is recommended, because the bot commits your post data). Then copy both `x-daily-automation/` and `.github/` into it:

```bash
mkdir my-x-bot && cd my-x-bot
git init -b main
cp -R /path/to/ENG-AUTOMATOR/x-daily-automation .
mkdir -p .github/workflows
cp /path/to/ENG-AUTOMATOR/.github/workflows/daily.yml .github/workflows/
git add .
git commit -m "Add X daily automation"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

> Private repos use your monthly GitHub Actions minutes. See [costs](#12-api-and-infrastructure-costs).

## 2. Set up Python on macOS

```bash
# Install Homebrew first if you don't have it: https://brew.sh
brew install python@3.12
cd ENG-AUTOMATOR/x-daily-automation
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest            # should print "... passed"; needs no API keys
cp .env.example .env        # then edit .env and fill in your values
```

Try a **dry run** first. It calls the real X API but saves nothing and sends nothing:

```bash
DRY_RUN=true python main.py
```

`.env` is listed in `.gitignore`. Never commit it.

## 3. Get X API credentials

1. Sign in at the X Developer Platform (<https://developer.x.com> → Developer Portal / Console) with the account whose posts you want to track, or with any account that can read them.
2. Create a **Project** and an **App** inside it.
3. Under **Keys and tokens**, generate the **Bearer Token** (app-only authentication). That is your `X_BEARER_TOKEN`.
4. Make sure your plan includes the **user posts timeline** endpoint (`GET /2/users/:id/tweets`) and user lookup. Check the current access tiers and pricing on the developer site. They have changed several times. **An X Premium subscription does not include X API access**, and neither does any other X or Copilot subscription. API access is a separate developer product.
5. `X_USERNAME` is your handle without `@`. `X_USER_ID` is optional. If you leave it out, the bot looks the ID up once and caches it in `data/state.json`.

The bot uses only read endpoints. It never posts, likes, or follows.

## 4. Add GitHub Actions secrets and variables

On GitHub, open the repo → **Settings → Secrets and variables → Actions**.

**Secrets** tab → **New repository secret**:

| Name | Required | Value |
| --- | --- | --- |
| `X_BEARER_TOKEN` | yes | your X Bearer Token |
| `SMM_API_KEY` | only if the SMM panel is enabled | API key from your BulkFollows Account Settings |
| `TELEGRAM_BOT_TOKEN` | only if Telegram is enabled | from @BotFather |
| `TELEGRAM_CHAT_ID` | only if Telegram is enabled | your chat ID |

**Variables** tab → **New repository variable** (these values are not secret):

| Name | Example | Notes |
| --- | --- | --- |
| `X_USERNAME` | `yourhandle` | required unless `X_USER_ID` is set |
| `X_USER_ID` | | optional |
| `INCLUDE_QUOTE_POSTS` | `true` | set `false` to ignore quote posts |
| `IMPRESSION_TARGET` / `LIKE_TARGET` | `1000` / `30` | planning targets |
| `TELEGRAM_ENABLED` | `true` | optional |
| `SMM_ENABLED`, `SMM_AUTO_ORDER`, `SMM_*_SERVICE_ID` | | see step 5 |

Every option is listed in the [Configuration reference](#configuration-reference). GitHub masks secret values in logs, and the bot also strips its known secrets from everything it logs.

## 5. Configure the SMM panel (optional)

The panel integration is **off by default**, and the bot works fully without it. The default provider is **BulkFollows**; the client follows its documented API (<https://bulkfollows.com/api>): `POST https://bulkfollows.com/api/v2` with form fields `key` and `action` (`balance`, `services`, `status`, `add`).

1. Get your API key from BulkFollows → **Account Settings** and add it as the secret `SMM_API_KEY`.
2. Set the variable `SMM_ENABLED=true`. On its own this only **reads**: balance, service metadata, and order status.
3. Pick the services you want from the BulkFollows service list and set `SMM_IMPRESSION_SERVICE_ID` and/or `SMM_LIKE_SERVICE_ID`. Use "Default"-type services (they take a link and a quantity). The report shows each service's name, rate, min and max so you can confirm you picked the right ones.
4. To place orders automatically, set `SMM_AUTO_ORDER=true`. **This spends your panel balance.** For every newly discovered post the bot orders `IMPRESSION_TARGET` impressions and `LIKE_TARGET` likes (whichever service IDs are set). Make sure the targets are within each service's min/max, or the panel rejects the order.

Safety properties of auto-ordering:

- One order per type per post. The order ID is saved in `data/campaigns.json`, and reruns never re-order a post that already has one.
- Dry runs never place orders.
- A failed order (low balance, bad service ID, quantity out of range) is recorded as an error on that post and never fails the scan. It is **not retried automatically** on later runs.
- To turn it off, set `SMM_AUTO_ORDER=false` (or delete the variable).

**Using another panel.** Any panel that speaks the same v2 protocol works: set `SMM_API_URL` to its endpoint. The provider name stored on each order comes from the URL's domain (`bulkfollows.com` → `bulkfollows`), and status checks only query orders placed on the currently configured panel, so orders from a previous panel are left alone.

### Linking an authorized campaign

Planning targets are kept separate from campaigns. To have the bot report on a promotion **you created and paid for yourself**, edit `data/campaigns.json` and add an entry to that post's `authorized_campaigns` list:

```json
"authorized_campaigns": [
  {"provider": "bulkfollows", "reference": "<your order ID>", "authorized_by": "<your name>", "authorized_at": "2026-10-26T12:00:00Z"}
]
```

Each run then fetches the read-only status and the provider-reported charge, which appears in the report as "verified campaign spend". `provider: "x_ads"` is reserved for the official X Ads API. `XAdsStatusProvider` in `src/campaign_manager.py` is a documented placeholder, because the Ads API needs a separately approved Ads API application and ads-account credentials. Until it is implemented, `x_ads` references are listed but not checked.

## 6. Enable GitHub Actions

1. Repo → **Actions** tab. If you see a prompt to enable workflows, click **I understand my workflows, go ahead and enable them**.
2. Repo → **Settings → Actions → General**:
   - *Actions permissions*: allow actions (at least those created by GitHub, which covers `actions/checkout` and `actions/setup-python`).
   - *Workflow permissions*: the workflow requests `contents: write` itself. If your organization forces read-only tokens, select **Read and write permissions** so the bot can commit `data/` and `reports/`.
3. Make sure the workflow file is on the **default branch**. Schedules only fire from there.

**About timing:** GitHub runs scheduled workflows on a best-effort basis. Runs often start a few minutes, and sometimes 30+ minutes, after 17:00 UTC. During heavy load a run can occasionally be skipped. On public repos, GitHub automatically disables scheduled workflows after 60 days without repository activity, and the bot's own commits count as activity. The overlapping scan window and the saved checkpoint mean a late or skipped run never loses posts. The next run picks them up.

## 7. Run the workflow manually

Repo → **Actions** → **X daily automation** → **Run workflow** → choose the branch → **Run workflow**.

Optional inputs:

- **dry_run**: scans and prints the report in the log; commits and sends nothing.
- **scan_since**: rescans from a UTC time, e.g. `2026-10-01T00:00:00Z` (see recovery).
- **allow_state_reinit**: recovery for corrupt state files (see recovery).

## 8. Verify the first report

After a successful run:

1. The run shows green checkmarks. In the **Run daily scan** step you'll see `Done: scan=SUCCESS new=N ...`.
2. A commit `chore(bot): daily state and report YYYY-MM-DD` appears on your default branch.
3. Open `x-daily-automation/reports/YYYY-MM-DD.md` (the date is Bangladesh time). Check that:
   - **Scan status** is `SUCCESS` and **Checkpoint advanced** is `yes`.
   - the post links open your real posts.
   - the first report includes the warning *"First run: … scanned the last 24h only"*. This is expected.
4. Open `data/posts.json` and `data/campaigns.json` to see the records.
5. If Telegram is enabled, you receive the summary, and the report shows `Telegram | sent (1 message)`.

Run it a second time. You should see **New posts this run: 0** and duplicates skipped, which shows that reruns are safe.

## 9. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Invalid configuration: X_BEARER_TOKEN is required` (exit 2) | The secret is missing or misspelled. Names are case-sensitive. |
| `X API rejected the credentials … (HTTP 401)` | The Bearer Token is wrong or was regenerated. Update the secret. |
| `… (HTTP 403)` | Your X API plan or app doesn't include this endpoint, or the app is suspended. Check the developer portal. |
| `X API rate limit hit … reset in ~Ns` | Too many requests on your tier. The bot waits up to `MAX_RATE_LIMIT_WAIT_SECONDS` (120s), then fails safely. Re-run later. |
| `X user @… was not found` | Check `X_USERNAME` (no `@` needed). |
| `Scan stopped after X_MAX_PAGES=5 pages` (INCOMPLETE) | You posted more than about 500 posts in the window. Raise the `X_MAX_PAGES` variable and re-run. |
| `State is invalid; refusing to continue` (exit 3) | A JSON file was hand-edited incorrectly. See [recovery](#10-recovering-from-failed-scans). |
| `Another run appears to be in progress` (exit 3) | A local run crashed and left `data/.run.lock`. Delete it if no run is active. It is never committed. |
| `SMM panel balance unavailable: …` | A wrong `SMM_API_KEY`, or a wrong `SMM_API_URL` if you set one. This is a warning only. |
| `Auto-order failed (…)` in `data/campaigns.json` | The panel rejected the order: low balance, wrong service ID, or quantity outside the service's min/max. |
| `Telegram | FAILED: …` | Check the bot token and chat ID, and send your bot a message first. This never fails the scan. |
| Commit step: `Conflict between this run's state and newer commits` | Someone edited `data/` at the same moment. Nothing was force-pushed, and the next run recovers automatically. |
| Workflow never runs on schedule | It isn't on the default branch, Actions is disabled, or the repo has been inactive for 60+ days. Re-enable it in the Actions tab. |

For more detail, set the variable `LOG_LEVEL=DEBUG`.

## 10. Recovering from failed scans

- **A scan failed (network, rate limit, auth):** fix the cause and re-run. You don't need to do anything else, because the checkpoint was not moved and the next run rescans from it.
- **Posts were missed or the bot was off for a while:** run the workflow manually with **scan_since** set to a UTC time before the gap. Known posts are skipped by ID, so nothing is duplicated.
- **A corrupt or hand-edited state file:** prefer restoring the last good version from git:
  ```bash
  git log --oneline -- x-daily-automation/data/
  git checkout <good-commit> -- x-daily-automation/data/posts.json
  git commit -m "Restore bot state" && git push
  ```
  As a last resort, run the workflow with **allow_state_reinit** checked. The bad file moves to `data/quarantine/` and that file alone starts fresh. Valid files and the checkpoint are kept, and the report lists what was reinitialized. The bot never resets history silently.

## 11. Disabling the automation

- **Pause:** repo → **Actions** → **X daily automation** → **⋯** → **Disable workflow**. Enable it again from the same menu.
- **Stop permanently:** delete `.github/workflows/daily.yml` and commit.
- **Revoke access:** regenerate or delete the Bearer Token in the X developer portal, and delete the repository secrets.

## 12. API and infrastructure costs

| Item | Cost |
| --- | --- |
| GitHub Actions | Public repos: free for standard runners. Private repos: covered by your plan's included minutes; GitHub Free currently includes 2,000 minutes per month. Each run takes about 1 minute, so roughly 30 minutes per month. |
| Storage | JSON and Markdown in git: a few KB per day. |
| Telegram | Free. |
| X API | **Depends on your developer plan. Check it yourself.** X's tiers and prices have changed repeatedly, and this project does not assume any reads are free. |
| SMM panel | Free to read (balance, services, status). With `SMM_AUTO_ORDER=true`, each new post costs the panel's rate for the configured impressions and likes. |

**X API usage per day** (typical):

- 1 timeline request (plus 1 more per extra 100 posts)
- 1 metrics lookup request for posts from the last `METRICS_LOOKBACK_DAYS` (up to 100 posts per request; set to `0` to disable)
- 1 user lookup, on the first run only

That is about **2 requests per day, around 60 per month**. Posts read per day are roughly: new posts, plus posts in the 2-hour overlap, plus the recent posts whose metrics are refreshed. Every report lists the exact request and post-read counts. To have reports estimate a dollar cost, set `X_COST_PER_REQUEST_USD` and/or `X_COST_PER_POST_READ_USD` using prices **you have confirmed for your plan**. Otherwise the report says the cost is unknown.

---

## Telegram setup

1. In Telegram, message **@BotFather** → `/newbot` → copy the token (`TELEGRAM_BOT_TOKEN`).
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `chat.id` (`TELEGRAM_CHAT_ID`). Don't share this URL, because it contains your token.
4. Add both as secrets and set the variable `TELEGRAM_ENABLED=true`.

Long reports are split into several messages under Telegram's 4,096-character limit. Timeouts and server errors are retried a bounded number of times.

## Configuration reference

| Variable | Default | Description |
| --- | --- | --- |
| `X_BEARER_TOKEN` | — | **Secret.** X API v2 app-only Bearer Token. |
| `X_USERNAME` | — | Handle (without `@`). |
| `X_USER_ID` | — | Numeric ID; skips the lookup. |
| `INCLUDE_QUOTE_POSTS` | `true` | Include quote posts. Replies and reposts are always excluded. |
| `X_PAGE_SIZE` | `100` | Posts per page (5–100). |
| `X_MAX_PAGES` | `5` | Page cap per scan; hitting it marks the scan INCOMPLETE. |
| `SCAN_OVERLAP_HOURS` | `2` | Overlap with the previous checkpoint. |
| `INITIAL_LOOKBACK_HOURS` | `24` | First-run window. |
| `METRICS_LOOKBACK_DAYS` | `7` | Refresh metrics for posts this recent (`0` = off). |
| `SCAN_SINCE` | — | Recovery: scan from this UTC ISO time. |
| `ALLOW_STATE_REINIT` | `false` | Recovery: quarantine invalid state files. |
| `DRY_RUN` | `false` | Save nothing, send nothing, print the report. |
| `IMPRESSION_TARGET` / `LIKE_TARGET` | `1000` / `30` | Planning targets per post. |
| `PROMOTION_BUDGET` / `PROMOTION_BUDGET_CURRENCY` | — / `USD` | Optional planning budget recorded per post. |
| `SMM_ENABLED` | `false` | Enable the SMM panel (balance, services, order status). |
| `SMM_AUTO_ORDER` | `false` | Place orders for each new post. **Spends money.** |
| `SMM_API_URL` | `https://bulkfollows.com/api/v2` | Set only to use another v2-compatible panel. |
| `SMM_API_KEY` | — | **Secret.** |
| `SMM_IMPRESSION_SERVICE_ID` / `SMM_LIKE_SERVICE_ID` | — | Panel service IDs used for metadata and auto-orders. |
| `TELEGRAM_ENABLED` | `false` | Send the daily summary. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | **Secrets.** |
| `X_COST_PER_REQUEST_USD` / `X_COST_PER_POST_READ_USD` | — | Your verified prices, for cost estimates. |
| `HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout. |
| `HTTP_MAX_RETRIES` | `3` | Retries for timeouts and 5xx errors. |
| `MAX_RATE_LIMIT_WAIT_SECONDS` | `120` | Longest wait for an X rate-limit reset before failing. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

### State files

- `data/state.json`: `account` (user ID and username cache), `last_successful_scan_at` (the checkpoint), `last_scan_window_start`, `last_run`, and `run_history` (the last 60 runs).
- `data/posts.json`: `posts[<post_id>]` = `post_id`, `post_url`, `text`, `kind`, `created_at`, `discovered_at`, `status` (`DISCOVERED` → `PLANNED`), `public_metrics`, `metrics_updated_at`.
- `data/campaigns.json`: `plans[<post_id>]` = `targets`, `budget`, `status`, `promotion_status` (`NOT_STARTED` / `CAMPAIGN_LINKED` / `TARGETS_REACHED`), `authorized_campaigns`, `performance`, `errors`, `created_at`, `updated_at`.

### Exit codes

`0` success · `1` scan failed or incomplete, or saving failed (the checkpoint is preserved) · `2` configuration error · `3` invalid state or lock held
