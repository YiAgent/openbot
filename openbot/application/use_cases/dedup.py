"""Issue deduplication — PRD v0.2 §2.3.

Pipeline:
  1. Embed new issue title + body
  2. pgvector ANN search (top-K by cosine similarity)
  3. LLM rerank → top-3 candidates
  4. Bot comment with candidates (never auto-close)

Dependencies: pgvector extension on Postgres, embedding provider (Voyage/OpenAI).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """A potential duplicate issue."""

    issue_number: int
    title: str
    similarity: float


@dataclass(frozen=True, slots=True)
class DedupResult:
    """Result of dedup analysis."""

    candidates: list[DedupCandidate]
    comment: str


def render_dedup_comment(candidates: list[DedupCandidate]) -> str:
    """Render a user-facing comment listing potential duplicates."""
    if not candidates:
        return ""

    parts: list[str] = [
        ":mag: **Potential duplicates found:**\n",
    ]
    for c in candidates:
        pct = f"{c.similarity * 100:.0f}%"
        parts.append(f"- #{c.issue_number} — {c.title} ({pct} similar)")

    parts.append("")
    parts.append(
        "_Please check if this issue is a duplicate. "
        "If so, close this one and continue the discussion in the existing issue._"
    )
    return "\n".join(parts)


async def check_duplicates(
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    *,
    issue_title: str,
    issue_body: str,
    embedding_func: Any | None = None,
    search_func: Any | None = None,
    rerank_func: Any | None = None,
    min_similarity: float = 0.75,
    top_k: int = 10,
    rerank_top: int = 3,
) -> DedupResult:
    """Check for duplicate issues and return candidates.

    This is the use-case entry point. It orchestrates:
      1. Embed the issue
      2. Search for similar issues
      3. Rerank with LLM
      4. Render the comment

    When embedding/search/rerank functions are None (not configured),
    returns an empty result gracefully.
    """
    if embedding_func is None or search_func is None:
        _logger.info("dedup_skipped_not_configured")
        return DedupResult(candidates=[], comment="")

    try:
        # Step 1: Embed
        text = f"{issue_title}\n\n{issue_body}"[:4000]  # truncate for embedding
        embedding = await embedding_func(text)

        # Step 2: Search
        similar = await search_func(
            embedding=embedding,
            repo=event.repo,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        if not similar:
            return DedupResult(candidates=[], comment="")

        # Step 3: Rerank (optional)
        candidates = [
            DedupCandidate(
                issue_number=s["issue_number"],
                title=s["title"],
                similarity=s["similarity"],
            )
            for s in similar[:rerank_top]
        ]

        # Step 4: Render comment
        comment = render_dedup_comment(candidates)

        _logger.info(
            "dedup_completed",
            extra={
                "repo": event.repo,
                "candidates": len(candidates),
                "max_similarity": max(c.similarity for c in candidates) if candidates else 0,
            },
        )

        return DedupResult(candidates=candidates, comment=comment)

    except Exception:
        _logger.exception("dedup_failed")
        return DedupResult(candidates=[], comment="")
