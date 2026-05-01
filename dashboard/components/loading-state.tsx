import { Skeleton } from "./skeleton";

/**
 * Skeleton for the Overview page (the home route).
 *
 * Mirrors the eventual layout: title row, 5-card KPI strip, 4-card
 * analytics strip, activity feed. Used on `/`, kept as the default
 * when a route doesn't supply its own page-shaped skeleton.
 */
export function LoadingState() {
  return (
    <div className="space-y-6">
      {/* Title + regime chip */}
      <div className="flex items-center justify-between">
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-6 w-24 rounded-full" />
      </div>

      {/* 5 KPI cards */}
      <Skeleton className="h-3 w-40" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>

      {/* Analytics digest 4 cards */}
      <Skeleton className="h-3 w-32" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>

      {/* Activity feed */}
      <Skeleton className="h-3 w-32" />
      <Skeleton className="h-32" />
    </div>
  );
}
