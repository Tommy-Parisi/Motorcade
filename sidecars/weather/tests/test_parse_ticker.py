"""
Tests for sidecar._parse_ticker.

Covers: new KXHIGHT{CITY} format, legacy KXHIGH{PHI*} format,
city extraction, date parsing, T/B direction flag, decimal thresholds, edge cases.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar import _parse_ticker, CITY_MAP


# ── New-format tickers (KXHIGHT prefix) ───────────────────────────────────────

def test_new_format_boston_t():
    city, target_date, threshold, below = _parse_ticker("KXHIGHTBOS-26APR01-T70")
    assert city == "TBOS"
    assert target_date == date(2026, 4, 1)
    assert threshold == 70.0
    assert below is False


def test_new_format_dallas_b():
    city, target_date, threshold, below = _parse_ticker("KXHIGHTDAL-26APR01-B84.5")
    assert city == "TDAL"
    assert target_date == date(2026, 4, 1)
    assert threshold == 84.5
    assert below is True


def test_new_format_seattle_b_decimal():
    city, target_date, threshold, below = _parse_ticker("KXHIGHTSEA-26APR01-B53.5")
    assert city == "TSEA"
    assert threshold == 53.5
    assert below is True


def test_new_format_houston():
    city, target_date, threshold, below = _parse_ticker("KXHIGHTHOU-26APR01-T91")
    assert city == "THOU"
    assert threshold == 91.0
    assert below is False


def test_new_format_phoenix():
    city, _, threshold, _ = _parse_ticker("KXHIGHTPHX-26APR01-T100")
    assert city == "TPHX"
    assert threshold == 100.0


def test_new_format_san_antonio():
    city, _, _, _ = _parse_ticker("KXHIGHTSATX-26APR01-T85")
    assert city == "TSATX"


def test_new_format_las_vegas():
    city, _, _, _ = _parse_ticker("KXHIGHTLV-26APR01-T95")
    assert city == "TLV"


def test_new_format_atlanta():
    city, _, _, _ = _parse_ticker("KXHIGHTATL-26APR01-T80")
    assert city == "TATL"


def test_new_format_minneapolis():
    city, _, _, _ = _parse_ticker("KXHIGHTMIN-26APR01-T65")
    assert city == "TMIN"


def test_new_format_new_orleans():
    city, _, _, _ = _parse_ticker("KXHIGHTNOLA-26APR01-T85")
    assert city == "TNOLA"


def test_new_format_dc():
    city, _, _, _ = _parse_ticker("KXHIGHTDC-26APR01-T75")
    assert city == "TDC"


def test_new_format_san_francisco():
    city, _, _, _ = _parse_ticker("KXHIGHTSFO-26APR01-T65")
    assert city == "TSFO"


def test_new_format_okc():
    city, _, _, _ = _parse_ticker("KXHIGHTOKC-26APR01-T80")
    assert city == "TOKC"


def test_all_new_format_cities_in_city_map():
    """Every new-format city code extracted from known tickers must be in CITY_MAP."""
    new_format_tickers = [
        "KXHIGHTBOS-26APR01-T70",
        "KXHIGHTDAL-26APR01-T84",
        "KXHIGHTHOU-26APR01-T91",
        "KXHIGHTSEA-26APR01-T54",
        "KXHIGHTPHX-26APR01-T100",
        "KXHIGHTSATX-26APR01-T85",
        "KXHIGHTLV-26APR01-T95",
        "KXHIGHTATL-26APR01-T80",
        "KXHIGHTMIN-26APR01-T65",
        "KXHIGHTNOLA-26APR01-T85",
        "KXHIGHTDC-26APR01-T75",
        "KXHIGHTSFO-26APR01-T65",
        "KXHIGHTOKC-26APR01-T80",
    ]
    for ticker in new_format_tickers:
        city, _, _, _ = _parse_ticker(ticker)
        assert city in CITY_MAP, f"City code '{city}' from ticker '{ticker}' not in CITY_MAP"


# ── Legacy Philadelphia format ─────────────────────────────────────────────────

def test_legacy_phi():
    city, target_date, threshold, below = _parse_ticker("KXHIGHPHI-26APR15-T55")
    assert city == "PHI"
    assert target_date == date(2026, 4, 15)
    assert threshold == 55.0
    assert below is False


def test_legacy_phil():
    city, target_date, threshold, below = _parse_ticker("KXHIGHPHIL-25JUL31-T92")
    assert city == "PHIL"
    assert target_date == date(2025, 7, 31)
    assert threshold == 92.0


def test_legacy_philly():
    city, _, threshold, below = _parse_ticker("KXHIGHPHILLY-26MAR10-T50")
    assert city == "PHILLY"
    assert threshold == 50.0
    assert below is False


def test_legacy_phl():
    city, _, threshold, below = _parse_ticker("KXHIGHPHL-26JAN05-T32")
    assert city == "PHL"
    assert threshold == 32.0
    assert below is False


# ── Decimal thresholds ─────────────────────────────────────────────────────────

def test_decimal_threshold_b():
    _, _, threshold, below = _parse_ticker("KXHIGHTDAL-26APR01-B88.5")
    assert threshold == 88.5
    assert below is True


def test_decimal_threshold_t():
    _, _, threshold, below = _parse_ticker("KXHIGHTSEA-26APR01-T49.5")
    assert threshold == 49.5
    assert below is False


# ── B/T direction flag ─────────────────────────────────────────────────────────

def test_b_ticker_sets_below_flag():
    _, _, _, below = _parse_ticker("KXHIGHTBOS-26APR01-B69.5")
    assert below is True


def test_t_ticker_clears_below_flag():
    _, _, _, below = _parse_ticker("KXHIGHTBOS-26APR01-T70")
    assert below is False


def test_b_and_t_same_ticker_differ_only_in_flag():
    _, _, thresh_t, below_t = _parse_ticker("KXHIGHTBOS-26APR01-T70")
    _, _, thresh_b, below_b = _parse_ticker("KXHIGHTBOS-26APR01-B70")
    assert thresh_t == thresh_b
    assert below_t is False
    assert below_b is True


# ── Case insensitivity ─────────────────────────────────────────────────────────

def test_case_insensitive_new_format():
    city, target_date, threshold, below = _parse_ticker("kxhightbos-26apr01-t70")
    assert city == "TBOS"
    assert target_date == date(2026, 4, 1)
    assert threshold == 70.0
    assert below is False


# ── Failure / unsupported inputs ───────────────────────────────────────────────

def test_non_weather_ticker_returns_none_city():
    city, _, _, _ = _parse_ticker("KXBTCD-26APR15-T55")
    assert city is None


def test_missing_threshold_returns_none():
    _, _, threshold, _ = _parse_ticker("KXHIGHTBOS-26APR01")
    assert threshold is None


def test_missing_date_returns_none():
    _, target_date, _, _ = _parse_ticker("KXHIGHTBOS-T70")
    assert target_date is None


def test_empty_string():
    city, target_date, threshold, below = _parse_ticker("")
    assert city is None
    assert target_date is None
    assert threshold is None
    assert below is False
