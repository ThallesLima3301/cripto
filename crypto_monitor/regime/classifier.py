"""BTC-based market regime classification.

Examines BTC daily candles and returns a ``RegimeSnapshot`` describing
the current macro environment.  The label is one of:

  * **risk_on**  — BTC EMA(20) > EMA(50) AND ATR is below the
    high-volatility percentile.  Conditions are favourable for
    dip-buying.
  * **risk_off** — BTC EMA(20) < EMA(50) AND ATR is above the
    high-volatility percentile.  Conditions suggest caution; the signal
    threshold should be raised.
  * **neutral**  — everything else (mixed signals).

The classification is intentionally simple, rule-based, and fully
inspectable via the snapshot fields.  No ML, no hidden state.
"""

from __future__ import annotations

from crypto_monitor.indicators.atr import atr as compute_atr, true_range
from crypto_monitor.indicators.trend import ema
from crypto_monitor.indicators.types import Candle
from crypto_monitor.regime.types import RegimeSnapshot
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


def classify_regime(
    candles_1d_btc: list[Candle],
    *,
    ema_short_period: int = 20,
    ema_long_period: int = 50,
    atr_period: int = 14,
    atr_lookback: int = 90,
    atr_high_percentile: float = 70.0,
    determined_at: str | None = None,
) -> RegimeSnapshot | None:
    """Classify BTC regime from daily candles.

    Returns ``None`` if there are fewer than ``ema_long_period`` candles
    (insufficient history to compute the slow EMA).
    """
    closes = [c.close for c in candles_1d_btc]

    if len(closes) < ema_long_period:
        return None

    # ---- EMAs ----
    ema_short = ema(closes, ema_short_period)
    ema_long = ema(closes, ema_long_period)
    if not ema_short or not ema_long:
        return None

    ema_short_val = ema_short[-1]
    ema_long_val = ema_long[-1]

    # ---- ATR + percentile ----
    current_atr = compute_atr(candles_1d_btc, period=atr_period)
    if current_atr is None:
        return None

    atr_pctile = _atr_percentile(candles_1d_btc, atr_period, atr_lookback)

    # ---- classification ----
    ema_bullish = ema_short_val > ema_long_val
    ema_bearish = ema_short_val < ema_long_val
    vol_high = atr_pctile > atr_high_percentile

    if ema_bullish and not vol_high:
        label = "risk_on"
    elif ema_bearish and vol_high:
        label = "risk_off"
    else:
        label = "neutral"

    return RegimeSnapshot(
        label=label,
        btc_ema_short=ema_short_val,
        btc_ema_long=ema_long_val,
        btc_atr_14d=current_atr,
        atr_percentile=atr_pctile,
        determined_at=determined_at or to_utc_iso(now_utc()),
    )


def _atr_percentile(
    candles: list[Candle],
    atr_period: int,
    lookback: int,
) -> float:
    """Compute where the current ATR sits within its recent range.

    Calculates ATR at each step over the last ``lookback`` candles and
    returns the percentile rank of the current value (0–100).

    If there are fewer historical ATR values than ``lookback``, uses
    whatever is available.
    """
    tr_values = true_range(candles)
    if len(tr_values) < atr_period:
        return 50.0  # neutral default when insufficient data

    # Build the ATR series: seed then Wilder-smooth.
    atr_series: list[float] = []
    avg = sum(tr_values[:atr_period]) / atr_period
    atr_series.append(avg)
    for i in range(atr_period, len(tr_values)):
        avg = (avg * (atr_period - 1) + tr_values[i]) / atr_period
        atr_series.append(avg)

    # Use the last `lookback` ATR values (or all if fewer).
    window = atr_series[-lookback:] if len(atr_series) >= lookback else atr_series
    current = window[-1]

    if len(window) < 2:
        return 50.0

    # Percentile rank: fraction of values ≤ current × 100.
    count_le = sum(1 for v in window if v <= current)
    return (count_le / len(window)) * 100.0
