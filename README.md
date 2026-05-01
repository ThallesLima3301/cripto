# crypto_monitor

Local-first crypto market analysis and alerting, plus a read-only
local dashboard.

`crypto_monitor` watches a small set of Binance pairs, scores price
action against a configurable rubric, persists everything to a SQLite
database, and pushes notifications through [ntfy](https://ntfy.sh)
when something interesting happens. It tracks user-recorded buys,
monitors them against a sell-rule engine, parks borderline-score
symbols on a watchlist, evaluates matured outcomes, and aggregates
expectancy/win-rate analytics.

A separate Next.js dashboard (under `dashboard/`) reads the same data
through a small FastAPI adapter — never directly from SQLite. The
dashboard is **read-only**.

The project is purely advisory: it ingests data and surfaces
decisions. It does not place orders on any exchange.

It can run in two modes:

- **Local (Windows)** — scheduled tasks on your own PC.
- **Cloud (GitHub Actions)** — scheduled workflows on free runners,
  with the SQLite DB encrypted and stored in a dedicated `state` git
  branch.

There is no server, no paid cloud account, no telemetry. The CLI is
the single entry point for every bot operation:

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
- [Dashboard](#dashboard)
- [CLI reference](#cli-reference)
- [Requirements](#requirements)
- [Running locally (Windows)](#running-locally-windows)
- [Running on GitHub Actions](#running-on-github-actions)
- [Limitations and tradeoffs](#limitations-and-tradeoffs)
- [Future work](#future-work)
- [Repository structure](#repository-structure)
- [Troubleshooting](#troubleshooting)

---

## What it does

- Pulls 1h / 4h / 1d candles for a configurable set of Binance pairs
  and persists them to SQLite.
- Computes a multi-factor buy score (drop magnitude with ATR
  normalization, RSI, relative volume, support distance, discount
  from highs, reversal confirmation, trend context) and emits a
  signal when the score crosses an emit threshold.
- Optionally classifies a market regime from BTC daily candles
  (`risk_on` / `neutral` / `risk_off`) and shifts the emit threshold
  accordingly.
- Parks borderline scores on a watchlist that promotes them into a
  real signal if the score later crosses the emit floor.
- Lets the user record manual buys, monitors them against four sell
  rules (stop-loss, trailing stop, take-profit, context
  deterioration), and dispatches advisory sell alerts.
- Evaluates each matured signal/buy after 30 days: returns at 24h /
  7d / 30d, MFE / MAE with timing, verdict.
- Aggregates evaluated signals into expectancy / win-rate / profit
  factor / MFE-MAE analytics, on demand from the CLI and as a digest
  in the weekly ntfy summary.
- Sends Portuguese-language ntfy notifications with quiet-hours
  queueing, per-symbol cooldown, and severity-based escalation.
- Exposes a read-only **FastAPI** adapter and a local **Next.js**
  dashboard (six pages: Overview, Signals, Watchlist, Sell monitor,
  Analytics, Reports).

## What it does NOT do

- **No automatic order execution.** Sell signals are advisory; the
  user decides whether and how to act.
- **No exchange-trading API integration.** No exchange credentials,
  no order endpoints, no balance lookup. The only Binance call is
  the public market-data endpoint.
- **No machine-learning models.** Every score and rule is rule-based
  and explicit in the codebase.
- **No authenticated multi-user web app.** The dashboard is local
  read-only with no auth, no sessions, no roles.
- **No write actions from the dashboard.** Recording buys, marking
  sales, and editing config still live on the CLI.
- **No realtime streaming.** Notifications come from the scheduled
  scan; the dashboard polls the API.

---

## Feature set

### Market data ingestion
- Per-symbol incremental ingest of 1h / 4h / 1d candles from
  `data-api.binance.vision`.
- Cold-start bootstrap of ~250 candles per (symbol, interval); next
  runs only fetch what's new.
- Per-interval retention caps prune old candles during daily
  maintenance.

### Buy-signal scoring
Seven additive factors, weights validated to sum to 100:

| Factor | Default weight |
|---|---|
| `drop_magnitude` (multi-horizon, ATR-normalized) | 25 |
| `rsi_oversold` (1h + 4h) | 20 |
| `relative_volume` | 15 |
| `support_distance` | 15 |
| `discount_from_high` (30d + 180d) | 10 |
| `reversal_pattern` | 10 |
| `trend_context` | 5 |

Reversal confirmation is additive within its 10-point cap:
candlestick pattern (+5), RSI recovery from oversold (+3), close
reclaiming a recent prior high (+2). An optional bullish-divergence
sub-signal (+2) is gated behind
`scoring.thresholds.divergence_enabled` (default `false`); when on,
the cap still applies, so the factor never grows past its budget.

### Regime awareness
- Optional BTC-daily classifier (EMA20 / EMA50 alignment + ATR
  percentile) producing `risk_on` / `neutral` / `risk_off`.
- Latest snapshot is stamped on every emitted signal as
  `signals.regime_at_signal` and persisted in `regime_snapshots`.
- The emit threshold is shifted by `threshold_adjust_risk_on` /
  `threshold_adjust_risk_off`. Severity tier boundaries are
  unchanged — only the emit floor moves.
- BTC candles are auto-seeded for ingestion when the regime feature
  is enabled, even if BTCUSDT isn't in `[symbols].tracked`.

### Sell-side monitoring
Sell engine evaluates every open buy each scan cycle. Four rules in
priority order:

1. `stop_loss` — `current <= entry × (1 - stop_loss_pct/100)`.
2. `trailing_stop` — needs a prior watermark; fires when
   `current <= watermark × (1 - trailing_stop_pct/100)`.
3. `take_profit` — `current >= entry × (1 + take_profit_pct/100)`.
4. `context_deterioration` — regime is `risk_off`, position is at a
   loss, and the flag is enabled.

Per-`(symbol, buy_id)` watermarks track post-entry highs and are
updated **after** evaluation so the trailing-stop rule always sees
the prior peak. Per-(buy, rule) cooldown prevents spam. Each fired
signal lands in `sell_signals` and ships a Portuguese-language ntfy
alert.

### Watchlist
- Borderline scores (between `floor_score` and the emit threshold)
  are tracked in the `watchlist` table.
- One active row per symbol, enforced by a partial unique index on
  `status='watching'`.
- Lifecycle: **WATCH** (insert / refresh) → **PROMOTE** (score
  crossed the emit threshold; a real signal is produced and linked
  via `signals.watchlist_id`) → **EXPIRE** (score dropped below
  floor) or **stale-expire** (no refresh inside `max_watch_hours`).
- Resolved rows stay in the table as an audit trail.

### Evaluations
- **Signal evaluations**: 24h / 7d / 30d returns, max-gain / max-loss
  over the 7-day post-signal window, time-to-MFE / time-to-MAE in
  hours, verdict.
- **Buy evaluations**: hourly-resolution intraday low for the buy
  day, 7d / 30d returns, MFE / MAE / timing over the 7-day post-buy
  window, verdict.
- 30-day maturation window, idempotent — re-running maintenance is
  a safe no-op.

### Analytics
- Pure aggregator (`compute_expectancy`) returns total signals, an
  overall bucket, and slicings by `severity`, `regime_at_signal`,
  score band (`50-64` / `65-79` / `80-100`), and
  `dominant_trigger_timeframe`.
- Per bucket: count, win rate, expectancy, profit factor, average
  win / loss, MFE / MAE averages, time-to-MFE / time-to-MAE
  averages.
- `min_signals` filter (default 5) drops sliced buckets below the
  threshold; the overall bucket is always present.

### Notifications
- ntfy POST with severity → priority mapping
  (`normal/strong/very_strong → default/high/max`).
- Portuguese client copy with friendly asset names, top-3 reason
  lines, regime annotation, and a one-line interpretation.
- Optional debug mode appends raw indicator dumps after a `--- debug
  ---` separator.
- Quiet-hours queue: alerts produced inside the configured local
  window are written with `queued=1` and flushed on the next
  post-quiet scan; `very_strong` bypasses and sends immediately.
- Sell signals use a dedicated formatter so the buy-side
  notification UX is unaffected. Non-ASCII titles (e.g. the weekly
  em-dash, sell-side accented Portuguese) are RFC-2047 encoded
  before reaching the HTTP layer so `requests` never refuses a
  header.

### CLI
One entry point (`python -m crypto_monitor.cli`) with subcommands
for setup, scan, weekly summary, maintenance, manual buy/sell
recording, and signal/sell/watchlist/analytics inspection. See the
[CLI reference](#cli-reference).

### Local scheduler mode
Three Windows Task Scheduler tasks driven by wrapper scripts under
`scripts\` (each `cd`s into the project root via `%~dp0` and uses
`.venv\Scripts\python.exe` if available).

### GitHub Actions mode
Four scheduled workflows operate on an encrypted SQLite DB stored in
a dedicated `state` git branch (AES-256-CBC + PBKDF2 via
`scripts/gha_state.sh`). A shared `crypto-state` concurrency group
serializes writers.

### Dashboard
Read-only, local-first. FastAPI adapter (`crypto_monitor.dashboard`)
exposes 12 GET endpoints; a Next.js 14 app under `dashboard/`
consumes them. Six pages: Overview, Signals (with detail view),
Watchlist, Sell monitor, Analytics, Reports. See the
[Dashboard](#dashboard) section.

---

## Architecture

The application is split into focused packages, each with a small
public surface:

| Module | Responsibility |
|---|---|
| `crypto_monitor/config/` | TOML + `.env` loader, frozen settings dataclasses (`ScoringSettings`, `RegimeSettings`, `SellSettings`, `WatchlistSettings`, …). |
| `crypto_monitor/database/` | Connection helper (WAL, busy_timeout, foreign keys), baseline schema, incremental migration runner, retention helpers. |
| `crypto_monitor/binance/` | Lean HTTP client exposing only `get_klines`. |
| `crypto_monitor/ingestion/` | Per-symbol incremental candle ingest with `UNIQUE(symbol, interval, open_time)` dedup. |
| `crypto_monitor/indicators/` | Pure indicator helpers — RSI / RSI series, ATR, EMA + trend label, support detection, candlestick patterns + reversal helpers, bullish divergence, relative volume. |
| `crypto_monitor/regime/` | BTC EMA + ATR-percentile classifier, snapshot store. |
| `crypto_monitor/signals/` | `score_signal` engine, factor scorers, dedup-aware persistence. |
| `crypto_monitor/buys/` | Manual buy ledger (`insert_buy`, `list_buys`, `count_buys`). |
| `crypto_monitor/sell/` | Pure rule evaluator + watermark store + scan-time runtime + ntfy dispatch. |
| `crypto_monitor/watchlist/` | Store + pure state machine (`decide_watch_action`). |
| `crypto_monitor/evaluation/` | Matured signal/buy evaluation incl. MFE/MAE timing; verdict mapping. |
| `crypto_monitor/analytics/` | Pure expectancy aggregator, scope-filtered loader, formatters used by both CLI and weekly digest. |
| `crypto_monitor/notifications/` | ntfy HTTP sender (with RFC-2047 header encoding), alert policy (cooldown / escalation / quiet hours), buy-side and sell-side formatters, queue + dispatch. |
| `crypto_monitor/reports/` | Weekly summary generation, persistence, ntfy send. |
| `crypto_monitor/scheduler/` | `run_scan` / `run_maintenance` / `run_weekly` orchestrators. |
| `crypto_monitor/cli/` | argparse front-end + per-subcommand handlers. |
| `crypto_monitor/dashboard/` | **Read-only FastAPI adapter.** Pydantic schemas, services, dependencies. Optional extra (`pip install -e ".[dashboard]"`); the bot's runtime never imports it. |
| `dashboard/` | **Next.js 14 frontend.** TypeScript + Tailwind + TanStack Query + Recharts. Talks only to the FastAPI adapter — never to SQLite directly. |

Every cross-layer call is dependency-injected (settings, DB
connection, ntfy sender, clock) so the orchestrators are
exhaustively tested against in-memory SQLite without touching the
network. The main test suite has **593 tests**.

---

## Data model

All bot state lives in one SQLite file. Key tables:

| Table | Purpose |
|---|---|
| `schema_meta` | Schema version + first-init timestamp. Migrations bump the value. |
| `symbols` | Symbols the scanner is allowed to ingest / score. |
| `candles` | Closed OHLCV per `(symbol, interval, open_time)`. |
| `signals` | Emitted buy signals — score, severity, drops, RSI, volume, support, regime, optional `watchlist_id` link. |
| `signal_evaluations` | One row per signal once 30 days have elapsed: 24h/7d/30d returns, 7-day MFE / MAE + timing, verdict. |
| `notifications` | ntfy dispatch log: queued / sent / retry state, per-attempt errors. |
| `buys` | Manual buy ledger; `sold_at` / `sold_price` / `sold_note` mark closed positions. |
| `buy_evaluations` | Matured buy outcomes — hourly intraday low, 7d/30d returns, MFE/MAE + timing, verdict. |
| `regime_snapshots` | One row per scan when the regime feature is enabled. |
| `sell_tracking` | Per-`(symbol, buy_id)` post-entry high watermark used by the trailing-stop rule. |
| `sell_signals` | Append-only log of fired sell rules: rule, severity, reason, P&L%, regime, alerted flag. |
| `watchlist` | One active row per symbol (partial unique index) plus an audit trail of resolved rows. |
| `weekly_summaries` | Persisted weekly digest body + structured fields (signal counts, top drop, sent flag). |
| `processing_state` | Generic key/value scratch space for ingestion + maintenance phases. |

The full DDL lives in
[`crypto_monitor/database/schema.py`](crypto_monitor/database/schema.py).
Five migrations have been applied in order (regime → sell → watchlist → eval timing).

---

## Buy-signal logic

Per scan cycle, for each tracked symbol:

1. Load up to 250 candles per `(symbol, interval)` for `1h`, `4h`, `1d`.
2. Compute the seven factors:
   - **Drop magnitude** — multi-horizon drop (1h, 24h, 7d, 30d, 180d).
     ATR-normalized when `atr(1h)` is available (raw drop divided by
     `atr_pct = atr / price * 100` before tier lookup), with a
     bit-for-bit v1 fallback when ATR isn't available.
   - **RSI oversold** — 1h + 4h RSI tiers, additive and capped.
   - **Relative volume** — last 1h volume vs. 20-bar average.
   - **Support distance** — heuristic swing-low support; closer
     scores higher.
   - **Discount from high** — distance below the 30d / 180d high.
   - **Reversal confirmation** — additive: candlestick pattern (+5),
     RSI recovery (+3), high reclaim (+2), optional bullish
     divergence (+2 when `divergence_enabled = true`); capped at 10.
   - **Trend context** — 1d trend label rewards buying dips inside
     a rising market.
3. Map total to a severity tier (`normal` / `strong` / `very_strong`).
4. Apply the regime threshold adjustment to the **emit floor only**
   (tier boundaries unchanged).
5. Insert the row when severity is non-None, with rule-driven dedup
   against existing rows for the same `(symbol, candle_hour)`.

When the watchlist feature is enabled and the regular emit declined,
the watchlist state machine takes over (see [Watchlist](#watchlist)).

---

## Sell logic

The sell engine runs each scan when `[sell].enabled = true`. For
each open buy:

1. Fetch the latest 1h close as the current price (best-effort —
   may be up to ~60 minutes stale).
2. Read the **prior** watermark from `sell_tracking`.
3. Call the pure evaluator with `(buy, current_price,
   prior_high_watermark, regime_label, settings, now)`.
4. Evaluator returns one `SellSignal` or `None` based on the
   priority order: `stop_loss > trailing_stop > take_profit >
   context_deterioration`.
5. If a signal fires AND the per-`(buy, rule)` cooldown elapsed:
   insert + send + flip `alerted=1`. Cooldown-suppressed signals
   are neither inserted nor sent.
6. Update the watermark to `max(buy.price, current_price)`. Stored
   value is monotone — never lowered by a falling price.

Severity → ntfy priority: `high → max`, `medium → high`. **Sell
signals are advisory.** The user records the actual sale through
`sell record`, which writes the `sold_*` columns on the buy and
removes it from future evaluations.

---

## Watchlist

When `[watchlist].enabled = true` and the buy-signal path returned
`severity is None`, the state machine decides:

- **PROMOTE** — `score >= min_signal_score` (the **base** value, not
  the regime-adjusted floor). Synthesizes a severity from the
  `[scoring.severity]` ladder, threads `watchlist_id` into the
  candidate, and runs the normal `insert_signal` path. On a
  successful insert, the watch row transitions to `status='promoted'`
  with `resolution_reason='promoted'` and stamps `promoted_signal_id`.
- **WATCH** — `floor_score <= score < min_signal_score`. Inserts (or
  refreshes) the active row; `expires_at` rolls forward to
  `now + max_watch_hours`.
- **EXPIRE** — `score < floor_score` and an active watch exists.
  Transitions to `status='expired'` with
  `resolution_reason='expired_below_floor'`.
- **IGNORE** — `score < floor_score` and no active watch. No-op.

Once per scan cycle, `expire_stale` transitions every
`status='watching'` row whose `expires_at <= now`, even for symbols
not currently scoring.

---

## Evaluations and analytics

### Evaluations

Maintenance (`evaluate`) walks every signal/buy older than 30 days
that has no row in `signal_evaluations` / `buy_evaluations` and
computes the price horizons + MFE / MAE / timing + verdict. NULL
columns (insufficient post-event candles) are surfaced explicitly,
not silently zeroed.

### Analytics

The analytics aggregator (`crypto_monitor.analytics.compute_expectancy`)
is **pure** — it accepts a list of dicts shaped like the
`signal_evaluations ⨝ signals` join and returns an `ExpectancyReport`
with no DB or I/O. The CLI loads rows via `load_evaluation_rows`,
optionally scope-filtered (`all` / `90d` / `30d`); the weekly report
does the same with a 90-day window.

The weekly summary appends a one-line digest (`📈 Análise (90d)`)
when at least 5 matured rows exist in the window, and a `Análise:
dados insuficientes` line otherwise — the section header always
appears so users notice the feature.

---

## Notifications

- **ntfy** is the only outbound integration. Priority and
  quiet-hours behavior live in
  `crypto_monitor/notifications/policy.py`.
- **Buy-side body**: friendly asset name, current price, 24h
  variation, top-3 reason lines, optional regime annotation,
  severity-driven decision phrase.
- **Sell-side body**: dedicated formatter with rule-specific
  headline (`🔴 Stop-loss acionado` / `🟠 Trailing stop` / `🟢
  Take-profit` / `🟡 Contexto deteriorando`), price, signed P&L,
  reason, optional regime line.
- **Weekly body**: signal count + severity breakdown, top drop of
  the week, buy count, matured-verdict histogram, conclusion line,
  optional analytics digest.
- **Debug mode** (`[ntfy].debug_notifications = true`) appends a raw
  data block after a `--- debug ---` separator.
- **Quiet hours**: alerts inside `[alerts].quiet_hours_*` (local
  time) are queued and flushed after the window;
  `very_strong` bypasses.

All notifications are advisory. The bot does not act on its own
output.

---

## Dashboard

Two processes; the browser never touches SQLite.

```
┌───────────────────────────┐        HTTP/JSON        ┌──────────────────────────┐
│  FastAPI read-only API    │ ◀─────────────────────▶ │  Next.js dashboard       │
│  uvicorn …:8787           │                         │  next dev :3000          │
└───────────────┬───────────┘                         └──────────────────────────┘
                │ existing readers
                ▼
┌───────────────────────────┐
│  data/crypto_monitor.db   │   (WAL — bot writes + dashboard reads concurrent)
└───────────────────────────┘
```

### FastAPI adapter (`crypto_monitor/dashboard/`)

Read-only Python adapter over the bot's existing reader functions.
Twelve GET endpoints under `/api/`:

| Path | What it returns |
|---|---|
| `/api/health` | DB liveness + `schema_version` + latest 1h candle close (freshness probe). |
| `/api/overview` | Dashboard home: KPIs, regime, 90-day analytics digest, recent-activity feed. |
| `/api/signals` | Filterable + paginated signals list (`symbol`, `severity`, `regime`, `from`, `to`, `limit`, `offset`). |
| `/api/signals/{id}` | One signal joined with its evaluation row. |
| `/api/watchlist` | Active watching rows. |
| `/api/open-buys` | Open buys + watermark + latest close + PnL + drawdown. |
| `/api/buys` | All buys (filterable by `status=open\|sold\|all`, `symbol`, paginated). |
| `/api/sell-signals` | Filterable + paginated sell signals. |
| `/api/analytics` | `compute_expectancy` output (`scope=all\|90d\|30d`, `min_signals`). |
| `/api/weekly-summaries` | Recent weekly summaries (with body). |
| `/api/regime/latest` | Most recent regime snapshot, or `null`. |
| `/api/regime/history` | Recent regime snapshots (timeline). |

Every response is `{"data": ..., "meta": {...}}`. List endpoints put
pagination (`total`, `limit`, `offset`, `next_offset`) in `meta`.
Auto-generated docs at `http://127.0.0.1:8787/api/docs`.

WAL mode lets the bot scan and the API read concurrently. If a scan
briefly locks the DB the API returns 503; the dashboard's frontend
retries cleanly on the next poll.

### Next.js frontend (`dashboard/`)

Six read-only pages, all consuming the FastAPI adapter via TanStack
Query (`lib/queries.ts`):

| Route | What it shows |
|---|---|
| `/` | Overview — KPI strip, latest regime, 90-day analytics digest, merged recent-activity feed. Polls every 30 s. |
| `/signals` | Filterable + paginated signals table. URL-driven filters (`?symbol=&severity=&regime=&from=&to=&offset=`). |
| `/signals/[id]` | One signal — core facts, evaluation block (when matured), parsed `score_breakdown`, clean 404 panel when absent. |
| `/watchlist` | Active borderline-score watches awaiting promotion or expiry. |
| `/sell` | Sell monitor — open buys (price / watermark / PnL / drawdown) + paginated recent sell signals. |
| `/analytics` | Expectancy aggregator UI. Scope picker (`all` / `90d` / `30d`), MFE / MAE KPIs, four bar-chart breakdowns. |
| `/reports` | Recent weekly summaries — left list, selected body in `<pre>`. |

Layout-matching skeletons keep first-paint stable; an API-down state
renders a friendly retry panel.

The dashboard is **local-first** by design: a single permanent dark
theme, no auth, bound to `127.0.0.1` on both ends. The architecture
is **Vercel-ready** — the contract between the frontend and the API
is a stable JSON shape, and the frontend reads the API URL from
`NEXT_PUBLIC_API_BASE_URL`. Cloud deployment is not implemented;
moving the frontend to Vercel later requires only setting that env
var to the deployed API URL and adding an auth layer to the API.

See [`dashboard/README.md`](dashboard/README.md) for the full folder
layout.

---

## CLI reference

Every command accepts a global `--project-root <path>` flag (default:
the current working directory). Exit codes: `0` on success, `1` on
runtime error, `2` on argparse usage error.

| Command | What it does |
|---|---|
| `init [--no-seed]` | Copy `config.example.toml` → `config.toml` if missing and initialize the SQLite database. Optionally seeds tracked symbols. Idempotent. |
| `scan` | One scan cycle: flush queued notifications, classify regime (if enabled), ingest new candles, score every active symbol (with watchlist branch when configured), evaluate open buys (sell engine), dispatch alerts. |
| `weekly` | Generate the weekly summary, persist it in `weekly_summaries`, push it via ntfy. Returns 0 when persisted, even if delivery failed (the row keeps `sent=0` for retry); a stderr warning surfaces real delivery failures. |
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
- **For the dashboard:** Node.js 20 + npm.

No database server, no Docker, no admin rights.

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

(Optional) install the dashboard adapter extra if you plan to run
the FastAPI server:

```
pip install -e ".[dashboard]"
```

### 4. Initialize the project

```
python -m crypto_monitor.cli init
```

Copies `config.example.toml` → `config.toml` (only if missing),
creates the SQLite database, runs all migrations, seeds tracked
symbols. Pass `--no-seed` to skip seeding.

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

The first run cold-starts ~250 candles per (symbol, interval).

### 8. (Optional) Register the scheduled tasks

```
scripts\register_tasks.cmd
```

Three Windows Task Scheduler jobs are created at user privilege:

| Task | Schedule | Command |
|---|---|---|
| `crypto_monitor scan` | every 5 minutes | `python -m crypto_monitor.cli scan` |
| `crypto_monitor maintenance` | daily 03:00 local | `python -m crypto_monitor.cli evaluate` |
| `crypto_monitor weekly` | Sunday 09:00 local | `python -m crypto_monitor.cli weekly` |

`scripts\unregister_tasks.cmd` tears them down. Re-running
`register_tasks.cmd` is idempotent.

### 9. Run the dashboard (optional)

Two processes side by side:

```
:: Terminal 1 — backend (FastAPI adapter)
scripts\dashboard.cmd
:: or directly:
:: uvicorn crypto_monitor.dashboard.api:app --reload --port 8787

:: Terminal 2 — frontend
cd dashboard
npm install                                 :: one-time
copy .env.local.example .env.local          :: one-time
npm run dev
```

Open <http://localhost:3000>. The dashboard ships six pages
(Overview, Signals + detail, Watchlist, Sell monitor, Analytics,
Reports).

For one-shot CI-style validation:

```
cd dashboard
npm run check        :: typecheck + production build
```

If the API isn't running, every page renders a friendly "Cannot
reach the API" panel with the exact `uvicorn` command.

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
accumulates binary history. A shared `crypto-state` concurrency
group ensures only one workflow writes at a time.

### Requirements

- A GitHub repository (public repos get unlimited free Actions
  minutes; private repos get 2,000 min/month).
- An ntfy topic.
- Two repository secrets:

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | Your ntfy topic name. |
| `STATE_ENCRYPTION_KEY` | A long random key, e.g. `openssl rand -base64 32`. |

### One-time setup

```bash
git remote add origin https://github.com/<you>/crypto_monitor.git
git push -u origin main

git checkout --orphan state
git rm -rf .
git commit --allow-empty -m "state: initial"
git push origin state
git checkout main
```

Set the two secrets under **Settings > Secrets and variables >
Actions**, then trigger **Actions > scan > Run workflow** to
bootstrap. After that the cron schedules take over.

### Workflows

| Workflow | File | Schedule | What it does |
|---|---|---|---|
| **scan** | `.github/workflows/scan.yml` | every 5 minutes | Pre-flight tests, ingest, regime, score, watchlist, sell, dispatch alerts. |
| **maintenance** | `.github/workflows/maintenance.yml` | daily 06:00 UTC | Evaluate matured signals + buys, prune candles. |
| **weekly** | `.github/workflows/weekly.yml` | Sunday 12:00 UTC | Generate and send the weekly summary. |
| **buy-add** | `.github/workflows/buy-add.yml` | manual only | Record a buy via the GitHub UI. |

All workflows accept `workflow_dispatch` triggers. The scan workflow
runs the full pytest suite as a pre-flight; dashboard tests skip
cleanly when fastapi isn't installed.

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

### Caveats

- **Cron jitter**: free-tier cron has 5–20 minute jitter; actual
  scan cadence is ~8–15 minutes. Scoring depends on hourly candle
  closes, not exact scan timing, so this is fine.
- **Skipped runs**: under heavy GitHub load a scheduled run may not
  fire. Ingestion is incremental — the next run catches up.
- **Minutes budget (private repos)**: at `*/5` cadence, scans use
  ~5,700 min/month — over the 2,000-minute free quota. Use a public
  repo or change the cron to `*/15` (~1,900 min/month).
- **Quiet hours**: the policy reads UTC and converts to the
  configured timezone internally — runner timezone is irrelevant.
- **Back up `STATE_ENCRYPTION_KEY` somewhere safe.** Without it the
  encrypted DB is unrecoverable; GitHub cannot help.

---

## Limitations and tradeoffs

- **Advisory only.** The system flags decisions but never executes
  them. Confirming a sell, recording a buy, or acting on a signal is
  always manual.
- **GitHub Actions schedule jitter.** 5–20 minute jitter on the free
  tier; cadence is approximate, not real-time.
- **Hourly resolution.** Every price calculation is bounded by 1h
  candles. Intraday moves between candle closes are invisible.
- **Analytics need history.** With a 30-day maturation window, a
  fresh install needs ~5+ weeks before `analytics summary` shows
  non-trivial buckets, and ~3 months before the weekly digest stops
  saying "dados insuficientes".
- **Watchlist + regime warm up.** Both features become meaningful
  after several scan cycles. `decide_watch_action` and
  `_classify_regime` return inert outputs without history.
- **Sell page price is stale.** "Current price" on the sell monitor
  is the latest 1h candle close; up to ~60 minutes old. The UI
  surfaces `latest_close_at` so the staleness is explicit.
- **No backfill of old signals.** The scoring engine only operates
  on the latest closed 1h candle; you can't replay history to
  retroactively generate signals.
- **No multi-exchange.** Binance public market data only.
- **Dashboard is local-only.** No auth, no cloud deployment, no
  websockets. Bound to `127.0.0.1` on both API and frontend.

---

## Future work

Explicitly **not** implemented today:

- **Hosted dashboard deployment.** The architecture is Vercel-ready
  (frontend reads `NEXT_PUBLIC_API_BASE_URL`, API contract is
  stable), but cloud deployment requires an auth layer first.
- **Auth.** Single-user only today. Going multi-user / off-localhost
  needs at minimum a static bearer token on the API.
- **Write actions.** The dashboard is read-only; future write
  endpoints (record buy, mark sold) would go via a new write router
  on the API rather than client-side SQLite.
- **Postgres migration.** Possible if the project ever needs
  multi-tenant or hosted serverless deployment. The reader/writer
  split is already centralized in the per-domain `*/store.py`
  modules, so dialect changes would be localized.
- **Paper trading.** A simulated-fills layer that scores hypothetical
  strategies against historical candles, feeding back into the
  analytics aggregator.
- **Exchange execution layer.** Broker-API integration would be
  opt-in, isolated behind a feature flag, and only built if the
  manual flow proves consistently profitable.
- **ML.** Only if a dataset of evaluated signals ever justifies
  replacing rule-based scoring. Not on the near horizon.

---

## Repository structure

```
crypto_monitor/
├── analytics/        expectancy aggregator + reporter + scope-filtered loader
├── binance/          lean HTTP client (get_klines only)
├── buys/             manual buy ledger
├── cli/              argparse front-end + handlers
├── config/           settings loader + frozen dataclasses
├── dashboard/        FastAPI adapter (Pydantic schemas + services + deps)
├── database/         schema + migrations + retention + connection helper
├── evaluation/       matured signal/buy evaluation, MFE/MAE timing, verdicts
├── indicators/       RSI, ATR, EMA/trend, support, candlestick patterns,
│                     bullish-divergence, volume
├── ingestion/        per-symbol incremental candle ingest
├── notifications/    ntfy sender (RFC-2047), policy, queue, formatters
├── regime/           BTC EMA + ATR percentile classifier + snapshot store
├── reports/          weekly summary generation + persistence + send
├── scheduler/        run_scan / run_maintenance / run_weekly orchestrators
├── sell/             pure evaluator + watermark store + scan-time runtime
├── signals/          score_signal engine + factor scorers + dedup persistence
├── utils/            time / ISO helpers
└── watchlist/        store + pure state machine

dashboard/
├── app/              Next.js routes (Overview / Signals[+detail] / Watchlist
│                     / Sell / Analytics / Reports / 404)
├── components/       Nav, KPI cards, table primitives, charts, skeletons,
│                     pills, page header, empty/error states
├── lib/              api.ts, queries.ts, types.ts, format.ts, styles.ts
├── package.json
├── tailwind.config.ts / postcss.config.mjs
└── next.config.mjs / tsconfig.json

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
├── dashboard.cmd          launch the FastAPI adapter locally
└── gha_state.sh           GitHub Actions encrypted-state helper
.github/workflows/
├── scan.yml
├── maintenance.yml
├── weekly.yml
└── buy-add.yml
tests/                   pytest suite (593 tests against in-memory SQLite)
```

`data\` and `logs\` are created on demand. Both, plus `.env` and
`config.toml`, are gitignored.

---

## Troubleshooting

### Missing `NTFY_TOPIC`

**Symptom:** `python -m crypto_monitor.cli ntfy-test` exits 1 with
`reason=missing_topic`. A scan succeeds but `process_pending_signals`
reports no sends.

**Fix:**

1. Create `.env` from `.env.example` if you haven't already.
2. Put your topic in it: `NTFY_TOPIC=your-topic-here`.
3. Re-run `python -m crypto_monitor.cli ntfy-test`.

The wrappers `cd` into the project root, so they pick up `.env`
automatically — you don't need to set the variable system-wide.

### Binance / network errors

`scan` reports `errors=N` on its summary line; `logs\scan.cmd.log`
contains tracebacks mentioning `requests`, `Connection`, or HTTP
status codes. Ingestion failures are isolated per phase — the rest
of the scan still runs.

- Check connectivity to `data-api.binance.vision`.
- Look at the actual error in the log. A 451 / 403 may mean Binance
  is blocking your region — switch `[binance].base_url` in
  `config.toml`.
- Bump `[binance].request_timeout` and `[binance].retry_count` if
  your connection is flaky.

### Quiet-hours queue behavior

A scan during the night reports `queued=N` instead of `sent=N`;
notifications arrive in a burst when quiet hours end. **This is by
design.** `very_strong` signals **bypass** quiet hours; raise
`[scoring.severity].very_strong` if you don't want that.

### Failed notifications

`notifications.delivered = 0` and `notifications.last_error` is set;
`scan` reports `failed=N`. Look at `logs\scan.cmd.log` for the
status code from the retry attempts. 4xx from ntfy usually means a
typo in the topic; 5xx / network failures retry with exponential
backoff up to `[ntfy].max_retries` and stay queued.

### Dashboard can't reach the API

The frontend renders a "Cannot reach the API" panel with the exact
`uvicorn` command. Run that command in another terminal (or
`scripts\dashboard.cmd`). If the page returns 503 transiently while
a scan is mid-write, the next poll clears it — that's WAL behavior,
not a bug.

### Database / log file locations

If `init` ran successfully:

```
data\crypto_monitor.db          SQLite database
logs\scan.cmd.log               scheduled scan output
logs\weekly.cmd.log             scheduled weekly output
logs\maintenance.cmd.log        scheduled evaluate output
```

Override `[general].db_path` and `[general].log_dir` in
`config.toml` if you want them somewhere else. Both paths resolve
relative to the project root.

To wipe everything and start over:

```
rmdir /S /Q data
del config.toml
python -m crypto_monitor.cli init
```

This drops the database and the local config but leaves your `.env`
and `logs\` untouched. Your buys and signals live in the database,
so back up `data\crypto_monitor.db` first if you care about them.
