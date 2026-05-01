"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Skeleton } from "@/components/skeleton";
import { formatDateTime } from "@/lib/format";
import { useWeeklySummaries } from "@/lib/queries";
import type { WeeklySummaryItem } from "@/lib/types";

function ReportsLoadingState() {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Skeleton className="h-6 w-32" />
      </div>
      <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
        <div className="space-y-2 rounded-md border border-slate-800 bg-slate-900 p-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
        <Skeleton className="h-72" />
      </div>
    </div>
  );
}

export default function ReportsPage() {
  // Suspense boundary needed for static export — useSearchParams()
  // bails out of pre-rendering otherwise.
  return (
    <Suspense fallback={<ReportsLoadingState />}>
      <ReportsPageInner />
    </Suspense>
  );
}

function ReportsPageInner() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const q = useWeeklySummaries(20);

  const selectedId = (() => {
    const raw = sp.get("id");
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  })();

  function selectId(id: number | null) {
    const next = new URLSearchParams(sp.toString());
    if (id === null) next.delete("id");
    else next.set("id", String(id));
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Reports"
        subtitle={
          <>
            Weekly summaries persisted in{" "}
            <code className="rounded bg-slate-800 px-1">
              weekly_summaries
            </code>
            . Each row is the body the bot also pushed via ntfy.
          </>
        }
      />

      {q.isPending ? <ReportsLoadingState /> : null}
      {q.isError ? <ErrorState error={q.error as Error} retry={() => q.refetch()} /> : null}
      {q.data ? (
        q.data.length === 0 ? (
          <EmptyState
            title="No weekly summaries yet"
            message="The first one lands the next time `python -m crypto_monitor.cli weekly` runs."
          />
        ) : (
          <ReportsLayout
            items={q.data}
            selectedId={selectedId ?? q.data[0]!.id}
            onSelect={(id) => selectId(id)}
          />
        )
      ) : null}
    </div>
  );
}

function ReportsLayout({
  items,
  selectedId,
  onSelect,
}: {
  items: WeeklySummaryItem[];
  selectedId: number;
  onSelect: (id: number) => void;
}) {
  const selected = items.find((i) => i.id === selectedId) ?? items[0]!;

  return (
    <div className="grid gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
      <ul className="overflow-hidden rounded-md border border-slate-800 bg-slate-900">
        {items.map((it) => {
          const active = it.id === selected.id;
          return (
            <li key={it.id} className="border-b border-slate-800 last:border-b-0">
              <button
                type="button"
                onClick={() => onSelect(it.id)}
                className={`block w-full px-3 py-2 text-left text-sm transition-colors ${
                  active
                    ? "bg-slate-800/60 text-slate-100"
                    : "text-slate-300 hover:bg-slate-800/30"
                }`}
              >
                <div className="font-medium">
                  {weekRange(it.week_start, it.week_end)}
                </div>
                <div className="mt-0.5 text-xs text-slate-500">
                  {it.signal_count} signals · {it.buy_count} buys
                  {it.sent ? "" : " · unsent"}
                </div>
              </button>
            </li>
          );
        })}
      </ul>

      <ReportBody item={selected} />
    </div>
  );
}

function ReportBody({ item }: { item: WeeklySummaryItem }) {
  return (
    <div className="space-y-3 rounded-md border border-slate-800 bg-slate-900 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-base font-medium text-slate-100">
          {weekRange(item.week_start, item.week_end)}
        </h2>
        <div className="text-xs text-slate-400">
          generated {formatDateTime(item.generated_at)} ·{" "}
          {item.sent ? "sent via ntfy" : "not sent"}
        </div>
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-slate-400">
        <span>signals: {item.signal_count}</span>
        <span>buys: {item.buy_count}</span>
        {item.top_drop_symbol ? (
          <span>
            top drop: {item.top_drop_symbol}{" "}
            {item.top_drop_pct !== null
              ? `(-${item.top_drop_pct.toFixed(1)}%)`
              : ""}
          </span>
        ) : null}
      </div>
      <pre className="whitespace-pre-wrap break-words rounded border border-slate-800 bg-slate-950 p-4 text-sm leading-relaxed text-slate-200">
        {item.body}
      </pre>
    </div>
  );
}

function weekRange(start: string, end: string): string {
  return `${start.slice(0, 10)} → ${end.slice(0, 10)}`;
}
