"""Plugin: summarize_pr_diff — generate a structured summary of a PR diff.

Parses a unified diff and produces a human-readable summary with:
- Changed files grouped by type (added/modified/deleted)
- Key changes per file (function/method level)
- Potential risks or concerns
"""

from __future__ import annotations

import re
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from openbot_plugins import register


class SummarizePrDiffInput(BaseModel):
    diff: Annotated[str, Field(description="The unified diff text (git diff format).")]
    max_files: Annotated[
        int,
        Field(description="Maximum number of files to include in the summary."),
    ] = Field(default=20, ge=1, le=100)


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$", re.MULTILINE)
_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_FUNC_DEF_RE = re.compile(
    r"^\+.*(?:def |class |function |const |let |var |export )(\w+)", re.MULTILINE
)


def _summarize_pr_diff(
    diff: str,
    max_files: int,
) -> str:
    """Produce a structured summary of a unified diff."""
    if not diff.strip():
        return "Empty diff — no changes to summarize."

    files = _FILE_HEADER_RE.findall(diff)
    if not files:
        return "Could not parse diff format."

    parts: list[str] = ["## PR Diff Summary\n"]

    # File-level summary
    parts.append(f"**Files changed:** {len(files)}\n")

    # Split diff by file
    file_diffs: dict[str, str] = {}
    current_file = None
    current_lines: list[str] = []

    for line in diff.split("\n"):
        match = _FILE_HEADER_RE.match(line)
        if match:
            if current_file and current_lines:
                file_diffs[current_file] = "\n".join(current_lines)
            current_file = match.group(2)
            current_lines = [line]
        elif current_file:
            current_lines.append(line)
    if current_file and current_lines:
        file_diffs[current_file] = "\n".join(current_lines)

    # Analyze each file
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for filepath, file_diff in list(file_diffs.items())[:max_files]:
        lines = file_diff.split("\n")
        added_lines = len([ln for ln in lines if ln.startswith("+") and not ln.startswith("+++")])
        removed_lines = len([ln for ln in lines if ln.startswith("-") and not ln.startswith("---")])

        if added_lines > 0 and removed_lines == 0:
            added.append(filepath)
        elif removed_lines > 0 and added_lines == 0:
            deleted.append(filepath)
        else:
            modified.append(filepath)

    if added:
        parts.append(f"**Added ({len(added)}):** {', '.join(added[:10])}")
    if modified:
        parts.append(f"**Modified ({len(modified)}):** {', '.join(modified[:10])}")
    if deleted:
        parts.append(f"**Deleted ({len(deleted)}):** {', '.join(deleted[:10])}")
    parts.append("")

    # Key changes per file (top 5 modified files)
    parts.append("### Key changes\n")
    for filepath in modified[:5]:
        file_diff = file_diffs[filepath]
        new_defs = _FUNC_DEF_RE.findall(file_diff)
        f_lines = file_diff.split("\n")
        adds = len([ln for ln in f_lines if ln.startswith("+") and not ln.startswith("+++")])
        dels = len([ln for ln in f_lines if ln.startswith("-") and not ln.startswith("---")])

        parts.append(f"**`{filepath}`** (+{adds}/-{dels})")
        if new_defs:
            parts.append(f"  New/changed: {', '.join(new_defs[:5])}")
        parts.append("")

    return "\n".join(parts)


_TOOL = StructuredTool.from_function(
    coroutine=None,
    func=_summarize_pr_diff,
    name="summarize_pr_diff",
    description=(
        "Generate a structured summary of a PR diff. Groups files by "
        "added/modified/deleted, lists key changes per file, and identifies "
        "new functions or classes."
    ),
    args_schema=SummarizePrDiffInput,
)

register(_TOOL)
