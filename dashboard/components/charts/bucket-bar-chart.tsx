"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { AnalyticsBucket } from "@/lib/types";

type Row = {
  /** Original key from the slicing dict (e.g. `"strong"` / `"50-64"`). */
  key: string;
  /** Display-formatted label rendered on the X axis. */
  label: string;
  /** The metric value being charted. ``null`` when the underlying
   *  bucket didn't have it (no wins, no losses, etc.). */
  value: number | null;
  /** Bucket count — surfaced in the tooltip alongside the metric. */
  count: number;
};

type ColorMode = "win-rate" | "expectancy";

type Props = {
  buckets: Record<string, AnalyticsBucket>;
  /** Pick the metric to chart out of each bucket. */
  metric: (b: AnalyticsBucket) => number | null;
  /** Optional preferred X-axis order. Keys not in the order list are
   *  appended alphabetically — useful for the score-bucket chart
   *  where the canonical order is "50-64 → 65-79 → 80-100". */
  order?: readonly string[];
  /** Override the X-axis label per key (e.g. `risk_on` → `risk-on`). */
  formatLabel?: (key: string) => string;
  /** Y-axis unit. ``"%"`` adds a percent suffix to ticks + tooltips. */
  unit?: "%" | "x" | "";
  /** Bar color strategy:
   *   - `"win-rate"`   → emerald.
   *   - `"expectancy"` → emerald for positive, red for negative.
   */
  colorMode?: ColorMode;
};

const EMERALD = "#10b981";
const RED = "#ef4444";
const SLATE = "#475569";

/**
 * Generic bar chart over an analytics slicing.
 *
 * Used for win-rate-by-severity, expectancy-by-score-bucket,
 * expectancy-by-regime, and expectancy-by-trigger. Keeping it
 * one component means the visual contract (axes, tooltip, color
 * rules) is centralized and the analytics page only describes
 * which metric/order/colors apply where.
 */
export function BucketBarChart({
  buckets,
  metric,
  order,
  formatLabel,
  unit = "",
  colorMode = "win-rate",
}: Props) {
  const rows = sortedBucketRows(buckets, metric, order, formatLabel);

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
        <XAxis
          dataKey="label"
          stroke="#94a3b8"
          tick={{ fill: "#94a3b8", fontSize: 11 }}
          tickLine={false}
          axisLine={{ stroke: "#1e293b" }}
        />
        <YAxis
          stroke="#94a3b8"
          tick={{ fill: "#94a3b8", fontSize: 11 }}
          tickLine={false}
          axisLine={{ stroke: "#1e293b" }}
          width={48}
          tickFormatter={(v: number) => `${v}${unit}`}
        />
        <Tooltip
          cursor={{ fill: "#1e293b" }}
          contentStyle={{
            backgroundColor: "#0f172a",
            border: "1px solid #1e293b",
            borderRadius: "0.375rem",
            fontSize: "12px",
          }}
          labelStyle={{ color: "#cbd5e1" }}
          formatter={(value: number, _name, payload) => {
            const row = payload?.payload as Row | undefined;
            const formatted =
              value === null || value === undefined
                ? "—"
                : `${formatNumber(value)}${unit}`;
            return [formatted, `n=${row?.count ?? 0}`];
          }}
        />
        <Bar dataKey="value" radius={[2, 2, 0, 0]}>
          {rows.map((r) => (
            <Cell
              key={r.key}
              fill={barColor(r.value, colorMode)}
              opacity={r.value === null ? 0.3 : 1}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function barColor(value: number | null, mode: ColorMode): string {
  if (value === null) return SLATE;
  if (mode === "win-rate") return EMERALD;
  return value >= 0 ? EMERALD : RED;
}

function formatNumber(v: number): string {
  if (Math.abs(v) >= 100) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

function sortedBucketRows(
  buckets: Record<string, AnalyticsBucket>,
  metric: (b: AnalyticsBucket) => number | null,
  order: readonly string[] | undefined,
  formatLabel: ((key: string) => string) | undefined,
): Row[] {
  const entries = Object.entries(buckets);
  if (order && order.length) {
    const fixed = order.filter((k) => k in buckets);
    const remaining = entries
      .map(([k]) => k)
      .filter((k) => !order.includes(k))
      .sort();
    const keys = [...fixed, ...remaining];
    return keys.map((k) => ({
      key: k,
      label: formatLabel ? formatLabel(k) : k,
      value: metric(buckets[k]!),
      count: buckets[k]!.count,
    }));
  }
  return entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => ({
      key: k,
      label: formatLabel ? formatLabel(k) : k,
      value: metric(v),
      count: v.count,
    }));
}
