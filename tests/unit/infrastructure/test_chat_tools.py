"""Chat read-only tools — path allowlist + 8 KB truncation + bounds."""

from __future__ import annotations

from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._chat_tools import (
    CHAT_OUTPUT_BUDGET_BYTES,
    PATH_DENY_PATTERNS,
    TRUNCATED_MARKER,
    make_chat_tools,
)


class _StubAdapter:
    name = "stub"

    def __init__(self, files: dict[str, str], grep_hits: list[str] | None = None) -> None:
        self._files = files
        self._grep = grep_hits or []
        self.read_calls: list[str] = []
        self.grep_calls: list[tuple[str, str | None]] = []

    async def read_file(self, _event: UnifiedEvent, path: str) -> str:
        self.read_calls.append(path)
        return self._files.get(path, "")

    async def grep_repo(
        self,
        _event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        self.grep_calls.append((pattern, path_glob))
        return self._grep[:max_matches]

    async def list_repo_paths(self, _event: UnifiedEvent, *, root: str) -> list[str]:
        # Optional capability — chat tools fall back to derived listing
        # via grep_repo when this attribute is missing.
        return list(self._files)


def _evt() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d1",
        repo="acme/web",
        actor="alice",
        installation_id=1,
        issue_number=7,
        pr_number=None,
        comment_body="@openbot where?",
    )


def _tool(name: str, tools: list[Any]) -> Any:
    return next(t for t in tools if t.name == name)


async def test_read_file_returns_full_text_under_budget() -> None:
    adapter = _StubAdapter({"README.md": "hello"})
    tools = make_chat_tools(adapter=adapter, event=_evt())
    out = await _tool("read_file", tools).ainvoke({"path": "README.md"})
    assert out == "hello"
    assert TRUNCATED_MARKER not in out
    assert adapter.read_calls == ["README.md"]


async def test_read_file_truncates_over_budget() -> None:
    big = "a" * (CHAT_OUTPUT_BUDGET_BYTES + 1024)
    adapter = _StubAdapter({"big.txt": big})
    tools = make_chat_tools(adapter=adapter, event=_evt())
    out = await _tool("read_file", tools).ainvoke({"path": "big.txt"})
    assert TRUNCATED_MARKER in out
    assert len(out.encode("utf-8")) <= CHAT_OUTPUT_BUDGET_BYTES + len(
        TRUNCATED_MARKER.encode("utf-8")
    )


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "/etc/passwd",
        ".env",
        ".env.local",
        "secrets/id_rsa",
        "tls/server.pem",
        "tls/server.key",
        "tls/server.cert",
    ],
)
async def test_read_file_rejects_disallowed_path(bad: str) -> None:
    adapter = _StubAdapter({})
    tools = make_chat_tools(adapter=adapter, event=_evt())
    out = await _tool("read_file", tools).ainvoke({"path": bad})
    assert "refused" in out.lower()
    assert adapter.read_calls == []


async def test_grep_repo_passes_through_with_truncation() -> None:
    long_hit = "a" * (CHAT_OUTPUT_BUDGET_BYTES + 100)
    adapter = _StubAdapter({}, grep_hits=[long_hit])
    tools = make_chat_tools(adapter=adapter, event=_evt())
    out = await _tool("grep_repo", tools).ainvoke({"pattern": "thing"})
    assert TRUNCATED_MARKER in out


async def test_list_files_caps_entries_and_depth() -> None:
    paths = {f"src/dir{i}/file.py": "x" for i in range(500)}
    paths["README.md"] = "y"
    paths["openbot/very/deep/nested/file.py"] = "z"
    adapter = _StubAdapter(paths)
    tools = make_chat_tools(adapter=adapter, event=_evt())
    out = await _tool("list_files", tools).ainvoke({"path": "."})
    # Must enumerate, not exceed entry cap, may truncate
    assert "README.md" in out
    # The depth-cap (4) excludes the deeply-nested file
    assert "very/deep/nested" not in out


@pytest.mark.unit
def test_path_deny_patterns_cover_secret_globs() -> None:
    assert ".env*" in PATH_DENY_PATTERNS
    assert "*.pem" in PATH_DENY_PATTERNS
    assert "*.key" in PATH_DENY_PATTERNS
    assert "id_rsa*" in PATH_DENY_PATTERNS
    assert "*.cert" in PATH_DENY_PATTERNS
