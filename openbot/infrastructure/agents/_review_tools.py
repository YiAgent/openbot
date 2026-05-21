"""LangChain tool wrappers for the review responder (slice A2).

The deepagents runtime takes a sequence of LangChain ``BaseTool`` (or plain
callables) and exposes them to the model. Our tools wrap two
``ChannelAdapterPort`` methods (``read_file`` / ``grep_repo``) so the
review agent can fetch repo context beyond what's in the inline diff.

Why a separate module:

  - Tools close over ``(adapter, event)``; they cannot live as
    bare module-level functions because each review run binds different
    state. The factory ``make_review_tools`` returns a fresh tool list
    per call.
  - The ``ToolBudget`` guard caps total tool invocations per run.
    opus-4-7 can comfortably issue 30+ grep calls if left unbounded,
    and every one hits ``api.github.com``. The default cap (5) keeps
    cost predictable; tests freeze the number so a bump is intentional.

The agent itself is built in ``deepagents_review.py``; this module only
owns the binding from port methods to the LangChain tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent


# Picked tight on purpose — opus-4-7 + unbounded greps = surprise spend.
# Each tool call is one GitHub REST request. 5 invocations comfortably
# covers "look at one file, grep for two patterns, double-check" without
# letting a confused agent loop forever.
DEFAULT_TOOL_BUDGET = 5


class ToolBudgetExceededError(RuntimeError):
    """Raised when a tool call would exceed the per-run budget.

    The deepagents runtime surfaces this back to the model as a tool error;
    the model is expected to synthesize a final answer from what it already
    has rather than retry the call.
    """

    def __init__(self, tool: str) -> None:
        super().__init__(f"tool budget exhausted on call to {tool!r}")
        self.tool = tool


@dataclass
class ToolBudget:
    """Counts down across tool invocations within a single review run."""

    remaining: int

    def consume(self, tool: str) -> None:
        if self.remaining <= 0:
            raise ToolBudgetExceededError(tool)
        self.remaining -= 1


def _read_file_description() -> str:
    return (
        "Read the UTF-8 text of a file in the repository. "
        "Returns an empty string if the file is missing or not decodable. "
        "Use this when the diff references a function you need to see in full."
    )


def _grep_repo_description() -> str:
    return (
        "Search the repository for a pattern via GitHub Code Search. "
        "`path_glob` is GitHub's `path:` qualifier (substring, not real glob). "
        "Returns up to `max_matches` lines formatted `path: fragment`. "
        "Returns an empty list on no matches OR on backend errors — do not "
        "infer absence from emptiness; fall back to `read_file` on a known path."
    )


def make_review_tools(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    budget: ToolBudget | None = None,
) -> list[StructuredTool]:
    """Build the per-run review tool list.

    Each tool closes over (adapter, event, budget). The same ``adapter``
    can back many concurrent runs because the tools are stateless aside
    from the budget — which is owned by the caller per-run.
    """
    bud = budget if budget is not None else ToolBudget(remaining=DEFAULT_TOOL_BUDGET)

    async def read_file(path: str) -> str:
        bud.consume("read_file")
        return await adapter.read_file(event, path)

    async def grep_repo(
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        bud.consume("grep_repo")
        return await adapter.grep_repo(
            event,
            pattern=pattern,
            path_glob=path_glob,
            max_matches=max_matches,
        )

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description=_read_file_description(),
        ),
        StructuredTool.from_function(
            coroutine=grep_repo,
            name="grep_repo",
            description=_grep_repo_description(),
        ),
    ]


__all__ = [
    "DEFAULT_TOOL_BUDGET",
    "ToolBudget",
    "ToolBudgetExceededError",
    "make_review_tools",
]
