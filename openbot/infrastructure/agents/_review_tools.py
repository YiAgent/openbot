# openbot/infrastructure/agents/_review_tools.py
"""LangChain tool wrappers for the review agent.

Tools close over (adapter, event). The per-tool budget is now enforced
by ToolCallLimitMiddleware in the runtime stack — ToolBudget is retired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent


def make_review_tools(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
) -> list[StructuredTool]:
    """Build the per-run review tool list."""

    async def read_file(path: str) -> str:
        return await adapter.read_file(event, path)

    async def grep_repo(
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
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
            description=(
                "Read the UTF-8 text of a file in the repository. "
                "Returns an empty string if the file is missing or not decodable."
            ),
        ),
        StructuredTool.from_function(
            coroutine=grep_repo,
            name="grep_repo",
            description=(
                "Search the repository for a pattern via GitHub Code Search. "
                "`path_glob` is GitHub's `path:` qualifier. "
                "Returns up to `max_matches` lines formatted `path: fragment`."
            ),
        ),
    ]


__all__ = ["make_review_tools"]
