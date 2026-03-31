#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_repo_env
ensure_logs_dir

# Dedicated profile for growing realistic paper execution rows.
# This intentionally avoids bootstrap forcing and keeps execution in paper mode.
export BOT_EXECUTION_MODE="${BOT_ORGANIC_PAPER_EXECUTION_MODE:-paper}"
export BOT_RUN_RESEARCH_PAPER_CAPTURE_ONLY="${BOT_ORGANIC_PAPER_CAPTURE_ONLY:-true}"
export BOT_FORCE_TEST_CANDIDATE="${BOT_ORGANIC_PAPER_FORCE_TEST_CANDIDATE:-false}"
export BOT_RUN_ONCE="${BOT_RUN_ONCE:-false}"
export BOT_CYCLE_SECONDS="${BOT_ORGANIC_PAPER_CYCLE_SECONDS:-600}"
export BOT_CARGO_PROFILE="${BOT_CARGO_PROFILE:-release}"

# Increase candidate flow so more realistic paper orders reach the lifecycle logger.
export BOT_MISPRICING_THRESHOLD="${BOT_ORGANIC_PAPER_MISPRICING_THRESHOLD:-0.04}"
export BOT_MIN_EDGE_PCT="${BOT_ORGANIC_PAPER_MIN_EDGE_PCT:-0.04}"
export BOT_FALLBACK_MISPRICING_THRESHOLD="${BOT_ORGANIC_PAPER_FALLBACK_THRESHOLD:-0.01}"
export BOT_MIN_CANDIDATES="${BOT_ORGANIC_PAPER_MIN_CANDIDATES:-10}"
export BOT_ALLOW_HEURISTIC_IN_LIVE="${BOT_ORGANIC_PAPER_ALLOW_HEURISTIC:-true}"
export BOT_MAX_OPEN_EXPOSURE="${BOT_ORGANIC_PAPER_MAX_OPEN_EXPOSURE:-1000000}"
export BOT_MAX_NOTIONAL_PER_TICKER="${BOT_ORGANIC_PAPER_MAX_NOTIONAL_PER_TICKER:-500}"

# Keep scan broad to maximise candidate variety.
# All 5 tier-2 categories so Politics/Financials/Weather fill overnight gaps when
# Sports/Crypto are quiet. allow_no_spread kept false (default): no-spread markets
# have fair≈mid so edge≈-cost on both sides and just dilute the candidate pool.
#
# Tier-1 allowlist: financial/commodity series that have real enrichment signal
# (finance_price_signal from Yahoo Finance) and are liquid 24/7. These are always
# fetched in tier-1 so a tier-2 series-discovery collapse cannot drop them.
# Override with BOT_ORGANIC_PAPER_SCAN_SERIES_ALLOWLIST='' to disable.
export BOT_SCAN_SERIES_ALLOWLIST="${BOT_ORGANIC_PAPER_SCAN_SERIES_ALLOWLIST:-KXSILVERD,KXGOLDMON,KXBTCD,KXETHD,KXSOLD,KXXRPD,KXNASDAQ100MINY}"
export BOT_SCAN_TIER2_CATEGORIES="${BOT_ORGANIC_PAPER_TIER2_CATEGORIES:-Sports,Crypto,Politics,Financials,Climate and Weather}"
export BOT_SCAN_MAX_TIER2_SERIES="${BOT_ORGANIC_PAPER_MAX_TIER2_SERIES:-25}"
export BOT_SCAN_SERIES_MAX_PER_FETCH="${BOT_ORGANIC_PAPER_SERIES_MAX_PER_FETCH:-100}"
export BOT_SCAN_MAX_MARKETS="${BOT_ORGANIC_PAPER_SCAN_MAX_MARKETS:-300}"
export BOT_VALUATION_MARKETS="${BOT_ORGANIC_PAPER_VALUATION_MARKETS:-60}"
# Must cover all 7 tier-1 Finance tickers in the round-robin (Finance is bucket 3/7).
# With limit=50, Finance gets ~7 slots even when all other verticals also have markets.
export BOT_ENRICHMENT_MARKETS="${BOT_ORGANIC_PAPER_ENRICHMENT_MARKETS:-50}"

run_cargo_bot
