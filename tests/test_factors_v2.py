"""Block 16 — ATR-normalized drop scoring for `score_drop_magnitude`.

These tests lock in two things:

  1. **Backward compatibility**: when no ATR data is provided (or
     it's invalid), the helper behaves exactly like v1. Every
     existing caller and every existing test keeps working.

  2. **Volatility awareness**: when valid ATR + price are provided,
     the same raw drop produces different scores depending on
     recent volatility. Quiet markets amplify modest drops; volatile
     markets dampen them.

Thresholds used (from the shared `scoring_settings` fixture):

    drop_1h          = (1.0, 2.0, 3.0)
    drop_1h_points   = (5, 8, 12)
    drop_magnitude cap = 25

So a raw 1h drop of 2.0 normally scores 8. When normalized by a
quiet atr_pct=0.5, the effective drop is 4.0 → top tier → 12. When
normalized by a stormy atr_pct=3.0, the effective drop is ~0.67 →
below the first threshold → 0.
"""

from __future__ import annotations

import pytest

from crypto_monitor.signals.factors import score_drop_magnitude


DROP_MAGNITUDE_CAP = 25


def _drops(**kwargs) -> dict[str, float | None]:
    """Build a drops dict with explicit None defaults for absent horizons."""
    out: dict[str, float | None] = {
        "1h": None, "24h": None, "7d": None, "30d": None, "180d": None,
    }
    out.update(kwargs)
    return out


# ---------- backward compatibility ----------

def test_atr_none_falls_back_to_v1(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    v1 = score_drop_magnitude(drops, th, DROP_MAGNITUDE_CAP)
    v2 = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=None, price=100.0
    )

    # Same points, same dominant horizon, same raw drop_pct.
    assert v1[0] == v2[0] == 8
    assert v1[1] == v2[1] == "1h"
    assert v1[2] == v2[2] == pytest.approx(2.0)
    # Detail dict flags that normalization did NOT happen.
    assert v2[3]["atr_normalized"] is False
    assert "atr_pct" not in v2[3]


def test_atr_zero_falls_back_to_v1(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    v1 = score_drop_magnitude(drops, th, DROP_MAGNITUDE_CAP)
    v2 = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.0, price=100.0
    )

    assert v1[0] == v2[0] == 8
    assert v2[3]["atr_normalized"] is False
    assert "atr_pct" not in v2[3]


def test_price_none_falls_back_to_v1(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    v2 = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=1.0, price=None
    )

    assert v2[0] == 8
    assert v2[3]["atr_normalized"] is False


def test_price_zero_falls_back_to_v1(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    v2 = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=1.0, price=0.0
    )

    assert v2[0] == 8
    assert v2[3]["atr_normalized"] is False


# ---------- same drop, different ATR → different scores ----------

def test_same_raw_drop_different_atr_different_scores(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    # Quiet market: atr_pct = 0.5% → normalized 1h drop = 4.0 → 12 pts.
    quiet = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.5, price=100.0
    )
    # Volatile market: atr_pct = 2% → normalized 1h drop = 1.0 → 5 pts.
    volatile = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=2.0, price=100.0
    )

    assert quiet[0] > volatile[0]


def test_quiet_market_amplifies_moderate_drop(scoring_settings):
    """A 2% drop in a quiet market should score higher than a raw 2% drop."""
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    raw = score_drop_magnitude(drops, th, DROP_MAGNITUDE_CAP)
    quiet = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.5, price=100.0
    )

    # Raw: drop_1h=2.0 → tier 2.0 → 8 pts.
    # Quiet: atr_pct=0.5 → normalized 4.0 → tier 3.0 → 12 pts.
    assert raw[0] == 8
    assert quiet[0] == 12
    assert quiet[3]["atr_normalized"] is True
    assert quiet[3]["atr_pct"] == pytest.approx(0.5)


def test_volatile_market_dampens_moderate_drop(scoring_settings):
    """A 2% drop in a high-volatility market should score lower than raw."""
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    raw = score_drop_magnitude(drops, th, DROP_MAGNITUDE_CAP)
    # atr_pct=3% → normalized drop = 0.67 → below drop_1h[0]=1.0 → 0 pts.
    volatile = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=3.0, price=100.0
    )

    assert raw[0] == 8
    assert volatile[0] == 0
    assert volatile[3]["atr_normalized"] is True


# ---------- detail dict observability ----------

def test_detail_dict_exposes_atr_pct_when_normalized(scoring_settings):
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    _, _, _, detail = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.5, price=100.0
    )
    assert detail["atr_normalized"] is True
    assert detail["atr_pct"] == pytest.approx(0.5)
    # Raw drop_pct survives for downstream callers.
    assert detail["drop_pct"] == pytest.approx(2.0)


def test_detail_dict_atr_pct_matches_ratio(scoring_settings):
    """atr_pct must equal atr_1h / price * 100 exactly."""
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    _, _, _, detail = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=50.0, price=40000.0
    )
    # 50 / 40000 * 100 = 0.125
    assert detail["atr_pct"] == pytest.approx(0.125)


def test_zero_drop_still_reports_observability(scoring_settings):
    """An all-zero drops dict still surfaces the atr_normalized flag."""
    th = scoring_settings.thresholds
    drops = _drops()  # all None

    _, dom, raw, detail = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=1.0, price=100.0
    )
    assert dom is None
    assert raw is None
    assert detail["points"] == 0
    assert detail["atr_normalized"] is True
    assert detail["atr_pct"] == pytest.approx(1.0)


def test_dominant_horizon_reports_raw_drop_pct_not_normalized(scoring_settings):
    """The caller-visible `drop_trigger_pct` must stay in raw %.

    Downstream code (alerts, weekly summaries, the `drop_trigger_pct`
    DB column) assumes this is a real percentage. Normalizing it
    would break the "BTC fell 2.0%" rendering.
    """
    th = scoring_settings.thresholds
    drops = _drops(**{"1h": 2.0})

    _, dom_tf, drop_pct, detail = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.5, price=100.0
    )
    assert dom_tf == "1h"
    assert drop_pct == pytest.approx(2.0)
    assert detail["drop_pct"] == pytest.approx(2.0)


# ---------- multi-horizon interaction ----------

def test_normalization_can_promote_a_different_horizon_to_dominant(scoring_settings):
    """With normalization, the horizon with the highest POINTS wins.

    Here the 24h drop normalizes into a much higher tier than the
    raw 1h drop, so 24h should become dominant.
    """
    th = scoring_settings.thresholds
    # Raw: 1h=2.0 → 8 pts; 24h=4.0 → drop_24h[0]=3.0 → 8 pts. Ties → 1h wins.
    # With atr_pct=0.5: 1h=4.0 → 12 pts; 24h=8.0 → drop_24h[2]=8.0 → 18 pts.
    drops = _drops(**{"1h": 2.0, "24h": 4.0})

    raw = score_drop_magnitude(drops, th, DROP_MAGNITUDE_CAP)
    normalized = score_drop_magnitude(
        drops, th, DROP_MAGNITUDE_CAP, atr_1h=0.5, price=100.0
    )

    # Raw winner picks whichever `max` sees first for equal points;
    # not asserting the tie-break direction here — focus on the
    # normalized case lifting 24h clear of 1h.
    assert normalized[1] == "24h"
    assert normalized[0] == 18
    # And it's strictly better than the raw-mode score.
    assert normalized[0] > raw[0]
