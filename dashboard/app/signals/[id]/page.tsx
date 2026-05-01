"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { Pill } from "@/components/pill";
import { ApiError } from "@/lib/api";
import { formatDateTime, formatPercent } from "@/lib/format";
import { useSignalDetail } from "@/lib/queries";
import {
  pnlColor,
  regimeColor,
  severityColor,
  verdictColor,
} from "@/lib/styles";
import type { SignalDetail } from "@/lib/types";

export default function SignalDetailPage() {
  const { id: idParam } = useParams<{ id: string }>();
  const id = Number(idParam);
  const q = useSignalDetail(id);

  if (!Number.isFinite(id) || id <= 0) {
    return <NotFound id={idParam ?? "?"} />;
  }
  if (q.isPending) return <LoadingState />;
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 404) {
      return <NotFound id={idParam ?? String(id)} />;
    }
    return <ErrorState error={q.error as Error} retry={() => q.refetch()} />;
  }
  const d = q.data;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/signals"
          className="text-xs text-slate-400 hover:text-slate-200"
        >
          ← Back to signals
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{d.symbol}</h1>
          <span className="text-sm text-slate-400">id={d.id}</span>
          {d.severity ? (
            <Pill className={severityColor(d.severity)}>
              {d.severity.replace("_", " ")}
            </Pill>
          ) : null}
          {d.regime_at_signal ? (
            <Pill className={regimeColor(d.regime_at_signal)}>
              {d.regime_at_signal.replace("_", "-")}
            </Pill>
          ) : null}
          {d.watchlist_id !== null ? (
            <span className="text-xs text-slate-400">
              promoted from watch #{d.watchlist_id}
            </span>
          ) : null}
        </div>
      </div>

      <CoreFacts d={d} />

      {d.evaluation ? <EvaluationBlock e={d.evaluation} /> : <PendingEvaluation />}

      <ScoreBreakdown breakdown={d.score_breakdown} />
    </div>
  );
}

// ---------- core facts ----------

function CoreFacts({ d }: { d: SignalDetail }) {
  return (
    <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
        Signal
      </h2>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-3">
        <Field label="Detected at" value={formatDateTime(d.detected_at)} />
        <Field label="Candle hour" value={formatDateTime(d.candle_hour)} />
        <Field
          label="Price at signal"
          value={`$${d.price_at_signal.toLocaleString()}`}
        />
        <Field label="Score" value={d.score} />
        <Field
          label="Trigger"
          value={d.dominant_trigger_timeframe ?? "—"}
        />
        <Field
          label="Drop (trigger horizon)"
          value={
            d.drop_trigger_pct !== null
              ? `-${d.drop_trigger_pct.toFixed(1)}%`
              : "—"
          }
        />
        <Field label="RSI 1h" value={fmt(d.rsi_1h, (v) => v.toFixed(0))} />
        <Field label="RSI 4h" value={fmt(d.rsi_4h, (v) => v.toFixed(0))} />
        <Field
          label="Rel. volume"
          value={fmt(d.rel_volume, (v) => `${v.toFixed(2)}x`)}
        />
        <Field label="Drop 24h" value={pctOrDash(d.drop_24h_pct, true)} />
        <Field label="Drop 7d" value={pctOrDash(d.drop_7d_pct, true)} />
        <Field label="Drop 30d" value={pctOrDash(d.drop_30d_pct, true)} />
        <Field label="Drop 180d" value={pctOrDash(d.drop_180d_pct, true)} />
        <Field
          label="Below 30d high"
          value={pctOrDash(d.distance_from_30d_high_pct, true)}
        />
        <Field
          label="Below 180d high"
          value={pctOrDash(d.distance_from_180d_high_pct, true)}
        />
        <Field
          label="Distance to support"
          value={pctOrDash(d.dist_support_pct)}
        />
        <Field
          label="Support level"
          value={
            d.support_level_price !== null
              ? `$${d.support_level_price.toLocaleString()}`
              : "—"
          }
        />
        <Field
          label="Trend (4h / 1d)"
          value={`${d.trend_context_4h ?? "—"} / ${d.trend_context_1d ?? "—"}`}
        />
        <Field
          label="Reversal pattern"
          value={d.reversal_signal ? "yes" : "no"}
        />
      </dl>
      {d.trigger_reason ? (
        <p className="mt-3 text-xs text-slate-400">
          <span className="text-slate-500">trigger reason:</span>{" "}
          {d.trigger_reason}
        </p>
      ) : null}
    </section>
  );
}

function Field({
  label,
  value,
}: {
  label: string;
  value: string | number | null | undefined;
}) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="font-mono text-sm tabular-nums text-slate-200">
        {value === null || value === undefined || value === "" ? "—" : value}
      </dd>
    </div>
  );
}

// ---------- evaluation block ----------

function EvaluationBlock({ e }: { e: NonNullable<SignalDetail["evaluation"]> }) {
  return (
    <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
          Evaluation
        </h2>
        {e.verdict ? (
          <Pill className={verdictColor(e.verdict)}>{e.verdict}</Pill>
        ) : null}
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-3">
        <Field label="Evaluated at" value={formatDateTime(e.evaluated_at)} />
        <PnlField label="Return 24h" value={e.return_24h_pct} />
        <PnlField label="Return 7d" value={e.return_7d_pct} />
        <PnlField label="Return 30d" value={e.return_30d_pct} />
        <PnlField label="Max gain (7d)" value={e.max_gain_7d_pct} />
        <PnlField label="Max loss (7d)" value={e.max_loss_7d_pct} />
        <Field
          label="Time to MFE"
          value={fmt(e.time_to_mfe_hours, (v) => `${v.toFixed(1)}h`)}
        />
        <Field
          label="Time to MAE"
          value={fmt(e.time_to_mae_hours, (v) => `${v.toFixed(1)}h`)}
        />
      </dl>
    </section>
  );
}

function PnlField({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className={`font-mono text-sm tabular-nums ${pnlColor(value)}`}>
        {formatPercent(value, true)}
      </dd>
    </div>
  );
}

function PendingEvaluation() {
  return (
    <section className="rounded-md border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
      <span className="text-slate-300 font-medium">Evaluation pending.</span>{" "}
      Signals are evaluated 30 days after their candle hour. Check back later.
    </section>
  );
}

// ---------- score breakdown ----------

function ScoreBreakdown({ breakdown }: { breakdown: Record<string, unknown> }) {
  const keys = Object.keys(breakdown);
  if (keys.length === 0) {
    return (
      <section className="rounded-md border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
        Score breakdown not available.
      </section>
    );
  }
  return (
    <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-slate-400">
        Score breakdown
      </h2>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {keys.map((k) => (
          <FactorCard key={k} name={k} value={breakdown[k]} />
        ))}
      </div>
    </section>
  );
}

function FactorCard({ name, value }: { name: string; value: unknown }) {
  if (!isObject(value)) {
    return (
      <div className="rounded border border-slate-800 bg-slate-950 p-3">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          {name}
        </div>
        <div className="mt-1 font-mono text-sm text-slate-200">
          {String(value)}
        </div>
      </div>
    );
  }
  const points =
    "points" in value && typeof value.points === "number"
      ? value.points
      : null;
  const entries = Object.entries(value).filter(([k]) => k !== "points");
  return (
    <div className="rounded border border-slate-800 bg-slate-950 p-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wide text-slate-400">
          {name}
        </span>
        {points !== null ? (
          <span className="font-mono text-sm tabular-nums text-slate-200">
            {points} pts
          </span>
        ) : null}
      </div>
      {entries.length > 0 ? (
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="truncate text-slate-500">{k}</dt>
              <dd className="truncate font-mono text-slate-300">
                {formatScalar(v)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function formatScalar(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    return Number.isInteger(v) ? String(v) : v.toFixed(3);
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

// ---------- helpers ----------

function fmt<T>(v: T | null, render: (v: T) => string): string {
  return v === null ? "—" : render(v);
}

function pctOrDash(v: number | null, signed = false): string {
  if (v === null) return "—";
  return signed
    ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%`
    : `${v.toFixed(2)}%`;
}

function NotFound({ id }: { id: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-6 text-center">
      <h1 className="text-base font-medium text-slate-200">Signal not found</h1>
      <p className="mt-1 text-sm text-slate-400">
        No signal with id <code className="rounded bg-slate-800 px-1">{id}</code>{" "}
        exists.
      </p>
      <Link
        href="/signals"
        className="mt-3 inline-block rounded border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700"
      >
        ← Back to signals
      </Link>
    </div>
  );
}
