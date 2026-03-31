# CLAUDE.md

## Project Overview

Rust trading bot for Kalshi event-contract markets. Two parallel pipelines:
1. **Legacy trading loop** — scan, enrich, value, allocate, execute (production-ready)
2. **Research & modeling pipeline** — capture market state, train forecast/execution/policy models (maturing)

The system is transitioning from pure heuristic trading toward data-informed modeling. Shadow-first is the culture: new logic runs in parallel before being trusted.

## Build & Run

```bash
# Build
cargo build --release

# Run (uses .env for all config)
./scripts/run_live_shadow.sh          # live trading + shadow models (recommended)
./scripts/run_research_paper_capture.sh   # paper trading + research capture
./scripts/run_research_capture.sh         # data collection only (no trading)

# Retrain models after new data
./scripts/rebuild_models.sh

# Morning routine (backfill outcomes, rebuild, check fills, evaluate)
./scripts/morning_review.sh

# Pre-release safety check
./scripts/release_check.sh
```

## Architecture

```
src/
├── main.rs            # Runtime orchestration, cycle loop, mode selection
├── data/              # Scanner, websocket delta merge, enrichment (weather/sports/crypto)
├── execution/         # Kalshi API client, paper simulator, execution engine
├── model/             # Legacy valuation (Claude + heuristic fallback), allocator
├── models/            # Forecast model, execution model, reporting
├── research/          # Market-state recording, order-lifecycle logging, outcome backfill
├── datasets/          # Training dataset builder (joins market state + outcomes)
├── features/          # Feature builders for forecast and execution models
├── policy/            # Policy layer (scores action grid, expected PnL)
├── outcomes/          # Resolved outcome backfill from Kalshi API
├── markets/           # Market mapper, market-type helpers
└── replay/            # Multi-day replay/backtesting
```

**Operating modes** (set via `BOT_POLICY_MODE` in `.env`):
- `legacy` — only legacy path (current trusted mode)
- `shadow` — legacy executes, models run in parallel for comparison
- `active` — policy decisions influence trading (requires validated execution data)

## Key Conventions

### Data Provenance — Never Merge These
Training data must preserve source labels:
- `bootstrap_synthetic` — retroactive artificial bootstrap
- `organic_paper` — paper trading
- `live_real` — real exchange fills

Models default to `organic_paper + live_real` only. Do not silently merge.

### Shadow-First Rollout
Any new policy or model-driven logic must go through `shadow` before `active`. The shadow→active promotion is now partly code-enforced: `BOT_POLICY_MODE=active` will fail at startup if fewer than `BOT_POLICY_ACTIVE_MIN_SHADOW_DECISIONS` (default 50) shadow policy records exist in the last `BOT_POLICY_ACTIVE_SHADOW_LOOKBACK_DAYS` (default 7) days, or if their mean `expected_realized_pnl` is below `BOT_POLICY_ACTIVE_MIN_SHADOW_MEAN_ERPNL` (default -200 bps).

### var/ is Generated — Do Not Commit
The entire `var/` tree (`cycles/`, `logs/`, `research/`, `features/`, `models/`, `state/`) is runtime output. Never commit it. Use `git rm --cached` if needed.

### No Secrets in Commits
`.env`, private keys, API tokens, account-specific credentials must never be committed.

## Known Issues (Priority Order)

See `docs/execution_aware_prediction_plan.md` for the full roadmap. Post shadow-mode post-mortem (2026-03-29):

**Fixed (2026-03-30):**
- ~~**Issue 5:** Sports outcome resolver not capturing sports markets.~~ Fixed: order_lifecycle source now scans the full history without a date cutoff so all sports fills are collected. (`src/outcomes/resolver.rs`)
- ~~**Issue 3:** `label_realized_net_pnl` computed theoretical entry edge, not actual resolution PnL.~~ Fixed: function now joins to outcomes by ticker and computes actual win/loss PnL for each side. (`src/datasets/builder.rs`)
- ~~**Issue 6:** Allocator used legacy `fair_price` for Kelly sizing even in active mode.~~ Fixed: `chosen_fair_price` (forecast-derived) is now stored on `PolicyDecision` and applied to the Kelly candidate in active mode. (`src/policy/decision.rs`, `src/main.rs`)
- ~~**Issue 7:** No shadow calibration gate before shadow→active promotion.~~ Fixed: `validate_shadow_calibration()` now blocks active mode if shadow data is insufficient or mean expected PnL is below threshold. (`src/main.rs`)

**Remaining:**
1. **Issue 4:** Execution model is 99.4% synthetic data (246K synthetic vs 1.4K organic rows). Accumulate more organic paper fills before relying on execution model predictions.
2. **Issues 1 & 2:** Both models are bucket lookup tables, not GBTs. Forecast correction capped at ±1.4 cents from mid. Fix: train real GBT models offline (LightGBM/XGBoost) once data foundation is solid (requires Issue 3 data to be meaningful).

## Important Files

| File | Purpose |
|------|---------|
| `src/main.rs` | Cycle orchestration, mode logic, entry point |
| `src/model/allocator.rs` | Capital allocation (Kelly-like sizing) |
| `src/models/forecast.rs` | Forecast model inference |
| `src/models/execution.rs` | Execution model inference |
| `src/policy/` | Policy layer — scores action grid |
| `src/datasets/builder.rs` | Training dataset builder |
| `src/outcomes/resolver.rs` | Outcome backfill |
| `docs/execution_aware_prediction_plan.md` | Full modeling roadmap |
| `scripts/evaluate_shadow.py` | Forecast calibration + policy hit-rate analysis |
| `scripts/check_fills.py` | Paper fill win/loss rate vs resolved outcomes |

## Analysis Scripts (Python)

```bash
python scripts/evaluate_shadow.py        # calibration + policy hit rate
python scripts/check_fills.py           # fill win/loss vs outcomes
python scripts/retroactive_execution_labels.py   # backfill execution labels
python scripts/validate_fair_value_calibration.py
```

## Branch & Release Workflow

See `docs/release_process.md`. In brief:
- Feature branches for meaningful work; don't stack experiments on `main`
- Run `scripts/release_check.sh` before pushing anything affecting runtime safety, reporting, or rollout logic
- Commit categories: runtime changes / model+reporting changes / docs+process changes
