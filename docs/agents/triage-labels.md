# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Relationship to OpenBot's business labels

This vocabulary is **separate from** OpenBot's product-level labels defined in [`docs/prd/openbot-prd.md`](../prd/openbot-prd.md):

- **Triage labels** (this file) describe the workflow state of an issue (where is it in the maintainer's review pipeline).
- **Business labels** (`cancel-openbot`, `priority/P0..P3`, `bug`, `enhancement`, `performance`, `docs`, `security`, `config-approved`, etc.) describe what the issue *is about*.

They coexist on the same issue without conflict — e.g. `priority/P1` + `ready-for-agent` is a valid combination.

Edit the right-hand column to match whatever vocabulary you actually end up using on GitHub.
