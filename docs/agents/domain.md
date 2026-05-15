# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The producer skill (`/grill-with-docs`) creates them lazily when terms or decisions actually get resolved.

## Layout (single-context)

OpenBot is a single-context repo (single service, single PRD, even when Web frontend lands in v0.3+ it lives in the same repo).

```
/
├── CLAUDE.md
├── CONTEXT.md                            ← created lazily by /grill-with-docs
├── docs/
│   ├── prd/                              ← product spec (already exists)
│   │   ├── openbot-prd.md
│   │   └── openbot-config-example.yaml
│   ├── research/                         ← v0.1→v0.3 evolution +调研 (already exists)
│   ├── agents/                           ← this file lives here
│   │   ├── issue-tracker.md
│   │   ├── triage-labels.md
│   │   └── domain.md
│   └── adr/                              ← architecture decisions (created lazily)
└── src/                                  ← code (not yet started; PRD §5 sketches the layout)
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

For OpenBot specifically, the PRD's [`§14 Glossary`](../prd/openbot-prd.md#14-glossary) already defines core terms (`ChannelAdapter`, `DeepAgent`, `Middleware stack`, `Modal sandbox`, `Thread metadata`, `LiteLLM`, `per_task budget`, `Shadow set`, etc.). Treat that section as the seed glossary until `CONTEXT.md` exists.

If a concept you need is not in either glossary, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_

For OpenBot, the PRD's [`§13 关键决策`](../prd/openbot-prd.md#13-关键决策全部锁定) acts as a pre-ADR ledger of 12 locked decisions. Treat contradictions to those rows with the same explicit-surface rule until they migrate to formal ADRs under `docs/adr/`.
