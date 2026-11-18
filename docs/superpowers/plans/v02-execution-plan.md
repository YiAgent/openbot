# v0.2 Execution Plan

## Status

| Feature | Status | Notes |
|---|---|---|
| F1: Per-task budget | ✅ DONE | BudgetGuard already in runtime.py line 121 |
| F2: Config approval gate | TODO | New middleware |
| F3: Audit CLI | TODO | New CLI commands |
| F4: LinearAdapter | TODO | New channel adapter |
| F5: Plugin system | TODO | New directory + CI |
| F6: Issue dedup | TODO | pgvector + embedding |

## Execution Order

1. F3: Audit CLI (reads existing data, no schema changes)
2. F2: Config approval gate (new middleware, small scope)
3. F5: Plugin system (scaffolding + 3 builtin plugins)
4. F4: LinearAdapter (new channel, webhook + write-back)
5. F6: Issue dedup (pgvector migration + embedding pipeline)
