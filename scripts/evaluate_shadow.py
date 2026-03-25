#!/usr/bin/env python3
"""
Shadow mode evaluation report.

Joins forecast and policy shadow logs against resolved outcomes to answer:
  1. Forecast calibration — is fair_prob_yes accurate vs actual resolution?
  2. Policy hit rate    — do should_trade=true calls win more than false?
  3. PnL accuracy       — does expected_realized_pnl predict actual outcome?

Usage:
    python3 scripts/evaluate_shadow.py [--shadow-dir var/shadow] [--training var/features/forecast/forecast_training.jsonl]
"""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_outcomes(training_path: Path) -> dict[str, bool]:
    """Build ticker -> outcome_yes from forecast training rows (resolved only)."""
    outcomes = {}
    if not training_path.exists():
        return outcomes
    with open(training_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                outcome = r.get("label_outcome_yes")
                if outcome is None:
                    continue
                ticker = r.get("feature", {}).get("ticker") or r.get("ticker")
                if ticker:
                    outcomes[ticker] = bool(outcome)
            except json.JSONDecodeError:
                pass
    return outcomes


def load_jsonl_dir(shadow_dir: Path, filename: str) -> list[dict]:
    """Load all JSONL records from shadow_dir/*/*/filename."""
    rows = []
    if not shadow_dir.exists():
        return rows
    for day_dir in sorted(shadow_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        f = day_dir / filename
        if not f.exists():
            continue
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


# ---------------------------------------------------------------------------
# Forecast calibration
# ---------------------------------------------------------------------------

def evaluate_forecast(forecast_rows: list[dict], outcomes: dict[str, bool]) -> None:
    # Deduplicate: keep latest shadow record per ticker
    latest: dict[str, dict] = {}
    for row in forecast_rows:
        ticker = row.get("ticker", "")
        recorded_at = row.get("recorded_at", "")
        if ticker not in latest or recorded_at > latest[ticker].get("recorded_at", ""):
            latest[ticker] = row

    resolved = [(r, outcomes[r["ticker"]]) for r in latest.values() if r["ticker"] in outcomes]

    if not resolved:
        print("FORECAST CALIBRATION: no resolved markets yet")
        return

    print(f"\n{'='*60}")
    print(f"FORECAST CALIBRATION  ({len(resolved)} resolved markets)")
    print(f"{'='*60}")

    # Calibration buckets
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "sum_fair": 0.0, "sum_mid": 0.0})
    model_brier, mid_brier = 0.0, 0.0

    for row, outcome_yes in resolved:
        fair = row.get("fair_prob_yes", 0.5)
        mid = row.get("market_mid_prob_yes") or 0.5
        y = 1.0 if outcome_yes else 0.0

        bucket = int(fair * 10) * 10  # 0, 10, 20, ..., 90
        buckets[bucket]["n"] += 1
        buckets[bucket]["wins"] += int(outcome_yes)
        buckets[bucket]["sum_fair"] += fair
        buckets[bucket]["sum_mid"] += mid

        model_brier += (fair - y) ** 2
        mid_brier += (mid - y) ** 2

    model_brier /= len(resolved)
    mid_brier /= len(resolved)
    brier_lift = (mid_brier - model_brier) / mid_brier * 100

    print(f"\n  Brier score:  model={model_brier:.4f}  market_mid={mid_brier:.4f}  lift={brier_lift:+.1f}%")

    print(f"\n  {'Bucket':>8}  {'N':>6}  {'Win%':>7}  {'Avg Fair':>9}  {'Avg Mid':>8}  {'Diff':>7}")
    print(f"  {'-'*56}")
    for bucket in sorted(buckets):
        b = buckets[bucket]
        n = b["n"]
        win_pct = b["wins"] / n * 100
        avg_fair = b["sum_fair"] / n
        avg_mid = b["sum_mid"] / n
        print(f"  {bucket:>6}%+  {n:>6}  {win_pct:>6.1f}%  {avg_fair:>9.3f}  {avg_mid:>8.3f}  {win_pct/100 - avg_fair:>+7.3f}")

    # Vertical breakdown
    vert_stats = defaultdict(lambda: {"n": 0, "wins": 0, "model_brier": 0.0, "mid_brier": 0.0})
    for row, outcome_yes in resolved:
        v = row.get("vertical", "other")
        fair = row.get("fair_prob_yes", 0.5)
        mid = row.get("market_mid_prob_yes") or 0.5
        y = 1.0 if outcome_yes else 0.0
        vert_stats[v]["n"] += 1
        vert_stats[v]["wins"] += int(outcome_yes)
        vert_stats[v]["model_brier"] += (fair - y) ** 2
        vert_stats[v]["mid_brier"] += (mid - y) ** 2

    print(f"\n  {'Vertical':>12}  {'N':>6}  {'Win%':>7}  {'ModelBrier':>11}  {'MidBrier':>9}  {'Lift':>7}")
    print(f"  {'-'*60}")
    for v, s in sorted(vert_stats.items(), key=lambda x: -x[1]["n"]):
        n = s["n"]
        mb = s["model_brier"] / n
        xb = s["mid_brier"] / n
        lift = (xb - mb) / xb * 100 if xb > 0 else 0
        print(f"  {v:>12}  {n:>6}  {s['wins']/n*100:>6.1f}%  {mb:>11.4f}  {xb:>9.4f}  {lift:>+6.1f}%")


# ---------------------------------------------------------------------------
# Policy shadow evaluation
# ---------------------------------------------------------------------------

def evaluate_policy(policy_rows: list[dict], outcomes: dict[str, bool]) -> None:
    # Deduplicate: keep latest shadow record per (ticker, outcome_id)
    latest: dict[tuple, dict] = {}
    for row in policy_rows:
        key = (row.get("ticker", ""), row.get("outcome_id", "yes"))
        recorded_at = row.get("recorded_at", "")
        if key not in latest or recorded_at > latest[key].get("recorded_at", ""):
            latest[key] = row

    resolved = []
    for (ticker, outcome_id), row in latest.items():
        if ticker not in outcomes:
            continue
        outcome_yes = outcomes[ticker]
        # A YES buy wins if outcome_yes=True; a NO buy wins if outcome_yes=False
        won = (outcome_id == "yes" and outcome_yes) or (outcome_id == "no" and not outcome_yes)
        resolved.append((row, won))

    if not resolved:
        print("\nPOLICY EVALUATION: no resolved markets yet")
        return

    print(f"\n{'='*60}")
    print(f"POLICY SHADOW EVALUATION  ({len(resolved)} resolved decisions)")
    print(f"{'='*60}")

    trade_yes = [(r, w) for r, w in resolved if r.get("should_trade")]
    trade_no  = [(r, w) for r, w in resolved if not r.get("should_trade")]

    def stats(group):
        if not group:
            return {"n": 0, "win_pct": 0, "avg_exp_pnl": 0, "avg_fill_prob": 0}
        wins = sum(w for _, w in group)
        return {
            "n": len(group),
            "win_pct": wins / len(group) * 100,
            "avg_exp_pnl": sum(r.get("expected_realized_pnl", 0) for r, _ in group) / len(group),
            "avg_fill_prob": sum(r.get("expected_fill_prob", 0) for r, _ in group) / len(group),
        }

    sy = stats(trade_yes)
    sn = stats(trade_no)

    print(f"\n  {'':20}  {'N':>6}  {'Win%':>7}  {'Avg ExpPnL':>11}  {'Avg FillProb':>13}")
    print(f"  {'-'*65}")
    print(f"  {'should_trade=true':20}  {sy['n']:>6}  {sy['win_pct']:>6.1f}%  {sy['avg_exp_pnl']:>11.2f}  {sy['avg_fill_prob']:>13.3f}")
    print(f"  {'should_trade=false':20}  {sn['n']:>6}  {sn['win_pct']:>6.1f}%  {sn['avg_exp_pnl']:>11.2f}  {sn['avg_fill_prob']:>13.3f}")

    if sy["n"] > 0 and sn["n"] > 0:
        lift = sy["win_pct"] - sn["win_pct"]
        print(f"\n  Hit rate lift (should_trade=true vs false): {lift:+.1f}pp")

    # Expected PnL calibration
    if trade_yes:
        print(f"\n  Expected PnL calibration (should_trade=true):")
        print(f"  {'ExpPnL bucket':>15}  {'N':>6}  {'Win%':>7}")
        print(f"  {'-'*32}")
        buckets = defaultdict(lambda: {"n": 0, "wins": 0})
        for row, won in trade_yes:
            pnl = row.get("expected_realized_pnl", 0)
            bucket = int(pnl / 5) * 5  # 0-5, 5-10, 10-15, ...
            buckets[bucket]["n"] += 1
            buckets[bucket]["wins"] += int(won)
        for b in sorted(buckets):
            s = buckets[b]
            print(f"  ${b:>4}-{b+5:<4}        {s['n']:>6}  {s['wins']/s['n']*100:>6.1f}%")

    # Top winning and losing tickers
    sorted_by_pnl = sorted(trade_yes, key=lambda x: -x[0].get("expected_realized_pnl", 0))
    print(f"\n  Top 10 highest expected_pnl decisions (resolved):")
    print(f"  {'Ticker':40}  {'Side':>4}  {'ExpPnL':>8}  {'Won':>5}")
    print(f"  {'-'*65}")
    for row, won in sorted_by_pnl[:10]:
        ticker = row.get("ticker", "")[:38]
        side = row.get("outcome_id", "?")
        pnl = row.get("expected_realized_pnl", 0)
        print(f"  {ticker:40}  {side:>4}  {pnl:>8.2f}  {'YES' if won else 'NO':>5}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-dir", default="var/shadow", help="Shadow root directory")
    parser.add_argument("--training", default="var/features/forecast/forecast_training.jsonl",
                        help="Forecast training JSONL (source of ground truth outcomes)")
    args = parser.parse_args()

    shadow_dir = Path(args.shadow_dir)
    training_path = Path(args.training)

    print("Loading resolved outcomes from training data...", flush=True)
    outcomes = load_outcomes(training_path)
    resolved_count = sum(1 for v in outcomes.values() if v is not None)
    print(f"  {resolved_count:,} resolved tickers", flush=True)

    print("Loading forecast shadow logs...", flush=True)
    forecast_rows = load_jsonl_dir(shadow_dir / "forecast", "forecast_shadow.jsonl")
    print(f"  {len(forecast_rows):,} forecast shadow records", flush=True)

    print("Loading policy shadow logs...", flush=True)
    policy_rows = load_jsonl_dir(shadow_dir / "policy", "policy_shadow.jsonl")
    print(f"  {len(policy_rows):,} policy shadow records", flush=True)

    evaluate_forecast(forecast_rows, outcomes)
    evaluate_policy(policy_rows, outcomes)

    print(f"\n{'='*60}")
    print("Note: run outcome backfill first to maximize resolved tickers:")
    print("  BOT_RUN_OUTCOME_BACKFILL=true cargo run --release --quiet")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
