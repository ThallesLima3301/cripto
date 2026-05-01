// Color helpers used by table cells / pills across the dashboard.
//
// Centralizing these means the severity/regime color contract is
// defined once. If we later swap to CSS variables for theming, this
// is the single file that needs to change.

export function severityColor(severity: string | null | undefined): string {
  switch (severity) {
    case "very_strong":
      return "border-red-500/40 text-red-300 bg-red-500/10";
    case "strong":
      return "border-orange-500/40 text-orange-300 bg-orange-500/10";
    case "normal":
      return "border-amber-500/40 text-amber-300 bg-amber-500/10";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

export function regimeColor(label: string | null | undefined): string {
  switch (label) {
    case "risk_on":
      return "border-emerald-500/40 text-emerald-300 bg-emerald-500/10";
    case "neutral":
      return "border-slate-500/40 text-slate-300 bg-slate-500/10";
    case "risk_off":
      return "border-red-500/40 text-red-300 bg-red-500/10";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

export function sellSeverityColor(severity: string | null | undefined): string {
  switch (severity) {
    case "high":
      return "border-red-500/40 text-red-300 bg-red-500/10";
    case "medium":
      return "border-amber-500/40 text-amber-300 bg-amber-500/10";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

export function ruleColor(rule: string | null | undefined): string {
  switch (rule) {
    case "stop_loss":
      return "border-red-500/40 text-red-300 bg-red-500/10";
    case "trailing_stop":
      return "border-orange-500/40 text-orange-300 bg-orange-500/10";
    case "take_profit":
      return "border-emerald-500/40 text-emerald-300 bg-emerald-500/10";
    case "context_deterioration":
      return "border-amber-500/40 text-amber-300 bg-amber-500/10";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

export function watchlistStatusColor(status: string | null | undefined): string {
  switch (status) {
    case "watching":
      return "border-amber-500/40 text-amber-300 bg-amber-500/10";
    case "promoted":
      return "border-emerald-500/40 text-emerald-300 bg-emerald-500/10";
    case "expired":
      return "border-slate-700 text-slate-400 bg-slate-800/40";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

export function verdictColor(verdict: string | null | undefined): string {
  switch (verdict) {
    case "great":
    case "good":
      return "border-emerald-500/40 text-emerald-300 bg-emerald-500/10";
    case "neutral":
      return "border-slate-500/40 text-slate-300 bg-slate-500/10";
    case "poor":
    case "bad":
      return "border-red-500/40 text-red-300 bg-red-500/10";
    default:
      return "border-slate-700 text-slate-400 bg-slate-800/40";
  }
}

/** Returns a Tailwind color class for a signed P&L value (or null). */
export function pnlColor(pnl: number | null | undefined): string {
  if (pnl === null || pnl === undefined) return "text-slate-400";
  return pnl >= 0 ? "text-emerald-300" : "text-red-300";
}
