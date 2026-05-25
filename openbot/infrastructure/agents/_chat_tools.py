# openbot/infrastructure/agents/_chat_tools.py
"""Read-only chat tools — PRD §4.4 / closure-followup workstream 3.

Tools close over (adapter, event) and call the adapter's ``read_file`` /
``grep_repo`` ports. The tool wrappers add three layers of safety:

  1. **Path allowlist.** Repo-relative only; reject ``..``, absolute paths,
     and a deny-list of secret-shaped globs (``.env*`` / ``*.pem`` / ``*.key``
     / ``id_rsa*`` / ``*.cert``). The deny-list is the same one the egress
     scanner backstops; defence-in-depth.
  2. **Output budget.** Each tool truncates at 8 KB and emits an explicit
     ``[truncated 8KB cap]`` marker the agent prompt explains. The model
     must not assume partial output is the full file.
  3. **Surface confinement.** No write_file, shell, branch, label, or PR
     tools are exported from this module — the symbol set IS the policy.

The closure-followup spec lists ``shell_readonly`` and ``web_fetch`` as
v0.1+ enhancements — they require an SSRF/argv allow-list story that
isn't in this slice.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent

CHAT_OUTPUT_BUDGET_BYTES: Final = 8 * 1024
TRUNCATED_MARKER: Final = "\n[truncated 8KB cap]"
LIST_FILES_MAX_ENTRIES: Final = 200
LIST_FILES_MAX_DEPTH: Final = 4

# Closed deny-list — globs that have ever been near a secret. Order:
# specific to general. New entries require code review.
PATH_DENY_PATTERNS: Final[tuple[str, ...]] = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.cert",
)

_REFUSAL_PREFIX: Final = (
    "[openbot] refused to read this path. Chat is restricted to "
    "repo-relative paths and the secret-deny-list blocks: "
)


def _path_is_allowed(path: str) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is empty when allowed."""
    if not path or path.strip() != path:
        return False, "empty or whitespace-padded path"
    if path.startswith("/"):
        return False, "absolute paths are not allowed"
    parts = path.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return False, "'..' segments are not allowed"
    last = parts[-1]
    for pattern in PATH_DENY_PATTERNS:
        if fnmatch.fnmatch(last, pattern):
            return False, f"path matches deny pattern '{pattern}'"
    return True, ""


def _truncate(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= CHAT_OUTPUT_BUDGET_BYTES:
        return text
    head = raw[:CHAT_OUTPUT_BUDGET_BYTES].decode("utf-8", errors="ignore")
    return head + TRUNCATED_MARKER


def _truncate_lines(lines: Sequence[str]) -> str:
    out: list[str] = []
    used = 0
    marker_size = len(TRUNCATED_MARKER.encode("utf-8"))
    for line in lines:
        line_size = len(line.encode("utf-8")) + 1
        if used + line_size + marker_size > CHAT_OUTPUT_BUDGET_BYTES:
            out.append(TRUNCATED_MARKER)
            return "\n".join(out)
        out.append(line)
        used += line_size
    return "\n".join(out)


def make_chat_tools(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
) -> list[StructuredTool]:
    """Return the closed v0.1 chat tool list: read_file, grep_repo, list_files."""

    async def read_file(path: str) -> str:
        allowed, reason = _path_is_allowed(path)
        if not allowed:
            return f"{_REFUSAL_PREFIX}{reason}"
        text = await adapter.read_file(event, path)
        return _truncate(text)

    async def grep_repo(
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> str:
        max_matches = max(1, min(int(max_matches), 100))
        hits = await adapter.grep_repo(
            event, pattern=pattern, path_glob=path_glob, max_matches=max_matches
        )
        return _truncate_lines(hits)

    async def list_files(path: str = ".") -> str:
        # Adapter port may not implement list_repo_paths in v0.1 — degrade
        # to a deterministic empty listing rather than raising; the agent
        # prompt explains that list_files may report empty when the adapter
        # lacks the capability.
        lister = getattr(adapter, "list_repo_paths", None)
        if lister is None:
            return "[openbot] list_files unavailable on this channel adapter."
        allowed, reason = _path_is_allowed(path if path != "." else "ok")
        if not allowed and path != ".":
            return f"{_REFUSAL_PREFIX}{reason}"
        all_paths = await lister(event, root=path)
        # Apply depth cap relative to ``path``. Sort lexicographically so the
        # cap surfaces stable, top-level entries first (README.md before
        # src/dirN/file.py) instead of leaking adapter iteration order.
        prefix = "" if path == "." else path.rstrip("/") + "/"
        results: list[str] = []
        for p in sorted(all_paths):
            if not p.startswith(prefix):
                continue
            rel = p[len(prefix) :]
            if rel.count("/") >= LIST_FILES_MAX_DEPTH:
                continue
            results.append(p)
            if len(results) >= LIST_FILES_MAX_ENTRIES:
                break
        return _truncate_lines(results)

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description=(
                "Read the UTF-8 text of a repo file at a given relative path. "
                "Returns at most 8 KB; truncated output ends with "
                "[truncated 8KB cap]. Refuses absolute paths, '..' segments, "
                "and secret-shaped paths (.env*, *.pem, *.key, id_rsa*, *.cert)."
            ),
        ),
        StructuredTool.from_function(
            coroutine=grep_repo,
            name="grep_repo",
            description=(
                "Search the repository for a literal/regex pattern. "
                "`path_glob` filters by GitHub Code Search's path: qualifier. "
                "Returns up to `max_matches` (≤100) lines, truncated at 8 KB."
            ),
        ),
        StructuredTool.from_function(
            coroutine=list_files,
            name="list_files",
            description=(
                "List repo paths under `path` (default '.'), capped at 200 "
                "entries and 4 directory levels deep. May return empty if the "
                "channel adapter does not implement repo-listing."
            ),
        ),
    ]


__all__ = [
    "CHAT_OUTPUT_BUDGET_BYTES",
    "LIST_FILES_MAX_DEPTH",
    "LIST_FILES_MAX_ENTRIES",
    "PATH_DENY_PATTERNS",
    "TRUNCATED_MARKER",
    "make_chat_tools",
]
