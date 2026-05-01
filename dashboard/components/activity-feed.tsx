import type { ActivityItem } from "@/lib/types";
import { formatRelative } from "@/lib/format";

const kindStyle: Record<ActivityItem["kind"], string> = {
  signal: "border-emerald-500/40 text-emerald-300",
  sell: "border-amber-500/40 text-amber-300",
};

const kindLabel: Record<ActivityItem["kind"], string> = {
  signal: "buy",
  sell: "sell",
};

/**
 * Newest-first feed of buy / sell events.
 *
 * Empty state renders a friendly note rather than a blank panel —
 * matches the "graceful empty state" convention used everywhere else
 * in the project.
 */
export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-md border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
        No activity yet — the bot has not produced any signals.
      </div>
    );
  }
  return (
    <ul className="divide-y divide-slate-800 rounded-md border border-slate-800 bg-slate-900">
      {items.map((it) => (
        <li
          key={`${it.kind}-${it.id}`}
          className="flex items-center gap-4 px-4 py-3"
        >
          <span
            className={`shrink-0 rounded border px-2 py-0.5 text-xs uppercase tracking-wide ${kindStyle[it.kind]}`}
          >
            {kindLabel[it.kind]}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium text-slate-200">
              {it.symbol}{" "}
              <span className="text-slate-400">— {it.headline}</span>
            </div>
            <div className="text-xs text-slate-500">
              {formatRelative(it.at)}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}
