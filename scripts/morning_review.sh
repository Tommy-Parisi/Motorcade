#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_repo_env

echo "== Research report =="
BOT_RUN_RESEARCH_REPORT=true run_cargo_bot

echo
echo "== Model report =="
BOT_RUN_MODEL_REPORT=true run_cargo_bot

echo
echo "== Execution slice comparison =="
"${SCRIPT_DIR}/compare_execution_slices.sh"

echo
echo "== Execution data report =="
"${SCRIPT_DIR}/execution_data_report.sh"

echo
echo "== Policy report =="
BOT_RUN_POLICY_REPORT=true \
BOT_POLICY_REPORT_DAY="${BOT_POLICY_REPORT_DAY:-$(date +%Y-%m-%d)}" \
run_cargo_bot
