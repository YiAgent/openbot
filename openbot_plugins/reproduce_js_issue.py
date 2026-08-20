"""Plugin: reproduce_js_issue — diagnose JavaScript/TypeScript issues.

Reads the issue body, extracts JS/TS stack traces, identifies the root
cause module, and suggests a minimal reproduction script.
"""

from __future__ import annotations

import re
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from openbot_plugins import register


class ReproduceJsIssueInput(BaseModel):
    issue_body: Annotated[str, Field(description="The full issue body text.")]
    repo_files: Annotated[
        list[str],
        Field(description="List of file paths in the repo for context."),
    ] = Field(default_factory=list)


_STACK_RE = re.compile(
    r"(?:Error|TypeError|ReferenceError|SyntaxError|RangeError|URIError|EvalError|InternalError)"
    r"[^\n]*\n(?:\s+at\s+[^\n]+\n?)+",
    re.MULTILINE,
)
_AT_RE = re.compile(r"at\s+(?:(.+?)\s+\()?(.+):(\d+):\d+\)?")
_ERROR_TYPE_RE = re.compile(
    r"^(Error|TypeError|ReferenceError|SyntaxError|RangeError):?\s*(.*)", re.MULTILINE
)


def _reproduce_js_issue(
    issue_body: str,
    repo_files: list[str],
) -> str:
    """Analyze a JS/TS issue and suggest reproduction steps."""
    stacks = _STACK_RE.findall(issue_body)
    error_matches = _ERROR_TYPE_RE.findall(issue_body)

    if not stacks and not error_matches:
        return "No JavaScript/Typestack trace found in the issue body."

    parts: list[str] = ["## JS/TS Issue Analysis\n"]

    if error_matches:
        error_type, error_msg = error_matches[-1]
        parts.append(f"**Error:** `{error_type}: {error_msg.strip()}`\n")

    if stacks:
        frames = _AT_RE.findall(stacks[-1])
        if frames:
            parts.append("**Stack trace (top frames):**\n")
            for func, filepath, lineno in frames[:5]:
                func_str = func if func else "<anonymous>"
                parts.append(f"- `{func_str}` at `{filepath}:{lineno}`")
            parts.append("")

    # Suggest reproduction
    parts.append("**Suggested reproduction:**\n")
    parts.append("1. Clone the repo and run `npm install` / `yarn install`")
    parts.append("2. Create a minimal script that triggers the same code path")
    parts.append("3. Run with the same Node.js version as the reporter")
    if error_matches:
        parts.append(f"4. Expect: `{error_matches[-1][0]}: {error_matches[-1][1].strip()}`")

    if repo_files:
        parts.append(f"\n**Repo files for context:** {', '.join(repo_files[:5])}")

    return "\n".join(parts)


_TOOL = StructuredTool.from_function(
    coroutine=None,
    func=_reproduce_js_issue,
    name="reproduce_js_issue",
    description=(
        "Analyze a JavaScript/TypeScript issue with stack trace. Extracts error type, "
        "affected files, and suggests a minimal reproduction script."
    ),
    args_schema=ReproduceJsIssueInput,
)

register(_TOOL)
