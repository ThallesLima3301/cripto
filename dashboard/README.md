# crypto_monitor dashboard

Read-only Next.js dashboard for the `crypto_monitor` bot. Six pages
sit on top of the FastAPI adapter (`crypto_monitor.dashboard.api`),
which is itself a thin reader over the bot's existing SQLite store.
The frontend never reaches SQLite directly.

## Stack

- Next.js 14 (App Router) + TypeScript (strict mode)
- Tailwind CSS for styling — single permanent dark theme.
- TanStack Query v5 for fetching + cache.
- Recharts for the analytics page.

## Pages

| Route | Purpose |
|---|---|
| `/` | Overview — KPI strip + 90-day analytics digest + recent activity feed. Polls `/api/overview` every 30 s. |
| `/signals` | Filterable + paginated table of buy signals. URL-driven filters (symbol / severity / regime / from / to / offset). |
| `/signals/[id]` | One signal: core facts, evaluation block (when matured), parsed `score_breakdown`. Returns a clean 404 panel when absent. |
| `/watchlist` | Active borderline-score watches (one row per `status='watching'`). |
| `/sell` | Sell monitor — open buys (price, watermark, PnL, drawdown) plus paginated recent sell signals. |
| `/analytics` | Expectancy aggregator UI. Scope picker (`all` / `90d` / `30d`). Overall + MFE/MAE KPIs + four bar-chart breakdowns. |
| `/reports` | Recent weekly summaries — left list, selected body in `<pre>`. |

The dashboard is **read-only**. There are no write endpoints, no
auth, and no realtime updates.

## Setup

From the repo root:

```bash
cd dashboard
npm install
cp .env.local.example .env.local
```

Edit `.env.local` if your API runs on a non-default URL — the default
`NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8787` matches
`scripts/dashboard.cmd`.

## Run locally

Two processes side by side.

```bash
# Terminal 1 — backend (Python venv with `[dashboard]` extra installed)
pip install -e ".[dashboard]"             # one-time
scripts\dashboard.cmd                     # or: uvicorn crypto_monitor.dashboard.api:app --port 8787

# Terminal 2 — frontend
cd dashboard
npm run dev                               # http://localhost:3000
```

Open <http://localhost:3000>.

If the API isn't running, every page renders a clean "Cannot reach
the API" panel with the exact `uvicorn` command to start it. Locked
DB during a bot scan surfaces as 503 and the next refetch clears.

## Scripts

| script | purpose |
|---|---|
| `npm run dev` | Fast Refresh dev server on port 3000. |
| `npm run build` | Production build (also runs typecheck + emits `.next/`). |
| `npm run start` | Serve the production build. |
| `npm run typecheck` | `tsc --noEmit` — strict TS validation. |
| `npm run lint` | `next lint`. |
| `npm run check` | `typecheck && build` — combined smoke check used as CI. |

## Folder layout

```
dashboard/
├── app/
│   ├── layout.tsx           root layout: Nav + QueryProvider
│   ├── page.tsx             /             (server shell)
│   ├── overview-view.tsx    /             (client view)
│   ├── globals.css          Tailwind directives + base styles
│   ├── not-found.tsx        custom dark 404 panel
│   ├── signals/
│   │   ├── page.tsx         /signals      (filter bar + table)
│   │   └── [id]/page.tsx    /signals/[id] (detail + score_breakdown)
│   ├── watchlist/page.tsx   /watchlist
│   ├── sell/page.tsx        /sell
│   ├── analytics/page.tsx   /analytics    (scope selector + KPIs + 4 charts)
│   └── reports/page.tsx     /reports      (left list + selected body)
├── components/
│   ├── nav.tsx              top nav with active-route highlight
│   ├── page-header.tsx      title + subtitle + actions slot
│   ├── query-provider.tsx   TanStack Query client (client component)
│   ├── kpi-card.tsx         single KPI tile
│   ├── activity-feed.tsx    merged buy/sell event list
│   ├── regime-chip.tsx      risk_on / neutral / risk_off / null pill
│   ├── pill.tsx             generic uppercase status pill
│   ├── table.tsx            <Table> / <Th> / <Td> / <EmptyRow>
│   ├── pagination.tsx       Prev / Next strip
│   ├── empty-state.tsx      centered "no rows yet" panel
│   ├── error-state.tsx      friendly API-down panel with retry
│   ├── skeleton.tsx         pulsing rectangle primitive
│   ├── loading-state.tsx    overview-shaped skeleton
│   ├── table-loading-state.tsx     table-page skeleton (5 routes)
│   ├── analytics-loading-state.tsx analytics-page skeleton
│   └── charts/
│       ├── chart-card.tsx   wrapper card with built-in empty state
│       └── bucket-bar-chart.tsx
│                            generic Recharts bar over a slicing
├── lib/
│   ├── api.ts               fetch wrapper + ApiError + buildQuery
│   ├── queries.ts           per-endpoint TanStack Query hooks
│   ├── types.ts             wire-contract types matching the API
│   ├── styles.ts            color helpers (severity / regime / pnl)
│   └── format.ts            formatPercent / formatRelative / formatDateTime
├── package.json
├── tsconfig.json
├── next.config.mjs
├── tailwind.config.ts
└── postcss.config.mjs
```

## What this dashboard intentionally does **not** do

- No write actions (buy / sell / record). Those stay on the CLI.
- No direct DB access. Every byte goes through the FastAPI adapter.
- No business-logic re-implementation. Numbers come from the bot's
  existing readers and analytics aggregator.
- No real-time websockets. Pages poll on focus + a per-route
  staleTime (30 s for the overview, 5 min for analytics).
- No auth. The API is bound to `127.0.0.1`; anyone with shell access
  already has the SQLite file.
- No light-mode toggle. Single permanent dark theme — solo-dev
  friendly, no theme infrastructure.

## Validation

The repo uses a single combined check as a CI gate:

```bash
cd dashboard
npm run check
```

This runs `tsc --noEmit` followed by `next build`, exercising every
route end-to-end against the wire-contract types. There is no
heavyweight test runner — adding one is deferred.
