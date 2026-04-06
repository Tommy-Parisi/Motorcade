"""
Price fetcher for CryptoPredictor sidecar.

Fetches spot price and recent OHLCV candles from Coinbase Advanced Trade API
(public endpoints, no auth required). Results are cached to avoid hammering
the API on every predict call.

Data sourced:
  - Spot price: GET /api/v3/brokerage/best_bid_ask (or similar public endpoint)
  - OHLCV: GET /api/v3/brokerage/market/candles
    Candle granularity: ONE_MINUTE for short-window vol, ONE_HOUR for long-window

Fallback: Binance REST API (api.binance.com/api/v3/) — same OHLCV structure,
no auth, global availability. Use if Coinbase is unavailable.

Cache structure:
  - _spot_cache: {asset -> (price, fetch_time)}
  - _ohlcv_cache: {(asset, granularity) -> ([candles], fetch_time)}

Both caches refresh every PRICE_REFRESH_SECS (default 30s). The predict
endpoint reads from cache only — no blocking network calls in the hot path.

Assets supported: BTC, ETH, SOL, XRP (maps to {asset}-USD trading pairs)

Example candle row from Coinbase:
    {"start": "1712345678", "low": "66100.0", "high": "66500.0",
     "open": "66200.0", "close": "66300.0", "volume": "123.45"}
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

ASSET_MAP = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}

COINBASE_BASE = "https://api.coinbase.com"
BINANCE_BASE  = "https://api.binance.com"

# Coinbase granularities (seconds) → label
GRANULARITY_ONE_MINUTE = "ONE_MINUTE"   # 60s candles
GRANULARITY_ONE_HOUR   = "ONE_HOUR"     # 3600s candles

# Number of candles to fetch for vol estimation windows.
# 15m vol → 15 one-minute candles; 1h vol → 60; 4h vol → 4 one-hour candles.
CANDLES_1M_COUNT = 70   # ~70 minutes; covers 15m + 1h windows with room
CANDLES_1H_COUNT = 8    # 8 hours; covers the 4h window

# Stale data threshold — callers check this
MAX_DATA_AGE_SECS = 60

# ── Cache ──────────────────────────────────────────────────────────────────────
# Keyed by asset string (e.g. "BTC"). Thread-safe via a single lock.

_cache_lock   = threading.Lock()
_spot_cache:  dict[str, tuple[float, datetime]] = {}   # asset → (price, fetch_time)
_ohlcv_1m:    dict[str, tuple[list, datetime]]  = {}   # asset → (candles, fetch_time)
_ohlcv_1h:    dict[str, tuple[list, datetime]]  = {}   # asset → (candles, fetch_time)


# ── Coinbase fetch ─────────────────────────────────────────────────────────────

def _coinbase_spot(product_id: str, session: requests.Session) -> Optional[float]:
    """Fetch mid price from Coinbase best_bid_ask endpoint."""
    try:
        url = f"{COINBASE_BASE}/api/v3/brokerage/best_bid_ask"
        resp = session.get(url, params={"product_ids": product_id}, timeout=5)
        resp.raise_for_status()
        entries = resp.json().get("pricebooks", [])
        if not entries:
            return None
        pb = entries[0]
        bid = float(pb.get("bids", [{}])[0].get("price", 0))
        ask = float(pb.get("asks", [{}])[0].get("price", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None
    except Exception as exc:
        logger.debug(f"Coinbase spot failed for {product_id}: {exc}")
        return None


def _coinbase_candles(
    product_id: str,
    granularity: str,
    count: int,
    session: requests.Session,
) -> Optional[list]:
    """Fetch recent OHLCV candles from Coinbase. Returns list of close prices (float)."""
    try:
        end   = int(time.time())
        gran_secs = 60 if granularity == GRANULARITY_ONE_MINUTE else 3600
        start = end - gran_secs * count
        url   = f"{COINBASE_BASE}/api/v3/brokerage/market/candles"
        resp  = session.get(
            url,
            params={
                "product_id":  product_id,
                "start":       str(start),
                "end":         str(end),
                "granularity": granularity,
            },
            timeout=5,
        )
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        # Each candle: {"start": str, "low": str, "high": str, "open": str, "close": str, ...}
        closes = [float(c["close"]) for c in candles if "close" in c]
        return closes if len(closes) >= 2 else None
    except Exception as exc:
        logger.debug(f"Coinbase candles failed for {product_id} gran={granularity}: {exc}")
        return None


# ── Binance fallback ───────────────────────────────────────────────────────────

def _binance_spot(symbol: str, session: requests.Session) -> Optional[float]:
    """Fetch mid price from Binance ticker/bookTicker."""
    try:
        url  = f"{BINANCE_BASE}/api/v3/ticker/bookTicker"
        resp = session.get(url, params={"symbol": symbol}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        bid  = float(data.get("bidPrice", 0))
        ask  = float(data.get("askPrice", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None
    except Exception as exc:
        logger.debug(f"Binance spot failed for {symbol}: {exc}")
        return None


def _binance_candles(
    symbol: str,
    interval: str,
    limit: int,
    session: requests.Session,
) -> Optional[list]:
    """Fetch klines from Binance. Returns list of close prices (float)."""
    try:
        url  = f"{BINANCE_BASE}/api/v3/klines"
        resp = session.get(
            url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=5,
        )
        resp.raise_for_status()
        klines = resp.json()
        # Each kline: [open_time, open, high, low, close, volume, ...]
        closes = [float(k[4]) for k in klines]
        return closes if len(closes) >= 2 else None
    except Exception as exc:
        logger.debug(f"Binance candles failed for {symbol} interval={interval}: {exc}")
        return None


# ── Refresh logic ──────────────────────────────────────────────────────────────

def _refresh_asset(asset: str, session: requests.Session) -> None:
    """Refresh spot price and OHLCV candles for one asset. Writes to cache."""
    product_id    = ASSET_MAP[asset]
    binance_sym   = asset + "USDT"
    now           = datetime.now(timezone.utc)

    # Spot price — try Coinbase, fall back to Binance
    spot = _coinbase_spot(product_id, session)
    if spot is None:
        logger.info(f"{asset}: Coinbase spot failed, trying Binance")
        spot = _binance_spot(binance_sym, session)
    if spot is None:
        logger.warning(f"{asset}: all spot sources failed")

    # 1-minute candles — try Coinbase, fall back to Binance
    closes_1m = _coinbase_candles(product_id, GRANULARITY_ONE_MINUTE, CANDLES_1M_COUNT, session)
    if closes_1m is None:
        logger.info(f"{asset}: Coinbase 1m candles failed, trying Binance")
        closes_1m = _binance_candles(binance_sym, "1m", CANDLES_1M_COUNT, session)

    # 1-hour candles — try Coinbase, fall back to Binance
    closes_1h = _coinbase_candles(product_id, GRANULARITY_ONE_HOUR, CANDLES_1H_COUNT, session)
    if closes_1h is None:
        logger.info(f"{asset}: Coinbase 1h candles failed, trying Binance")
        closes_1h = _binance_candles(binance_sym, "1h", CANDLES_1H_COUNT, session)

    with _cache_lock:
        if spot is not None:
            _spot_cache[asset]  = (spot, now)
        if closes_1m is not None:
            _ohlcv_1m[asset]    = (closes_1m, now)
        if closes_1h is not None:
            _ohlcv_1h[asset]    = (closes_1h, now)

    logger.info(
        f"cache updated: {asset}  spot={spot}  "
        f"1m_candles={len(closes_1m) if closes_1m else 0}  "
        f"1h_candles={len(closes_1h) if closes_1h else 0}"
    )


def refresh_all(refresh_secs: int) -> None:
    """Background thread: refresh all assets every refresh_secs. Runs forever."""
    session = requests.Session()
    while True:
        for asset in ASSET_MAP:
            try:
                _refresh_asset(asset, session)
            except Exception as exc:
                logger.error(f"refresh error for {asset}: {exc}", exc_info=True)
        time.sleep(refresh_secs)


def warmup() -> None:
    """Single-pass warmup for all assets. Called once at startup in a background thread."""
    session = requests.Session()
    for asset in ASSET_MAP:
        try:
            _refresh_asset(asset, session)
        except Exception as exc:
            logger.error(f"warmup error for {asset}: {exc}", exc_info=True)
    logger.info("Price fetcher warmup complete")


# ── Cache read API ─────────────────────────────────────────────────────────────

def get_spot(asset: str) -> Optional[tuple[float, datetime]]:
    """Return (price, fetch_time) or None if not cached."""
    with _cache_lock:
        return _spot_cache.get(asset)


def get_candles_1m(asset: str) -> Optional[tuple[list, datetime]]:
    """Return (closes_list, fetch_time) or None if not cached."""
    with _cache_lock:
        return _ohlcv_1m.get(asset)


def get_candles_1h(asset: str) -> Optional[tuple[list, datetime]]:
    """Return (closes_list, fetch_time) or None if not cached."""
    with _cache_lock:
        return _ohlcv_1h.get(asset)


def cache_age_secs(asset: str) -> int:
    """Return seconds since the spot price was last fetched. -1 if never cached."""
    entry = get_spot(asset)
    if entry is None:
        return -1
    _, fetch_time = entry
    return int((datetime.now(timezone.utc) - fetch_time).total_seconds())
