import { Skeleton } from "./skeleton";

type Props = {
  /** Number of header columns to show. Defaults to a reasonable 6. */
  cols?: number;
  /** Number of body rows to render in the placeholder. */
  rows?: number;
  /** Whether to show a fake filter bar above the table (for /signals). */
  withFilters?: boolean;
};

/**
 * Skeleton sized like a paginated list page — title, optional filter
 * bar, header row, body rows, footer pagination strip.
 *
 * Reused by /signals, /watchlist, /sell, /reports so the loading
 * shape doesn't shift when data arrives.
 */
export function TableLoadingState({
  cols = 6,
  rows = 6,
  withFilters = false,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-6 w-20" />
      </div>

      {withFilters ? (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      ) : null}

      <div className="overflow-hidden rounded-md border border-slate-800 bg-slate-900">
        <div className="grid gap-3 border-b border-slate-800 bg-slate-800/40 p-3"
             style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
          {Array.from({ length: cols }).map((_, i) => (
            <Skeleton key={i} className="h-3" />
          ))}
        </div>
        <div className="divide-y divide-slate-800">
          {Array.from({ length: rows }).map((_, r) => (
            <div
              key={r}
              className="grid gap-3 p-3"
              style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
            >
              {Array.from({ length: cols }).map((_, c) => (
                <Skeleton key={c} className="h-4" />
              ))}
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-between">
        <Skeleton className="h-3 w-40" />
        <div className="flex gap-2">
          <Skeleton className="h-7 w-16" />
          <Skeleton className="h-7 w-16" />
        </div>
      </div>
    </div>
  );
}
