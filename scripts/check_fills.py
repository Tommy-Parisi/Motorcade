#!/usr/bin/env python3
"""
Check resolution status of paper fills.

Cross-references the trade journal against resolved outcomes in the forecast
training data. Run after BOT_RUN_OUTCOME_BACKFILL + BOT_RUN_DATASET_BUILD to
get the latest resolutions.

Usage:
    python3 scripts/check_fills.py [--journal PATH] [--training PATH] [--series KXNBA,KXBTC]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_fills(journal_path: Path) -> dict[str, dict]:
    """Return ticker -> fill info for all Filled paper orders (latest fill wins per ticker)."""
    intents = {}
    fills = {}

    with open(journal_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = r.get("event", "")
            payload = r.get("payload", {})

            if event == "order_intent":
                order = payload.get("order", {})
                coid = order.get("client_order_id", "")
                intents[coid] = {
                    "ticker": order.get("market_id", ""),
                    "outcome_id": order.get("outcome_id", "yes"),
                    "limit_price": order.get("limit_price"),
                    "ts": r.get("ts", ""),
                }
            elif event == "order_report":
                report = payload.get("report", {})
                coid = report.get("client_order_id", "")
                if report.get("status") == "Filled" and coid in intents:
                    fills[coid] = {
                        **intents[coid],
                        "fill_price": report.get("avg_fill_price"),
                        "filled_qty": report.get("filled_qty", 0),
                    }

    # Deduplicate: keep latest fill per ticker
    by_ticker: dict[str, dict] = {}
    for info in sorted(fills.values(), key=lambda x: x["ts"]):
        by_ticker[info["ticker"]] = info
    return by_ticker


def load_outcomes(training_path: Path) -> dict[str, bool]:
    """Return ticker -> outcome_yes for all resolved markets."""
    outcomes = {}
    with open(training_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = r.get("label_outcome_yes")
            if outcome is None:
                continue
            ticker = r.get("feature", {}).get("ticker", "")
            if ticker:
                outcomes[ticker] = bool(outcome)
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="var/logs/trade_journal.jsonl")
    parser.add_argument("--training", default="var/features/forecast/forecast_training.jsonl")
    parser.add_argument("--series", default="", help="Comma-separated series filter (e.g. KXNBA,KXBTC)")
    args = parser.parse_args()

    fills = load_fills(Path(args.journal))
    outcomes = load_outcomes(Path(args.training))

    series_filter = [s.strip() for s in args.series.split(",") if s.strip()]
    if series_filter:
        fills = {t: v for t, v in fills.items()
                 if any(t.startswith(s) for s in series_filter)}

    resolved = {t: outcomes[t] for t in fills if t in outcomes}
    unresolved = [t for t in fills if t not in outcomes]

    total = len(fills)
    n_resolved = len(resolved)
    wins = sum(
        1 for t, outcome in resolved.items()
        if (fills[t]["outcome_id"] == "yes") == outcome
    )
    losses = n_resolved - wins

    # --- Summary ---
    print(f"\n{'='*60}")
    print(f"FILL RESOLUTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Total unique filled tickers : {total}")
    print(f"  Resolved                    : {n_resolved} ({n_resolved/total*100:.1f}%)" if total else "  No fills found")
    print(f"  Still open                  : {len(unresolved)}")
    if n_resolved:
        print(f"  Wins / Losses               : {wins} / {losses}  ({wins/n_resolved*100:.1f}% win rate)")

    # --- By series ---
    by_series: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "open": 0})
    for ticker, info in fills.items():
        series = ticker.split("-")[0]
        if ticker in resolved:
            outcome = resolved[ticker]
            won = (info["outcome_id"] == "yes") == outcome
            if won:
                by_series[series]["wins"] += 1
            else:
                by_series[series]["losses"] += 1
        else:
            by_series[series]["open"] += 1

    print(f"\n  {'Series':<26} {'W':>4} {'L':>4} {'Open':>6} {'Win%':>7}")
    print(f"  {'-'*52}")
    for series, s in sorted(by_series.items(), key=lambda x: -(x[1]["wins"] + x[1]["losses"] + x[1]["open"])):
        n = s["wins"] + s["losses"]
        win_pct = f"{s['wins']/n*100:.1f}%" if n else "  —"
        print(f"  {series:<26} {s['wins']:>4} {s['losses']:>4} {s['open']:>6} {win_pct:>7}")

    # --- Resolved detail ---
    if resolved:
        print(f"\n  {'Result':<6} {'Ticker':<46} {'Side':>4} {'Fill':>6} {'OutcYes':>8}")
        print(f"  {'-'*72}")
        for ticker in sorted(resolved, key=lambda t: fills[t]["ts"]):
            info = fills[ticker]
            outcome = resolved[ticker]
            won = (info["outcome_id"] == "yes") == outcome
            fill = info.get("fill_price")
            fill_str = f"{fill:.3f}" if fill is not None else "   ?"
            print(
                f"  {'WIN ' if won else 'LOSS':<6} {ticker:<46} {info['outcome_id']:>4}"
                f" {fill_str:>6} {str(outcome):>8}"
            )

    # --- Still open ---
    if unresolved:
        print(f"\n  Open positions ({len(unresolved)}):")
        for ticker in sorted(unresolved)[:30]:
            info = fills[ticker]
            fill = info.get("fill_price")
            fill_str = f"{fill:.3f}" if fill is not None else "?"
            print(f"    {ticker:<48} side={info['outcome_id']} fill={fill_str}")
        if len(unresolved) > 30:
            print(f"    ... and {len(unresolved)-30} more")

    print()


if __name__ == "__main__":
    main()
