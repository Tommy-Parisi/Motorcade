"""
Tests for ensemble_predictor.predict and _apply_bias.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ensemble_predictor import predict, _apply_bias, BIAS_CORRECTIONS


# ── _apply_bias ────────────────────────────────────────────────────────────────

def test_apply_bias_no_table_returns_raw():
    assert _apply_bias("c00", 4, 70.0) == 70.0


def test_apply_bias_with_correction(monkeypatch):
    monkeypatch.setitem(BIAS_CORRECTIONS, 4, {"c00": 2.0})
    assert _apply_bias("c00", 4, 70.0) == 68.0


def test_apply_bias_unknown_member_defaults_to_zero(monkeypatch):
    monkeypatch.setitem(BIAS_CORRECTIONS, 4, {"c00": 2.0})
    assert _apply_bias("p01", 4, 70.0) == 70.0


def test_apply_bias_unknown_month_defaults_to_zero(monkeypatch):
    monkeypatch.setitem(BIAS_CORRECTIONS, 4, {"c00": 2.0})
    assert _apply_bias("c00", 7, 70.0) == 70.0


# ── predict: basic probability ─────────────────────────────────────────────────

def test_all_above_threshold():
    highs = [60.0, 62.0, 65.0, 70.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert prob == 0.995


def test_all_below_threshold():
    highs = [40.0, 42.0, 45.0, 50.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert prob == 0.005


def test_half_above_threshold():
    highs = [50.0, 50.0, 60.0, 60.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert abs(prob - 0.5) < 1e-9


def test_exact_threshold_not_counted():
    highs = [55.0, 55.0, 55.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert prob == 0.005


def test_single_member_above():
    highs = [56.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert prob == 0.995


def test_single_member_below():
    highs = [54.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert prob == 0.005


def test_31_member_ensemble_fraction():
    highs = [60.0] * 10 + [50.0] * 21
    prob = predict(highs, floor_strike_f=55.0)
    assert abs(prob - 10 / 31) < 1e-9


# ── predict: clamping ──────────────────────────────────────────────────────────

def test_output_never_zero_or_one():
    highs_all_above = [70.0] * 31
    highs_all_below = [40.0] * 31
    assert predict(highs_all_above, 55.0) < 1.0
    assert predict(highs_all_below, 55.0) > 0.0


def test_output_within_bounds():
    import random
    random.seed(42)
    for _ in range(50):
        highs = [random.uniform(40, 90) for _ in range(31)]
        prob = predict(highs, floor_strike_f=65.0)
        assert 0.0 < prob < 1.0


# ── predict: empty input ───────────────────────────────────────────────────────

def test_empty_member_highs_raises():
    with pytest.raises(ValueError, match="empty"):
        predict([], floor_strike_f=55.0)


# ── predict: bias correction applied via target_date ──────────────────────────

def test_bias_correction_shifts_votes(monkeypatch):
    monkeypatch.setitem(BIAS_CORRECTIONS, 4, {"c00": 3.0, "p01": 3.0, "p02": 3.0})
    highs   = [56.0, 56.0, 56.0]
    members = ["c00", "p01", "p02"]
    d = date(2026, 4, 15)

    prob_no_correction   = predict(highs, 55.0)
    prob_with_correction = predict(highs, 55.0, target_date=d, members=members)

    assert prob_no_correction == 0.995
    assert prob_with_correction == 0.005


def test_target_date_none_skips_month_lookup():
    highs = [60.0, 50.0]
    prob = predict(highs, floor_strike_f=55.0)
    assert abs(prob - 0.5) < 1e-9
