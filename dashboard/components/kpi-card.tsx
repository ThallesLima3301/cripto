type Props = {
  label: string;
  value: string | number;
  hint?: string | null;
};

/**
 * One KPI tile. Pure presentation — caller owns the formatting.
 *
 * `tabular-nums` is set so a row of cards aligns even when the
 * numbers have different digit counts (e.g. `4` next to `1234`).
 */
export function KpiCard({ label, value, hint }: Props) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-400">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
        {value}
      </div>
      {hint ? (
        <div className="mt-1 text-xs text-slate-500">{hint}</div>
      ) : null}
    </div>
  );
}
