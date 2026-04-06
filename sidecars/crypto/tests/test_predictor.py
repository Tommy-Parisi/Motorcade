"""
Tests for the GBM threshold-crossing predictor.
"""

import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from predictor import predict, estimate_vol, _log_returns, _realized_vol


# ── Vol estimation ─────────────────────────────────────────────────────────────

def test_log_returns_basic():
    closes = [100.0, 105.0, 100.0]
    rets   = _log_returns(closes)
    assert len(rets) == 2
    assert abs(rets[0] - math.log(105 / 100)) < 1e-10
    assert abs(rets[1] - math.log(100 / 105)) < 1e-10


def test_realized_vol_constant_prices():
    # Flat prices → zero variance → returns MIN_VOL floor
    closes = [100.0] * 20
    rets   = _log_returns(closes)
    vol    = _realized_vol(rets, 365.25 * 24 * 60)
    assert vol > 0


def test_estimate_vol_uses_15m_window_first():
    # 15 one-minute candles with some variance — should use 15m window
    closes_1m = [100.0 + i * 0.1 for i in range(20)]
    vol = estimate_vol(closes_1m, None)
    assert vol > 0


def test_estimate_vol_fallback_to_1h():
    # Only 5 one-minute candles — below 15m threshold, uses 1h fallback if available
    closes_1m = [100.0, 101.0, 100.5, 102.0, 101.5]  # 5 closes, 4 returns < MIN_RETURNS_15M
    closes_1h = [100.0 + i for i in range(10)]
    vol = estimate_vol(closes_1m, closes_1h)
    assert vol > 0


def test_estimate_vol_default_when_no_data():
    vol = estimate_vol(None, None)
    assert vol == 0.80


# ── Core GBM formula ─────────────────────────────────────────────────────────

def test_predict_at_the_money_roughly_half():
    """ATM strike with short time → prob near 0.5 (slightly below due to drift=0, lognormal skew)."""
    closes_1m = [60000.0 + i * 10 for i in range(20)]
    prob = predict(
        spot=60000.0,
        strike=60000.0,
        seconds_remaining=3600,
        closes_1m=closes_1m,
        closes_1h=None,
        below=False,
        asset="BTC",
    )
    assert 0.3 < prob < 0.7


def test_predict_deep_itm_high_probability():
    """Spot well above strike → P(S_T > K) should be high."""
    closes_1m = [70000.0] * 20
    prob = predict(
        spot=70000.0,
        strike=50000.0,
        seconds_remaining=3600,
        closes_1m=closes_1m,
        closes_1h=None,
        below=False,
        asset="BTC",
    )
    assert prob > 0.9


def test_predict_deep_otm_low_probability():
    """Spot well below strike → P(S_T > K) should be low."""
    closes_1m = [50000.0] * 20
    prob = predict(
        spot=50000.0,
        strike=70000.0,
        seconds_remaining=3600,
        closes_1m=closes_1m,
        closes_1h=None,
        below=False,
        asset="BTC",
    )
    assert prob < 0.1


def test_predict_below_flips_probability():
    """P(S_T < K) = 1 - P(S_T > K)."""
    closes_1m = [60000.0 + i * 10 for i in range(20)]
    prob_above = predict(
        spot=60000.0, strike=62000.0, seconds_remaining=3600,
        closes_1m=closes_1m, closes_1h=None, below=False, asset="BTC",
    )
    prob_below = predict(
        spot=60000.0, strike=62000.0, seconds_remaining=3600,
        closes_1m=closes_1m, closes_1h=None, below=True, asset="BTC",
    )
    assert abs(prob_above + prob_below - 1.0) < 0.001


def test_predict_zero_seconds_remaining():
    """At settlement: deterministic based on current spot vs strike."""
    closes_1m = [60000.0] * 20
    prob = predict(
        spot=60000.0, strike=55000.0, seconds_remaining=0,
        closes_1m=closes_1m, closes_1h=None, below=False, asset="BTC",
    )
    assert prob == 1.0


def test_predict_zero_seconds_below_strike():
    closes_1m = [50000.0] * 20
    prob = predict(
        spot=50000.0, strike=55000.0, seconds_remaining=0,
        closes_1m=closes_1m, closes_1h=None, below=False, asset="BTC",
    )
    assert prob == 0.0


def test_predict_clamped():
    """Result is always in [0.001, 0.999]."""
    closes_1m = [100.0] * 20
    for strike in [1.0, 1_000_000.0]:
        prob = predict(
            spot=100.0, strike=strike, seconds_remaining=60,
            closes_1m=closes_1m, closes_1h=None, below=False, asset="ETH",
        )
        assert 0.001 <= prob <= 0.999


def test_predict_longer_time_higher_uncertainty():
    """More time remaining → distribution widens → OTM strike becomes more reachable."""
    # Use realistic BTC-like prices with some variance so vol > 0
    import random
    random.seed(42)
    closes_1m = [60000.0 + random.gauss(0, 200) for _ in range(20)]
    # Strike slightly above spot — short time should be low probability, longer time higher
    prob_short = predict(
        spot=60000.0, strike=60500.0, seconds_remaining=300,
        closes_1m=closes_1m, closes_1h=None, below=False, asset="BTC",
    )
    prob_long = predict(
        spot=60000.0, strike=60500.0, seconds_remaining=86400,
        closes_1m=closes_1m, closes_1h=None, below=False, asset="BTC",
    )
    # With more time, prob should be higher (more chance to reach the strike)
    assert prob_long > prob_short
