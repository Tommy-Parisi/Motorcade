#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${CRYPTO_SIDECAR_PORT:-8766}"
if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "crypto sidecar already running on port ${PORT}, skipping start"
    exit 0
fi

exec .venv/bin/python sidecar.py
