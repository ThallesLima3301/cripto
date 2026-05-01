import type { OverviewRegime, RegimeLabel } from "@/lib/types";

const palette: Record<RegimeLabel, string> = {
  risk_on: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  neutral: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  risk_off: "bg-red-500/15 text-red-300 border-red-500/30",
};

const human: Record<RegimeLabel, string> = {
  risk_on: "Risk-on",
  neutral: "Neutral",
  risk_off: "Risk-off",
};

/**
 * Small status pill that surfaces the latest BTC regime classification.
 *
 * Renders a "—" pill when the regime feature is disabled or has not
 * produced a snapshot yet. Hover-title carries the ATR percentile +
 * timestamp so debugging without opening the API console is feasible.
 */
export function RegimeChip({ regime }: { regime: OverviewRegime | null }) {
  if (!regime) {
    return (
      <span
        className="rounded-full border border-slate-700 bg-slate-800/40 px-3 py-1 text-xs text-slate-400"
        title="Regime feature off, or no snapshot yet"
      >
        regime: —
      </span>
    );
  }
  return (
    <span
      className={`rounded-full border px-3 py-1 text-xs font-medium ${palette[regime.label]}`}
      title={`ATR percentile ${regime.atr_percentile.toFixed(0)} — ${regime.determined_at}`}
    >
      {human[regime.label]}
    </span>
  );
}
