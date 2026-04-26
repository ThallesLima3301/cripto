"""Relative Strength Index — Wilder's smoothing.

Returns the latest RSI value for a closing-price series, or `None` if
there aren't at least `period + 1` data points.

Definition (period = 14):
  delta[i]   = close[i] - close[i-1]
  gain[i]    = max(delta[i], 0)
  loss[i]    = max(-delta[i], 0)
  avg_gain_0 = mean(gain[1..period])
  avg_loss_0 = mean(loss[1..period])
  avg_gain_n = (avg_gain_(n-1) * (period-1) + gain[n]) / period
  avg_loss_n = same with losses
  RS         = avg_gain / avg_loss
  RSI        = 100 - 100 / (1 + RS)

Edge cases:
  - if avg_gain == 0 and avg_loss == 0 -> RSI = 50 (no movement at all,
    neutral; treating it as 100 would falsely signal "fully overbought"
    on a flat market)
  - if avg_loss == 0 (and gains exist)  -> RSI = 100 (all up moves)
  - if avg_gain == 0 (and losses exist) -> RSI = 0   (all down moves)
"""

from __future__ import annotations


def rsi(closes: list[float], period: int = 14) -> float | None:
    if period < 2 or len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Return the per-bar RSI series aligned with ``closes``.

    The first ``period`` entries (the warmup band) are ``None``; from
    index ``period`` onward each entry is the Wilder RSI computed over
    the trailing ``period`` deltas. Used by the bullish-divergence
    detector (Block 27) which needs RSI **at specific candle indices**,
    not just the latest scalar.

    Edge cases mirror :func:`rsi`: a fully flat trailing window
    yields 50.0; all-up yields 100.0; all-down yields 0.0.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if period < 2 or n < period + 1:
        return out

    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_avgs(avg_gain, avg_loss)

    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
