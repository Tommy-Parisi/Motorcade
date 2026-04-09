"""
WeatherPredictor sidecar — HTTP service for the Kalshi trading bot.

Prediction path
---------------
Uses GEFS 31-member ensemble forecasts from NOMADS. A background thread fetches
and caches ensemble data for every supported city for today and tomorrow,
refreshing every GEFS_REFRESH_SECS.
The /predict endpoint reads from that cache and returns in well under the Rust
bot's 3-second timeout.

Response contract (motorcade standard)
---------------------------------------
    {
        "probability":    0.62,       # P(daily_high > floor_strike_f)
        "data_age_secs":  1800,       # seconds since GEFS data was fetched
        "data_source_ok": true,       # false → Rust bot falls back to bucket model
        "model_version":  "gefs_v1"
    }

data_source_ok is false when:
  - GEFS cache is empty (startup warmup not complete)
  - Cached data is older than MAX_DATA_AGE_SECS (default 2 h)
  - Fewer than MIN_MEMBERS_REQUIRED ensemble members succeeded

Endpoints
---------
    GET /health              → {"status": "ok", "cities": [...], "cache_keys": [...], "model_version": "gefs_v1"}
    GET /predict?ticker=...  → motorcade response contract above

Supported tickers
-----------------
New format:  KXHIGHT{CITY}-{DATE}-{T|B}{THRESHOLD}
  Examples:  KXHIGHTBOS-26APR01-T70, KXHIGHTDAL-26APR01-B84.5

Legacy format (Philadelphia): KXHIGH{PHI|PHIL|PHILLY|PHL}-{DATE}-{T|B}{THRESHOLD}

Environment variables
---------------------
    WEATHER_SIDECAR_HOST         Optional. Default: 127.0.0.1
    WEATHER_SIDECAR_PORT         Optional. Default: 8765
    GEFS_REFRESH_SECS            Optional. Default: 7200 (2 hours)
    GEFS_MAX_DATA_AGE_SECS       Optional. Default: 7200 (2 hours)
    GEFS_PREDICTION_LOG_DIR      Optional. Default: var/logs/gefs_predictions
"""

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

from gefs_fetcher import CityConfig, GEFSResult, fetch_ensemble_daily_highs, MEMBERS
from ensemble_predictor import predict as ensemble_predict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="WeatherPredictor Sidecar", version="3.0")

# ── Config ─────────────────────────────────────────────────────────────────────

GEFS_REFRESH_SECS  = int(os.getenv("GEFS_REFRESH_SECS",      "7200"))
MAX_DATA_AGE_SECS  = int(os.getenv("GEFS_MAX_DATA_AGE_SECS", "7200"))
PREDICTION_LOG_DIR = Path(os.getenv("GEFS_PREDICTION_LOG_DIR", "var/logs/gefs_predictions"))

MODEL_VERSION = "gefs_v1"

# ── City map ───────────────────────────────────────────────────────────────────
#
# Keys are the city codes extracted by _parse_ticker (everything alpha after "KXHIGH").
# New Kalshi format: KXHIGHT{CITY} → extracted code = "T" + city letters.
# Legacy Philly format: KXHIGH{PHI|PHIL|PHILLY|PHL} → extracted code = city letters.
#
# Coordinates are for the primary official weather station (usually ASOS airport).
# BBox is a 3° window centred on the station (1.5° each direction, rounded).

def _bbox(lat: float, lon: float, pad: float = 1.5) -> dict:
    return {
        "toplat":    str(round(lat + pad, 1)),
        "bottomlat": str(round(lat - pad, 1)),
        "leftlon":   str(round(lon - pad, 1)),
        "rightlon":  str(round(lon + pad, 1)),
    }

CITY_MAP: dict[str, CityConfig] = {
    # ── New-format cities (KXHIGHT prefix) ──────────────────────────────────
    "TBOS":  CityConfig("Boston",          42.36, -71.01, _bbox(42.36, -71.01)),
    "TDAL":  CityConfig("Dallas",          32.90, -97.04, _bbox(32.90, -97.04)),
    "THOU":  CityConfig("Houston",         29.99, -95.34, _bbox(29.99, -95.34)),
    "TSEA":  CityConfig("Seattle",         47.45, -122.31, _bbox(47.45, -122.31)),
    "TPHX":  CityConfig("Phoenix",         33.44, -112.01, _bbox(33.44, -112.01)),
    "TSATX": CityConfig("San Antonio",     29.53, -98.47, _bbox(29.53, -98.47)),
    "TLV":   CityConfig("Las Vegas",       36.08, -115.15, _bbox(36.08, -115.15)),
    "TATL":  CityConfig("Atlanta",         33.64,  -84.43, _bbox(33.64,  -84.43)),
    "TMIN":  CityConfig("Minneapolis",     44.88,  -93.22, _bbox(44.88,  -93.22)),
    "TNOLA": CityConfig("New Orleans",     29.99,  -90.26, _bbox(29.99,  -90.26)),
    "TDC":   CityConfig("Washington DC",   38.85,  -77.04, _bbox(38.85,  -77.04)),
    "TSFO":  CityConfig("San Francisco",   37.62, -122.38, _bbox(37.62, -122.38)),
    "TOKC":  CityConfig("Oklahoma City",   35.39,  -97.60, _bbox(35.39,  -97.60)),
    # ── Legacy Philadelphia codes (KXHIGH{PHI|PHIL|PHILLY|PHL}) ─────────────
    "PHI":   CityConfig("Philadelphia",    39.87,  -75.24, _bbox(39.87, -75.24)),
    "PHIL":  CityConfig("Philadelphia",    39.87,  -75.24, _bbox(39.87, -75.24)),
    "PHILLY":CityConfig("Philadelphia",    39.87,  -75.24, _bbox(39.87, -75.24)),
    "PHL":   CityConfig("Philadelphia",    39.87,  -75.24, _bbox(39.87, -75.24)),
}

# ── Prediction log ─────────────────────────────────────────────────────────────

_log_lock = threading.Lock()


def _write_prediction_log(record: dict) -> None:
    """Append one prediction record to today's JSONL log. Silently swallows errors."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = PREDICTION_LOG_DIR / f"predictions_{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"
        with _log_lock:
            with open(path, "a") as f:
                f.write(line)
    except Exception as exc:
        logger.warning(f"prediction log write failed: {exc}")


# ── Cache ──────────────────────────────────────────────────────────────────────
#
# Keyed by (city_code, target_date). Each value is a GEFSResult.
# A single lock guards all reads and writes.

_cache: dict[tuple[str, date], GEFSResult] = {}
_cache_lock = threading.Lock()


def _refresh(city_code: str, target_date: date) -> None:
    """Fetch GEFS data for one (city, target_date) and store in cache."""
    city_cfg = CITY_MAP[city_code]
    logger.info(f"GEFS refresh: city={city_cfg.name} date={target_date}")
    result = fetch_ensemble_daily_highs(target_date, city_cfg)
    if result is not None:
        with _cache_lock:
            _cache[(city_code, target_date)] = result
        logger.info(
            f"GEFS cache updated: city={city_cfg.name} date={target_date} "
            f"members={result.n_members}"
        )
    else:
        logger.warning(f"GEFS refresh failed: city={city_cfg.name} date={target_date}")


def _background_refresh() -> None:
    """Background thread: refresh all cities × {-1, today, +1, +2} every GEFS_REFRESH_SECS.
    Covers today-1 because markets for yesterday can still be open awaiting resolution.
    Covers today+2 because weather markets open ~14:00 UTC roughly 2 days before resolution.
    Sleeps first so startup warmup and first background fetch don't overlap."""
    time.sleep(GEFS_REFRESH_SECS)
    while True:
        today = datetime.now(timezone.utc).date()
        for city_code in CITY_MAP:
            # Deduplicate: multiple Philly aliases all point to same config — skip dupes.
            if CITY_MAP[city_code].name == "Philadelphia" and city_code != "PHI":
                continue
            for target_date in [today - timedelta(days=1), today, today + timedelta(days=1), today + timedelta(days=2)]:
                try:
                    _refresh(city_code, target_date)
                except Exception as exc:
                    logger.error(
                        f"GEFS refresh error: city={city_code} date={target_date}: {exc}",
                        exc_info=True,
                    )
        time.sleep(GEFS_REFRESH_SECS)


# ── Ticker parsing ─────────────────────────────────────────────────────────────

def _parse_ticker(ticker: str) -> tuple[Optional[str], Optional[date], Optional[float], bool]:
    """
    Parse a Kalshi weather ticker into (city_code, target_date, floor_strike_f, below).

    New format:     KXHIGHT{CITY}-{DATE}-{T|B}{THRESHOLD}
    Legacy format:  KXHIGH{CITY}-{DATE}-{T|B}{THRESHOLD}

    Examples:
        KXHIGHTBOS-26APR01-T70   → ("TBOS",  date(2026, 4, 1),   70.0, False)
        KXHIGHTDAL-26APR01-B84.5 → ("TDAL",  date(2026, 4, 1),   84.5, True)
        KXHIGHPHI-26APR15-T55    → ("PHI",   date(2026, 4, 15),  55.0, False)
    """
    upper = ticker.upper()
    if not upper.startswith("KXHIGH"):
        return None, None, None, False

    rest     = upper[len("KXHIGH"):]
    city_end = next((i for i, c in enumerate(rest) if not c.isalpha()), len(rest))
    city     = rest[:city_end]

    date_match = re.search(r"-(\d{2}[A-Z]{3}\d{2})-", upper)
    target_date = None
    if date_match:
        try:
            target_date = datetime.strptime(date_match.group(1), "%y%b%d").date()
        except ValueError:
            pass

    thresh_match = re.search(r"-([TB])(\d+(?:\.\d+)?)$", upper)
    floor_strike_f = float(thresh_match.group(2)) if thresh_match else None
    below = thresh_match.group(1) == "B" if thresh_match else False

    return city, target_date, floor_strike_f, below


# ── Routes ─────────────────────────────────────────────────────────────────────

def _startup_warmup() -> None:
    """Warm the cache for all cities in the background so uvicorn starts immediately.
    Runs once at startup, then hands off to _background_refresh."""
    today = datetime.now(timezone.utc).date()
    seen_names: set[str] = set()
    for city_code, city_cfg in CITY_MAP.items():
        if city_cfg.name in seen_names:
            continue
        seen_names.add(city_cfg.name)
        for target_date in [today - timedelta(days=1), today, today + timedelta(days=1), today + timedelta(days=2)]:
            try:
                _refresh(city_code, target_date)
            except Exception as exc:
                logger.error(
                    f"Startup GEFS fetch failed: city={city_cfg.name} date={target_date}: {exc}"
                )
    logger.info("Startup warmup complete")


@app.on_event("startup")
def on_startup():
    # Warmup runs in background — uvicorn is ready to serve immediately.
    # Predict calls before warmup finishes return data_source_ok=false (cache miss).
    threading.Thread(target=_startup_warmup, daemon=True).start()
    threading.Thread(target=_background_refresh, daemon=True).start()
    logger.info("Startup: warmup + background refresh threads started")


@app.get("/health")
def health():
    with _cache_lock:
        cache_keys = sorted(f"{c}:{d}" for c, d in _cache.keys())
    return {
        "status":        "ok",
        "cities":        sorted(set(cfg.name for cfg in CITY_MAP.values())),
        "cache_keys":    cache_keys,
        "model_version": MODEL_VERSION,
    }


@app.get("/predict")
def predict(ticker: str):
    city_code, target_date, floor_strike_f, below = _parse_ticker(ticker)

    if city_code is None or city_code not in CITY_MAP:
        raise HTTPException(404, f"Unsupported city code '{city_code}' in ticker '{ticker}'")
    if target_date is None:
        raise HTTPException(400, f"Cannot parse target date from ticker '{ticker}'")
    if floor_strike_f is None:
        raise HTTPException(400, f"Cannot parse floor_strike_f from ticker '{ticker}'")

    with _cache_lock:
        result = _cache.get((city_code, target_date))

    if result is None:
        logger.warning(f"predict: cache miss for city={city_code} date={target_date} ({ticker})")
        return {
            "probability":    0.5,
            "data_age_secs":  -1,
            "data_source_ok": False,
            "model_version":  MODEL_VERSION,
        }

    data_age_secs = int((datetime.now(timezone.utc) - result.fetch_time).total_seconds())

    # For past target dates the weather already happened — a refetch would not
    # produce better data, so the age limit does not apply.
    today = datetime.now(timezone.utc).date()
    if target_date >= today and data_age_secs > MAX_DATA_AGE_SECS:
        logger.warning(
            f"predict: stale cache for city={city_code} date={target_date} "
            f"(age={data_age_secs}s > max={MAX_DATA_AGE_SECS}s)"
        )
        return {
            "probability":    0.5,
            "data_age_secs":  data_age_secs,
            "data_source_ok": False,
            "model_version":  MODEL_VERSION,
        }

    prob = ensemble_predict(
        member_highs_f=result.member_highs_f,
        floor_strike_f=floor_strike_f,
        target_date=target_date,
        members=MEMBERS[:result.n_members],
    )
    if below:
        prob = 1.0 - prob

    logger.info(
        f"predict  ticker={ticker}  city={city_code}  threshold={floor_strike_f}°F  "
        f"below={below}  prob={prob:.4f}  members={result.n_members}  age={data_age_secs}s"
    )

    _write_prediction_log({
        "ts":             datetime.now(timezone.utc).isoformat(),
        "ticker":         ticker,
        "city":           city_code,
        "target_date":    str(target_date),
        "threshold_f":    floor_strike_f,
        "probability":    prob,
        "n_members":      result.n_members,
        "member_highs_f": result.member_highs_f,
        "run_time":       result.run_time.isoformat(),
        "data_age_secs":  data_age_secs,
        "model_version":  MODEL_VERSION,
    })

    return {
        "probability":    prob,
        "data_age_secs":  data_age_secs,
        "data_source_ok": True,
        "model_version":  MODEL_VERSION,
    }


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEATHER_SIDECAR_PORT", "8765"))
    host = os.getenv("WEATHER_SIDECAR_HOST", "127.0.0.1")
    uvicorn.run("sidecar:app", host=host, port=port, reload=False)
