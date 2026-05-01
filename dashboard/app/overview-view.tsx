"use client";

import { ActivityFeed } from "@/components/activity-feed";
import { ErrorState } from "@/components/error-state";
import { KpiCard } from "@/components/kpi-card";
import { LoadingState } from "@/components/loading-state";
import { PageHeader } from "@/components/page-header";
import { RegimeChip } from "@/components/regime-chip";
import { formatPercent } from "@/lib/format";
import { useOverview } from "@/lib/queries";

/**
 * Dashboard home — the only page wired in Step 3.
 *
 * One API call (/api/overview) drives every widget on this page; the
 * regime chip reads from the same payload (`regime: OverviewRegime |
 * null`) so a fresh install renders coherently.
 */
export function OverviewView() {
  const q = useOverview();

  if (q.isPending) {
    return <LoadingState />;
  }
  if (q.isError) {
    return <ErrorState error={q.error as Error} retry={() => q.refetch()} />;
  }
  const d = q.data;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Overview"
        subtitle="Live counts + 90-day analytics digest, refreshed every 30 s."
        actions={<RegimeChip regime={d.regime} />}
      />

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          Recent activity counts
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
          <KpiCard label="Signals 24h" value={d.signals_24h} />
          <KpiCard label="Signals 7d" value={d.signals_7d} />
          <KpiCard label="Watchlist active" value={d.watchlist_active} />
          <KpiCard label="Open buys" value={d.open_buys} />
          <KpiCard label="Sell signals 7d" value={d.sell_signals_7d} />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          Analytics ({d.analytics.scope})
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard
            label="Total signals"
            value={d.analytics.total_signals}
            hint={d.analytics.total_signals < 5 ? "needs more data" : null}
          />
          <KpiCard label="Win rate" value={formatPercent(d.analytics.win_rate)} />
          <KpiCard
            label="Expectancy"
            value={formatPercent(d.analytics.expectancy, true)}
          />
          <KpiCard
            label="Profit factor"
            value={
              d.analytics.profit_factor !== null
                ? d.analytics.profit_factor.toFixed(2)
                : "—"
            }
          />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          Recent activity
        </h2>
        <ActivityFeed items={d.recent_activity} />
      </section>
    </div>
  );
}
