#!/bin/bash
# Start the weather sidecar. Reads WEATHER_SIDECAR_HOST/PORT from environment.
# Logs go to stdout; redirect as needed.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${WEATHER_SIDECAR_PORT:-8765}"
if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "weather sidecar already running on port ${PORT}, skipping start"
    exit 0
fi

exec .venv/bin/python sidecar.py
