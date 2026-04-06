#!/bin/bash
# Start the weather sidecar. Reads WEATHER_SIDECAR_HOST/PORT from environment.
# Logs go to stdout; redirect as needed.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python sidecar.py
