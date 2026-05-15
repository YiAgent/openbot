#!/usr/bin/env python3
"""Validate a LangSmith run against the OpenBot eval contract.

This is the E0-T04 **占位** (placeholder) — the real LangSmith-client wiring
lands in E1-T02 (`evals.common.langsmith`). For now `--help` exposes the
checklist of what *will* be validated, so reviewers / CI know the contract
even before the implementation exists.

Reference: eval PRD §10.1 (run-level metadata) · §10.4 (experiment naming).
"""

from __future__ import annotations

import argparse
import sys

# eval PRD §10.1 — every run MUST carry these 14 metadata fields, else gate fails.
REQUIRED_RUN_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("suite_name", "e.g. review_shadow"),
    ("suite_version", "integer, e.g. 1"),
    ("dataset_version", "e.g. internal_prs_v1"),
    ("dataset_sha256", "SHA256 from manifest"),
    ("git_sha", "current commit"),
    ("prompt_version", "openbot/prompts/__version__"),
    ("workflow_version", "openbot/workflows/__version__"),
    ("model_id", "e.g. anthropic/claude-opus-4-7"),
    ("judge_model_id", "e.g. anthropic/claude-opus-4-7"),
    ("judge_prompt_version", "integer"),
    ("sandbox_backend", "modal | inspect_docker"),
    ("runner_version", "inspect-ai version"),
    ("mode", "smoke | regression | weekly | release"),
    ("started_at", "ISO-8601 UTC"),
    # NOTE: PRD §10.1 also lists `triggered_by` (github_actor | local | cron),
    # so the contract is actually 15 fields — both will be enforced.
    ("triggered_by", "github_actor | local | cron"),
)

# eval PRD §10.4 — exact pattern. Violating names fail the run.
EXPERIMENT_NAME_PATTERN = "{suite}-{dataset_version}-{git_sha_short}-{model_alias}-{mode}"
EXPERIMENT_NAME_EXAMPLES = (
    "review-shadow-internal_prs_v1-a1b2c3-opus47-regression",
    "swe-lite-upstream2026w20-d4e5f6-haiku45-weekly",
    "redteam-prompt_injection_v1-a1b2c3-opus47-release",
)


def _format_checklist() -> str:
    lines = [
        "Validation checklist (run will fail the gate if any item is violated):",
        "",
        "1. Required run-level metadata (eval PRD §10.1):",
    ]
    lines.extend(f"     - {name:<22} {hint}" for name, hint in REQUIRED_RUN_METADATA_FIELDS)
    lines.extend(
        [
            "",
            "2. Experiment naming (eval PRD §10.4):",
            f"     pattern : {EXPERIMENT_NAME_PATTERN}",
            "     examples:",
        ]
    )
    lines.extend(f"       {ex}" for ex in EXPERIMENT_NAME_EXAMPLES)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_langsmith_run.py",
        description=(
            "Validate a LangSmith run id against the OpenBot eval contract "
            "(metadata + experiment naming). E0-T04 stub — real client wiring "
            "lands in E1-T02."
        ),
        epilog=_format_checklist(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "run_id",
        nargs="?",
        help="LangSmith run id to validate (omitted in stub).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="LangSmith project name (defaults to LANGSMITH_PROJECT_INTERNAL env var).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.run_id is None:
        parser.print_help()
        return 0
    # Real validation lands in E1-T02. Until then refuse loudly so nobody
    # mistakes a green exit for "this run passed the contract".
    print(
        "validate_langsmith_run.py is an E0-T04 placeholder — "
        "real validation is wired in E1-T02 (evals.common.langsmith).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
