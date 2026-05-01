"use client";

// Per-endpoint TanStack Query hooks.
//
// One hook per endpoint, one stable query key. Stale times reflect
// "how live should this look": the overview is refreshed often (the
// home page is the most frequently visited); the table pages refresh
// less aggressively because the user is typically reading a fixed
// snapshot.

import { useQuery } from "@tanstack/react-query";

import { apiGet, buildQuery } from "./api";
import type {
  AnalyticsData,
  AnalyticsScope,
  OpenBuyItem,
  OverviewData,
  PageMeta,
  SellSignalItem,
  SignalDetail,
  SignalListItem,
  WatchlistItem,
  WeeklySummaryItem,
} from "./types";

/** Stable query keys, exported so future hooks can invalidate cleanly. */
export const queryKeys = {
  overview: ["overview"] as const,
  signals: (filters: SignalsFilters) => ["signals", filters] as const,
  signal: (id: number) => ["signal", id] as const,
  watchlist: ["watchlist"] as const,
  openBuys: ["open-buys"] as const,
  sellSignals: (filters: SellSignalsFilters) =>
    ["sell-signals", filters] as const,
  weeklySummaries: (limit: number) => ["weekly-summaries", limit] as const,
  analytics: (scope: AnalyticsScope, minSignals: number) =>
    ["analytics", scope, minSignals] as const,
} as const;

// ---------- /api/overview ----------

/**
 * Hook for the dashboard home page.
 *
 * Refetched on window focus and every 30 seconds while mounted, so a
 * user who leaves the tab open sees fresh KPIs the next time they
 * look without manually reloading.
 */
export function useOverview() {
  return useQuery({
    queryKey: queryKeys.overview,
    queryFn: async () => {
      const res = await apiGet<OverviewData>("/api/overview");
      return res.data;
    },
    staleTime: 30 * 1000,
    refetchInterval: 30 * 1000,
  });
}

// ---------- /api/signals ----------

export type SignalsFilters = {
  symbol?: string;
  severity?: string;
  regime?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
};

export function useSignals(filters: SignalsFilters) {
  return useQuery({
    queryKey: queryKeys.signals(filters),
    queryFn: async () => {
      const qs = buildQuery({
        symbol: filters.symbol,
        severity: filters.severity,
        regime: filters.regime,
        from: filters.from,
        to: filters.to,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      });
      const res = await apiGet<SignalListItem[]>(`/api/signals${qs}`);
      return { items: res.data, meta: res.meta as unknown as PageMeta };
    },
    staleTime: 60 * 1000,
  });
}

// ---------- /api/signals/{id} ----------

export function useSignalDetail(id: number) {
  return useQuery({
    queryKey: queryKeys.signal(id),
    queryFn: async () => {
      const res = await apiGet<SignalDetail>(`/api/signals/${id}`);
      return res.data;
    },
    // Signal rows are immutable once written, so the detail view can
    // cache aggressively. The evaluation block changes once when the
    // signal matures (~30 days later), but a manual refresh handles
    // that edge case.
    staleTime: 5 * 60 * 1000,
    enabled: Number.isFinite(id) && id > 0,
  });
}

// ---------- /api/watchlist ----------

export function useWatchlist() {
  return useQuery({
    queryKey: queryKeys.watchlist,
    queryFn: async () => {
      const res = await apiGet<WatchlistItem[]>("/api/watchlist");
      return res.data;
    },
    staleTime: 60 * 1000,
  });
}

// ---------- /api/open-buys ----------

export function useOpenBuys() {
  return useQuery({
    queryKey: queryKeys.openBuys,
    queryFn: async () => {
      const res = await apiGet<OpenBuyItem[]>("/api/open-buys");
      return res.data;
    },
    // Open buys carry a "current_price" snapshot derived from the
    // latest 1h candle; refresh once a minute so the staleness
    // indicator on the page actually moves.
    staleTime: 30 * 1000,
    refetchInterval: 60 * 1000,
  });
}

// ---------- /api/sell-signals ----------

export type SellSignalsFilters = {
  symbol?: string;
  rule?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
};

export function useSellSignals(filters: SellSignalsFilters) {
  return useQuery({
    queryKey: queryKeys.sellSignals(filters),
    queryFn: async () => {
      const qs = buildQuery({
        symbol: filters.symbol,
        rule: filters.rule,
        from: filters.from,
        to: filters.to,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      });
      const res = await apiGet<SellSignalItem[]>(`/api/sell-signals${qs}`);
      return { items: res.data, meta: res.meta as unknown as PageMeta };
    },
    staleTime: 60 * 1000,
  });
}

// ---------- /api/analytics ----------

/**
 * Hook for the Analytics page.
 *
 * The aggregator is pure on the server side, so the same scope +
 * min_signals always returns identical numbers — cache aggressively
 * (5 min stale) since the underlying input changes only when a new
 * matured evaluation lands (~once a day at most).
 *
 * Returns the full `{ data, meta }` envelope so the UI can also
 * render the active scope label without re-deriving it.
 */
export function useAnalytics(
  scope: AnalyticsScope,
  minSignals = 5,
) {
  return useQuery({
    queryKey: queryKeys.analytics(scope, minSignals),
    queryFn: async () => {
      const qs = buildQuery({ scope, min_signals: minSignals });
      const res = await apiGet<AnalyticsData>(`/api/analytics${qs}`);
      return {
        data: res.data,
        scope: (res.meta?.scope as AnalyticsScope | undefined) ?? scope,
        minSignals:
          typeof res.meta?.min_signals === "number"
            ? (res.meta.min_signals as number)
            : minSignals,
      };
    },
    staleTime: 5 * 60 * 1000,
  });
}

// ---------- /api/weekly-summaries ----------

export function useWeeklySummaries(limit = 20) {
  return useQuery({
    queryKey: queryKeys.weeklySummaries(limit),
    queryFn: async () => {
      const res = await apiGet<WeeklySummaryItem[]>(
        `/api/weekly-summaries?limit=${limit}`,
      );
      return res.data;
    },
    // Weekly summaries are a once-per-week event; cache long.
    staleTime: 5 * 60 * 1000,
  });
}
