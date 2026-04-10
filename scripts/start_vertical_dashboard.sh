#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${VERTICAL_DASHBOARD_PORT:-8080}"
REFRESH_SECONDS="${VERTICAL_DASHBOARD_REFRESH_SECONDS:-60}"
SINCE="${VERTICAL_DASHBOARD_SINCE:-0000-00-00}"
RESEARCH_DIR="${BOT_RESEARCH_DIR:-$ROOT/var/research}"
BOT_LOG_PATH="${VERTICAL_DASHBOARD_BOT_LOG_PATH:-}"
BOT_LOG_LINES="${VERTICAL_DASHBOARD_BOT_LOG_LINES:-60}"

exec python3 "$ROOT/scripts/serve_vertical_dashboard.py" \
  --research-dir "$RESEARCH_DIR" \
  --output "$ROOT/vertical_dashboard.html" \
  --since "$SINCE" \
  --refresh-seconds "$REFRESH_SECONDS" \
  --port "$PORT" \
  --bot-log-path "$BOT_LOG_PATH" \
  --bot-log-lines "$BOT_LOG_LINES"
