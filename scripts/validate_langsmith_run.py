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
import pathlib
import sys

# Allow `python scripts/validate_langsmith_run.py` from any CWD to find the
# repo-root `evals` package without forcing the caller to set PYTHONPATH.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.common._metadata_spec import (  # noqa: E402
    ALLOWED_FAILURE_CATEGORIES,
    EXPERIMENT_NAME_EXAMPLES,
    EXPERIMENT_NAME_PATTERN,
    REQUIRED_RUN_METADATA_FIELDS,
)


def validate_failure_category(value: str) -> None:
    """Reject any `failure_category` that isn't in the PRD §12.4 enum.

    Raises `ValueError` with a clear message listing the allowed values.
    This is the surface the LangSmith-side validator (and CI) will call
    per sample before accepting a run.
    """
    if value not in ALLOWED_FAILURE_CATEGORIES:
        raise ValueError(
            f"failure_category {value!r} is not in the PRD §12.4 enum. "
            f"Allowed: {sorted(ALLOWED_FAILURE_CATEGORIES)}"
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
    lines.extend(
        [
            "",
            "3. failure_category fixed enum (eval PRD §12.4):",
            f"     {sorted(ALLOWED_FAILURE_CATEGORIES)}",
        ]
    )
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
