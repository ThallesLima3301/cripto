// Tiny presentation-only helpers. No business logic.

/**
 * Format a 0..100 percentage as a fixed-1-decimal string.
 *
 * `signed=true` adds a leading `+` for positive values — used for
 * expectancy where the sign carries information.
 */
export function formatPercent(
  value: number | null | undefined,
  signed = false,
): string {
  if (value === null || value === undefined) return "—";
  const sign = signed && value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

/**
 * Render a UTC ISO timestamp as a tabular `YYYY-MM-DD HH:MM:SS` string.
 *
 * Stays in UTC (no local-time conversion) so a row's timestamp lines
 * up with what the bot wrote to the DB. Falls back to the raw input
 * when parsing fails.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(iso);
  if (!m) return iso;
  return `${m[1]} ${m[2]}`;
}

/** Render a UTC ISO timestamp as a relative "5m ago" string. */
export function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diff = Date.now() - t;
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
