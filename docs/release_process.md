# Release Process

This repository now spans:

- live and paper trading logic
- research capture
- dataset generation
- model training
- policy shadow/active rollout

That means release discipline matters. The goal of this process is to keep `main` understandable, keep live behavior predictable, and make it obvious when a change is safe for `legacy`, `shadow`, or `active`.

## Branch rules

- Use feature branches for meaningful work.
- Avoid landing broad experiments directly on `main`.
- Keep branch scopes narrow:
  - runtime logic
  - model/reporting logic
  - docs
  - scripts
- Keep generated `var/` artifacts out of commits.

Recommended branch naming:

- `codex/runtime-...`
- `codex/models-...`
- `codex/policy-...`
- `codex/docs-...`

## Commit rules

- Prefer focused commits over mixed “kitchen sink” commits.
- Separate source changes from docs changes when practical.
- Separate repo hygiene / operational changes from model logic changes when practical.
- Do not commit secrets, `.env`, or machine-specific paths.

## Promotion ladder

Use this progression unless there is a very strong reason not to:

1. `legacy`
2. `shadow`
3. `active`

Interpretation:

- `legacy`: trusted execution path
- `shadow`: new logic runs and logs without steering trades
- `active`: new policy can control ranking, sizing, and pricing

`active` should only be used after the startup guards pass and recent shadow results have been reviewed.

## Release checklist

Before treating a runtime/model change as release-ready:

1. Repo hygiene
   - working tree clean
   - no `var/` churn staged
   - no secrets or local paths introduced
2. Runtime checks
   - `cargo check` passes
   - relevant targeted tests pass
   - startup/reconcile behavior looks sane
3. Model checks
   - `scripts/morning_review.sh` reviewed
   - model report warnings understood
   - execution source mix is acceptable
4. Rollout checks
   - new behavior has run in `shadow` first
   - `active` mode is only considered if policy prerequisites pass
5. Commit discipline
   - commit messages explain what changed
   - generated files are not included

## Recommended release flow

1. Build and test locally
2. Run `scripts/release_check.sh`
3. Review `scripts/morning_review.sh` output if model/policy changes are involved
4. Merge focused commits
5. Push to `main`
6. Run in `shadow` before considering `active`

## Notes on models

- Forecast improvements can move faster than execution improvements.
- Execution changes need stricter scrutiny because data quality and source mix matter more.
- If execution data is still mostly `organic_paper` and lacks `live_real`, treat policy outputs as advisory.
