#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_repo_env

echo "== cargo check =="
cargo check

echo
echo "== active-mode policy gate tests =="
cargo test active_policy_validation_rejects_insufficient_live_real_rows -- --nocapture
cargo test active_policy_validation_accepts_sufficient_models -- --nocapture

echo
echo "== recovery tests =="
cargo test reconcile_prunes_exchange_not_found_orders -- --nocapture
cargo test summarize_performance_ignores_malformed_journal_lines -- --nocapture

echo
echo "== startup validation test =="
cargo test startup_validation_creates_parent_directories -- --nocapture

echo
echo "release check complete"
