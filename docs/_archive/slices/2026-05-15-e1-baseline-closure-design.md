# E1 Baseline Closure Design

> Date: 2026-05-15  
> Status: approved for implementation planning  
> Scope: repair eval documentation drift and close the E1 review-eval loop without waiting for the future OpenBot production agent

## 1. Goal

Turn the current E1 review eval from a successful prototype into a truthful, durable baseline system:

1. docs describe the real state of the system rather than optimistic milestone status;
2. the current deepagents path becomes a first-class **baseline solver**, not a temporary stand-in;
3. future `openbot_prod` review runs can be compared against that baseline on the exact same eval surface;
4. E1's observability loop is made real enough to trust: artifacts, LangSmith metadata, sample telemetry, and summary export all work end-to-end.

## 2. Non-goals

- Do **not** wait for or implement the future `openbot.application.workflows.review.run(...)` production agent.
- Do **not** expand into E2 suites, GitHub Actions scheduling, or weekly/release orchestration.
- Do **not** replace the synthetic smoke dataset with the real upstream Martian dataset in this change; that remains E2-T01.

## 3. Core architecture

### 3.1 Shared task, multiple solver providers

Review evals should use one canonical task surface and vary only the solver provider:

| Layer | Responsibility |
|---|---|
| canonical task factory | builds a `review_martian` task from a `solver_id` |
| solver registry | maps `deepagents_baseline` now and `openbot_prod` later |
| convenience entry points | expose easy-to-run baseline and future prod tasks |
| scorer / judge / dataset / metadata | shared so comparisons stay apples-to-apples |

The target shape is:

- canonical review task accepts `solver_id`
- convenience entry point now: `review_martian_baseline`
- convenience entry point later: `review_martian_openbot`
- `solver_id` is recorded in run metadata and reports

This keeps the comparison scientifically clean while still making daily use convenient.

### 3.2 Deepagents is a durable baseline

`deepagents_baseline` should be documented and implemented as a persistent comparator:

- it is not deleted when `openbot_prod` arrives;
- it remains runnable on the same public benchmark datasets;
- future reporting can answer whether OpenBot is better on quality, cost, latency, or artifact completeness.

### 3.3 E1 observability closure

The E1 loop is only considered truly closed when a baseline run can:

1. emit structured findings;
2. score them with the shared scorer surface;
3. export real artifacts;
4. attach run-level and sample-level LangSmith metadata;
5. capture non-zero token / cost telemetry when the model provider reports usage;
6. produce a report from either a local `.eval` log or a LangSmith run source;
7. pass a real validator rather than only displaying a checklist.

## 4. Documentation contract

The docs should distinguish three states explicitly:

| State | Meaning |
|---|---|
| done | implementation + acceptance criteria are actually satisfied |
| partial | useful code exists, but one or more original ACs are not yet satisfied |
| deferred | intentionally postponed by product decision |

Required doc corrections:

- `STATE.md` must stop calling partially satisfied tasks fully done.
- E1 handoffs must describe `deepagents_baseline` as a supported baseline provider.
- Any doc that mentions “temporary stand-in” should be rewritten unless it specifically refers to the future `openbot_prod` gap.
- Baseline logs must clearly separate synthetic smoke baselines from future real Martian baselines.

## 5. Data flow

### 5.1 Baseline run

```text
review_martian_baseline
  -> review task factory(solver_id="deepagents_baseline")
  -> deepagents solver
  -> shared review scorer / judge surface
  -> artifact exporter
  -> LangSmith run + sample metadata
  -> summary export
```

### 5.2 Future production comparison

```text
review_martian_openbot
  -> review task factory(solver_id="openbot_prod")
  -> OpenBot production solver
  -> same dataset / scorer / judge / metadata / reporting path
```

Any difference in results should therefore be attributable to the solver implementation rather than drift in the harness.

## 6. Error handling and reliability

- Artifact upload failures should be explicit and surfaced as eval failures, not silently ignored.
- Missing LangSmith metadata must still fail fast.
- Missing provider usage data should not crash the run, but the report must say usage is unavailable rather than fabricating `$0`.
- Missing dataset manifest fields continue to fail safe toward internal/private routing.
- Synthetic smoke data should remain available even after real Martian data arrives, because it is useful for cheap local pipeline checks.

## 7. Testing strategy

Use TDD for behavior changes:

1. add failing tests for solver selection / metadata fields / validator behavior / artifact export contracts / summary export sources;
2. implement the minimal code to make them pass;
3. preserve existing eval tests;
4. run a smoke baseline and one real LangSmith-backed verification path when credentials are available.

## 8. Expected outcomes

After implementation:

- docs no longer overstate E1 completion;
- `deepagents_baseline` is an intentional permanent comparator;
- review evals are structurally ready for baseline-vs-prod comparison;
- E1 observability is materially closer to production truth instead of prototype truth;
- future E2 work can build on a more honest foundation.
