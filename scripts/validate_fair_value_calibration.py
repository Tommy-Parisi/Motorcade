#!/usr/bin/env python3
"""
Validate heuristic fair value calibration against resolved outcomes.

Cross-references organic paper fills in execution_training.jsonl against
outcomes.jsonl to measure whether the signal edge is real or fictitious.

Key questions:
  1. For NO buys at observed_price X, how often does NO actually win?
  2. Does the claimed fair_price correlate with actual win rates?
  3. Which verticals / price buckets have the worst calibration?

Usage:
  python3 scripts/validate_fair_value_calibration.py
"""

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
EXEC_TRAINING = ROOT / "var/features/execution/execution_training.jsonl"
OUTCOMES = ROOT / "var/research/outcomes/outcomes.jsonl"


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def brier_score(predictions, actuals):
    """Mean squared error between predicted probability and binary outcome."""
    if not predictions:
        return float("nan")
    return sum((p - a) ** 2 for p, a in zip(predictions, actuals)) / len(predictions)


def infer_vertical(ticker: str, title: str) -> str:
    """
    Re-infer market vertical from ticker + title when the stored vertical is
    'unknown'.  Mirrors the logic in src/data/market_enrichment.rs
    detect_vertical(), kept intentionally simple so it stays in sync by eye.
    """
    t = ticker.upper()
    lo = title.lower()

    # Esports — check before generic sports so CS2/DOTA/VALORANT don't fall into sports
    if re.search(r"CS2|DOTA2|VALORANT|LOL|LEAGUEOFLEGENDS|ROCKETLEAGUE|OVERWATCH", t):
        return "esports"

    # Sports
    if re.search(r"NBA|NFL|MLB|NHL|WNBA|MLS|UFC|PGA|ATP|WTA|IPL|CPL|F1|NASCAR|MOTOGP", t):
        return "sports"
    if re.search(r"soccer|football|cricket|tennis|golf|rugby|boxing|mma|racing", lo):
        return "sports"

    # Weather
    if "KXHIGH" in t or "KXLOW" in t or "KXPRECIP" in t or "KXSNOW" in t:
        return "weather"
    if re.search(r"temperature|high temp|low temp|rainfall|snowfall|precipitation", lo):
        return "weather"

    # Finance
    if re.search(r"NASDAQ|KXINX|KXTNOTE|KXGOLD|KXSILVER|KXCOPPER|KXBRENT|KXWTI|KXEURUSD|KXUSDJPY|KXUSDGBP|KXGBPUSD", t):
        return "finance"
    if re.search(r"s&p|nasdaq|treasury|yield|gold price|silver price|crude oil|forex", lo):
        return "finance"

    # Crypto
    if re.search(r"KXBTC|KXETH|KXSOL|KXXRP|KXDOGE|KXBNB|KXAVAX|KXLINK|KXLTC", t):
        return "crypto"
    if re.search(r"bitcoin|ethereum|solana|ripple|dogecoin|crypto|blockchain", lo):
        return "crypto"

    # Politics
    if re.search(r"KXPRES|KXGOV|KXSEN|KXHOUSE|KXPRIMARY|KXELECT|KXTRUMP|KXBIDEN|KXHARRIS", t):
        return "politics"
    if re.search(r"election|president|senate|congress|vote|ballot|democrat|republican|legislat", lo):
        return "politics"

    return "other"


def enrich_vertical(feature: dict) -> str:
    v = feature.get("vertical", "unknown")
    if v != "unknown":
        return v
    return infer_vertical(feature.get("ticker", ""), feature.get("title", ""))


def print_block(title, rows):
    """Print a calibration block for a slice of matched rows."""
    if not rows:
        print("  (no data)")
        return
    wins = sum(1 for r in rows if r["_win"])
    total = len(rows)
    fairs = [r["_fair"] for r in rows if r["_fair"] is not None]
    avg_fair = sum(fairs) / len(fairs) if fairs else float("nan")
    obs = [r["_obs"] for r in rows if r["_obs"] is not None]
    avg_obs = sum(obs) / len(obs) if obs else float("nan")
    win_rate = wins / total
    gap = avg_fair - win_rate if fairs else float("nan")
    print(f"  n={total:4d}  win={wins:4d} ({win_rate*100:5.1f}%)  "
          f"avg_fair={avg_fair:.3f}  avg_obs={avg_obs:.3f}  gap={gap*100:+.1f}pp")

    brier_rows = [(r["_fair"], 1.0 if r["_win"] else 0.0)
                  for r in rows if r["_fair"] is not None]
    if brier_rows:
        bs = brier_score([p for p, _ in brier_rows], [a for _, a in brier_rows])
        bs_base = brier_score([win_rate] * len(brier_rows), [a for _, a in brier_rows])
        flag = "  *** WORSE THAN BASE RATE ***" if bs > bs_base else ""
        print(f"  Brier={bs:.4f}  baseline={bs_base:.4f}{flag}")


def main():
    exec_rows = load_jsonl(EXEC_TRAINING)
    outcome_records = load_jsonl(OUTCOMES)

    outcomes = {r["ticker"]: r for r in outcome_records}

    # Filter to organic paper fills only
    organic_fills = [
        r for r in exec_rows
        if r.get("is_organic_paper") and r.get("terminal_status") == "Filled"
    ]
    print(f"Organic paper fills:  {len(organic_fills)}")
    print(f"Resolved outcomes:    {len(outcomes)}")

    # Join fills to outcomes by ticker
    matched = []
    for r in organic_fills:
        feat = r["feature"]
        ticker = feat["ticker"]
        if ticker not in outcomes:
            continue
        outcome = outcomes[ticker]
        if outcome.get("resolution_status") != "resolved":
            continue
        outcome_yes = outcome.get("outcome_yes")
        if outcome_yes is None:
            continue

        outcome_id = feat["outcome_id"].lower()
        fair_price = feat.get("candidate_fair_price")
        obs_price = feat.get("candidate_observed_price")
        vertical = enrich_vertical(feat)
        we_win = (outcome_yes is False) if outcome_id == "no" else (outcome_yes is True)
        # A trade is "pre-guard" if it bought NO at near-zero price — these are
        # the converged-market buys that the mid-price guard now blocks.
        pre_guard = (outcome_id == "no" and obs_price is not None and obs_price < 0.05)

        matched.append({
            "ticker": ticker,
            "outcome_id": outcome_id,
            "fair_price": fair_price,
            "obs_price": obs_price,
            "fill_price": r.get("label_terminal_avg_fill_price"),
            "outcome_yes": outcome_yes,
            "_win": we_win,
            "_fair": fair_price,
            "_obs": obs_price,
            "markout_5m": r.get("label_markout_bps_5m"),
            "vertical": vertical,
            "pre_guard": pre_guard,
        })

    print(f"Matched to outcomes:  {len(matched)}")
    if not matched:
        print("No matches — cannot compute calibration.")
        return

    pre_guard = [r for r in matched if r["pre_guard"]]
    post_guard = [r for r in matched if not r["pre_guard"]]
    print(f"  pre-guard (NO<5c):  {len(pre_guard)}  (converged-mid guard now blocks these)")
    print(f"  post-guard trades:  {len(post_guard)}")

    # -------------------------------------------------------------------------
    # 1. Overall — all matched (historical context)
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("OVERALL (all matched, including pre-guard historical data):")
    print_block("all", matched)

    # -------------------------------------------------------------------------
    # 2. Post-guard only — the trades the current config would generate
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("POST-GUARD (obs 5%-95%, what current code would allow):")
    print_block("post-guard", post_guard)

    # -------------------------------------------------------------------------
    # 3. By outcome side (post-guard)
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("POST-GUARD — BY OUTCOME SIDE:")
    for side in ("no", "yes"):
        sub = [r for r in post_guard if r["outcome_id"] == side]
        if sub:
            print(f"  {side.upper():3s}:", end="  ")
            print_block(side, sub)

    # -------------------------------------------------------------------------
    # 4. By observed price bucket (post-guard, NO side)
    # -------------------------------------------------------------------------
    no_post = [r for r in post_guard if r["outcome_id"] == "no"]
    if no_post:
        print(f"\n{'='*60}")
        print("POST-GUARD NO — WIN RATE BY OBSERVED PRICE BUCKET (5c):")
        buckets = defaultdict(list)
        for r in no_post:
            if r["_obs"] is not None:
                b = round(r["_obs"] * 20) / 20
                buckets[b].append(r)
        for b in sorted(buckets):
            rows = buckets[b]
            w = sum(1 for r in rows if r["_win"])
            afs = [r["_fair"] for r in rows if r["_fair"] is not None]
            avg_f = sum(afs) / len(afs) if afs else float("nan")
            print(f"  obs~{b:.2f}: n={len(rows):4d}  win={w:4d} ({w/len(rows)*100:5.1f}%)"
                  f"  claimed_fair={avg_f:.4f}")

    # -------------------------------------------------------------------------
    # 5. By vertical (post-guard, re-inferred)
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("POST-GUARD — BY VERTICAL (re-inferred from ticker+title):")
    vert_buckets = defaultdict(list)
    for r in post_guard:
        vert_buckets[r["vertical"]].append(r)
    for v in sorted(vert_buckets, key=lambda x: -len(vert_buckets[x])):
        rows = vert_buckets[v]
        print(f"  {v:20s}:", end="  ")
        print_block(v, rows)

    # -------------------------------------------------------------------------
    # 6. By fair price bucket (post-guard, all sides)
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("POST-GUARD — ACTUAL VS PREDICTED BY FAIR PRICE BUCKET (10c):")
    fair_buckets = defaultdict(list)
    for r in post_guard:
        if r["_fair"] is not None:
            b = round(r["_fair"] * 10) / 10
            fair_buckets[b].append(r)
    for b in sorted(fair_buckets):
        rows = fair_buckets[b]
        w = sum(1 for r in rows if r["_win"])
        print(f"  fair~{b:.1f}: n={len(rows):4d}  actual_win={w/len(rows)*100:5.1f}%"
              f"  predicted={b*100:.0f}%"
              f"  gap={w/len(rows)*100 - b*100:+.1f}pp")

    # -------------------------------------------------------------------------
    # 7. Markout distribution (post-guard)
    # -------------------------------------------------------------------------
    m5s = [r["markout_5m"] for r in post_guard if r["markout_5m"] is not None]
    if m5s:
        print(f"\n{'='*60}")
        print("POST-GUARD — MARKOUT_BPS_5M DISTRIBUTION:")
        pos = sum(1 for m in m5s if m > 0)
        neg = sum(1 for m in m5s if m <= 0)
        avg_m5 = sum(m5s) / len(m5s)
        print(f"  n={len(m5s)}  positive={pos}  negative={neg}  avg={avg_m5:+.0f}bps")
        # Deciles
        m5s_sorted = sorted(m5s)
        n = len(m5s_sorted)
        deciles = [m5s_sorted[min(int(n * q / 10), n - 1)] for q in range(0, 11)]
        print(f"  p0={deciles[0]:+.0f}  p10={deciles[1]:+.0f}  p25={deciles[2]:+.0f}"
              f"  p50={deciles[5]:+.0f}  p75={deciles[7]:+.0f}  p90={deciles[9]:+.0f}"
              f"  p100={deciles[10]:+.0f}  (bps)")

    # -------------------------------------------------------------------------
    # 8. Summary / verdict
    # -------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("VERDICT (post-guard trades only):")
    if post_guard:
        wins = sum(1 for r in post_guard if r["_win"])
        total = len(post_guard)
        fairs = [r["_fair"] for r in post_guard if r["_fair"] is not None]
        avg_claimed = sum(fairs) / len(fairs) if fairs else 0.0
        actual_win_rate = wins / total
        gap = avg_claimed - actual_win_rate
        print(f"  Claimed avg fair:    {avg_claimed*100:.1f}%")
        print(f"  Actual win rate:     {actual_win_rate*100:.1f}%")
        print(f"  Calibration gap:     {gap*100:+.1f}pp")
        if gap > 0.20:
            print("  STATUS: SEVERELY OVERCONFIDENT")
            print("          Use label_markout_bps_5m / label_markout_bps_30m as training targets.")
        elif gap > 0.05:
            print("  STATUS: MODERATELY OVERCONFIDENT — fair values need recalibration.")
        else:
            print("  STATUS: REASONABLY CALIBRATED.")
    else:
        print("  No post-guard trades to evaluate.")

    if pre_guard:
        print(f"\n  Note: {len(pre_guard)} pre-guard NO-at-<5c trades excluded from verdict.")
        print("  Those were blocked by the converged-mid guard added in a recent commit.")


if __name__ == "__main__":
    main()
