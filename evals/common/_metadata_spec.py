"""LangSmith metadata contract — eval PRD §10.1 / §10.2 / §10.4.

This module is the single source of truth for what every eval run must record.
Imported by:
- `scripts/validate_langsmith_run.py` (CLI gate, exposed via --help)
- `evals/common/langsmith.py` (runtime guard, raises ValueError on missing fields)

Keep it dependency-free so the validator script can import it without the eval
extras installed.
"""

from __future__ import annotations

# eval PRD §10.1 — every run MUST carry these run-level metadata fields.
# Missing any one fails the gate.
REQUIRED_RUN_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("suite_name", "e.g. review_shadow"),
    ("suite_version", "integer, e.g. 1"),
    ("dataset_version", "e.g. internal_prs_v1"),
    ("dataset_sha256", "SHA256 from manifest"),
    ("git_sha", "current commit"),
    ("prompt_version", "openbot/prompts/__version__"),
    ("workflow_version", "openbot/workflows/__version__"),
    ("solver_id", "e.g. deepagents_baseline"),
    ("solver_family", "baseline | production"),
    ("model_id", "e.g. anthropic/claude-opus-4-7"),
    ("judge_model_id", "e.g. anthropic/claude-opus-4-7"),
    ("judge_prompt_version", "integer"),
    ("sandbox_backend", "modal | inspect_docker"),
    ("runner_version", "inspect-ai version"),
    ("mode", "smoke | regression | weekly | release"),
    ("started_at", "ISO-8601 UTC"),
    ("triggered_by", "github_actor | local | cron"),
)

REQUIRED_RUN_FIELD_NAMES: frozenset[str] = frozenset(
    name for name, _ in REQUIRED_RUN_METADATA_FIELDS
)

# eval PRD §10.2 — every sample MUST carry these.
REQUIRED_SAMPLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("sample_id", "dataset-unique sample id"),
    ("input_artifact_refs", "artifact ref list (LangSmith URLs)"),
    ("output_artifact_refs", "artifact ref list"),
    ("score_payload", "scorer-shaped structured dict"),
    ("tokens_in", "int"),
    ("tokens_out", "int"),
    ("cost_usd", "float"),
    ("latency_ms", "int"),
    ("step_count", "int"),
    ("tool_call_count", "int"),
    ("retry_count", "int"),
    ("sandbox_restart_count", "int"),
    ("failure_category", "see PRD §12.4 enum"),
)

REQUIRED_SAMPLE_FIELD_NAMES: frozenset[str] = frozenset(name for name, _ in REQUIRED_SAMPLE_FIELDS)

# eval PRD §10.4 — exact pattern. Violating names fail the run.
EXPERIMENT_NAME_PATTERN = "{suite}-{dataset_version}-{git_sha_short}-{model_alias}-{mode}"
EXPERIMENT_NAME_EXAMPLES: tuple[str, ...] = (
    "review-shadow-internal_prs_v1-a1b2c3-opus47-regression",
    "swe-lite-upstream2026w20-d4e5f6-haiku45-weekly",
    "redteam-prompt_injection_v1-a1b2c3-opus47-release",
)

# eval PRD §12.4 — failure_category fixed enum (subset; full list lives in PRD).
ALLOWED_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "setup_failure",
        "sandbox_error",
        "agent_failure",
        "test_failure",
        "budget_stop",
        "timeout",
        "judge_error",
        "scorer_bug",
        "transient_network",
        "transient_modal",
        # also accept the success sentinel — sample passed cleanly
        "none",
    }
)
