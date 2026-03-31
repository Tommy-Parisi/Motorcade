#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_repo_env

(cd "${REPO_ROOT}" && python3 - <<'PY'
import json
import os
from collections import Counter, defaultdict

paths = {
    "base": "var/features/execution/execution_training.jsonl",
    "bootstrap": "var/features/execution/execution_training_bootstrap.jsonl",
    "organic_paper": "var/features/execution/execution_training_organic_paper.jsonl",
    "live_real": "var/features/execution/execution_training_live_real.jsonl",
    "retroactive": "var/features/execution/execution_training_retroactive.jsonl",
}

def summarize_jsonl(path):
    total = 0
    splits = Counter()
    sources = Counter()
    by_day = Counter()
    if not os.path.exists(path):
        return {"total": 0, "splits": {}, "sources": {}, "by_day": {}}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1
            splits[row.get("split")] += 1
            sources[row.get("execution_source_class")] += 1
            ts = (
                row.get("feature", {}).get("feature_ts")
                or row.get("feature_ts")
                or ""
            )
            day = ts[:10] if len(ts) >= 10 else "unknown"
            by_day[day] += 1
    return {
        "total": total,
        "splits": dict(splits),
        "sources": dict(sources),
        "by_day": dict(sorted(by_day.items())),
    }

report = {name: summarize_jsonl(path) for name, path in paths.items()}

print(json.dumps(report, indent=2))
PY
)
