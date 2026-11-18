"""Plugin: reproduce_python_issue — diagnose Python issues from tracebacks.

Reads the issue body, extracts Python tracebacks, identifies the root
cause module/package, and suggests a minimal reproduction script.

This plugin does NOT mutate the repo. It only reads files and produces
a diagnosis string for the agent to incorporate into its response.
"""

from __future__ import annotations

import re
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from openbot_plugins import register


class ReproducePythonIssueInput(BaseModel):
    issue_body: Annotated[str, Field(description="The full issue body text.")]
    repo_files: Annotated[
        list[str],
        Field(description="List of file paths in the repo for context."),
    ] = Field(default_factory=list)


_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):(.*?\n\S[^\n]*)",
    re.DOTALL,
)
_FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')
_IMPORT_ERROR_RE = re.compile(r"(\w+Error|ImportError|ModuleNotFoundError): (.+)")


def _reproduce_python_issue(
    issue_body: str,
    repo_files: list[str],
) -> str:
    """Analyze a Python issue and suggest reproduction steps."""
    tracebacks = _TRACEBACK_RE.findall(issue_body)
    if not tracebacks:
        return "No Python traceback found in the issue body."

    tb = tracebacks[-1]  # most relevant traceback
    files = _FILE_LINE_RE.findall(tb)
    errors = _IMPORT_ERROR_RE.findall(tb)

    parts: list[str] = ["## Python Issue Analysis\n"]

    if errors:
        error_type, error_msg = errors[-1]
        parts.append(f"**Error:** `{error_type}: {error_msg}`\n")

    if files:
        parts.append("**Stack trace (most recent call):**\n")
        for filepath, lineno in files[-3:]:
            parts.append(f"- `{filepath}` line {lineno}")
        parts.append("")

    # Suggest reproduction
    parts.append("**Suggested reproduction:**\n")
    parts.append("1. Clone the repo and install dependencies")
    if files:
        parts.append(f"2. Create a minimal script that imports/calls code from `{files[-1][0]}`")
    parts.append("3. Run with the same Python version as the reporter")
    if errors:
        parts.append(f"4. Expect: `{errors[-1][0]}: {errors[-1][1]}`")

    # Mention relevant repo files
    if repo_files:
        matching = [f for f in repo_files if any(fp in f for fp, _ in files)]
        if matching:
            parts.append(f"\n**Relevant repo files:** {', '.join(matching[:5])}")

    return "\n".join(parts)


_TOOL = StructuredTool.from_function(
    coroutine=None,
    func=_reproduce_python_issue,
    name="reproduce_python_issue",
    description=(
        "Analyze a Python issue with traceback. Extracts error type, "
        "affected files, and suggests a minimal reproduction script."
    ),
    args_schema=ReproducePythonIssueInput,
)

register(_TOOL)
