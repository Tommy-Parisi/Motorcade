#!/usr/bin/env python3
"""
backfill_historical.py

Pulls settled Kalshi markets + outcomes and writes them as forecast training rows
in the same schema the Rust pipeline already reads. Then cleans junk from any
existing forecast_training.jsonl.

Requirements:
    pip install cryptography requests

Usage:
    python scripts/backfill_historical.py                         # fetch + clean
    python scripts/backfill_historical.py --max-markets 20000     # pull more history
    python scripts/backfill_historical.py --with-history          # also fetch per-market price snapshots (slower)
    python scripts/backfill_historical.py --clean-only            # only clean existing file, no API calls

After running, rebuild training data:
    BOT_RUN_DATASET_BUILD=true cargo run --release
    BOT_RUN_FORECAST_TRAIN=true cargo run --release
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("ERROR: pip install cryptography")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_dotenv(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip()
                    # skip commented-out prod/demo lines like "#PROD-- KEY=val"
                    if k and not k.startswith("#"):
                        env[k] = v
    except FileNotFoundError:
        pass
    return env


def sign_request(private_key_pem: str, method: str, path: str):
    """RSA-PSS SHA256 signature matching the Rust client's auth_headers()."""
    timestamp_ms = str(int(time.time() * 1000))
    msg = (timestamp_ms + method.upper() + path).encode()
    key = serialization.load_pem_private_key(
        private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
        password=None,
        backend=default_backend(),
    )
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return timestamp_ms, base64.b64encode(sig).decode()


def kalshi_get(session, base_url, key_id, key_pem, path, params=None):
    timestamp_ms, sig = sign_request(key_pem, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    return session.get(f"{base_url}{path}", headers=headers, params=params, timeout=30)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_settled_markets(session, base_url, key_id, key_pem, max_markets):
    markets = []
    cursor = None
    path = "/trade-api/v2/markets"

    while len(markets) < max_markets:
        remaining = max_markets - len(markets)
        params = {"status": "settled", "limit": str(min(remaining, 1000))}
        if cursor:
            params["cursor"] = cursor

        resp = kalshi_get(session, base_url, key_id, key_pem, path, params)
        if not resp.ok:
            print(f"\n  WARNING: markets fetch failed ({resp.status_code}): {resp.text[:300]}")
            break

        data = resp.json()
        batch = data.get("markets", [])
        if not batch:
            break

        markets.extend(batch)
        print(f"  fetched {len(markets)} settled markets...", end="\r", flush=True)

        cursor = data.get("cursor")
        if not cursor:
            break

        time.sleep(0.1)

    print(f"  fetched {len(markets)} settled markets total        ")
    return markets


def fetch_market_history(session, base_url, key_id, key_pem, ticker):
    """Try to get historical price snapshots for a single market. Returns list or None."""
    path = f"/trade-api/v2/markets/{ticker}/history"
    resp = kalshi_get(session, base_url, key_id, key_pem, path, {"limit": "200"})
    if not resp.ok:
        return None
    data = resp.json()
    # Kalshi may return 'history', 'price_history', or 'snapshots'
    return data.get("history") or data.get("price_history") or data.get("snapshots")


# ---------------------------------------------------------------------------
# Parsing market data
# ---------------------------------------------------------------------------

def parse_outcome(market):
    """Extract bool outcome_yes from settled market dict."""
    yr = market.get("yes_result")
    if isinstance(yr, bool):
        return yr
    if yr is not None:
        try:
            return bool(yr)
        except Exception:
            pass

    sv = market.get("settlement_value") or market.get("settlementValue")
    if sv is not None:
        try:
            f = float(sv)
            if abs(f - 1.0) < 1e-9:
                return True
            if abs(f) < 1e-9:
                return False
        except (TypeError, ValueError):
            pass

    r = market.get("result") or market.get("outcome")
    if isinstance(r, str):
        r = r.strip().lower()
        if r in ("yes", "true", "1"):
            return True
        if r in ("no", "false", "0"):
            return False

    return None


def parse_resolution_status(market):
    status = (
        market.get("status") or market.get("market_status") or market.get("marketStatus") or ""
    ).lower()
    if status in ("settled", "resolved", "finalized", "closed", "expired"):
        return "resolved"
    if status in ("canceled", "void", "cancelled"):
        return "canceled"
    # Infer from presence of settlement fields
    if market.get("yes_result") is not None or market.get("settlement_value") is not None:
        return "resolved"
    return "unresolved"


def parse_prices(market):
    """Returns (yes_bid_cents, yes_ask_cents) or (None, None)."""
    bid = market.get("yes_bid") or market.get("yesBid")
    ask = market.get("yes_ask") or market.get("yesAsk")

    # Some endpoints return dollar-string variants
    bid_dollars = market.get("yes_bid_dollars") or market.get("yesBidDollars")
    ask_dollars = market.get("yes_ask_dollars") or market.get("yesAskDollars")
    if bid_dollars is not None:
        try:
            bid = float(bid_dollars) * 100
        except (TypeError, ValueError):
            pass
    if ask_dollars is not None:
        try:
            ask = float(ask_dollars) * 100
        except (TypeError, ValueError):
            pass

    try:
        bid = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        bid = None
    try:
        ask = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        ask = None

    return bid, ask


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def market_to_state_event(market, ts=None, cycle_id="backfill"):
    """Convert a settled Kalshi market dict to a MarketStateEvent-compatible dict."""
    ticker = market.get("ticker", "")
    close_time = market.get("close_time") or market.get("closeTime") or market.get("expiration_time")

    # Use close_time as the feature timestamp (snapshot at settlement)
    if ts is None:
        ts = close_time or now_iso()

    bid, ask = parse_prices(market)

    mid = None
    spread = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0 / 100.0
        spread = ask - bid

    volume = market.get("volume") or 0.0
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        volume = 0.0

    return {
        "schema_version": "v1",
        "ts": ts,
        "ticker": ticker,
        "title": market.get("title") or "",
        "subtitle": market.get("subtitle"),
        "market_type": market.get("market_type") or market.get("marketType"),
        "event_ticker": market.get("event_ticker") or market.get("eventTicker"),
        "series_ticker": market.get("series_ticker") or market.get("seriesTicker"),
        "close_time": close_time,
        "yes_bid_cents": bid,
        "yes_ask_cents": ask,
        "mid_prob_yes": mid,
        "spread_cents": spread,
        "volume": volume,
        "traded_count_delta": None,
        "source": "backfill_snapshot",
        "cycle_id": cycle_id,
    }


def history_snapshot_to_state_event(market, snapshot, cycle_id="backfill_history"):
    """Build a state event from a historical price snapshot entry."""
    ts = (
        snapshot.get("ts")
        or snapshot.get("timestamp")
        or snapshot.get("end_period_ts")
        or snapshot.get("time")
    )
    if ts is None:
        return None

    event = market_to_state_event(market, ts=ts, cycle_id=cycle_id)

    # Override prices from the snapshot if present
    snap_bid = snapshot.get("yes_bid") or snapshot.get("yes_bid_close") or snapshot.get("bid")
    snap_ask = snapshot.get("yes_ask") or snapshot.get("yes_ask_close") or snapshot.get("ask")
    if snap_bid is not None:
        try:
            event["yes_bid_cents"] = float(snap_bid)
        except (TypeError, ValueError):
            pass
    if snap_ask is not None:
        try:
            event["yes_ask_cents"] = float(snap_ask)
        except (TypeError, ValueError):
            pass
    b, a = event.get("yes_bid_cents"), event.get("yes_ask_cents")
    if b is not None and a is not None:
        event["mid_prob_yes"] = (b + a) / 2.0 / 100.0
        event["spread_cents"] = a - b

    return event


def market_to_outcome(market):
    close_time = market.get("close_time") or market.get("closeTime")
    return {
        "schema_version": "v1",
        "ticker": market.get("ticker", ""),
        "resolved_at": now_iso(),
        "outcome_yes": parse_outcome(market),
        "resolution_status": parse_resolution_status(market),
        "source": "backfill",
        "close_time": close_time,
    }


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_existing_outcome_tickers(path):
    tickers = set()
    if not Path(path).exists():
        return tickers
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tickers.add(json.loads(line)["ticker"])
            except (json.JSONDecodeError, KeyError):
                pass
    return tickers


def append_jsonl(path, records):
    if not records:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def overwrite_jsonl(path, records):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Data cleaning
# ---------------------------------------------------------------------------

def clean_forecast_training(path):
    """
    Remove junk rows from forecast_training.jsonl:
      - time_to_close_secs < 0  (already expired at capture time)
      - KXQUICKSETTLE series    (synthetic test markets)
      - bid=0 AND ask=0         (no real orderbook) — UNLESS source is backfill
    """
    p = Path(path)
    if not p.exists():
        print(f"  {path} not found, skipping")
        return 0, 0

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]

    original = len(lines)
    kept = []
    n_expired = n_quicksettle = n_no_quotes = 0

    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        feat = row.get("feature", {})
        ticker = feat.get("ticker", "")
        bid = feat.get("yes_bid_cents")
        ask = feat.get("yes_ask_cents")
        ttc = feat.get("time_to_close_secs")
        source = feat.get("source", "")

        if ttc is not None and ttc < 0:
            n_expired += 1
            continue

        if "QUICKSETTLE" in ticker.upper():
            n_quicksettle += 1
            continue

        # Only drop no-quote rows for live-captured data, not backfill.
        # Backfill rows with bid=ask=0 may represent settled-NO markets
        # where 0/0 is a legitimate post-settlement price.
        if "backfill" not in source:
            bid_zero = bid is None or bid == 0.0
            ask_zero = ask is None or ask == 0.0
            if bid_zero and ask_zero:
                n_no_quotes += 1
                continue

        kept.append(row)

    overwrite_jsonl(path, kept)
    print(f"  original : {original:>7}")
    print(f"  expired  : {n_expired:>7}  (time_to_close_secs < 0)")
    print(f"  quicksettle: {n_quicksettle:>5}  (KXQUICKSETTLE)")
    print(f"  no quotes: {n_no_quotes:>7}  (bid=ask=0, live-captured)")
    print(f"  kept     : {len(kept):>7}")
    return original, len(kept)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill historical Kalshi forecast training data")
    parser.add_argument("--max-markets", type=int, default=10000, help="Max settled markets to fetch (default 10000)")
    parser.add_argument("--with-history", action="store_true", help="Also fetch per-market price snapshots (much slower)")
    parser.add_argument("--clean-only", action="store_true", help="Only clean existing training data, skip API calls")
    parser.add_argument("--research-dir", default="var/research")
    parser.add_argument("--features-dir", default="var/features")
    args = parser.parse_args()

    # Load credentials
    env = load_dotenv(".env")
    env.update({k: v for k, v in os.environ.items() if v})  # os.environ wins

    base_url = env.get("KALSHI_API_BASE_URL", "https://api.elections.kalshi.com")
    key_id = env.get("KALSHI_API_KEY_ID", "")
    key_path = env.get("KALSHI_PRIVATE_KEY_PATH", "")
    key_pem_inline = env.get("KALSHI_PRIVATE_KEY_PEM", "")

    # --- Clean existing training data ---
    forecast_path = f"{args.features_dir}/forecast/forecast_training.jsonl"
    print("=== Cleaning existing forecast_training.jsonl ===")
    clean_forecast_training(forecast_path)

    if args.clean_only:
        print("\nDone (clean-only mode).")
        return

    # --- Load private key ---
    if key_pem_inline:
        key_pem = key_pem_inline
    elif key_path:
        try:
            with open(key_path) as f:
                key_pem = f.read()
        except FileNotFoundError:
            print(f"ERROR: private key not found at {key_path}")
            sys.exit(1)
    else:
        print("ERROR: KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM not set")
        sys.exit(1)

    if not key_id:
        print("ERROR: KALSHI_API_KEY_ID not set")
        sys.exit(1)

    # --- Fetch settled markets ---
    print(f"\n=== Fetching settled markets from {base_url} ===")
    session = requests.Session()

    try:
        markets = fetch_settled_markets(session, base_url, key_id, key_pem, args.max_markets)
    except Exception as e:
        print(f"ERROR fetching markets: {e}")
        sys.exit(1)

    if not markets:
        print("No settled markets returned. Check credentials and API URL.")
        return

    # Load existing outcomes to skip already-processed tickers
    outcomes_path = f"{args.research_dir}/outcomes/outcomes.jsonl"
    existing_tickers = load_existing_outcome_tickers(outcomes_path)
    print(f"  existing outcomes on disk: {len(existing_tickers)}")

    # --- Process ---
    state_events = []
    new_outcomes = []
    n_skipped_dup = 0
    n_skipped_no_outcome = 0

    for i, market in enumerate(markets):
        ticker = market.get("ticker", "")
        resolution = parse_resolution_status(market)
        outcome = parse_outcome(market)

        # Skip duplicates
        if ticker in existing_tickers:
            n_skipped_dup += 1
            continue

        # Only include markets with a known YES/NO outcome
        if resolution != "resolved" or outcome is None:
            n_skipped_no_outcome += 1
            continue

        # Base snapshot at close
        state_events.append(market_to_state_event(market))
        new_outcomes.append(market_to_outcome(market))

        # Optionally fetch price history for additional mid-lifetime snapshots
        if args.with_history:
            history = fetch_market_history(session, base_url, key_id, key_pem, ticker)
            if history:
                for snap in history:
                    event = history_snapshot_to_state_event(market, snap)
                    if event:
                        state_events.append(event)
            if i % 50 == 0:
                print(f"  history: processed {i+1}/{len(markets)} markets, {len(state_events)} events so far...", end="\r", flush=True)
            time.sleep(0.05)

    print(f"\n  new state events    : {len(state_events)}")
    print(f"  new outcomes        : {len(new_outcomes)}")
    print(f"  skipped (duplicate) : {n_skipped_dup}")
    print(f"  skipped (no outcome): {n_skipped_no_outcome}")

    if not state_events:
        print("\nNothing new to write.")
        return

    # --- Write ---
    state_path = f"{args.research_dir}/market_state/backfill/market_state.jsonl"
    append_jsonl(state_path, state_events)
    print(f"\nWrote {len(state_events)} state events  -> {state_path}")

    append_jsonl(outcomes_path, new_outcomes)
    print(f"Appended {len(new_outcomes)} outcomes      -> {outcomes_path}")

    print("\n=== Next steps ===")
    print("  BOT_RUN_DATASET_BUILD=true cargo run --release")
    print("  BOT_RUN_FORECAST_TRAIN=true cargo run --release")
    print("  BOT_RUN_MODEL_REPORT=true cargo run --release")


if __name__ == "__main__":
    main()
