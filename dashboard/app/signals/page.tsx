"use client";

import Link from "next/link";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { Suspense, useMemo } from "react";

import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Pagination } from "@/components/pagination";
import { Pill } from "@/components/pill";
import { EmptyRow, Table, Tbody, Td, Th, Thead } from "@/components/table";
import { TableLoadingState } from "@/components/table-loading-state";
import { formatDateTime } from "@/lib/format";
import { useSignals, type SignalsFilters } from "@/lib/queries";
import { regimeColor, severityColor } from "@/lib/styles";

const PAGE_LIMIT = 50;

const SEVERITIES = ["normal", "strong", "very_strong"] as const;
const REGIMES = ["risk_on", "neutral", "risk_off"] as const;

/**
 * Read filters from URL search params.
 *
 * URL is the source of truth — deep links work, the back button
 * works, and refreshing keeps you on the same view. The filter bar
 * inputs below are controlled mirrors of these values.
 */
function readFilters(sp: URLSearchParams): SignalsFilters {
  const limit = Number(sp.get("limit") ?? PAGE_LIMIT);
  const offset = Number(sp.get("offset") ?? 0);
  return {
    symbol: sp.get("symbol") ?? undefined,
    severity: sp.get("severity") ?? undefined,
    regime: sp.get("regime") ?? undefined,
    from: sp.get("from") ?? undefined,
    to: sp.get("to") ?? undefined,
    limit: Number.isFinite(limit) && limit > 0 ? limit : PAGE_LIMIT,
    offset: Number.isFinite(offset) && offset >= 0 ? offset : 0,
  };
}

export default function SignalsPage() {
  // useSearchParams() requires a Suspense boundary for static export
  // (Next.js 14 client-side bailout rule). The wrapper here also gives
  // first-paint a clean skeleton while the URL params resolve.
  return (
    <Suspense fallback={<TableLoadingState cols={7} rows={8} withFilters />}>
      <SignalsPageInner />
    </Suspense>
  );
}

function SignalsPageInner() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = useMemo(() => readFilters(new URLSearchParams(sp.toString())), [sp]);
  const q = useSignals(filters);

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(sp.toString());
    if (value === null || value === "") {
      next.delete(key);
    } else {
      next.set(key, value);
    }
    // Any filter change resets pagination — otherwise the offset
    // refers to the previous result set.
    if (key !== "offset") next.delete("offset");
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  }

  function resetFilters() {
    router.replace(pathname, { scroll: false });
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Signals"
        subtitle={
          q.data
            ? `${q.data.meta.total} matching signals`
            : "Buy candidates emitted by the scoring engine."
        }
        actions={
          <button
            type="button"
            onClick={resetFilters}
            className="rounded border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700"
          >
            Reset filters
          </button>
        }
      />

      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <input
          type="text"
          placeholder="Symbol (e.g. BTCUSDT)"
          value={filters.symbol ?? ""}
          onChange={(e) => setParam("symbol", e.target.value.toUpperCase())}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 placeholder:text-slate-500"
        />
        <select
          value={filters.severity ?? ""}
          onChange={(e) => setParam("severity", e.target.value || null)}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
        >
          <option value="">All severities</option>
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
        <select
          value={filters.regime ?? ""}
          onChange={(e) => setParam("regime", e.target.value || null)}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100"
        >
          <option value="">All regimes</option>
          {REGIMES.map((r) => (
            <option key={r} value={r}>
              {r.replace("_", "-")}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="From (UTC ISO)"
          value={filters.from ?? ""}
          onChange={(e) => setParam("from", e.target.value)}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 placeholder:text-slate-500"
        />
        <input
          type="text"
          placeholder="To (UTC ISO)"
          value={filters.to ?? ""}
          onChange={(e) => setParam("to", e.target.value)}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 placeholder:text-slate-500"
        />
      </div>

      {q.isPending ? <TableLoadingState cols={7} rows={8} /> : null}
      {q.isError ? <ErrorState error={q.error as Error} retry={() => q.refetch()} /> : null}
      {q.data ? (
        <>
          <Table>
            <Thead>
              <tr>
                <Th>Detected</Th>
                <Th>Symbol</Th>
                <Th>Score</Th>
                <Th>Severity</Th>
                <Th>Trigger</Th>
                <Th>Regime</Th>
                <Th className="text-right">Price</Th>
              </tr>
            </Thead>
            <Tbody>
              {q.data.items.length === 0 ? (
                <EmptyRow colSpan={7} message="No signals match these filters." />
              ) : (
                q.data.items.map((s) => (
                  <tr key={s.id} className="hover:bg-slate-800/40">
                    <Td className="whitespace-nowrap font-mono text-xs text-slate-400">
                      <Link href={`/signals/${s.id}`} className="hover:text-slate-200">
                        {formatDateTime(s.detected_at)}
                      </Link>
                    </Td>
                    <Td className="font-medium">
                      <Link href={`/signals/${s.id}`} className="hover:underline">
                        {s.symbol}
                      </Link>
                    </Td>
                    <Td className="tabular-nums">{s.score}</Td>
                    <Td>
                      {s.severity ? (
                        <Pill className={severityColor(s.severity)}>
                          {s.severity.replace("_", " ")}
                        </Pill>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </Td>
                    <Td className="text-slate-300">
                      {s.dominant_trigger_timeframe ?? "—"}
                      {s.drop_trigger_pct !== null && s.drop_trigger_pct > 0 ? (
                        <span className="ml-1 text-slate-500">
                          (-{s.drop_trigger_pct.toFixed(1)}%)
                        </span>
                      ) : null}
                    </Td>
                    <Td>
                      {s.regime_at_signal ? (
                        <Pill className={regimeColor(s.regime_at_signal)}>
                          {s.regime_at_signal.replace("_", "-")}
                        </Pill>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </Td>
                    <Td className="text-right tabular-nums">
                      ${s.price_at_signal.toLocaleString()}
                    </Td>
                  </tr>
                ))
              )}
            </Tbody>
          </Table>
          <Pagination
            meta={q.data.meta}
            onPage={(off) => setParam("offset", String(off))}
          />
        </>
      ) : null}
    </div>
  );
}
