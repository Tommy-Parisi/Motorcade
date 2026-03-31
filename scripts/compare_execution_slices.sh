#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_repo_env

FULL_SOURCES="${BOT_EXECUTION_TRAIN_SOURCES:-organic_paper,live_real}"
CLEAN_SOURCES="${BOT_EXECUTION_COMPARE_CLEAN_SOURCES:-organic_paper,live_real}"

echo "== Execution report: configured training mix =="
echo "sources=${FULL_SOURCES}"
BOT_EXECUTION_TRAIN_SOURCES="${FULL_SOURCES}" \
BOT_RUN_MODEL_REPORT=true \
run_cargo_bot

echo
echo "== Execution report: clean slice =="
echo "sources=${CLEAN_SOURCES}"
BOT_EXECUTION_TRAIN_SOURCES="${CLEAN_SOURCES}" \
BOT_RUN_MODEL_REPORT=true \
run_cargo_bot
