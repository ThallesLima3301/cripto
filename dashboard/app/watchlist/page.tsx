"use client";

import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Pill } from "@/components/pill";
import { EmptyRow, Table, Tbody, Td, Th, Thead } from "@/components/table";
import { TableLoadingState } from "@/components/table-loading-state";
import { formatDateTime } from "@/lib/format";
import { useWatchlist } from "@/lib/queries";
import { watchlistStatusColor } from "@/lib/styles";

export default function WatchlistPage() {
  const q = useWatchlist();

  return (
    <div className="space-y-4">
      <PageHeader
        title="Watchlist"
        subtitle={
          <>
            Borderline scores below the buy-signal emit floor but above{" "}
            <code className="rounded bg-slate-800 px-1">floor_score</code>.
            Promoted into a real signal if the score crosses the emit floor
            before{" "}
            <code className="rounded bg-slate-800 px-1">expires_at</code>.
          </>
        }
        actions={
          <span className="text-xs text-slate-400">
            {q.data ? `${q.data.length} active` : ""}
          </span>
        }
      />

      {q.isPending ? <TableLoadingState cols={6} rows={5} /> : null}
      {q.isError ? (
        <ErrorState error={q.error as Error} retry={() => q.refetch()} />
      ) : null}
      {q.data ? (
        <Table>
          <Thead>
            <tr>
              <Th>Symbol</Th>
              <Th>Status</Th>
              <Th>Last score</Th>
              <Th>First seen</Th>
              <Th>Last seen</Th>
              <Th>Expires</Th>
            </tr>
          </Thead>
          <Tbody>
            {q.data.length === 0 ? (
              <EmptyRow
                colSpan={6}
                message="No active watchlist entries. The bot adds symbols here when they score in the borderline band."
              />
            ) : (
              q.data.map((w) => (
                <tr key={w.id} className="hover:bg-slate-800/40">
                  <Td className="font-medium">{w.symbol}</Td>
                  <Td>
                    <Pill className={watchlistStatusColor(w.status)}>
                      {w.status}
                    </Pill>
                  </Td>
                  <Td className="tabular-nums">{w.last_score}</Td>
                  <Td className="font-mono text-xs text-slate-400">
                    {formatDateTime(w.first_seen_at)}
                  </Td>
                  <Td className="font-mono text-xs text-slate-400">
                    {formatDateTime(w.last_seen_at)}
                  </Td>
                  <Td className="font-mono text-xs text-slate-400">
                    {formatDateTime(w.expires_at)}
                  </Td>
                </tr>
              ))
            )}
          </Tbody>
        </Table>
      ) : null}
    </div>
  );
}
