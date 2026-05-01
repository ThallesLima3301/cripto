import { Skeleton } from "./skeleton";

/**
 * Skeleton sized like the Analytics page: header + scope selector,
 * 4 overall KPIs, 4 MFE/MAE KPIs, 4 chart cards.
 *
 * Mirrors the layout precisely so Recharts' first paint doesn't push
 * the rest of the page around when the chart cards mount.
 */
export function AnalyticsLoadingState() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="space-y-2">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-3 w-72" />
        </div>
        <Skeleton className="h-7 w-44 rounded-md" />
      </div>

      {/* Overall row */}
      <Skeleton className="h-3 w-40" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>

      {/* MFE/MAE row */}
      <Skeleton className="h-3 w-24" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>

      {/* Charts grid */}
      <Skeleton className="h-3 w-28" />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="rounded-md border border-slate-800 bg-slate-900 p-4"
          >
            <Skeleton className="mb-2 h-4 w-40" />
            <Skeleton className="mb-3 h-3 w-64" />
            <Skeleton className="h-64 w-full" />
          </div>
        ))}
      </div>
    </div>
  );
}
