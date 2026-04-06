#!/usr/bin/env python3
"""
Simulate realistic PnL from the trade journal with configurable risk guards.

Paper mode never applies cooldowns or notional caps, so the raw journal is
misleading (75 fills on one market ≠ 75 independent bets). This script
replays the journal with the same guards that live mode uses and computes
what PnL would have been.

Usage:
    python3 scripts/simulate_pnl.py
    python3 scripts/simulate_pnl.py --notional-cap 500 --fee-pct 7
    python3 scripts/simulate_pnl.py --cooldown 3600   # re-add hourly cooldown

Kalshi PnL model (event contracts, 100-cent settlement):
    Buy YES at P, qty Q:
        Win  → profit  = Q * (1 - P) * (1 - fee_rate)
        Lose → loss    = Q * P
    Buy NO at P, qty Q:
        Win  → profit  = Q * (1 - P) * (1 - fee_rate)
        Lose → loss    = Q * P
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_journal(journal_path: Path):
    intents, reports = {}, []
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
                    "limit_price": order.get("limit_price", 0.0),
                    "ts": parse_ts(r["ts"]),
                }
            elif event == "order_report":
                rep = payload.get("report", {})
                coid = rep.get("client_order_id", "")
                if rep.get("status") == "Filled" and coid in intents:
                    reports.append({
                        **intents[coid],
                        "fill_price": rep.get("avg_fill_price") or intents[coid]["limit_price"],
                        "filled_qty": rep.get("filled_qty", 0.0),
                        "coid": coid,
                    })
    reports.sort(key=lambda r: r["ts"])
    return reports


def load_outcomes(training_path: Path) -> dict[str, bool]:
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


def simulate(fills, notional_cap: float, cooldown_secs: int) -> list[dict]:
    """Apply guards and return the subset of fills that would have executed."""
    open_notional: dict[str, float] = defaultdict(float)
    last_fill_ts: dict[str, datetime] = {}
    accepted = []

    for f in fills:
        ticker = f["ticker"]
        notional = f["filled_qty"] * f["fill_price"]

        # Cooldown check
        if cooldown_secs > 0 and ticker in last_fill_ts:
            age = (f["ts"] - last_fill_ts[ticker]).total_seconds()
            if age < cooldown_secs:
                continue

        # Notional cap check
        if open_notional[ticker] + notional > notional_cap:
            continue

        open_notional[ticker] += notional
        last_fill_ts[ticker] = f["ts"]
        accepted.append(f)

    return accepted


def compute_pnl(fills: list[dict], outcomes: dict[str, bool], fee_pct: float) -> dict:
    fee_rate = fee_pct / 100.0
    total_pnl = 0.0
    total_cost = 0.0
    wins = losses = unresolved = 0
    by_series: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "wins": 0, "losses": 0, "orders": 0})

    for f in fills:
        ticker = f["ticker"]
        series = ticker.split("-")[0]
        qty = f["filled_qty"]
        price = f["fill_price"]
        cost = qty * price
        total_cost += cost
        by_series[series]["cost"] += cost
        by_series[series]["orders"] += 1

        if ticker not in outcomes:
            unresolved += 1
            continue

        outcome_yes = outcomes[ticker]
        bet_yes = f["outcome_id"] == "yes"
        won = bet_yes == outcome_yes

        if won:
            pnl = qty * (1.0 - price) * (1.0 - fee_rate)
            wins += 1
        else:
            pnl = -cost
            losses += 1

        total_pnl += pnl
        by_series[series]["pnl"] += pnl
        if won:
            by_series[series]["wins"] += 1
        else:
            by_series[series]["losses"] += 1

    return {
        "total_pnl": total_pnl,
        "total_cost": total_cost,
        "wins": wins,
        "losses": losses,
        "unresolved_orders": unresolved,
        "by_series": dict(by_series),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--journal", default="var/logs/trade_journal.jsonl")
    parser.add_argument("--training", default="var/features/forecast/forecast_training.jsonl")
    parser.add_argument("--notional-cap", type=float, default=500.0,
                        help="Max $ notional per ticker (default: 500, same as live)")
    parser.add_argument("--cooldown", type=int, default=0,
                        help="Seconds between fills on same ticker (default: 0 = disabled)")
    parser.add_argument("--fee-pct", type=float, default=7.0,
                        help="Fee as %% of winnings (default: 7)")
    parser.add_argument("--series", default="",
                        help="Comma-separated series prefixes to include (e.g. KXBTCD,KXHIGHT). Default: all.")
    args = parser.parse_args()

    journal_path = Path(args.journal)
    training_path = Path(args.training)

    if not journal_path.exists():
        print(f"ERROR: journal not found at {journal_path}")
        return
    if not training_path.exists():
        print(f"ERROR: training data not found at {training_path}")
        return

    all_fills = load_journal(journal_path)
    outcomes = load_outcomes(training_path)

    series_filter = [s.strip() for s in args.series.split(",") if s.strip()]
    if series_filter:
        all_fills = [f for f in all_fills if any(f["ticker"].startswith(s) for s in series_filter)]

    # Naive: all fills (current paper behavior)
    naive = compute_pnl(all_fills, outcomes, args.fee_pct)

    # Simulated: with guards applied
    guarded_fills = simulate(all_fills, args.notional_cap, args.cooldown)
    simulated = compute_pnl(guarded_fills, outcomes, args.fee_pct)

    sep = "=" * 64

    print(f"\n{sep}")
    print(f"  PnL SIMULATION  (notional_cap=${args.notional_cap:.0f}, cooldown={args.cooldown}s, fee={args.fee_pct}%)")
    print(sep)

    def print_summary(label, r, n_fills):
        n = r["wins"] + r["losses"]
        win_pct = f"{r['wins']/n*100:.1f}%" if n else "n/a"
        roi = f"{r['total_pnl']/r['total_cost']*100:.1f}%" if r["total_cost"] > 0 else "n/a"
        print(f"\n  [{label}]")
        print(f"    Orders accepted   : {n_fills}")
        print(f"    Resolved          : {n}  ({r['unresolved_orders']} unresolved orders excluded)")
        print(f"    Wins / Losses     : {r['wins']} / {r['losses']}  ({win_pct} win rate)")
        print(f"    Capital deployed  : ${r['total_cost']:.2f}")
        print(f"    Net PnL           : ${r['total_pnl']:+.2f}")
        print(f"    ROI on deployed   : {roi}")

    print_summary("NAIVE — all paper fills", naive, len(all_fills))
    print_summary(f"SIMULATED — with live guards", simulated, len(guarded_fills))

    print(f"\n  Fill reduction: {len(all_fills)} → {len(guarded_fills)} orders "
          f"({len(all_fills) - len(guarded_fills)} filtered by guards)")

    # By-series breakdown for simulated
    print(f"\n  {'Series':<26} {'Orders':>6} {'W':>4} {'L':>4} {'Win%':>7} {'PnL':>10} {'ROI':>8}")
    print(f"  {'-'*68}")
    rows = sorted(simulated["by_series"].items(),
                  key=lambda x: -(x[1]["wins"] + x[1]["losses"] + (1 if x[1]["pnl"] == 0 else 0)))
    for series, s in rows:
        n = s["wins"] + s["losses"]
        win_pct = f"{s['wins']/n*100:.1f}%" if n else "  —"
        roi = f"{s['pnl']/s['cost']*100:.1f}%" if s["cost"] > 0 else "  —"
        print(f"  {series:<26} {s['orders']:>6} {s['wins']:>4} {s['losses']:>4} {win_pct:>7} {s['pnl']:>+10.2f} {roi:>8}")

    print()


if __name__ == "__main__":
    main()
