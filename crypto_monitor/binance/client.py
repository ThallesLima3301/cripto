"""Binance public market data client.

Lean v1 implementation — exposes only `get_klines()`, which is the sole
endpoint the scan pipeline needs. Scoring reads drop/volume/RSI from
stored candles, so the 24h ticker endpoint is deliberately not exposed
(revision #5).

Key property: returned klines are guaranteed to be CLOSED candles. Any
candle whose `close_time_ms` exceeds the provided (or wall-clock) `now_ms`
is filtered out before returning — this is the single enforcement point
for the "official signals come only from closed candles" rule.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests


logger = logging.getLogger(__name__)


# Binance REST weight budget is thousands/min; we make at most
# len(symbols) * len(intervals) calls per scan, so we're far under.
_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 500


@dataclass(frozen=True)
class Kline:
    """Parsed Binance kline for one closed candle.

    Timestamps are kept as millisecond epochs at the client boundary.
    Ingestion converts them to UTC ISO strings at insert time so the
    rest of the codebase only deals with one timestamp format.
    """
    symbol: str
    interval: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


class BinanceError(RuntimeError):
    """Raised when the Binance client exhausts its retry budget."""


class BinanceClient:
    """Thin wrapper over Binance public REST endpoints.

    Only `get_klines` is exposed. All methods are synchronous; retries
    use simple exponential backoff (1s, 2s, 4s, ...) on network errors
    and HTTP 5xx responses. HTTP 4xx responses are not retried — they
    indicate a client-side mistake (bad symbol, invalid interval) that
    won't be fixed by waiting.
    """

    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        timeout: int = 10,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = _DEFAULT_LIMIT,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        now_ms: int | None = None,
    ) -> list[Kline]:
        """Fetch candles for `symbol` at `interval`.

        Behavior:
          * `limit` is clamped to `_MAX_LIMIT`.
          * `start_time_ms` / `end_time_ms`, if provided, are passed
            straight through to Binance.
          * Any candle whose `close_time_ms > now_ms` is filtered out.
            If `now_ms` is None, the current wall-clock time is used.
        """
        url = f"{self.base_url}/api/v3/klines"
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, _MAX_LIMIT),
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)

        raw = self._get_with_retry(url, params)
        cutoff_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        klines: list[Kline] = []
        for row in raw:
            close_time_ms = int(row[6])
            if close_time_ms > cutoff_ms:
                # Candle is still forming; skip it.
                continue
            klines.append(
                Kline(
                    symbol=symbol,
                    interval=interval,
                    open_time_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time_ms=close_time_ms,
                )
            )
        return klines

    # ---------- internals ----------

    def _get_with_retry(self, url: str, params: dict[str, Any]) -> list[Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Binance network error on attempt %d/%d: %s",
                    attempt + 1, self.retries + 1, exc,
                )
            else:
                if 200 <= resp.status_code < 300:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise BinanceError(
                            f"Binance returned non-JSON: {exc}"
                        ) from exc
                if 400 <= resp.status_code < 500:
                    # Client error — do not retry.
                    raise BinanceError(
                        f"Binance HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                last_error = BinanceError(
                    f"Binance HTTP {resp.status_code}: {resp.text[:200]}"
                )
                logger.warning(
                    "Binance server error on attempt %d/%d: %s",
                    attempt + 1, self.retries + 1, last_error,
                )

            if attempt < self.retries:
                backoff = 2 ** attempt
                time.sleep(backoff)

        assert last_error is not None
        raise BinanceError(f"Binance request failed after retries: {last_error}")
