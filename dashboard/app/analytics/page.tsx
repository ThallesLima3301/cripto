"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { AnalyticsLoadingState } from "@/components/analytics-loading-state";
import { BucketBarChart } from "@/components/charts/bucket-bar-chart";
import { ChartCard } from "@/components/charts/chart-card";
import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { KpiCard } from "@/components/kpi-card";
import { PageHeader } from "@/components/page-header";
import { formatPercent } from "@/lib/format";
import { useAnalytics } from "@/lib/queries";
import type { AnalyticsBucket, AnalyticsData, AnalyticsScope } from "@/lib/types";

const SCOPES: readonly AnalyticsScope[] = ["all", "90d", "30d"] as const;
const SCORE_BUCKET_ORDER = ["50-64", "65-79", "80-100"] as const;
const SEVERITY_ORDER = ["normal", "strong", "very_strong"] as const;
const REGIME_ORDER = ["risk_on", "neutral", "risk_off"] as const;
const TRIGGER_ORDER = ["1h", "24h", "7d", "30d", "180d"] as const;

function readScope(sp: URLSearchParams): AnalyticsScope {
  const raw = sp.get("scope");
  return SCOPES.includes(raw as AnalyticsScope) ? (raw as AnalyticsScope) : "all";
}

export default function AnalyticsPage() {
  // Suspense boundary needed for static export — useSearchParams()
  // bails out of pre-rendering otherwise.
  return (
    <Suspense fallback={<AnalyticsLoadingState />}>
      <AnalyticsPageInner />
    </Suspense>
  );
}

function AnalyticsPageInner() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const scope = readScope(new URLSearchParams(sp.toString()));
  const q = useAnalytics(scope);

  function selectScope(next: AnalyticsScope) {
    const params = new URLSearchParams(sp.toString());
    if (next === "all") params.delete("scope");
    else params.set("scope", next);
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics"
        subtitle={
          <>
            Pure expectancy aggregator over matured signals. Numbers come
            from{" "}
            <code className="rounded bg-slate-800 px-1">/api/analytics</code>;
            the frontend only formats them.
          </>
        }
        actions={<ScopeSelector active={scope} onSelect={selectScope} />}
      />

      {q.isPending ? <AnalyticsLoadingState /> : null}
      {q.isError ? <ErrorState error={q.error as Error} retry={() => q.refetch()} /> : null}
      {q.data ? <AnalyticsBody data={q.data.data} /> : null}
    </div>
  );
}

// ---------- top-level body ----------

function AnalyticsBody({ data }: { data: AnalyticsData }) {
  const overall = data.overall;
  const insufficient =
    data.total_signals === 0 ||
    overall.count === 0 ||
    overall.win_rate === null;

  if (insufficient) {
    return (
      <EmptyState
        title="Dados insuficientes"
        message={
          data.total_signals === 0
            ? "No matured evaluations in this scope yet. Try widening to `all`, or wait until more signals mature (~30 days each)."
            : "There are signals in this window but none have evaluable returns yet."
        }
      />
    );
  }

  return (
    <>
      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          Overall ({data.total_signals} matured signals)
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard label="Total signals" value={overall.count} />
          <KpiCard label="Win rate" value={formatPercent(overall.win_rate)} />
          <KpiCard
            label="Expectancy"
            value={formatPercent(overall.expectancy, true)}
          />
          <KpiCard
            label="Profit factor"
            value={
              overall.profit_factor !== null
                ? overall.profit_factor.toFixed(2)
                : "—"
            }
            hint={overall.profit_factor === null ? "no losses in window" : null}
          />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          MFE / MAE
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard
            label="Avg MFE"
            value={formatPercent(overall.avg_mfe_pct, true)}
            hint="max favorable excursion (7d window)"
          />
          <KpiCard
            label="Avg MAE"
            value={formatPercent(overall.avg_mae_pct, true)}
            hint="max adverse excursion (7d window)"
          />
          <KpiCard
            label="Avg time to MFE"
            value={
              overall.avg_time_to_mfe_hours !== null
                ? `${overall.avg_time_to_mfe_hours.toFixed(1)}h`
                : "—"
            }
          />
          <KpiCard
            label="Avg time to MAE"
            value={
              overall.avg_time_to_mae_hours !== null
                ? `${overall.avg_time_to_mae_hours.toFixed(1)}h`
                : "—"
            }
          />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-slate-400">
          Breakdowns
        </h2>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <ChartCard
            title="Win rate by severity"
            subtitle="Higher = more wins. Buckets below min_signals are omitted."
            empty={Object.keys(data.by_severity).length === 0}
          >
            <BucketBarChart
              buckets={data.by_severity}
              metric={(b) => b.win_rate}
              order={SEVERITY_ORDER}
              formatLabel={(k) => k.replace("_", " ")}
              unit="%"
              colorMode="win-rate"
            />
          </ChartCard>

          <ChartCard
            title="Expectancy by score bucket"
            subtitle="Average P&L per signal in each score band."
            empty={Object.keys(data.by_score_bucket).length === 0}
          >
            <BucketBarChart
              buckets={data.by_score_bucket}
              metric={(b) => b.expectancy}
              order={SCORE_BUCKET_ORDER}
              unit="%"
              colorMode="expectancy"
            />
          </ChartCard>

          <ChartCard
            title="Expectancy by regime"
            subtitle="How performance varies by BTC regime label."
            empty={Object.keys(data.by_regime).length === 0}
          >
            <BucketBarChart
              buckets={data.by_regime}
              metric={(b) => b.expectancy}
              order={REGIME_ORDER}
              formatLabel={(k) => k.replace("_", "-")}
              unit="%"
              colorMode="expectancy"
            />
          </ChartCard>

          <ChartCard
            title="Expectancy by dominant trigger"
            subtitle="Which timeframe drove the signal — short-horizon or long-horizon."
            empty={Object.keys(data.by_dominant_trigger).length === 0}
          >
            <BucketBarChart
              buckets={data.by_dominant_trigger}
              metric={(b) => b.expectancy}
              order={TRIGGER_ORDER}
              unit="%"
              colorMode="expectancy"
            />
          </ChartCard>
        </div>
      </section>

      <BucketTablesNote
        bySeverity={data.by_severity}
        byScoreBucket={data.by_score_bucket}
        byRegime={data.by_regime}
        byDominantTrigger={data.by_dominant_trigger}
      />
    </>
  );
}

// ---------- scope selector ----------

function ScopeSelector({
  active,
  onSelect,
}: {
  active: AnalyticsScope;
  onSelect: (s: AnalyticsScope) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border border-slate-700">
      {SCOPES.map((s) => {
        const on = s === active;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onSelect(s)}
            className={`px-3 py-1 text-xs font-medium transition-colors ${
              on
                ? "bg-slate-700 text-slate-100"
                : "bg-slate-900 text-slate-300 hover:bg-slate-800"
            }`}
          >
            {s}
          </button>
        );
      })}
    </div>
  );
}

// ---------- helper note about omitted buckets ----------

function BucketTablesNote({
  bySeverity,
  byScoreBucket,
  byRegime,
  byDominantTrigger,
}: {
  bySeverity: Record<string, AnalyticsBucket>;
  byScoreBucket: Record<string, AnalyticsBucket>;
  byRegime: Record<string, AnalyticsBucket>;
  byDominantTrigger: Record<string, AnalyticsBucket>;
}) {
  const allEmpty =
    Object.keys(bySeverity).length === 0 &&
    Object.keys(byScoreBucket).length === 0 &&
    Object.keys(byRegime).length === 0 &&
    Object.keys(byDominantTrigger).length === 0;
  if (!allEmpty) return null;
  return (
    <p className="text-xs text-slate-500">
      Every slicing was filtered out by{" "}
      <code className="rounded bg-slate-800 px-1">min_signals</code>. The
      overall numbers above still apply.
    </p>
  );
}
