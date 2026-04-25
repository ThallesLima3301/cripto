# crypto_monitor

Local-first crypto market analysis and alerting.

`crypto_monitor` watches a small set of Binance pairs, scores price
action against a configurable rubric, persists everything to a SQLite
database, and pushes notifications through [ntfy](https://ntfy.sh)
when something interesting happens. It is purely advisory — the
project ingests data and surfaces decisions, it does not place orders
on any exchange.

It can run in two modes:

- **Local (Windows)** — scheduled tasks on your own PC.
- **Cloud (GitHub Actions)** — scheduled workflows on free runners,
  with the database encrypted and stored in a dedicated `state` git
  branch.

There is no server, no paid cloud account, no telemetry. The CLI is
the single entry point for both modes:

```
python -m crypto_monitor.cli <command> [args]
```

---

## Contents

- [What it does](#what-it-does)
- [What it does NOT do](#what-it-does-not-do)
- [Feature set](#feature-set)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Buy-signal logic](#buy-signal-logic)
- [Sell logic](#sell-logic)
- [Watchlist](#watchlist)
- [Evaluations and analytics](#evaluations-and-analytics)
- [Notifications](#notifications)
- [CLI reference](#cli-reference)
- [Requirements](#requirements)
- [Running locally (Windows)](#running-locally-windows)
- [Running on GitHub Actions](#running-on-github-actions)
- [File and directory layout](#file-and-directory-layout)
- [Limitations and tradeoffs](#limitations-and-tradeoffs)
- [Future work](#future-work)
- [Troubleshooting](#troubleshooting)

---

## What it does

- Pulls hourly / 4h / daily candles for a configurable set of Binance
  pairs and persists them to SQLite.
- Computes a multi-factor buy score (drop magnitude, RSI, relative
  volume, support distance, discount-from-high, reversal confirmation,
  trend context) and emits a signal when the score crosses an emit
  threshold.
- Optionally classifies a market regime from BTC daily candles
  (`risk_on` / `neutral` / `risk_off`) and shifts the emit threshold
  accordingly.
- Supports a watchlist for borderline scores: setups below the emit
  floor but above a configured `floor_score` are tracked and may be
  promoted into a real signal if they later cross the threshold.
- Lets the user record manual buys, then evaluates each buy and signal
  after a 30-day maturation window: 24h / 7d / 30d returns, MFE / MAE,
  time-to-MFE, time-to-MAE, and a verdict.
- Monitors open positions against four sell rules (stop-loss, trailing
  stop, take-profit, context deterioration) and dispatches advisory
  sell alerts.
- Aggregates evaluated signals into expectancy / win-rate / profit-factor
  / MFE-MAE analytics, on demand from the CLI and as a digest line in
  the weekly summary.
- Sends Portuguese-language ntfy notifications with a quiet-hours
  queue, per-symbol cooldown, and severity-based escalation.

## What it does NOT do

- **No automatic order execution.** Sell signals are advisory; the
  user decides whether and how to act.
- **No exchange trading integration.** No API keys, no order endpoints,
  no balance lookup. The only Binance call is the public market-data
  endpoint for candles.
- **No machine-learning models.** Every score and rule is rule-based
  and explicit in the codebase.
- **No web dashboard.** The CLI and ntfy notifications are the entire
  user interface.
- **No bullish-divergence indicator yet.** Reversal confirmation today
  uses candlestick patterns + RSI recovery + high-reclaim only.

---

## Feature set

### Market data ingestion
- Per-symbol incremental ingest of 1h / 4h / 1d candles from
  `data-api.binance.vision`.
- Cold-start bootstraps the most recent ~250 candles per
  `(symbol, interval)`; subsequent runs only fetch what's new.
- Per-`(symbol, interval)` retention caps prune old candles during
  daily maintenance.

### Buy-signal scoring
Seven additive factors capped at 100 points total (validated at
config-load time):

| Factor | Default weight |
|---|---|
| `drop_magnitude` (multi-horizon) | 25 |
| `rsi_oversold` (1h + 4h) | 20 |
| `relative_volume` | 15 |
| `support_distance` | 15 |
| `discount_from_high` (30d + 180d) | 10 |
| `reversal_pattern` | 10 |
| `trend_context` | 5 |

Drop scoring is **ATR-normalized**: when `atr(1h)` and the current
price are both available, raw drop percentages are divided by
`atr_pct = atr(1h) / price * 100` before tier lookup. When ATR is
unavailable the helper falls back to v1 raw-drop behavior bit-for-bit.

Reversal confirmation is additive: detected pattern (+5),
RSI-recovery from oversold (+3), close reclaiming a recent prior
high (+2), capped at the factor's budget.

### Regime awareness
- Optional BTC-daily classifier (EMA20 / EMA50 alignment + ATR
  percentile) producing `risk_on` / `neutral` / `risk_off`.
- Latest snapshot is persisted in `regime_snapshots` and stamped on
  every emitted signal as `signals.regime_at_signal`.
- The emit threshold is shifted by `threshold_adjust_risk_on` /
  `threshold_adjust_risk_off` (typical values: −5 / +5). The
  severity-tier ladder itself is unchanged.
- BTC candles are auto-seeded for ingestion when the regime feature
  is enabled, even if BTCUSDT is not in `[symbols].tracked`. BTC is
  excluded from the buy-scoring loop in that case.

### Sell-side monitoring
- Sell engine evaluates every open buy each scan cycle.
- Four rules, evaluated in this priority order:
  1. `stop_loss`             — `current_price <= entry * (1 - stop_loss_pct/100)`
  2. `trailing_stop`         — needs a prior watermark; fires on
     `current_price <= watermark * (1 - trailing_stop_pct/100)`
  3. `take_profit`           — `current_price >= entry * (1 + take_profit_pct/100)`
  4. `context_deterioration` — regime is `risk_off`, position is at a
     loss, and the flag is enabled
- A high-watermark per `(symbol, buy_id)` is stored and updated
  *after* evaluation, so the trailing-stop rule always sees the prior
  peak.
- Per-`(buy, rule)` cooldown prevents spam.
- Each sell signal lands in `sell_signals` and (on success) ships a
  Portuguese-language ntfy notification.

### Watchlist
- Borderline scores (between `floor_score` and the emit threshold)
  are tracked in the `watchlist` table.
- One active row per symbol, enforced by a partial unique index.
- Lifecycle: **WATCH** (insert / refresh) → **PROMOTE** (score
  crossed the emit threshold; a real signal is produced and linked
  via `signals.watchlist_id`) → **EXPIRE** (score dropped below
  floor) or **stale-expire** (no refresh inside `max_watch_hours`).
- Disabling the feature short-circuits the entire path; resolved
  rows remain as an audit trail.

### Evaluations
- Signal evaluations: 24h / 7d / 30d returns, max-gain / max-loss
  over the 7-day post-signal window, time-to-MFE / time-to-MAE in
  hours, and a verdict (`great` / `good` / `neutral` / `poor` /
  `bad` / `pending`).
- Buy evaluations: hourly-resolution intraday low for the buy day,
  7d / 30d returns, MFE / MAE / timing over the 7-day post-buy
  window, and a verdict.
- A 30-day maturation window means each evaluation is one-shot.
  Re-running maintenance is idempotent.

### Analytics
- Pure aggregator: `compute_expectancy(rows)` returns total signals,
  an overall bucket, and slicings by `severity`, `regime_at_signal`,
  score band (`50-64` / `65-79` / `80-100`), and
  `dominant_trigger_timeframe`.
- Per bucket: `count`, `win_rate`, `avg_win_pct`, `avg_loss_pct`,
  `expectancy`, `profit_factor`, `avg_mfe_pct`, `avg_mae_pct`,
  `avg_time_to_mfe_hours`, `avg_time_to_mae_hours`. None values
  are surfaced explicitly when the underlying data is missing.
- `min_signals` filter (default 5) drops sliced buckets below the
  threshold; the overall bucket is always present.

### Notifications
- ntfy POST with severity → priority mapping
  (`normal/strong/very_strong → default/high/max`).
- Portuguese client copy with friendly asset names, top-3 reason
  lines, regime annotation, and a one-line interpretation.
- Optional debug mode appends raw indicator dumps to the body.
- Per-symbol cooldown and escalation jump (`escalation_jump`).
- Quiet-hours queue: alerts produced inside the configured local
  window are written to `notifications` with `queued=1` and flushed
  on the next scan after the window ends. `very_strong` bypasses
  quiet hours.
- Sell signals use a dedicated formatter so the buy-side notification
  UX is unaffected.

### CLI
- One entry point (`python -m crypto_monitor.cli`) with subcommands
  for setup, scan, weekly summary, maintenance, manual buy/sell
  recording, signal/sell/watchlist inspection, and analytics
  reporting. See [CLI reference](#cli-reference).

### Local scheduler mode
- Three wrapper scripts under `scripts\` registered with Windows
  Task Scheduler. Each script `cd`s into the project root and runs
  the matching CLI command.

### GitHub Actions mode
- Four workflows (scan / maintenance / weekly / buy-add) operate on
  an encrypted SQLite DB stored in a dedicated `state` branch.
- AES-256-CBC + PBKDF2 via `openssl enc`; the key lives in the
  `STATE_ENCRYPTION_KEY` repo secret.
- A shared `crypto-state` concurrency group serializes writers.

---

## Architecture

The application package is split into focused modules:

| Module | Responsibility |
|---|---|
| `crypto_monitor/config/` | TOML + `.env` loader, frozen `Settings` dataclasses (`ScoringSettings`, `RegimeSettings`, `SellSettings`, `WatchlistSettings`, …). |
| `crypto_monitor/database/` | Connection helper, baseline schema (`schema.py`), incremental migration runner (`migrations.py`), retention helpers. |
| `crypto_monitor/binance/` | Lean HTTP client exposing only `get_klines()` against `data-api.binance.vision`. |
| `crypto_monitor/ingestion/` | Per-symbol incremental candle ingest + dedup via `UNIQUE(symbol, interval, open_time)`. |
| `crypto_monitor/indicators/` | Pure indicator helpers — RSI, ATR, EMA / trend label, support detection, candlestick patterns, RSI-recovery + high-reclaim, relative volume. |
| `crypto_monitor/regime/` | BTC EMA + ATR-percentile classifier, snapshot store, type definitions. |
| `crypto_monitor/signals/` | `score_signal()` engine, factor scorers, dedup-aware persistence (`insert_signal`). |
| `crypto_monitor/buys/` | Manual buy ledger (`insert_buy`, `list_buys`). |
| `crypto_monitor/sell/` | Sell-rule evaluator (pure), watermark + signal store, scan-time runtime + ntfy dispatch. |
| `crypto_monitor/watchlist/` | Watchlist store + pure state machine (`decide_watch_action`). |
| `crypto_monitor/evaluation/` | Matured signal / buy evaluation with MFE / MAE timing; verdict mapping. |
| `crypto_monitor/analytics/` | Pure expectancy aggregator, scope-filtered loader, CLI / weekly formatters. |
| `crypto_monitor/notifications/` | ntfy HTTP sender, alert policy (cooldown / escalation / quiet hours), buy-side and sell-side formatters, queue + dispatch service. |
| `crypto_monitor/reports/` | Weekly summary generation, persistence, send orchestrator. |
| `crypto_monitor/scheduler/` | `run_scan` / `run_maintenance` / `run_weekly` orchestrators that stitch the layers together for one cycle. |
| `crypto_monitor/cli/` | argparse front-end and per-subcommand handlers. |
| `crypto_monitor/utils/` | Time / ISO helpers shared across the codebase. |

Every cross-layer call is dependency-injected (settings, DB connection,
ntfy sender, clock) so the orchestrators are exhaustively tested
against in-memory SQLite without touching the network.

---

## Data model

All tables live in one SQLite file. Key tables:

| Table | Purpose |
|---|---|
| `schema_meta` | Schema version + first-init timestamp. Migrations bump the version. |
| `symbols` | Symbols the scanner is allowed to ingest / score. |
| `candles` | Closed OHLCV per `(symbol, interval, open_time)`. The longest history any factor needs (180d on 1d). |
| `signals` | Emitted buy signals — score, severity, drops, RSI, volume, support, regime, optional `watchlist_id` link. |
| `signal_evaluations` | One row per signal once 30 days have elapsed: 24h/7d/30d returns, 7-day MFE / MAE + timing, verdict. |
| `notifications` | ntfy dispatch log: queued / sent state, retries, per-attempt errors. |
| `buys` | Manual buy ledger; `sold_at` / `sold_price` / `sold_note` capture user-recorded sales. |
| `buy_evaluations` | Matured buy outcomes — hourly intraday low, 7d/30d returns, MFE / MAE + timing, verdict. |
| `regime_snapshots` | One row per scan cycle when the regime feature is enabled. |
| `sell_tracking` | Per-`(symbol, buy_id)` post-entry high watermark for the trailing-stop rule. |
| `sell_signals` | Append-only log of fired sell rules: rule, severity, reason, P&L%, regime, alerted flag. |
| `watchlist` | One active row per symbol (partial unique index) plus a permanent audit trail of resolved rows. |
| `weekly_summaries` | Persisted weekly digest body + structured fields (signal counts, top drop, sent flag). |
| `processing_state` | Generic key/value scratch space for the ingestion + maintenance phases. |

The full DDL lives in
[`crypto_monitor/database/schema.py`](crypto_monitor/database/schema.py)
and the additive migrations in
[`crypto_monitor/database/migrations.py`](crypto_monitor/database/migrations.py).

---

## Buy-signal logic

For each tracked symbol, every scan cycle:

1. Loads up to 250 candles per `(symbol, interval)` for `1h`, `4h`, `1d`.
2. Computes the seven factors:
   - **Drop magnitude** — multi-horizon drop (1h, 24h, 7d, 30d, 180d),
     ATR-normalized when `atr(1h)` is available, scored against
     ascending tiers.
   - **RSI oversold** — 1h + 4h RSI tiers, additive and capped.
   - **Relative volume** — last 1h volume vs. 20-bar average.
   - **Support distance** — heuristic swing-low support; closer scores
     more.
   - **Discount from high** — distance below the 30d / 180d high.
   - **Reversal confirmation** — additive: candlestick pattern (5) +
     RSI recovery from oversold (3) + close reclaiming a prior
     window high (2), capped at 10.
   - **Trend context** — 1d trend label (`uptrend` / `sideways` /
     `downtrend`) with rewards for buying dips inside a rising market.
3. Maps the total to a severity tier (`normal` / `strong` /
   `very_strong`) using `[scoring.severity]` thresholds.
4. Applies the regime threshold adjustment to the emit floor (no
   change to tier boundaries).
5. Inserts a row when the candidate's severity is non-None, with
   rule-driven dedup against existing rows for the same
   `(symbol, candle_hour)`. Higher severity rows override lower ones
   (escalation).

When watchlist is enabled and the regular emit declined, the watchlist
state machine takes over (see below).

---

## Sell logic

The sell engine runs on every scan cycle when `[sell].enabled = true`.
For each open buy (a `buys` row with `sold_at IS NULL`):

1. Fetches the latest 1h close as the current price.
2. Reads the **prior** watermark from `sell_tracking`.
3. Calls the pure evaluator with `(buy, current_price,
   prior_high_watermark, regime_label, settings, now)`.
4. The evaluator returns a single `SellSignal` or `None` based on the
   priority order: `stop_loss > trailing_stop > take_profit >
   context_deterioration`.
5. If a signal fires and the per-`(buy, rule)` cooldown has elapsed:
   inserts the row, sends a sell-side ntfy notification, flips
   `alerted=1`. Cooldown-suppressed signals are neither inserted nor
   sent.
6. Updates the watermark to `max(buy.price, current_price)`. The
   stored value is monotone — never lowered by a falling price.

Severity → priority: `high → max`, `medium → high`. Stop-loss is the
only `high`-severity rule. **Sell signals are advisory.** The user
records the actual sale through `sell record`, which writes the
`sold_*` columns on the buy row and removes it from future
evaluations.

---

## Watchlist

The watchlist captures borderline setups that aren't strong enough
to emit but are worth waiting on. When `[watchlist].enabled = true`
and the buy-signal path returned `severity is None`, the state
machine decides:

- **PROMOTE** — `score >= min_signal_score` (the **base** value, not
  the regime-adjusted floor). Synthesizes a severity from the
  `[scoring.severity]` ladder, threads `watchlist_id` into the
  candidate, and runs the normal `insert_signal` path. On a
  successful insert the watch row transitions to `status='promoted'`
  with `resolution_reason='promoted'` and stamps `promoted_signal_id`.
- **WATCH** — `floor_score <= score < min_signal_score`. Inserts (or
  refreshes) the active row, extending `expires_at` to
  `now + max_watch_hours`.
- **EXPIRE** — `score < floor_score` and an active watch exists.
  Transitions the row to `status='expired'` with
  `resolution_reason='expired_below_floor'`.
- **IGNORE** — `score < floor_score` and no active watch. No-op.

Once per scan cycle the orchestrator also calls `expire_stale` to
transition every `status='watching'` row whose `expires_at <= now`,
even for symbols not currently being scored.

The intent is to surface "patient" setups that approach the emit
threshold gradually instead of in a single dramatic candle, while
clearly marking them as borderline in the audit trail.

---

## Evaluations and analytics

### Evaluations

Maintenance (`evaluate`) walks every signal / buy older than 30 days
that has no row in `signal_evaluations` / `buy_evaluations` and
computes:

- **Signals**: `price_at_signal`, `price_24h_later`, `price_7d_later`,
  `price_30d_later`, returns at each horizon, MFE / MAE over the
  7-day window with `time_to_mfe_hours` / `time_to_mae_hours`, and
  a verdict assigned from the 7-day return.
- **Buys**: hourly-resolution intraday low on the buy day, day-open
  vs. low percentages, 7d / 30d returns, MFE / MAE / timing over
  the 7-day window, and a verdict.

Insufficient post-event candles surface as NULL — never as
silently-zero values.

### Analytics

The analytics aggregator is **pure** — it accepts a list of dicts
shaped like the `signal_evaluations ⨝ signals` join and returns an
`ExpectancyReport` with no DB or I/O. The CLI loads the rows,
optionally filtered by scope (`all` / `90d` / `30d`); the weekly
report does the same with a 90-day window.

Per bucket the aggregator computes win rate, expectancy, profit
factor, average win / loss, MFE / MAE, and time-to-MFE /
time-to-MAE. Profit factor is explicitly `None` when there are no
losses (rather than crashing on division). Sliced buckets below
`min_signals` (default 5) are omitted; the overall bucket is always
present.

The weekly summary appends a one-line digest (`📈 Análise (90d)`)
when at least 5 matured rows exist, and a `Análise: dados
insuficientes` line otherwise — the section header always appears
so users notice the feature exists.

---

## Notifications

- **ntfy** is the only outbound integration. Priority and quiet-hours
  behavior live in `crypto_monitor/notifications/policy.py`.
- **Buy-side body**: friendly asset name, current price, 24h
  variation, top-3 reason lines (drop horizon, RSI tier, volume
  spike, reversal pattern, support proximity, discount from high),
  optional regime annotation, severity-driven decision phrase.
- **Sell-side body**: dedicated formatter with rule-specific headline
  (`🔴 Stop-loss acionado` / `🟠 Trailing stop` / `🟢 Take-profit` /
  `🟡 Contexto deteriorando`), price, signed P&L, reason, optional
  regime line, decision suggestion.
- **Weekly body**: signal count + severity breakdown, top drop of
  the week, buy count, matured-verdict histogram, conclusion line,
  optional analytics digest.
- **Debug mode** (`[ntfy].debug_notifications = true`) appends a raw
  data block (scores, every indicator value, regime label, raw pair
  name) after a `--- debug ---` separator.
- **Quiet hours**: alerts produced inside `[alerts].quiet_hours_*`
  (local time) are queued and flushed on the next post-quiet scan;
  `very_strong` signals bypass and send immediately.

---

## CLI reference

Every command accepts a global `--project-root <path>` flag (default:
the current working directory). Exit codes: `0` on success, `1` on
runtime error, `2` on argparse usage error.

| Command | What it does |
|---|---|
| `init [--no-seed]` | Copy `config.example.toml` → `config.toml` if missing and initialize the SQLite database. Optionally seeds tracked symbols. Idempotent. |
| `scan` | One scan cycle: flush queued notifications, classify regime (if enabled), ingest new candles, score every active symbol (with watchlist branch when configured), evaluate open buys (sell engine), dispatch alerts. |
| `weekly` | Generate the weekly summary, persist it in `weekly_summaries`, push it via ntfy. |
| `evaluate` | Maintenance pass: evaluate matured signals + buys, prune old candles, optional `VACUUM`. |
| `buy add` | Record a manual buy. |
| `buy list` | Print recorded buys (newest last). `--symbol`, `--limit`. |
| `signals list` | Print recent signals (newest first). `--symbol`, `--severity`, `--limit`. |
| `sell record` | Mark an open buy as sold. `--buy-id`, `--price`, `--at`, `--note`. |
| `sell list` | Print rows from `sell_signals`. `--symbol`, `--rule`, `--limit`. |
| `watchlist list` | Print active `status='watching'` rows. |
| `analytics summary` | Print the expectancy report. `--scope all\|90d\|30d` (default `all`), `--min-signals N` (default 5). |
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
`amount / price`.

`sell record` arguments:

```
python -m crypto_monitor.cli sell record ^
    --buy-id 7 ^
    --price 71250.0 ^
    [--at 2026-04-22T15:00:00Z] ^
    [--note "took profit at trailing stop"]
```

The store helper validates: positive sold price, tz-aware sold_at,
sold_at not earlier than bought_at, no double-sell.

---

## Requirements

- **Windows 10 or 11** for the local Task Scheduler flow (the
  workflows on GitHub Actions run on Linux).
- **Python 3.12 or newer**.
- An **ntfy topic** — public `https://ntfy.sh` or self-hosted.
- Outbound HTTPS to `data-api.binance.vision` and your ntfy server.

No database server, no Docker, no admin rights are required.

---

## Running locally (Windows)

All commands assume you are at the project root in a regular
(non-elevated) terminal.

### 1. Clone or unpack the project

Place it anywhere you can write to. Avoid paths with spaces — Task
Scheduler is happier without them.

### 2. Create a virtual environment

```
python -m venv .venv
.venv\Scripts\activate
```

The wrapper scripts in `scripts\` automatically pick up
`.venv\Scripts\python.exe` if it exists.

### 3. Install runtime dependencies

```
pip install -r requirements.txt
```

### 4. Initialize the project

```
python -m crypto_monitor.cli init
```

This copies `config.example.toml` to `config.toml` (only if missing),
creates the SQLite database, runs all migrations, and seeds the
tracked symbols. Pass `--no-seed` to skip seeding.

### 5. Configure your ntfy topic

```
copy .env.example .env
notepad .env
```

Set `NTFY_TOPIC` to a long, hard-to-guess value. Anyone who knows the
topic can read your notifications, so treat it like a password.

### 6. Send a test notification

```
python -m crypto_monitor.cli ntfy-test
```

Expected: `sent ok status=200` on stdout and the message on your phone.

### 7. Run a manual scan

```
python -m crypto_monitor.cli scan
```

The first run cold-starts ~250 candles per (symbol, interval), so it
takes longer than subsequent runs.

### 8. (Optional) Register the scheduled tasks

```
scripts\register_tasks.cmd
```

This creates three tasks via `schtasks /Create /F` running at user
privilege (no admin):

| Task name | Schedule | Command |
|---|---|---|
| `crypto_monitor scan` | every 5 minutes | `python -m crypto_monitor.cli scan` |
| `crypto_monitor maintenance` | daily 03:00 local | `python -m crypto_monitor.cli evaluate` |
| `crypto_monitor weekly` | Sunday 09:00 local | `python -m crypto_monitor.cli weekly` |

`scripts\unregister_tasks.cmd` tears them down. Re-running
`register_tasks.cmd` is idempotent.

The wrappers (`scan.cmd`, `weekly.cmd`, `maintenance.cmd`) `cd` into
the project root via `%~dp0`, prefer the venv interpreter, and append
stdout + stderr to `logs\<task>.cmd.log`.

**Local mode is good for**: developers who already keep a PC on, want
the lowest possible cron jitter, want the database on local disk, and
don't mind the machine being the single point of failure.

---

## Running on GitHub Actions

Cloud mode runs the same CLI on free GitHub-hosted runners. The
database lives in a dedicated `state` branch as an
AES-256-CBC + PBKDF2 encrypted blob (`crypto_monitor.db.enc`).

### How it works

Each workflow run:

1. Checks out `main` and the encrypted DB from `state`.
2. Decrypts the DB using `STATE_ENCRYPTION_KEY` (`scripts/gha_state.sh`).
3. Runs the CLI command.
4. Checkpoints the WAL, re-encrypts, verifies the round-trip, pushes
   the updated blob back to `state` with `--force-with-lease`.

The `state` branch is amended to a single commit so the repo never
accumulates binary history. A shared `crypto-state` concurrency group
ensures only one workflow writes at a time.

### Requirements

- A GitHub repository (public repos get unlimited free Actions
  minutes; private repos get 2,000 min/month).
- An ntfy topic (same as local).
- Two repository secrets:

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | Your ntfy topic name. |
| `STATE_ENCRYPTION_KEY` | A long random key, e.g. `openssl rand -base64 32`. |

### One-time setup

```bash
git remote add origin https://github.com/<you>/crypto_monitor.git
git push -u origin main

# Create the orphan state branch with a single empty commit.
git checkout --orphan state
git rm -rf .
git commit --allow-empty -m "state: initial"
git push origin state
git checkout main
```

Set the two secrets under **Settings > Secrets and variables >
Actions**, then trigger **Actions > scan > Run workflow** to bootstrap.
The first run ingests ~250 candles per (symbol, interval), creates the
DB, encrypts it, and pushes to `state`. A summary line in the run log
confirms success:

```
scan ingest=2250 scored=3 inserted=0 sent=0 queued=0 cooldown=0 failed=0 ... errors=0
```

After this the cron schedules take over.

### Workflows

| Workflow | File | Schedule | What it does |
|---|---|---|---|
| **scan** | `.github/workflows/scan.yml` | every 5 minutes | Ingest, regime, score, watchlist, sell, dispatch alerts. |
| **maintenance** | `.github/workflows/maintenance.yml` | daily 06:00 UTC | Evaluate matured signals + buys, prune candles. |
| **weekly** | `.github/workflows/weekly.yml` | Sunday 12:00 UTC | Generate and send the weekly summary. |
| **buy-add** | `.github/workflows/buy-add.yml` | manual only | Record a buy via the GitHub UI. |

All workflows accept `workflow_dispatch` triggers.

### Recording a buy on GitHub Actions

**Actions > buy-add > Run workflow**. Provide `symbol`, `price`,
`amount`, optional `bought_at`, optional `note`. The workflow
restores the DB, records the buy, and pushes the updated state.

### Inspecting the encrypted state locally

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

- **Cron jitter**: free-tier cron has 5–20 minute jitter; actual scan
  cadence is ~8–15 minutes. Scoring depends on hourly candle closes,
  not exact scan timing, so this is fine.
- **Skipped runs**: under heavy GitHub load a scheduled run may not
  fire. Ingestion is incremental, so the next run catches up.
- **Minutes budget (private repos)**: at `*/5` cadence, scans use
  ~5,700 min/month — over the 2,000-minute free quota for private
  repos. Either use a public repo or change the cron to `*/15`
  (~1,900 min/month).
- **Double-alert on crash**: if a workflow sends a notification but
  crashes before pushing state, the next run may re-send. Rare and a
  safer tradeoff than missing alerts.
- **Quiet hours**: the policy reads UTC and converts to the configured
  timezone internally — runner timezone is irrelevant.
- **Back up `STATE_ENCRYPTION_KEY` somewhere safe.** Without it the
  encrypted DB is unrecoverable; GitHub cannot help.

---

## File and directory layout

```
crypto_monitor/
├── analytics/        expectancy aggregator + reporter + scope-filtered loader
├── binance/          lean HTTP client (get_klines only)
├── buys/             manual buy ledger
├── cli/              argparse front-end + handlers
├── config/           settings loader + frozen dataclasses
├── database/         schema + migrations + retention + connection helper
├── evaluation/       matured signal/buy evaluation, MFE/MAE timing, verdicts
├── indicators/       RSI, ATR, EMA/trend, support, candlestick patterns, volume
├── ingestion/        per-symbol incremental candle ingest
├── notifications/    ntfy sender, policy, queue, formatters (buy + sell + weekly)
├── regime/           BTC EMA + ATR percentile classifier + snapshot store
├── reports/          weekly summary generation + persistence + send
├── scheduler/        run_scan / run_maintenance / run_weekly orchestrators
├── sell/             pure evaluator + watermark + signal store + scan-time runtime
├── signals/          score_signal engine + factor scorers + dedup-aware persistence
├── utils/            time / ISO helpers
└── watchlist/        store + pure state machine (decide_watch_action)

config.example.toml   default config (tracked in git)
config.toml           YOUR config (gitignored, created by `init`)
.env.example          secrets template (tracked)
.env                  YOUR secrets (gitignored)
data/
└── crypto_monitor.db SQLite database (gitignored)
logs/
├── scan.cmd.log
├── weekly.cmd.log
└── maintenance.cmd.log
scripts/
├── scan.cmd               Windows Task Scheduler wrapper
├── weekly.cmd
├── maintenance.cmd
├── register_tasks.cmd     one-shot task registration
├── unregister_tasks.cmd   teardown
└── gha_state.sh           GitHub Actions encrypted-state helper
.github/workflows/
├── scan.yml
├── maintenance.yml
├── weekly.yml
└── buy-add.yml
tests/                   pytest suite (500+ tests against in-memory SQLite)
```

`data\` and `logs\` are created on demand. Both, plus `.env` and
`config.toml`, are gitignored.

---

## Limitations and tradeoffs

- **Advisory only.** The system flags decisions but never executes
  them. Confirming a sell, recording a buy, or acting on a signal is
  always manual.
- **GitHub Actions schedule jitter.** 5–20 minute jitter on the free
  tier; cadence is approximate, not real-time.
- **Hourly resolution.** Every price calculation is bounded by 1h
  candles. Intraday moves between candle closes are invisible.
- **Analytics need maturation history.** With a 30-day maturation
  window, a fresh install needs ~5+ weeks of data before
  `analytics summary` shows non-trivial buckets, and ~3 months
  before the weekly digest stops saying "dados insuficientes".
- **Watchlist + regime require live data.** Both features become
  meaningful after several scan cycles — `decide_watch_action` and
  `_classify_regime` return inert outputs without history.
- **No backfill of old signals.** The scoring engine only operates on
  the latest closed 1h candle in scope; you can't replay history to
  retroactively generate signals.
- **No multi-exchange.** Binance public market data only. Other venues
  would need their own ingestion module.
- **No ML, no exchange integration, no web UI.** See
  [What it does NOT do](#what-it-does-not-do).

---

## Future work

These are explicitly **not** implemented today and are tracked as
possible future blocks:

- **Bullish divergence indicator** (deferred). Reversal confirmation
  currently relies on candlestick patterns + RSI recovery + high
  reclaim only.
- **Lightweight web dashboard** for browsing signals, evaluations,
  and analytics without `sqlite3`.
- **Paper-trading layer** that simulates fills against historical
  candles so the analytics aggregator can score hypothetical
  strategies before risking real capital.
- **Exchange execution layer** (broker API integration). Would be
  opt-in, isolated behind a feature flag, and only built if the
  manual flow proves consistently profitable.
- **ML models**, only if a large enough dataset of evaluated signals
  ever justifies replacing rule-based scoring. Not on the near
  horizon.

---

## Troubleshooting

### Missing `NTFY_TOPIC`

**Symptom:**

- `python -m crypto_monitor.cli ntfy-test` exits with code 1 and
  `ntfy test failed reason=missing_topic` on stderr.
- A scan otherwise succeeds but `process_pending_signals` reports no
  sends — the alert was queued instead.

**Cause:** `NTFY_TOPIC` is empty when the CLI runs. Validation is
deliberately deferred to send time so `init` and the schedulers
work before you've picked a topic.

**Fix:**

1. Create `.env` from `.env.example` if you haven't already.
2. Put your topic in it: `NTFY_TOPIC=your-topic-here`.
3. Re-run `python -m crypto_monitor.cli ntfy-test`.

The Task Scheduler wrappers `cd` into the project root, so they pick
up `.env` automatically — you do not need to set the variable
system-wide.

### Binance / network errors

**Symptom:**

- `scan` reports `errors=N` on its summary line.
- `logs\scan.cmd.log` contains tracebacks mentioning `requests`,
  `Connection`, or HTTP status codes.

**What's happening:** ingestion failures are isolated per phase. A
failed Binance call sets `report.errors` and stops ingestion, but
the rest of the scan (queue flush, scoring on whatever candles you
already have, alert dispatch, sell pass) still runs.

**Fix:**

1. Check the obvious: connectivity to `data-api.binance.vision`.
2. Look at the actual error in the log. A 451 / 403 may mean
   Binance is blocking your region — switch `[binance].base_url`
   in `config.toml`.
3. Transient 5xx and timeouts are normal; the next run catches up
   because ingestion is incremental.
4. Bump `[binance].request_timeout` and `[binance].retry_count` if
   your connection is flaky.

### Quiet-hours queue behavior

**Symptom:** a scan during the night reports `queued=N` instead of
`sent=N`; notifications arrive in a burst when quiet hours end.

**This is by design.** During the configured quiet hours
(`[alerts].quiet_hours_start`..`quiet_hours_end` in **local** time,
wrap-around supported), `process_pending_signals` writes alerts to
the `notifications` table with `queued=1, delivered=0`. The next
scan after the window calls `flush_queue` first.

`very_strong` signals **bypass** quiet hours by design. Raise
`[scoring.severity].very_strong` in `config.toml` if you don't want
that.

### Failed notifications

**Symptom:** `notifications.delivered = 0` and
`notifications.last_error` is set; `scan` reports `failed=N`.

**Where to look:**

- `logs\scan.cmd.log` — tracebacks / status codes from retries.
- The `notifications` table — `delivery_attempts` increments per
  failed retry.

**Common causes:**

- 4xx from ntfy: typo in the topic, server URL with a stray slash.
  The CLI strips the trailing slash on load, but verify
  `[ntfy].server_url` in `config.toml`.
- 5xx / network: same advice as the Binance section. Failed sends
  retry with exponential backoff up to `[ntfy].max_retries` and
  remain queued.

### Database / log file locations

If `init` ran successfully:

```
data\crypto_monitor.db          # SQLite database
logs\scan.cmd.log               # scheduled scan output
logs\weekly.cmd.log             # scheduled weekly output
logs\maintenance.cmd.log        # scheduled evaluate output
```

Override `[general].db_path` and `[general].log_dir` in
`config.toml` if you want them somewhere else. Both paths resolve
relative to the project root.

To wipe everything and start over:

```
python -m crypto_monitor.cli ntfy-test    # confirm ntfy still works
rmdir /S /Q data
del config.toml
python -m crypto_monitor.cli init
```

This drops the database and the local config but leaves your `.env`,
`logs\`, and registered tasks untouched. Your buys live in the
database, so back up `data\crypto_monitor.db` first if you care
about them.
