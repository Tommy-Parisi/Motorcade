# Contributing

Thanks for contributing.

This repository mixes trading logic, research capture, model training, and operational tooling. To keep the codebase reviewable and safe to share, please follow these rules.

## Repo Hygiene

- Do not commit secrets.
- Do not commit `.env`.
- Do not commit private keys, API tokens, or account-specific credentials.
- Do not commit machine-specific paths or hostnames in docs.

## Generated Data

The `var/` tree is treated as generated runtime output.

Do not commit:

- `var/cycles/`
- `var/logs/`
- `var/research/`
- `var/features/`
- `var/models/`
- `var/state/`

If examples are needed, prefer adding small sanitized fixtures in a dedicated testdata-style location rather than committing live runtime artifacts.

## Data Provenance

Keep execution provenance explicit.

Execution and training data should preserve distinctions such as:

- `bootstrap_synthetic`
- `organic_paper`
- `live_real`

Do not silently merge these categories in training or evaluation.

## Rollout Discipline

Use a shadow-first workflow for new policy or model-driven behavior.

- `legacy` for the current trusted path
- `shadow` for side-by-side evaluation
- `active` only after review and explicit validation

## Reviewability

Try to keep commits focused:

- code changes separate from docs changes when practical
- source changes separate from generated output
- operational scripts separate from runtime artifacts

## Branch and Release Discipline

- Prefer feature branches for meaningful work instead of stacking large experiments directly on `main`.
- Keep release-oriented changes easy to audit:
  - runtime changes
  - model/reporting changes
  - docs/process changes
- Use `docs/release_process.md` as the default release workflow.
- Use `scripts/release_check.sh` before pushing changes that affect runtime safety, reporting, or rollout logic.

## Safety

Prefer non-destructive cleanup steps.

- use `git rm --cached` instead of deleting local research/runtime files
- preserve local data unless the user explicitly asks to remove it
