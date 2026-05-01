"use client";

import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Pagination } from "@/components/pagination";
import { Pill } from "@/components/pill";
import { EmptyRow, Table, Tbody, Td, Th, Thead } from "@/components/table";
import { TableLoadingState } from "@/components/table-loading-state";
import { formatDateTime, formatPercent } from "@/lib/format";
import {
  useOpenBuys,
  useSellSignals,
  type SellSignalsFilters,
} from "@/lib/queries";
import { pnlColor, ruleColor, sellSeverityColor } from "@/lib/styles";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { Suspense, useMemo } from "react";

const PAGE_LIMIT = 20;

function readSellFilters(sp: URLSearchParams): SellSignalsFilters {
  const offset = Number(sp.get("offset") ?? 0);
  return {
    limit: PAGE_LIMIT,
    offset: Number.isFinite(offset) && offset >= 0 ? offset : 0,
  };
}

export default function SellPage() {
  // Suspense boundary needed for static export — useSearchParams()
  // bails out of pre-rendering otherwise.
  return (
    <Suspense fallback={<TableLoadingState cols={8} rows={6} />}>
      <SellPageInner />
    </Suspense>
  );
}

function SellPageInner() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = useMemo(
    () => readSellFilters(new URLSearchParams(sp.toString())),
    [sp],
  );

  const buysQuery = useOpenBuys();
  const sellsQuery = useSellSignals(filters);

  function setOffset(off: number) {
    const next = new URLSearchParams(sp.toString());
    if (off === 0) next.delete("offset");
    else next.set("offset", String(off));
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Sell monitor"
        subtitle="Open buys + sell signals raised by the engine."
      />

      <section className="space-y-2">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Open buys
          </h2>
          <span className="text-xs text-slate-400">
            {buysQuery.data ? `${buysQuery.data.length} positions` : ""}
          </span>
        </div>
        <p className="text-xs text-slate-500">
          Current price comes from the latest 1h candle close, so it may be up
          to ~60 minutes stale. <code className="mx-1 rounded bg-slate-800 px-1">latest_close_at</code> shows exactly when.
        </p>

        {buysQuery.isPending ? <TableLoadingState cols={8} rows={4} /> : null}
        {buysQuery.isError ? (
          <ErrorState
            error={buysQuery.error as Error}
            retry={() => buysQuery.refetch()}
          />
        ) : null}
        {buysQuery.data ? (
          <Table>
            <Thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Bought</Th>
                <Th className="text-right">Buy price</Th>
                <Th className="text-right">Current</Th>
                <Th className="text-right">PnL</Th>
                <Th className="text-right">High watermark</Th>
                <Th className="text-right">Drawdown</Th>
                <Th>Latest close</Th>
              </tr>
            </Thead>
            <Tbody>
              {buysQuery.data.length === 0 ? (
                <EmptyRow
                  colSpan={8}
                  message="No open buys. Record one with `crypto_monitor.cli buy add`."
                />
              ) : (
                buysQuery.data.map((b) => (
                  <tr key={b.id} className="hover:bg-slate-800/40">
                    <Td className="font-medium">{b.symbol}</Td>
                    <Td className="font-mono text-xs text-slate-400">
                      {formatDateTime(b.bought_at)}
                    </Td>
                    <Td className="text-right tabular-nums">
                      ${b.price.toLocaleString()}
                    </Td>
                    <Td className="text-right tabular-nums">
                      {b.current_price !== null
                        ? `$${b.current_price.toLocaleString()}`
                        : "—"}
                    </Td>
                    <Td className={`text-right tabular-nums ${pnlColor(b.pnl_pct)}`}>
                      {formatPercent(b.pnl_pct, true)}
                    </Td>
                    <Td className="text-right tabular-nums">
                      {b.high_watermark !== null
                        ? `$${b.high_watermark.toLocaleString()}`
                        : "—"}
                    </Td>
                    <Td
                      className={`text-right tabular-nums ${pnlColor(b.drawdown_from_high_pct)}`}
                    >
                      {formatPercent(b.drawdown_from_high_pct, true)}
                    </Td>
                    <Td className="font-mono text-xs text-slate-400">
                      {formatDateTime(b.latest_close_at)}
                    </Td>
                  </tr>
                ))
              )}
            </Tbody>
          </Table>
        ) : null}
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-sm font-medium uppercase tracking-wide text-slate-400">
            Recent sell signals
          </h2>
          <span className="text-xs text-slate-400">
            {sellsQuery.data ? `${sellsQuery.data.meta.total} total` : ""}
          </span>
        </div>

        {sellsQuery.isPending ? <TableLoadingState cols={8} rows={6} /> : null}
        {sellsQuery.isError ? (
          <ErrorState
            error={sellsQuery.error as Error}
            retry={() => sellsQuery.refetch()}
          />
        ) : null}
        {sellsQuery.data ? (
          <>
            <Table>
              <Thead>
                <tr>
                  <Th>Detected</Th>
                  <Th>Symbol</Th>
                  <Th>Buy</Th>
                  <Th>Rule</Th>
                  <Th>Severity</Th>
                  <Th className="text-right">Price</Th>
                  <Th className="text-right">PnL</Th>
                  <Th>Reason</Th>
                </tr>
              </Thead>
              <Tbody>
                {sellsQuery.data.items.length === 0 ? (
                  <EmptyRow
                    colSpan={8}
                    message="No sell signals yet. The sell engine raises one whenever a rule fires for an open buy."
                  />
                ) : (
                  sellsQuery.data.items.map((s) => (
                    <tr key={s.id} className="hover:bg-slate-800/40">
                      <Td className="font-mono text-xs text-slate-400 whitespace-nowrap">
                        {formatDateTime(s.detected_at)}
                      </Td>
                      <Td className="font-medium">{s.symbol}</Td>
                      <Td className="text-slate-400">#{s.buy_id}</Td>
                      <Td>
                        <Pill className={ruleColor(s.rule_triggered)}>
                          {s.rule_triggered.replace(/_/g, " ")}
                        </Pill>
                      </Td>
                      <Td>
                        <Pill className={sellSeverityColor(s.severity)}>
                          {s.severity}
                        </Pill>
                      </Td>
                      <Td className="text-right tabular-nums">
                        ${s.price_at_signal.toLocaleString()}
                      </Td>
                      <Td className={`text-right tabular-nums ${pnlColor(s.pnl_pct)}`}>
                        {formatPercent(s.pnl_pct, true)}
                      </Td>
                      <Td
                        className="max-w-[26rem] truncate text-xs text-slate-400"
                        title={s.reason ?? undefined}
                      >
                        {s.reason ?? "—"}
                      </Td>
                    </tr>
                  ))
                )}
              </Tbody>
            </Table>
            <Pagination meta={sellsQuery.data.meta} onPage={setOffset} />
          </>
        ) : null}
      </section>
    </div>
  );
}
