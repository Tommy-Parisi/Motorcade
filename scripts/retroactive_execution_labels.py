#!/usr/bin/env python3
"""
Retroactive execution label generator.

Reads market_state snapshots, generates synthetic IOC buy-YES orders at
multiple price points around the spread, labels fills/cancels, computes
markout, and appends ExecutionTrainingRow-compatible JSONL to the main
execution training file.

Usage:
    python3 scripts/retroactive_execution_labels.py [--output PATH] [--dry-run]

Re-run this after any BOT_RUN_DATASET_BUILD=true rebuild, as that overwrites
the training file.
"""

import argparse
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema constants (must match Rust)
# ---------------------------------------------------------------------------
EXECUTION_FEATURE_SCHEMA_VERSION = "v1"
DATASET_SCHEMA_VERSION = "v1"
EXECUTION_SOURCE_CLASS = "retroactive_synthetic"

# Price offsets to generate synthetic orders at (relative to ask, in cents).
# Negative = below ask (cancel), zero/positive = at or above ask (fill).
PRICE_OFFSETS_CENTS = [-10, -5, -2, -1, 0, 2, 5, 10]

# Markout horizons in seconds (must match Rust defaults)
MARKOUT_WINDOW_0_SECS = 300   # 5 min
MARKOUT_WINDOW_1_SECS = 1800  # 30 min

# Fill-within windows in seconds
FILL_WINDOW_0_SECS = 30
FILL_WINDOW_1_SECS = 300

# IOC orders fill instantly if limit >= ask; "terminal" after 0s effectively
IOC_FILL_LATENCY_SECS = 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_ts(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp, always returning UTC-aware datetime."""
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    return datetime.fromisoformat(ts_str)


def infer_vertical(title: str) -> str:
    t = title.upper()
    if any(k in t for k in ("NBA", "NFL", "NHL", "MLB", "SOCCER", "CS2", "CBA", "ESPORT")):
        return "sports"
    if any(k in t for k in ("BTC", "ETH", "BITCOIN", "CRYPTO")):
        return "crypto"
    if "HIGH" in t or "TEMP" in t or "WEATHER" in t:
        return "weather"
    return "other"


def ts_to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def make_client_order_id(ticker: str, ts: datetime, offset_cents: float) -> str:
    """Deterministic client_order_id so re-runs produce same IDs (idempotent)."""
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")
    offset_tag = f"off{int(offset_cents):+d}"
    return f"retro-{ticker}-{ts_str}-{offset_tag}"


def assign_splits(rows: list) -> list:
    """Temporal 70/15/15 split by feature_ts."""
    rows_sorted = sorted(rows, key=lambda r: r["feature"]["feature_ts"])
    total = max(len(rows_sorted), 1)
    for idx, row in enumerate(rows_sorted):
        ratio = idx / total
        if ratio < 0.70:
            row["split"] = "train"
        elif ratio < 0.85:
            row["split"] = "validation"
        else:
            row["split"] = "test"
    return rows_sorted


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_market_state_events(research_dir: Path) -> list[dict]:
    """Load all market_state JSONL events across all date subdirs."""
    out = []
    market_state_dir = research_dir / "market_state"
    if not market_state_dir.exists():
        return out
    for day_dir in sorted(market_state_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        for f in sorted(day_dir.glob("*.jsonl")):
            with open(f, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return out


def load_existing_ids(output_path: Path) -> set[str]:
    """Return set of existing client_order_ids to avoid duplicates."""
    if not output_path.exists():
        return set()
    ids = set()
    with open(output_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                ids.add(row.get("client_order_id", ""))
            except json.JSONDecodeError:
                pass
    return ids


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------

def build_ticker_time_series(events: list[dict]) -> dict[str, list[dict]]:
    """Map ticker → sorted list of events."""
    index = defaultdict(list)
    for e in events:
        index[e["ticker"]].append(e)
    for k in index:
        index[k].sort(key=lambda x: x["ts"])
    return dict(index)


def find_event_at_horizon(ticker_events: list[dict], base_ts: datetime, horizon_secs: int) -> dict | None:
    """Find the first event at or after base_ts + horizon_secs."""
    target = base_ts + timedelta(seconds=horizon_secs)
    for e in ticker_events:
        if parse_ts(e["ts"]) >= target:
            return e
    return None


def compute_markout_bps(ticker_events: list[dict], base_ts: datetime, fill_price: float,
                        horizon_secs: int) -> float | None:
    """Mark-out in bps for a YES buy at fill_price, measured at base_ts + horizon_secs."""
    future = find_event_at_horizon(ticker_events, base_ts, horizon_secs)
    if future is None:
        return None
    mid = future.get("mid_prob_yes")
    if mid is None:
        bid = future.get("yes_bid_cents")
        ask = future.get("yes_ask_cents")
        if bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0 / 100.0
    if mid is None:
        return None
    # YES buy: profit = mid - fill_price (both in [0,1] space)
    signed = mid - fill_price
    return (signed / max(fill_price, 0.0001)) * 10_000.0


def generate_rows_for_snapshot(event: dict, ticker_events: list[dict]) -> list[dict]:
    """Generate one ExecutionTrainingRow per price offset for a snapshot event."""
    ticker = event["ticker"]
    ts_str = event["ts"]
    ts = parse_ts(ts_str)

    bid_cents = event.get("yes_bid_cents")
    ask_cents = event.get("yes_ask_cents")
    mid_prob_yes = event.get("mid_prob_yes")
    spread_cents = event.get("spread_cents")
    volume = event.get("volume", 0.0)
    close_time_str = event.get("close_time")
    title = event.get("title", "")
    vertical = infer_vertical(title)

    if bid_cents is None or ask_cents is None:
        return []
    # Require real quotes
    if ask_cents >= 100.0 or bid_cents <= 0.0 or ask_cents <= bid_cents:
        return []

    close_time = parse_ts(close_time_str) if close_time_str else None
    time_to_close_secs = int((close_time - ts).total_seconds()) if close_time else None
    if time_to_close_secs is not None and time_to_close_secs < 0:
        return []  # Already closed

    rows = []
    for offset_cents in PRICE_OFFSETS_CENTS:
        limit_cents = ask_cents + offset_cents
        # Clamp to valid range
        if limit_cents <= 0 or limit_cents >= 100:
            continue
        limit_price = limit_cents / 100.0
        ask_price = ask_cents / 100.0

        # IOC fill logic: filled iff limit >= ask
        filled = limit_cents >= ask_cents
        fill_price = ask_price if filled else None

        # Markout (only meaningful if filled)
        markout_5m = None
        markout_30m = None
        if filled:
            markout_5m = compute_markout_bps(ticker_events, ts, ask_price, MARKOUT_WINDOW_0_SECS)
            markout_30m = compute_markout_bps(ticker_events, ts, ask_price, MARKOUT_WINDOW_1_SECS)

        # Fill-within labels: IOC fills are immediate (within 30s and 5m)
        label_filled_within_30s = filled
        label_filled_within_5m = filled

        price_vs_bid = limit_cents - bid_cents
        price_vs_ask = limit_cents - ask_cents
        aggressiveness_bps = None
        if spread_cents and spread_cents > 0:
            aggressiveness_bps = (price_vs_ask / spread_cents) * 10_000.0

        client_order_id = make_client_order_id(ticker, ts, offset_cents)

        feature = {
            "schema_version": EXECUTION_FEATURE_SCHEMA_VERSION,
            "feature_ts": ts_to_iso(ts),
            "ticker": ticker,
            "outcome_id": "yes",
            "side": "Buy",
            "tif": "Ioc",
            "title": title,
            "vertical": vertical,
            "candidate_limit_price": round(limit_price, 6),
            "candidate_observed_price": ask_price,
            "candidate_fair_price": mid_prob_yes if mid_prob_yes is not None else (bid_cents + ask_cents) / 200.0,
            "raw_edge_pct": 0.0,
            "confidence": 0.0,
            "yes_bid_cents": bid_cents,
            "yes_ask_cents": ask_cents,
            "spread_cents": spread_cents if spread_cents is not None else (ask_cents - bid_cents),
            "mid_prob_yes": mid_prob_yes,
            "volume": volume,
            "time_to_close_secs": time_to_close_secs,
            "price_vs_best_bid_cents": price_vs_bid,
            "price_vs_best_ask_cents": price_vs_ask,
            "aggressiveness_bps": aggressiveness_bps,
            "open_order_count_same_ticker": 0,
            "recent_fill_count_same_ticker": 0,
            "recent_cancel_count_same_ticker": 0,
            "same_event_exposure_notional": 0.0,
        }

        row = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "split": "",  # Assigned later
            "client_order_id": client_order_id,
            "execution_source_class": EXECUTION_SOURCE_CLASS,
            "is_bootstrap_synthetic": False,
            "is_organic_paper": True,   # Include in default training sources
            "is_live_real": False,
            "terminal_status": "Filled" if filled else "Canceled",
            "label_filled_within_30s": label_filled_within_30s,
            "label_filled_within_5m": label_filled_within_5m,
            "label_terminal_filled_qty": 1.0 if filled else 0.0,
            "label_terminal_avg_fill_price": fill_price,
            "label_canceled": not filled,
            "label_rejected": False,
            "label_markout_bps_5m": markout_5m,
            "label_markout_bps_30m": markout_30m,
            "label_realized_net_pnl": None,
            "feature": feature,
        }
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-dir",
        default="var/research",
        help="Path to research dir (default: var/research)",
    )
    parser.add_argument(
        "--output",
        default="var/features/execution/execution_training_retroactive.jsonl",
        help="Output JSONL path (appended to). Default: var/features/execution/execution_training_retroactive.jsonl",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats without writing",
    )
    parser.add_argument(
        "--max-per-ticker",
        type=int,
        default=0,
        help="Max snapshots per ticker (0 = unlimited, useful for testing)",
    )
    args = parser.parse_args()

    research_dir = Path(args.research_dir)
    output_path = Path(args.output)

    print(f"Loading market_state events from {research_dir / 'market_state'} ...", flush=True)
    events = load_market_state_events(research_dir)
    print(f"  Loaded {len(events):,} total events", flush=True)

    print("Building per-ticker time series ...", flush=True)
    ticker_ts = build_ticker_time_series(events)
    print(f"  {len(ticker_ts):,} unique tickers", flush=True)

    # Filter to real-quote snapshots
    real_quote_events = [
        e for e in events
        if e.get("yes_bid_cents", 0) > 0
        and e.get("yes_ask_cents", 100) < 100
        and e.get("yes_ask_cents", 0) > e.get("yes_bid_cents", 0)
        # Skip KXQUICKSETTLE junk
        and not e["ticker"].startswith("KXQUICKSETTLE")
    ]
    print(f"  {len(real_quote_events):,} real-quote snapshots across all tickers", flush=True)

    # Load existing IDs for dedup
    if not args.dry_run:
        existing_ids = load_existing_ids(output_path)
        print(f"  {len(existing_ids):,} existing rows in output file (will skip duplicates)", flush=True)
    else:
        existing_ids = set()

    # Per-ticker cap
    if args.max_per_ticker > 0:
        ticker_count: dict[str, int] = defaultdict(int)
        capped = []
        for e in real_quote_events:
            if ticker_count[e["ticker"]] < args.max_per_ticker:
                capped.append(e)
                ticker_count[e["ticker"]] += 1
        real_quote_events = capped
        print(f"  {len(real_quote_events):,} after applying --max-per-ticker={args.max_per_ticker}", flush=True)

    print("Generating synthetic execution rows ...", flush=True)
    all_rows = []
    skipped_dup = 0
    n_events = len(real_quote_events)
    for i, event in enumerate(real_quote_events):
        if i % 500 == 0 and i > 0:
            print(f"  {i:,}/{n_events:,} events processed, {len(all_rows):,} rows so far ...", flush=True)
        ticker = event["ticker"]
        ticker_events = ticker_ts.get(ticker, [])
        rows = generate_rows_for_snapshot(event, ticker_events)
        for row in rows:
            if row["client_order_id"] in existing_ids:
                skipped_dup += 1
                continue
            all_rows.append(row)

    print(f"  Generated {len(all_rows):,} new rows ({skipped_dup:,} skipped as duplicates)", flush=True)

    # Assign temporal splits
    all_rows = assign_splits(all_rows)

    # Stats
    n_filled = sum(1 for r in all_rows if r["label_filled_within_30s"])
    n_canceled = sum(1 for r in all_rows if r["label_canceled"])
    n_with_markout_5m = sum(1 for r in all_rows if r["label_markout_bps_5m"] is not None)
    n_with_markout_30m = sum(1 for r in all_rows if r["label_markout_bps_30m"] is not None)
    splits = defaultdict(int)
    for r in all_rows:
        splits[r["split"]] += 1

    print(f"\nSummary:")
    print(f"  Total new rows:       {len(all_rows):>8,}")
    print(f"  Filled (IOC):         {n_filled:>8,}  ({100*n_filled/max(len(all_rows),1):.1f}%)")
    print(f"  Canceled (IOC):       {n_canceled:>8,}  ({100*n_canceled/max(len(all_rows),1):.1f}%)")
    print(f"  With 5m markout:      {n_with_markout_5m:>8,}")
    print(f"  With 30m markout:     {n_with_markout_30m:>8,}")
    print(f"  Splits: train={splits['train']:,}  val={splits['validation']:,}  test={splits['test']:,}")

    if args.dry_run:
        print("\n[dry-run] Not writing output.")
        return

    # Write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "a") as f:
        for row in all_rows:
            f.write(json.dumps(row) + "\n")
    print(f"\nAppended {len(all_rows):,} rows to {output_path}")
    print("Done.")


if __name__ == "__main__":
    main()
