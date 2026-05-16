#!/usr/bin/env python3
"""Validate a LangSmith run against the OpenBot eval contract.

Reference: eval PRD §10.1 (run-level metadata) · §10.4 (experiment naming).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Any

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

_EXPERIMENT_NAME_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*-[A-Za-z0-9_]+-[0-9a-f]{6,12}-[A-Za-z0-9_]+-"
    r"(smoke|regression|weekly|release)$"
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


def _run_metadata(run: Any) -> dict[str, Any]:
    extra = getattr(run, "extra", None) or {}
    metadata = extra.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def validate_run(run: Any) -> None:
    """Validate one LangSmith run object against the eval contract."""
    metadata = _run_metadata(run)
    required = {name for name, _hint in REQUIRED_RUN_METADATA_FIELDS}
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"run metadata missing required fields: {missing}")

    name = getattr(run, "name", "")
    if not isinstance(name, str) or not _EXPERIMENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"experiment name {name!r} does not match contract {EXPERIMENT_NAME_PATTERN!r}"
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
            "(metadata + experiment naming)."
        ),
        epilog=_format_checklist(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "run_id",
        nargs="?",
        help="LangSmith run id to validate.",
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

    from langsmith import Client

    client = Client()
    run = client.read_run(args.run_id)
    try:
        validate_run(run)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
