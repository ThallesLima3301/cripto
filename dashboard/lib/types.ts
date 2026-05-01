// Wire-contract types mirroring the FastAPI response shapes.
//
// These are hand-written for the MVP. When/if it becomes painful, swap
// to `openapi-typescript` against the API's /api/openapi.json — the
// shapes here intentionally match the Pydantic models in
// `crypto_monitor/dashboard/schemas.py` 1:1 so a switch is mechanical.

export type Envelope<T> = {
  data: T;
  meta: Record<string, unknown>;
};

export type RegimeLabel = "risk_on" | "neutral" | "risk_off";

export type OverviewRegime = {
  label: RegimeLabel;
  determined_at: string;
  atr_percentile: number;
};

export type OverviewAnalytics = {
  scope: "all" | "90d" | "30d";
  total_signals: number;
  win_rate: number | null;
  expectancy: number | null;
  profit_factor: number | null;
};

export type ActivityItem = {
  kind: "signal" | "sell";
  id: number;
  at: string;
  symbol: string;
  headline: string;
};

export type OverviewData = {
  signals_24h: number;
  signals_7d: number;
  watchlist_active: number;
  open_buys: number;
  sell_signals_7d: number;
  regime: OverviewRegime | null;
  analytics: OverviewAnalytics;
  recent_activity: ActivityItem[];
};

// =====================================================================
// Step 4 — list / detail page types
// =====================================================================

/** Pagination metadata returned in the envelope's `meta` for list endpoints. */
export type PageMeta = {
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
};

// ---------- /api/signals ----------

export type SignalListItem = {
  id: number;
  symbol: string;
  detected_at: string;
  candle_hour: string;
  price_at_signal: number;
  score: number;
  severity: string | null;
  trigger_reason: string | null;
  dominant_trigger_timeframe: string | null;
  drop_trigger_pct: number | null;
  rsi_1h: number | null;
  rsi_4h: number | null;
  rel_volume: number | null;
  regime_at_signal: string | null;
  watchlist_id: number | null;
};

export type SignalEvaluation = {
  evaluated_at: string | null;
  return_24h_pct: number | null;
  return_7d_pct: number | null;
  return_30d_pct: number | null;
  max_gain_7d_pct: number | null;
  max_loss_7d_pct: number | null;
  time_to_mfe_hours: number | null;
  time_to_mae_hours: number | null;
  verdict: string | null;
};

export type SignalDetail = SignalListItem & {
  drop_24h_pct: number | null;
  drop_7d_pct: number | null;
  drop_30d_pct: number | null;
  drop_180d_pct: number | null;
  distance_from_30d_high_pct: number | null;
  distance_from_180d_high_pct: number | null;
  dist_support_pct: number | null;
  support_level_price: number | null;
  reversal_signal: boolean;
  trend_context_4h: string | null;
  trend_context_1d: string | null;
  score_breakdown: Record<string, unknown>;
  evaluation: SignalEvaluation | null;
};

// ---------- /api/watchlist ----------

export type WatchlistStatus = "watching" | "promoted" | "expired";

export type WatchlistItem = {
  id: number;
  symbol: string;
  status: WatchlistStatus;
  first_seen_at: string;
  last_seen_at: string;
  last_score: number;
  expires_at: string;
  promoted_signal_id: number | null;
  resolved_at: string | null;
  resolution_reason: string | null;
};

// ---------- /api/open-buys ----------

export type OpenBuyItem = {
  id: number;
  symbol: string;
  bought_at: string;
  price: number;
  quantity: number;
  amount_invested: number;
  quote_currency: string;
  note: string | null;
  high_watermark: number | null;
  current_price: number | null;
  latest_close_at: string | null;
  pnl_pct: number | null;
  drawdown_from_high_pct: number | null;
};

// ---------- /api/sell-signals ----------

export type SellSignalItem = {
  id: number;
  symbol: string;
  buy_id: number;
  rule_triggered: string;
  severity: string;
  detected_at: string;
  price_at_signal: number;
  pnl_pct: number | null;
  regime_at_signal: string | null;
  reason: string | null;
  alerted: number;
};

// ---------- /api/analytics ----------

export type AnalyticsBucket = {
  count: number;
  win_rate: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  expectancy: number | null;
  profit_factor: number | null;
  avg_mfe_pct: number | null;
  avg_mae_pct: number | null;
  avg_time_to_mfe_hours: number | null;
  avg_time_to_mae_hours: number | null;
};

export type AnalyticsScope = "all" | "90d" | "30d";

export type AnalyticsData = {
  total_signals: number;
  overall: AnalyticsBucket;
  by_severity: Record<string, AnalyticsBucket>;
  by_regime: Record<string, AnalyticsBucket>;
  by_score_bucket: Record<string, AnalyticsBucket>;
  by_dominant_trigger: Record<string, AnalyticsBucket>;
};

// ---------- /api/weekly-summaries ----------

export type WeeklySummaryItem = {
  id: number;
  week_start: string;
  week_end: string;
  generated_at: string;
  body: string;
  signal_count: number;
  buy_count: number;
  top_drop_symbol: string | null;
  top_drop_pct: number | null;
  sent: number;
};
