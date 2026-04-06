# EventTradingBot

A Rust trading and research system for Kalshi-style event contracts.

This repository combines two closely related jobs:

1. operate a rules-based event trading loop with risk controls and exchange execution
2. collect and process structured research data for a more execution-aware modeling stack

The codebase started as a live trading bot with heuristic and Claude-assisted valuation. It now also includes the foundation for a two-layer modeling system:

- a forecast layer that estimates fair event probability
- an execution layer that estimates whether that edge is actually tradable after fill behavior, slippage, and short-horizon markout

The project is still in transition. The legacy trading path is fully usable. The forecast path is already useful in shadow mode. The execution-aware path is implemented end to end, but still data-limited and should be treated as research-stage.

## What This Repo Does

At a high level, the system:

1. scans Kalshi markets
2. enriches a subset of them with lightweight external signals
3. values markets using either Claude or a heuristic fallback
4. generates trade candidates
5. allocates capital under bankroll and exposure constraints
6. executes through either a paper simulator or the live Kalshi API
7. records structured research data for later training and evaluation
8. optionally runs forecast, execution, and policy models in shadow alongside the live system

There are effectively two pipelines living side by side.

### Legacy Trading Pipeline

This is the currently trusted production path.

- market scan
- market enrichment
- valuation
- candidate generation
- allocation
- execution
- journal/state updates

### Research and Modeling Pipeline

This is the newer path used to improve the system over time.

- market-state capture
- order-lifecycle capture
- outcome backfill
- dataset generation
- forecast model training
- execution model training
- policy scoring and reporting

## Current State

The project is best understood as four layers.

### 1. Trading Layer

Implemented and actively usable.

- Kalshi REST scanner
- short websocket delta merge
- live and paper execution engines
- hybrid IOC/GTC execution policy
- state tracking and journaling
- risk guards for stale signals, edge thresholds, cooldowns, duplicate open orders, and notional caps

### 2. Research Capture Layer

Implemented and actively usable.

- point-in-time market-state logging
- order-lifecycle logging
- outcome backfill for settled markets
- research coverage reporting

### 3. Modeling Layer

Implemented, but still maturing.

- baseline forecast model
- baseline execution model
- model report tooling
- dataset builder

### 4. Policy Layer

Implemented, but recommended in `shadow` mode for now.

- policy action scoring over a small action grid
- expected realized PnL scoring
- policy decision logging
- policy report tooling

## Recommended Reality Check

- The legacy trading loop is operational and is what executes trades in both `shadow` and `live` mode.
- The forecast model is promising but underperforms market mid on crypto (~96% of volume) because no CryptoPredictor sidecar exists yet.
- The execution model's `fill_30s`/`fill_5m` labels are meaningless on paper data — the paper path always fills. Organic paper rows help `markout` and `fill_price` calibration only. Fill probability requires live-real data.
- `BOT_POLICY_MODE=active` is gated on actual live trade performance (markout), not estimated ERPNL from the execution model.

**Before going live:** run `python3 scripts/simulate_pnl.py` and confirm positive simulated ROI. Win rate alone is misleading — buying high-probability contracts at 70% win rate still loses money because losses are larger than wins. The simulation applies the $500/ticker notional cap and computes actual dollar PnL.

**Before enabling active mode:** the code-enforced gate requires 50+ live-real execution rows with mean `markout_5m_bps` ≥ 0.

## Architecture

## Top-Level Layout

- `src/main.rs`
  - runtime orchestration, mode selection, cycle execution
- `src/data/`
  - market scanning, websocket deltas, enrichment
- `src/execution/`
  - exchange client, paper simulator, execution engine, order types
- `src/model/`
  - legacy valuation and allocation logic
- `src/features/`
  - forecast and execution feature builders
- `src/models/`
  - forecast model, execution model, model reporting
- `src/policy/`
  - policy decision logic and reporting
- `src/research/`
  - structured market-state and order-lifecycle capture
- `src/outcomes/`
  - resolved outcome backfill
- `src/datasets/`
  - dataset generation from research logs
- `src/replay/`
  - replay utilities
- `docs/`
  - project roadmap and feature catalog
  - release workflow and operating guidance
- `var/`
  - generated runtime, research, datasets, models, logs, and artifacts

## Main Runtime Flow

The normal runtime loop lives in `src/main.rs`.

A typical cycle looks like this:

1. scan open markets from Kalshi REST
2. collect a short websocket delta window and merge quote/trade updates
3. filter markets by volume and spread
4. enrich a limited subset with weather, sports, or crypto context
5. build valuation inputs
6. optionally score the forecast model in shadow
7. run Claude or heuristic valuation
8. generate legacy candidate trades
9. optionally score the execution model and policy layer in shadow
10. allocate capital
11. execute trades through live or paper execution
12. append journals, runtime state, cycle artifacts, and research logs

## Scanner and Market Selection

The scanner lives in `src/data/market_scanner.rs`.

It:

- fetches open markets through `GET /trade-api/v2/markets`
- normalizes Kalshi wire fields into `ScannedMarket`
- merges short-lived websocket deltas from `ticker_v2` and `trade` channels
- ranks and filters markets by:
  - minimum volume
  - maximum spread
  - maximum market count

The websocket layer lives in `src/data/ws_delta.rs`.

It is intentionally lightweight. It is not a full depth-of-book feed. It exists to slightly improve point-in-time state over pure snapshot polling.

## Enrichment Layer

The enricher lives in `src/data/market_enrichment.rs`.

It adds lightweight external context based on a coarse market vertical classification:

- weather: NOAA
- sports: optional injury feed
- crypto: optional sentiment feed
- other: no enrichment

This is not a full prediction engine. It is a shallow signal layer that the legacy valuation engine and newer feature builders can use.

## Valuation Layer

The valuation engine lives in `src/model/valuation.rs`.

It supports two paths:

- Claude-enabled valuation
- heuristic fallback valuation

The valuation engine produces:

- `MarketValuation`
- `CandidateTrade`
- diagnostics about thresholds, edge, and fallback behavior

The legacy candidate logic is based on the difference between:

- estimated fair probability
- observed market price

adjusted by configurable fee and slippage assumptions.

## Allocation Layer

The allocator lives in `src/model/allocator.rs`.

The legacy allocator:

- ranks candidates by edge and confidence
- uses a Kelly-like sizing approximation
- caps per-trade and per-cycle bankroll fractions
- can enforce one trade per event root via an event mutex

In newer policy-enabled modes, ranking can be influenced by expected realized PnL from the policy layer.

## Execution Layer

Execution logic lives in:

- `src/execution/engine.rs`
- `src/execution/client.rs`
- `src/execution/paper_sim.rs`

Capabilities include:

- paper execution mode
- live Kalshi execution mode
- IOC, GTC, and hybrid execution policies
- order normalization against Kalshi market constraints
- open-order reconciliation
- journal logging
- runtime state persistence
- smoke testing for live exchange connectivity

The execution client also applies market constraints before submit, including:

- non-binary market rejection
- tick/range snapping
- normalized yes/no price handling

## Research Data Model

The research layer exists so the system can be trained and evaluated honestly.

### Market-State Capture

Recorded in `src/research/market_recorder.rs`.

Written to:

- `var/research/market_state/YYYY-MM-DD/market_state.jsonl`

Each row captures a point-in-time market observation, including:

- ticker and title metadata
- bid/ask
- implied mid
- spread
- volume
- event and series identifiers when available
- source (`snapshot` or `ws_delta`)
- cycle id

### Order-Lifecycle Capture

Recorded in `src/research/order_recorder.rs`.

Written to:

- `var/research/order_lifecycle/YYYY-MM-DD/order_lifecycle.jsonl`

Rows include:

- order intent
- ack
- reconcile / terminal report
- side and time in force
- requested quantity
- fill quantity
- average fill price
- fee paid
- signal context at order time
- execution mode
- signal origin / provenance

### Outcome Capture

Resolved outcomes are written to:

- `var/research/outcomes/outcomes.jsonl`

The resolver in `src/outcomes/resolver.rs` scans captured market-state data, identifies markets whose close time is in the past, queries Kalshi for settlement state, and records final labels when resolution is available.

## Data Provenance and Execution Source Classes

One of the most important cleanups in this repo is explicit execution provenance.

Execution rows are classified into:

- `bootstrap_synthetic`
- `organic_paper`
- `live_real`

Not all execution data is equally useful:

- `bootstrap_synthetic` — useful only for plumbing validation
- `organic_paper` — helps `markout` and `fill_price` calibration; fill probability labels (`fill_30s`/`fill_5m`) are always 1.0 because the paper path always fills (`limit >= ask` is true for all candidates)
- `live_real` — the only source with real fill variance; required for fill probability and the active-mode gate

Execution training defaults to:

- `organic_paper`
- `live_real`

and excludes bootstrap synthetic rows by default.

That change exists specifically to prevent fake or forced bootstrap activity from polluting execution-model training.

## Feature Layer

Feature builders live in:

- `src/features/forecast.rs`
- `src/features/execution.rs`

The current feature layer includes:

### Forecast Features

- current market state
- time-to-close and time features
- parsed threshold and direction
- inferred vertical
- entity extraction from ticker/title
- enrichment-derived context

### Execution Features

- action parameters such as side, TIF, and limit price
- relative aggressiveness to observed price
- market spread and liquidity bucket
- forecast edge and confidence
- simple order-history context such as:
  - recent fills on same ticker
  - recent cancels on same ticker
  - same-event exposure

This is a first feature layer, not a finished execution-research feature store.

## Dataset Builder

The dataset builder lives in `src/datasets/builder.rs`.

It reads research logs and writes training tables to `var/features/`.

### Forecast Dataset

Written to:

- `var/features/forecast/forecast_training.jsonl`

It joins:

- market-state observations
- settled outcome labels

### Execution Dataset

Written to:

- `var/features/execution/execution_training.jsonl`
- `var/features/execution/execution_training_bootstrap.jsonl`
- `var/features/execution/execution_training_organic_paper.jsonl`
- `var/features/execution/execution_training_live_real.jsonl`

It groups order-lifecycle events by `client_order_id` and derives labels such as:

- filled within 30s
- filled within 5m
- terminal filled quantity
- terminal average fill price
- canceled / rejected
- 5m and 30m markout
- realized net PnL

Splits are time-based, not random.

## Forecast Model

The forecast model lives in `src/models/forecast.rs`.

Current implementation:

- empirical shrinkage baseline
- bucketed by global, vertical, direction, entity, and threshold groupings

Outputs:

- `fair_prob_yes`
- `uncertainty`
- `confidence`
- `model_version`
- `feature_ts`

Artifacts are written to:

- `var/models/forecast/<version>/artifact.json`
- `var/models/forecast/latest.json`
- `var/models/forecast/manifest.jsonl`

The forecast model is currently the strongest part of the new modeling stack.

## Execution Model

The execution model lives in `src/models/execution.rs`.

Current implementation:

- empirical execution baseline
- bucketed by vertical, vertical+TIF, and vertical+liquidity

Outputs:

- fill probability within 30s
- fill probability within 5m
- expected fill price
- expected slippage in bps
- expected 5m markout in bps
- expected 30m markout in bps

Artifacts are written to:

- `var/models/execution/<version>/artifact.json`
- `var/models/execution/latest.json`
- `var/models/execution/manifest.jsonl`

Important caveat:

The execution model's fill probability outputs are meaningless until live-real data accumulates. Organic paper data helps markout and fill-price calibration only. Treat the model as provisional until 50+ live-real rows exist.

### Data governance rules

- Keep execution provenance separated: `bootstrap_synthetic`, `organic_paper`, `live_real`.
- Do not use `bootstrap_synthetic` as a default training source for execution models.
- Treat `organic_paper + live_real` as the clean execution slice.
- `BOT_POLICY_MODE=active` is gated on live-real markout ≥ 0 over 50+ live rows — not on paper row count or estimated ERPNL.

### Retraining rules of thumb

- Forecast:
  - retrain after meaningful new settled outcomes are available
  - prefer waiting for larger labeled batches over constant tiny refreshes
  - a forecast model is more trustworthy once train rows are comfortably above `1000`
- Execution:
  - retrain when clean execution rows (`organic_paper + live_real`) materially increase
  - avoid trusting retrains built on fewer than `100` clean execution train rows
  - treat `live_real < 25` as below the bar for active-mode trust
- Reports:
  - `BOT_RUN_MODEL_REPORT=true` now surfaces warning messages when sample size or source mix is weak
  - read those warnings before considering rollout changes

## Policy Layer

The policy layer lives in `src/policy/decision.rs`.

It combines:

- a forecast output
- an execution estimate
- a legacy candidate
- current market state

It scores a small action grid, typically variations of:

- GTC near observed price
- IOC near ask
- more or less aggressive variants
- implicit skip if expected realized PnL is negative

The policy output is `PolicyDecision`, which includes:

- whether to trade
- chosen time in force
- chosen limit price
- size multiplier
- expected fill probability
- expected gross edge
- expected realized PnL
- rejection reason and rationale

## Policy Modes

Policy mode is controlled by `BOT_POLICY_MODE`.

### `legacy`

- legacy trading path decides trades
- newer models may still run if separately enabled, but do not control execution

### `shadow`

- legacy trading path still executes
- forecast, execution, and policy models run in parallel
- shadow outputs are recorded for comparison
- recommended mode today

### `active`

- policy decisions influence ranking, pricing, and sizing
- fails closed at startup if prerequisites are not met:
  - forecast and execution models within max age
  - execution model has ≥ `BOT_POLICY_ACTIVE_MIN_EXECUTION_LIVE_REAL_ROWS` (default 50) live-real rows
  - mean `markout_5m_bps` across those live-real rows ≥ `BOT_POLICY_ACTIVE_MIN_LIVE_MEAN_MARKOUT_BPS` (default 0)
- do not enable until `check_fills.py` confirms the legacy path has positive EV on live data

## Reports and Artifacts

### Cycle Artifacts

The runtime writes per-cycle artifacts under:

- `var/cycles/`

These capture selected markets, valuations, candidates, policy decisions, allocations, and execution outcomes.

### Model Reports

Generated by `src/models/report.rs`.

These compare:

- forecast model vs market-mid baseline
- execution model performance on the selected source classes

### Policy Reports

Generated by `src/policy/report.rs`.

These summarize:

- cycle counts
- policy mode usage
- rank changes
- should-trade vs rejected counts
- average expected realized PnL
- selected TIF distribution

### Research Reports

Generated by `src/research/report.rs`.

These summarize:

- research file counts
- row counts
- unique tickers and client order ids
- per-day collection volume

## Important Environment Variables

This project is heavily env-driven. The most important controls are:

### Exchange and execution

- `KALSHI_API_BASE_URL`
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY_PEM` or equivalent private key env
- `BOT_EXECUTION_MODE=paper|live`
- `BOT_EXEC_POLICY=ioc|gtc|hybrid`
- `BOT_HYBRID_IOC_FRACTION`

### Market selection

- `BOT_SCAN_MAX_MARKETS`
- `BOT_SCAN_MIN_VOLUME`
- `BOT_SCAN_MAX_SPREAD_CENTS`
- `BOT_SCAN_WS_DELTA_WINDOW_SECS`

### Valuation

- `BOT_MISPRICING_THRESHOLD`
- `BOT_FALLBACK_MISPRICING_THRESHOLD`
- `BOT_MIN_CANDIDATES`
- `BOT_ALLOW_HEURISTIC_IN_LIVE`
- `CLAUDE_MODEL`
- `ANTHROPIC_API_KEY`

### Allocation and risk

- `BOT_MAX_NOTIONAL_PER_TICKER`
- `BOT_REENTRY_COOLDOWN_SECS`
- `BOT_INVALID_PARAM_COOLDOWN_SECS`
- `BOT_ENFORCE_EVENT_MUTEX`

### Research and models

- `BOT_RESEARCH_CAPTURE_ENABLED`
- `BOT_RESEARCH_DIR`
- `BOT_RUN_OUTCOME_BACKFILL`
- `BOT_RUN_DATASET_BUILD`
- `BOT_RUN_FORECAST_TRAIN`
- `BOT_RUN_EXECUTION_TRAIN`
- `BOT_MODEL_FORECAST_PATH`
- `BOT_MODEL_EXECUTION_PATH`
- `BOT_EXECUTION_TRAIN_SOURCES`

### Policy and shadow

- `BOT_POLICY_MODE=legacy|shadow|active`
- `BOT_FORECAST_SHADOW_ENABLED`
- `BOT_EXECUTION_SHADOW_ENABLED`
- `BOT_POLICY_SHADOW_ENABLED`
- `BOT_POLICY_MIN_EXPECTED_REALIZED_PNL`
- `BOT_POLICY_MAX_ACTIONS_PER_CANDIDATE`
- `BOT_POLICY_ACTIVE_MAX_MODEL_AGE_HOURS`
- `BOT_POLICY_ACTIVE_MIN_FORECAST_TRAIN_ROWS`
- `BOT_POLICY_ACTIVE_MIN_EXECUTION_TRAIN_ROWS`
- `BOT_POLICY_ACTIVE_MIN_EXECUTION_LIVE_REAL_ROWS` (default 50)
- `BOT_POLICY_ACTIVE_MIN_LIVE_MEAN_MARKOUT_BPS` (default 0 — mean markout_5m_bps across live-real rows must be ≥ this)

## Common Workflows

Load env first:

```bash
set -a; source .env; set +a
```

### Morning routine (run after overnight)

```bash
BOT_RUN_OUTCOME_BACKFILL=true cargo run --release --quiet   # pull resolutions from Kalshi
BOT_RUN_DATASET_BUILD=true cargo run --release --quiet       # rebuild training datasets
python3 scripts/check_fills.py                               # check paper fill win/loss rate
python3 scripts/simulate_pnl.py                              # realistic PnL with guards applied
python3 scripts/evaluate_shadow.py                           # forecast calibration + policy hit rate
BOT_RUN_FORECAST_TRAIN=true cargo run --release --quiet      # retrain forecast model on new outcomes
```

### Release preflight

```bash
scripts/release_check.sh
```

### Operational profiles

Two main scripts:

```bash
scripts/trade.sh    # live trading, Claude enabled, shadow policy (production)
scripts/collect.sh  # paper trading, no Claude, data collection (runs 24/7 on server)
```

Supporting scripts:

```bash
scripts/rebuild_models.sh
scripts/morning_review.sh
scripts/compare_execution_slices.sh
scripts/execution_data_report.sh
```

### Run one cycle

```bash
BOT_RUN_ONCE=true scripts/trade.sh
BOT_RUN_ONCE=true scripts/collect.sh
```

### Run research capture only

```bash
BOT_RUN_ONCE=true scripts/run_research_capture.sh
```

### Run research paper capture only

```bash
BOT_RUN_ONCE=true scripts/run_research_paper_capture.sh
```

### Run organic paper execution collection

```bash
BOT_RUN_ONCE=true scripts/run_organic_paper_collection.sh
```

### Backfill outcomes

```bash
BOT_RUN_OUTCOME_BACKFILL=true cargo run --quiet
```

### Build datasets

```bash
BOT_RUN_DATASET_BUILD=true cargo run --quiet
```

### Train forecast model

```bash
BOT_RUN_FORECAST_TRAIN=true cargo run --quiet
```

### Train execution model

```bash
BOT_RUN_EXECUTION_TRAIN=true cargo run --quiet
```

### Generate model report

```bash
BOT_RUN_MODEL_REPORT=true cargo run --quiet
```

### Generate policy report

```bash
BOT_RUN_POLICY_REPORT=true cargo run --quiet
```

Latest day only:

```bash
BOT_RUN_POLICY_REPORT=true BOT_POLICY_REPORT_DAY=$(date +%Y-%m-%d) cargo run --quiet
```

### Generate execution data source report

```bash
scripts/execution_data_report.sh
```

### Generate research report

```bash
BOT_RUN_RESEARCH_REPORT=true cargo run --quiet
```

### Rebuild datasets and models

```bash
scripts/rebuild_models.sh
```

### Manual cargo entrypoints

The script entrypoints above are the recommended default. If you need to bypass them for debugging or one-off experiments, the raw `cargo run` mode flags still work.

## Open Source and Contribution Rules

If you contribute here, please follow these rules.

### 1. Do not commit secrets

Never commit:

- `.env`
- API keys
- private keys
- account identifiers that are not already intentionally public

### 2. Treat `var/` as generated and potentially sensitive

`var/` contains runtime artifacts, research logs, state, datasets, and models.

Even when it does not include direct personal identifiers, it may expose:

- strategy behavior
- market coverage
- timestamps
- execution history
- training data provenance

Do not casually commit new `var/` artifacts. If examples are needed, prefer small sanitized fixtures.

### 3. Preserve provenance

Do not mix synthetic, paper, and live execution data without labeling it.

Execution rows should continue to preserve source distinctions such as:

- `bootstrap_synthetic`
- `organic_paper`
- `live_real`

### 4. Shadow first

New modeling or policy logic should be introduced in `shadow` before being allowed to control live decisions.

### 5. Optimize for honest evaluation

Do not judge success only by mark-to-mid or paper fill assumptions.

Prefer:

- realized fill behavior
- explicit fill labels
- explicit markout labels
- time-based validation splits
- clean comparisons to baseline behavior

### 6. Avoid destructive cleanup of user data

Generated data can still be valuable for research. Prefer:

- `git rm --cached` over deleting local files
- sanitized fixtures over broad purges
- additive migration steps over irreversible cleanup

### 7. Keep docs general

Do not put machine-specific paths, personal hostnames, or local absolute paths in public-facing docs.

## What This Project Is Not

This is not yet:

- a high-frequency market maker
- a full order-book microstructure simulator
- a deep-learning serving stack
- a fully validated execution optimizer

It is better understood as:

- a solid Rust trading core
- a growing research/data pipeline
- an increasingly execution-aware modeling system under active development

## Roadmap Docs

- `docs/execution_aware_prediction_plan.md`
  - broad implementation roadmap for the execution-aware stack
- `docs/feature_catalog_step3.md`
  - feature definitions for the current forecast and execution feature layer
- `docs/release_process.md`
  - branch, release, and promotion workflow

## License

No explicit license file is currently included in the repository.

That means outside users should not assume broad reuse rights until a license is added.
