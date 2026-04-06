# Motorcade Plan: Sidecar-Heavy Architecture Roadmap

## What This Is

The Rust bot runs in the center, flanked by a convoy of out-of-process Python
specialist sidecars — one per vertical, each purpose-built for its data sources.
The execution model is shared across verticals (microstructure generalizes).
Forecast does not — a single cross-vertical model has nothing to learn beyond
market mid when enrichment signals are null for most rows.

Each sidecar exposes `/health` and `/predict?ticker=` and returns the motorcade
response contract. `src/data/market_enrichment.rs` detects the vertical from the
ticker, calls the sidecar, and populates `specialist_prob_yes`. The forecast model
uses it as a hard override of the bucket model. Sidecar down = silent fallback,
trading continues.

**Response contract (all sidecars):**
```json
{
  "probability":    0.73,
  "data_age_secs":  12,
  "data_source_ok": true,
  "model_version":  "v1"
}
```
`data_source_ok: false` collapses to `None` in `market_enrichment.rs` — sidecar
returning garbage is treated identically to sidecar being down.

---

## Current State (2026-04-06)

| Component | Status |
|---|---|
| Weather sidecar (`sidecars/weather/`) | **Live.** GEFS 31-member ensemble, all 13 KXHIGHT cities. Replaced old XGBoost/Philadelphia-only version. First sidecar-backed weather trades accumulating now. |
| Crypto sidecar (`sidecars/crypto/`) | **Built, shadow mode.** GBM threshold-crossing probability. Rust calls sidecar, logs predictions, but does NOT override bucket model yet. |
| Bucket model | Permanent fallback for all non-specialist verticals. Works. |
| Execution model | Bucket lookup table. Fill probability labels meaningless on paper data — requires live-real fills. |
| Policy layer | Wired in shadow mode. Active mode gated on live-real markout ≥ 0 over 50+ rows. |
| Live trading | Not started. Simulation shows +1.0% ROI with sports + oil removed (pre-sidecar weather). Waiting on weather validation. |

**Sports and oil removed from allowlists** (2026-04-06):
- Sports: 0% win rate on resolved paper trades
- Oil (KXBRENTW, KXWTIMAX): -24% ROI, no macro signal, Iran conflict regime

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Rust Bot (main)                        │
│   scan → enrich → forecast → policy → execute            │
│                      │                                    │
│          specialist_prob_yes (Option<f64>)                │
│          overrides bucket model when present              │
└──────────────┬────────────────────────────────────────────┘
               │  HTTP (3s timeout, graceful fallback)
    ┌──────────┴──────────────────────────────┐
    │                                         │
┌───▼─────────────────────┐   ┌──────────────▼──────────────┐
│   WeatherPredictor ✓    │   │   CryptoPredictor (shadow) ✓│
│   sidecars/weather/     │   │   sidecars/crypto/          │
│   GEFS 31-member        │   │   GBM threshold probability │
│   13 KXHIGHT cities     │   │   BTC, ETH, SOL, XRP        │
│   Live since 2026-04-06 │   │   Scaffolded, not built     │
└─────────────────────────┘   └─────────────────────────────┘
```

---

## Sidecar Build Guide

Each sidecar follows the same pattern:

1. **Ticker parser** — extracts asset/city, date, threshold, above/below
2. **Data fetcher** — fetches and caches external data on a background thread
3. **Predictor** — computes probability from cached data (no blocking I/O in hot path)
4. **FastAPI app** — `/health` and `/predict?ticker=` endpoints
5. **Rust wiring** — add detection in `market_enrichment.rs`, add `*_SPECIALIST_URL` env var
6. **Tests** — ticker parser coverage, predictor unit tests

Startup must be non-blocking: warmup runs in a background thread so uvicorn
serves immediately (returning `data_source_ok: false` until cache warms).

---

## Roadmap

### Phase 1 — CryptoPredictor Sidecar

**Goal:** Cover `KXBTCD-*`, `KXETHD-*`, `KXSOLD-*`, `KXXRPD-*`.

**Approach:** GBM threshold-crossing probability — NOT price prediction.

```
P(S_T > K) = N( (log(S/K) + (μ - σ²/2)·T) / (σ·√T) )
```

Where `S` = current spot, `K` = strike, `T` = time to 5pm EDT settlement,
`σ` = realized vol from recent OHLCV, `μ` = 0 (intraday drift negligible).
No training required. Works immediately from exchange price data.

**Do not use LSTM/price-prediction models.** We need P(price > threshold),
not the price itself. The GBM formula is analytically exact under standard
assumptions, interpretable, and requires no historical collection period.

**Price feed:** Coinbase Advanced Trade API (public endpoints, no auth).
Binance REST as fallback. Cache spot + OHLCV in background thread every 30s.

**Vol estimation:** close-to-close realized vol over 15m/1h/4h windows.
Use shortest window with sufficient candles — intraday crypto vol is fast-moving.

**Settlement time:** all Kalshi daily crypto markets settle at 17:00 EDT.

**Rust wiring:**
- `CRYPTO_SPECIALIST_URL` env var wired into `EnrichmentConfig` (default unset)
- When set, Rust calls the sidecar on every crypto market hit, logs
  `crypto_specialist_shadow ticker=... prob=...` to stderr, but does **not**
  write to `specialist_prob_yes` — bucket model remains authoritative

**Why shadow first:** crypto is already working as-is (time-decay edge on
high-probability markets). Promoting GBM predictions immediately risks
degrading an edge that's real money once live. Shadow mode lets us verify
GBM calibration against resolved outcomes before trusting it with sizing.

**Promote to active** (flip `specialist_prob_yes` assignment in
`market_enrichment.rs`) when 1-week shadow shows GBM Brier score better
than bucket on crypto markets.

**Gate:** 1-week shadow holdout showing GBM better calibrated than bucket.

---

### Phase 2 — Go Live (Micro-Sized)

**Trigger:** `simulate_pnl.py` shows positive ROI after GEFS-backed weather
trades start resolving (~2-3 weeks from 2026-04-06).

**Script:** `scripts/trade.sh` with `BOT_EXECUTION_MODE=live`. Already
micro-sized: $5-10/trade, $25 daily loss cap, $500 max exposure.

**What this unlocks:** real fill/no-fill variance in execution data. Paper
fills always fill (limit >= ask is always true for candidates) so fill
probability labels in the execution model are currently all 1.0.

---

### Phase 3 — Execution Model: Fix Training Data

Before training an execution GBT, three gaps must be closed:

**Gap 1 — `raw_edge_pct` / `confidence` are 0.0 in all retroactive rows**
During dataset rebuild, join market state snapshots to forecast feature rows
by ticker + timestamp; populate from closest preceding forecast row.

**Gap 2 — No external fill data**
1. Kalshi public trade history: walk historical markets via `/trades`, reconstruct
   fill events with surrounding market state features.
2. Polymarket data: binary outcome markets on Polygon, fully public, structurally
   identical. Build importer mapping to `ExecutionTrainingRow` schema.

**Gap 3 — Book depth missing**
`yes_bid_size` / `yes_ask_size` not collected in market state snapshots. Add
to `MarketStateEvent` schema, populate from scanner/WS delta. Re-run retroactive
label generation after a few weeks of richer snapshots.

**Gate:** all three gaps closed AND >10K rows with non-zero `raw_edge_pct`,
plus real fills from at least one external source.

---

### Phase 4 — Execution GBT

Train on IOC fill probability first. GTC is a separate problem — defer.

Target labels (already in `ExecutionTrainingRow`):
- `label_filled_within_30s` — primary fill target
- `label_filled_within_5m` — secondary
- `label_markout_bps_5m` / `label_markout_bps_30m` — adverse selection

Key features: `aggressiveness_bps`, `spread_cents`, `book_pressure`,
`yes_bid_size`, `yes_ask_size`, `raw_edge_pct`, `confidence`,
`time_to_close_secs`, `volume`, `vertical`.

**Gate:** GBT improves fill probability ranking vs. bucket lookup on holdout.
Runs in shadow 1 week without degrading legacy PnL.

---

### Phase 5 — Policy Active Mode

Active mode gate (code-enforced in `main.rs`):
- Execution model has ≥ 50 live-real rows
- Mean `markout_5m_bps` across those rows ≥ 0

Before promoting beyond the code gate, also verify manually:
- Specialist coverage ≥ 50% of traded markets
- No vertical materially worse than legacy in shadow logs
- Predicted fill rate vs. actual fill rate calibrated

**Promotion path:**
1. Shadow with real specialist probs (after crypto sidecar live)
2. Active with strict notional cap (`BOT_POLICY_MAX_NOTIONAL_ACTIVE`)
3. Active at normal sizing after 2 weeks of cap-mode data

---

### Phase 6 — Additional Specialists (Later)

**Sports** — removed from allowlists pending a real signal. Approach:
monitor beat reporter Twitter/X accounts (Shams, Woj, Rapoport, ~50/sport)
every 60s, Claude classifies injury/lineup news, per-ticker probability
updated on event fire. Per-ticker state needs explicit `valid_until`
timestamps — injury news expires at game tip-off, not at data staleness.
Cycle latency matters: if edge window is tighter than cycle, push events
into the main loop rather than polling.

**Economic indicators (Fed rate, CPI)** — only build with a clear signal
hypothesis. Efficient market; hard to beat without proprietary data.
Reference: hawkwatchers (NLP on Fed press releases) as a lightweight
first signal layer.

---

## Production Gates Summary

| Gate | Condition |
|---|---|
| Go live (`trade.sh`) | `simulate_pnl.py` shows positive ROI with guards applied |
| Crypto sidecar to shadow | GBM predictions better calibrated than bucket on 1-week holdout |
| Execution GBT to shadow | Improves fill ranking vs. bucket on holdout |
| Policy active | ≥ 50 live-real rows, mean `markout_5m_bps` ≥ 0 (code-enforced) |
| Policy active (normal sizing) | 2 weeks capped-mode data, no vertical worse than legacy |
