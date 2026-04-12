# crypto_monitor

Local-first crypto market analysis and alerting.

`crypto_monitor` watches a small set of Binance pairs, scores price
action against a configurable rubric, persists the results to a SQLite
database, and pushes notifications through [ntfy](https://ntfy.sh)
when something interesting happens. It can run in two modes:

- **Local (Windows)** — scheduled tasks on your own PC
- **Cloud (GitHub Actions)** — scheduled workflows on GitHub's free
  runners, with encrypted state stored in a dedicated git branch

There is no server, no paid cloud account, no telemetry. The CLI is
the single entry point for both modes — every command in this file is
invoked the same way:

```
python -m crypto_monitor.cli <command> [args]
```

## Contents
- [Requirements](#requirements)
- [First-time setup (Windows)](#first-time-setup-windows)
- [Configuration](#configuration)
  - [config.toml](#configtoml)
  - [.env (secrets)](#env-secrets)
  - [ntfy setup](#ntfy-setup)
- [CLI reference](#cli-reference)
- [Windows Task Scheduler setup](#windows-task-scheduler-setup)
- [GitHub Actions deployment](#github-actions-deployment)
- [File and directory layout](#file-and-directory-layout)
- [Troubleshooting](#troubleshooting)

---

## Requirements

- **Windows 10 or 11** (the scheduler scripts use `schtasks.exe`)
- **Python 3.12 or newer** on `PATH`, or installed in a project venv
- An **ntfy topic** — either on the public `https://ntfy.sh` server
  or on a self-hosted instance
- Outbound HTTPS to `api.binance.com` and your ntfy server

No database server, no Docker, no admin rights are required.

---

## First-time setup (Windows)

All commands below assume you are at the project root in a regular
(non-elevated) terminal.

### 1. Clone or unpack the project

Place the project anywhere you can write to, e.g.
`C:\Users\<you>\crypto_monitor`. Avoid paths that contain spaces if
you can — Task Scheduler is happier without them.

### 2. Create a virtual environment (recommended)

```
python -m venv .venv
.venv\Scripts\activate
```

The wrapper scripts in `scripts\` automatically pick up
`.venv\Scripts\python.exe` if it exists, so you do not have to
re-activate the venv before each scheduled run.

### 3. Install runtime dependencies

```
pip install -r requirements.txt
```

### 4. Initialize the project

```
python -m crypto_monitor.cli init
```

This single command:

1. Copies `config.example.toml` to `config.toml` (only if `config.toml`
   does not already exist — your edits are never overwritten).
2. Creates the SQLite database at the path declared in
   `[general].db_path` (default: `data\crypto_monitor.db`).
3. Seeds the tracked symbols listed in `[symbols].tracked` into the
   `symbols` table.

If you want to skip the seed step, pass `--no-seed`:

```
python -m crypto_monitor.cli init --no-seed
```

### 5. Configure your ntfy topic

Copy `.env.example` to `.env` and set a unique, hard-to-guess topic
name (see [ntfy setup](#ntfy-setup)).

```
copy .env.example .env
notepad .env
```

### 6. Send a test notification

```
python -m crypto_monitor.cli ntfy-test
```

You should see `sent ok status=200` in the terminal and the test
message on your phone within a few seconds. If not, jump to
[Troubleshooting](#troubleshooting).

### 7. Run a manual scan

```
python -m crypto_monitor.cli scan
```

This is the same command Task Scheduler will run every 5 minutes.
On the first run it will pull historical candles for every tracked
symbol — expect it to take longer than subsequent runs.

### 8. (Optional) Register the scheduled tasks

```
scripts\register_tasks.cmd
```

See [Windows Task Scheduler setup](#windows-task-scheduler-setup)
for what this creates and how to verify it.

---

## Configuration

`crypto_monitor` has two configuration files:

| File | Purpose | Tracked in git? |
|---|---|---|
| `config.toml`     | Tunable runtime parameters         | No (gitignored) |
| `.env`            | Secrets (currently just the ntfy topic) | No (gitignored) |
| `config.example.toml` | Defaults shipped with the project | Yes |
| `.env.example`    | Template for `.env`                | Yes |

### config.toml

Read [`config.example.toml`](config.example.toml) for the full,
heavily-commented schema. The fields you are most likely to edit:

- **`[general].timezone`** — IANA name (e.g. `"America/Sao_Paulo"`).
  Used only for quiet-hours decisions; all stored timestamps stay
  in UTC.
- **`[general].db_path`** / **`[general].log_dir`** — relative to the
  project root. The default `data\` and `logs\` are gitignored.
- **`[symbols].tracked`** — the Binance pairs you want to watch.
  Re-running `init` (or letting `auto_seed = true` do it on the next
  scan) seeds new entries; symbols are never silently removed.
- **`[scoring.weights]`** and **`[scoring.thresholds]`** — the rubric
  itself. Weights must sum to 100 (validated at load time).
- **`[alerts]`** — cooldown, escalation jump, and quiet-hours window
  in **local** time.
- **`[retention]`** — per-interval candle caps and the
  `vacuum_on_maintenance` toggle.

Most settings only take effect on the next CLI invocation; nothing is
hot-reloaded.

### .env (secrets)

Only one variable is read today:

```
NTFY_TOPIC=your-unique-topic-here
```

The topic is **deliberately** validated at send time, not at
config-load time, so `init` and the scheduler scripts succeed even
before you've picked one. The first attempted notification is where
a missing topic surfaces — see
[Troubleshooting → missing NTFY_TOPIC](#missing-ntfy_topic).

`python-dotenv` loads the file automatically; you don't have to
`set` anything in your shell.

### ntfy setup

If you don't already have an ntfy topic:

1. Pick a long, hard-to-guess topic name. Anyone who knows the
   topic can read your notifications, so treat it like a password.
   Example: `cm-7f3c1a2e9b4d-alerts`.
2. Install the **ntfy** app on your phone (Android, iOS) or use the
   web UI at `https://ntfy.sh`.
3. Subscribe to the topic in the app.
4. Put the topic in your `.env`:
   ```
   NTFY_TOPIC=cm-7f3c1a2e9b4d-alerts
   ```
5. (Optional) Point `[ntfy].server_url` in `config.toml` at your
   self-hosted ntfy server. The default is the public `ntfy.sh`.
6. Verify with `python -m crypto_monitor.cli ntfy-test`.

---

## CLI reference

Every command accepts a global `--project-root <path>` flag (default:
the current working directory). Exit code is `0` on success, `1` on
runtime error, `2` on argparse usage error.

| Command | What it does |
|---|---|
| `init [--no-seed]` | Copy `config.example.toml` → `config.toml` if missing, run schema migrations, optionally seed tracked symbols. Idempotent. |
| `scan` | One scan cycle: flush queued notifications, ingest fresh Binance candles, score every active symbol, persist new signals (with dedup), dispatch pending alerts. |
| `weekly` | Generate the weekly summary, persist it in `weekly_summaries`, push it via ntfy. |
| `evaluate` | Maintenance pass: evaluate matured signals and buys, prune old candles, optionally `VACUUM`. |
| `buy add` | Record a manual buy. See args below. |
| `buy list` | Print the recorded buys (newest last). `--symbol`, `--limit`. |
| `signals list` | Print recent signals (newest first). `--symbol`, `--severity`, `--limit`. |
| `ntfy-test` | Send a one-shot test notification. `--title`, `--body`. |

`buy add` arguments:

```
python -m crypto_monitor.cli buy add ^
    --symbol BTCUSDT ^
    --price 64500.5 ^
    --amount 100 ^
    [--quote-currency USDT] ^
    [--bought-at 2026-04-10T14:30:00Z] ^
    [--quantity 0.0015503] ^
    [--signal-id 42] ^
    [--note "first nibble"]
```

`--quantity` is optional; when omitted, the CLI derives it as
`amount / price`. Pass it explicitly when you want the ledger to
match an exact Binance fill.

---

## Windows Task Scheduler setup

`crypto_monitor` runs as three scheduled tasks. The defaults match
what most people want; tweak the schedules in
`scripts\register_tasks.cmd` if you need something different.

| Task name | Default schedule | What it runs |
|---|---|---|
| `crypto_monitor scan`        | every 5 minutes      | `python -m crypto_monitor.cli scan` |
| `crypto_monitor maintenance` | daily at 03:00 local | `python -m crypto_monitor.cli evaluate` |
| `crypto_monitor weekly`      | Sundays at 09:00 local | `python -m crypto_monitor.cli weekly` |

### Register the tasks

From the project root:

```
scripts\register_tasks.cmd
```

This calls `schtasks /Create /F` for each task with `/RL LIMITED`,
which means:

- The tasks run **as the current user**.
- **No admin rights required** to register or run them.
- Re-running the script overwrites the existing tasks (`/F`), so it
  is safe to run multiple times.

### Verify

```
schtasks /Query /TN "crypto_monitor scan"
schtasks /Query /TN "crypto_monitor maintenance"
schtasks /Query /TN "crypto_monitor weekly"
```

You should see each task with `Ready` status. Open
**Task Scheduler → Task Scheduler Library** in the GUI to see the
next run time and recent history.

### Test a task by hand

You can fire any task immediately without waiting for its schedule:

```
schtasks /Run /TN "crypto_monitor scan"
```

Then read `logs\scan.cmd.log` to see what the wrapper captured.

### Unregister

```
scripts\unregister_tasks.cmd
```

Safe to run even if some tasks don't exist.

### What the wrapper scripts do

The three wrappers in `scripts\` (`scan.cmd`, `weekly.cmd`,
`maintenance.cmd`) are tiny shims that:

1. `cd` into the project root (one level up from the script).
2. Use `.venv\Scripts\python.exe` if present, otherwise the system
   `python` on `PATH`.
3. Run the matching CLI command.
4. Append stdout and stderr to `logs\<task>.cmd.log`.

If you move the project to a new path, you do **not** need to edit
the wrappers — they always resolve their own folder via `%~dp0`.
Just re-run `register_tasks.cmd` so Task Scheduler picks up the new
absolute path.

---

## GitHub Actions deployment

`crypto_monitor` can run entirely on GitHub Actions free runners.
No credit card, no cloud VM, no always-on PC. The database is
encrypted and stored in a dedicated `state` branch — safe for
public repositories.

### How it works

Each workflow run:

1. Checks out the code (`main`) and the encrypted DB (`state` branch)
2. Decrypts the database using `STATE_ENCRYPTION_KEY`
3. Runs the CLI command (scan, evaluate, weekly, buy add)
4. Checkpoints WAL, re-encrypts, verifies round-trip integrity
5. Pushes the updated encrypted DB back to the `state` branch

The `state` branch is kept at exactly one commit (`--amend` +
`--force-with-lease`) so the repo never accumulates binary history.

A shared concurrency group (`crypto-state`) ensures only one
workflow touches the database at a time.

### Requirements

- A GitHub repository (public repos get unlimited free Actions
  minutes; private repos get 2,000 min/month)
- An ntfy topic (same as the local setup)
- Two repository secrets (see below)

### One-time setup

**1. Push the code to GitHub**

```bash
git remote add origin https://github.com/<you>/crypto_monitor.git
git push -u origin main
```

**2. Create the state branch**

```bash
git checkout --orphan state
git rm -rf .
git commit --allow-empty -m "state: initial"
git push origin state
git checkout main
```

**3. Generate an encryption key**

```bash
openssl rand -base64 32
```

Copy the output — this is your `STATE_ENCRYPTION_KEY`.

**4. Set repository secrets**

Go to the GitHub repo **Settings > Secrets and variables > Actions**
and create two secrets:

| Secret name | Value |
|---|---|
| `NTFY_TOPIC` | Your ntfy topic name |
| `STATE_ENCRYPTION_KEY` | The key from step 3 |

**5. Trigger the first scan**

Go to **Actions > scan > Run workflow**. The first run cold-starts:
it bootstraps ~250 candles per (symbol, interval) from Binance,
creates the database, encrypts it, and pushes to the `state` branch.

Check the run log — you should see a summary line like:

```
scan ingest=2250 scored=3 inserted=0 sent=0 queued=0 cooldown=0 failed=0 errors=0
```

After this, the scheduled cron takes over automatically.

### Workflows

| Workflow | File | Schedule | What it does |
|---|---|---|---|
| **scan** | `.github/workflows/scan.yml` | Every 5 min | Ingest candles, score signals, dispatch alerts |
| **maintenance** | `.github/workflows/maintenance.yml` | Daily 06:00 UTC | Evaluate matured signals/buys, prune candles |
| **weekly** | `.github/workflows/weekly.yml` | Sunday 12:00 UTC | Generate and send weekly summary |
| **buy-add** | `.github/workflows/buy-add.yml` | Manual only | Record a buy via the GitHub UI |

All workflows also accept manual `workflow_dispatch` triggers.

### Recording a buy on GitHub Actions

Go to **Actions > buy-add > Run workflow**. Fill in:

- **symbol**: e.g. `BTCUSDT`
- **price**: e.g. `64500.5`
- **amount**: e.g. `100`
- **bought_at**: (optional) ISO-8601 UTC timestamp
- **note**: (optional) free-form text

The workflow restores the DB, records the buy, and pushes the
updated state.

### Inspecting state

To read your data locally from the encrypted state branch:

```bash
git fetch origin state
git checkout origin/state -- crypto_monitor.db.enc
openssl enc -d -aes-256-cbc -pbkdf2 \
    -pass pass:YOUR_KEY_HERE \
    -in crypto_monitor.db.enc -out crypto_monitor.db
sqlite3 crypto_monitor.db "SELECT * FROM signals ORDER BY detected_at DESC LIMIT 10;"
rm crypto_monitor.db crypto_monitor.db.enc
```

### GitHub Actions caveats

- **Cron jitter**: GitHub free-tier cron has 5-20 minute jitter.
  Actual scan cadence is ~8-15 minutes. This is fine — scoring
  depends on hourly candle closes, not exact scan timing.
- **Skipped runs**: under high GitHub load, a scheduled run may not
  fire. Ingestion is incremental, so the next run catches up.
- **Minutes budget (private repos)**: at `*/5` cadence, scans use
  ~5,700 min/month. This exceeds the 2,000 free minutes for private
  repos. Either use a public repo, or change the cron to `*/15`
  (~1,900 min/month).
- **Double-alert on crash**: if a workflow sends a notification but
  crashes before pushing state, the next run may re-send. This is
  rare and the correct tradeoff (better to double-alert than miss).
- **Quiet hours work correctly**: the policy reads UTC and converts
  to the configured timezone internally — no runner-timezone issues.

---

## File and directory layout

```
crypto_monitor/             # the application package
config.example.toml         # default config (tracked in git)
config.toml                 # YOUR config (gitignored, created by `init`)
.env.example                # secrets template (tracked)
.env                        # YOUR secrets (gitignored)
data/
    crypto_monitor.db       # SQLite database (gitignored)
logs/
    scan.cmd.log            # Task wrapper output (stdout + stderr)
    weekly.cmd.log
    maintenance.cmd.log
scripts/
    scan.cmd                # Windows Task Scheduler wrapper
    weekly.cmd
    maintenance.cmd
    register_tasks.cmd      # one-shot task registration
    unregister_tasks.cmd    # tear-down
    gha_state.sh            # GitHub Actions encrypted state helper
.github/workflows/
    scan.yml                # GitHub Actions: scheduled scan
    maintenance.yml         # GitHub Actions: daily maintenance
    weekly.yml              # GitHub Actions: weekly summary
    buy-add.yml             # GitHub Actions: manual buy recording
tests/                      # pytest suite
```

`data\` and `logs\` are created on demand. Both are gitignored, as is
`.env` and `config.toml`.

To inspect the database directly, any SQLite browser will do
(DB Browser for SQLite is a good free option). The schema is in
[`crypto_monitor/database/schema.py`](crypto_monitor/database/schema.py).

---

## Troubleshooting

### Missing `NTFY_TOPIC`

**Symptom:**

- `python -m crypto_monitor.cli ntfy-test` exits with code 1 and
  `ntfy test failed reason=missing_topic` on stderr.
- A scan otherwise succeeds but `process_pending_signals` reports
  no sends — the alert was queued instead.

**Cause:** the `NTFY_TOPIC` environment variable is empty when the
CLI runs. The validation is intentionally deferred until send time
so `init` and the scheduler scripts work before you've picked a
topic.

**Fix:**

1. Create `.env` from `.env.example` if you haven't already.
2. Put your topic in it: `NTFY_TOPIC=your-topic-here`.
3. Re-run `python -m crypto_monitor.cli ntfy-test`.

The Task Scheduler wrappers `cd` into the project root before
running, so they pick up `.env` automatically — you do **not** need
to set the variable system-wide.

### Binance / network errors

**Symptom:**

- `scan` reports `errors=N` on its summary line.
- `logs\scan.cmd.log` contains tracebacks mentioning `requests`,
  `Connection`, or HTTP status codes.

**What's happening:** ingestion failures are **isolated per phase**.
A failed Binance call sets `report.errors` and stops ingestion, but
the rest of the scan (queue flush, scoring on whatever candles you
already have, alert dispatch) still runs.

**Fix:**

1. Check the obvious: are you connected to the internet? Is
   `api.binance.com` reachable from your machine?
2. Look at the actual error in the log. A 451 / 403 may mean Binance
   is blocking your region — switch `[binance].base_url` to a
   regional endpoint in `config.toml`.
3. Transient 5xx and timeouts are normal; the scan will catch up
   on the next run because ingestion is incremental (it asks for
   candles strictly newer than the last `open_time` it has).
4. Bump `[binance].request_timeout` and `[binance].retry_count` if
   your connection is flaky.

### Quiet-hours queue behavior

**Symptom:**

- A scan during the night reports `queued=N` instead of `sent=N`.
- Notifications arrive in a burst the moment quiet hours end.

**This is by design.** During the configured quiet hours
(`[alerts].quiet_hours_start`..`quiet_hours_end` in **local** time,
wrap-around supported), `process_pending_signals` writes new alerts
to the `notifications` table with `queued=1, delivered=0` instead of
sending them. The next scan after quiet hours end calls `flush_queue`
first, which dispatches everything that piled up.

**Things to check if it doesn't behave that way:**

- `[general].timezone` must be the IANA name for **your** locale, not
  UTC, otherwise the quiet-hours window will be off by your offset.
- `very_strong` signals **bypass** quiet hours by design — they are
  sent immediately and recorded with `bypass_quiet=1`. If you do not
  want this, lower the severity bar by raising
  `[scoring.severity].very_strong` in `config.toml`.

### Failed notifications

**Symptom:**

- `notifications.delivered = 0` and `notifications.last_error` is set.
- A scan reports `failed=N` on its summary line.

**Where to look:**

- `logs\scan.cmd.log` — the full traceback / status code from the
  retry attempts.
- The `notifications` table — `delivery_attempts` increments on each
  failed retry.

**Common causes:**

- 4xx from ntfy: usually a typo in the topic or a server URL with a
  trailing slash issue. The CLI strips the trailing slash on load,
  but double-check what's actually in `config.toml`.
- 5xx / network: same advice as the Binance section. Failed sends
  are retried with exponential backoff up to `[ntfy].max_retries`,
  and undelivered rows stay in the queue, so you won't lose alerts
  unless you manually delete them.

### Database / log file locations

If `init` ran successfully, both directories are under the project
root:

```
data\crypto_monitor.db          # SQLite database
logs\scan.cmd.log               # scheduled scan output
logs\weekly.cmd.log             # scheduled weekly output
logs\maintenance.cmd.log        # scheduled evaluate output
```

Override `[general].db_path` and `[general].log_dir` in `config.toml`
if you want them somewhere else (e.g. on a different drive). Both
paths are resolved relative to the project root, so a relative path
like `..\shared\crypto_monitor.db` works too.

To wipe everything and start over:

```
python -m crypto_monitor.cli ntfy-test    # confirm ntfy still works
rmdir /S /Q data
del config.toml
python -m crypto_monitor.cli init
```

This drops the database and the local config but leaves your `.env`,
`logs\`, and recorded buys untouched (buys live in the database, so
they will be lost too — back up `data\crypto_monitor.db` first if
you care about them).
