# v0.2 Overall Spec

> 6 features, 3 sprints, 6 weeks. PRD: `docs/prd/openbot-v02-prd.md`

## Feature List

| # | Feature | Sprint | Effort | Files |
|---|---|---|---|---|
| F1 | Per-task budget fix | 1 | 2d | `_budget_middleware.py`, tests |
| F2 | Config approval gate | 1 | 1d | `config_approval.py`, tests |
| F3 | Audit CLI | 1 | 2d | `openbot/cli/audit.py`, tests |
| F4 | LinearAdapter | 2 | 5d | `linear.py`, `channel_credentials.py`, webhook route, tests |
| F5 | Plugin system | 2 | 3d | `openbot_plugins/`, CI gate, CONTRIBUTING.md |
| F6 | Issue dedup | 3 | 4d | `dedup.py`, pgvector migration, embedding + rerank, tests |

## Execution Order

F1 → F2 → F3 → F4 → F5 → F6

## Shared Constraints

- `make check` must pass after each feature
- No hardcoded values — env vars via `Settings`
- Immutable data patterns (frozen dataclasses)
- Hexagonal architecture — domain ← application ← infrastructure
- Egress scanning on all bot-authored output
