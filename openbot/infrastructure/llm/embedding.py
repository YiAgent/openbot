"""Embedding service for issue dedup — PRD v0.2 §2.3.

Supports Voyage-3-large (primary) and OpenAI text-embedding-3-large (fallback).
Provider is selected via OPENBOT_EMBEDDING_PROVIDER env var.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from openbot.core.settings import get_settings

_logger = logging.getLogger(__name__)

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_OPENAI_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingService:
    """Generate embeddings via Voyage or OpenAI."""

    def __init__(self) -> None:
        settings = get_settings()
        self._provider = getattr(settings, "embedding_provider", "voyage") or "voyage"
        self._model = getattr(settings, "embedding_model", "voyage-3-large") or "voyage-3-large"
        self._voyage_key = getattr(settings, "voyage_api_key", None)
        self._openai_key = getattr(settings, "openai_api_key", None)

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        if self._provider == "voyage" and self._voyage_key:
            return await self._embed_voyage(text)
        if self._openai_key:
            return await self._embed_openai(text)
        _logger.warning("embedding_no_provider_configured")
        return []

    async def _embed_voyage(self, text: str) -> list[float]:
        """Embed via Voyage API."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _VOYAGE_URL,
                    headers={"Authorization": f"Bearer {self._voyage_key}"},
                    json={"model": self._model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]
        except Exception:
            _logger.exception("voyage_embedding_failed")
            return []

    async def _embed_openai(self, text: str) -> list[float]:
        """Embed via OpenAI API (fallback)."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _OPENAI_URL,
                    headers={"Authorization": f"Bearer {self._openai_key}"},
                    json={"model": "text-embedding-3-large", "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]
        except Exception:
            _logger.exception("openai_embedding_failed")
            return []


async def search_similar_issues(
    embedding: list[float],
    repo: str,
    *,
    top_k: int = 10,
    min_similarity: float = 0.75,
) -> list[dict[str, Any]]:
    """Search for similar issues using pgvector cosine similarity.

    This requires:
      1. pgvector extension installed
      2. issue_embeddings table with vector column
      3. OPENBOT_POSTGRES_URL configured

    Returns list of dicts: {issue_number, title, similarity}
    """
    if not embedding:
        return []

    settings = get_settings()
    postgres_url = settings.postgres_url
    if not postgres_url:
        _logger.info("dedup_search_skipped_no_postgres")
        return []

    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(postgres_url)
        async with engine.connect() as conn:
            # pgvector cosine distance: 1 - cosine_similarity
            query = text("""
                SELECT issue_number, title, 1 - (embedding <=> :embedding::vector) AS similarity
                FROM issue_embeddings
                WHERE repo = :repo
                  AND 1 - (embedding <=> :embedding::vector) >= :min_sim
                ORDER BY embedding <=> :embedding::vector
                LIMIT :top_k
            """)
            result = await conn.execute(
                query,
                {
                    "embedding": str(embedding),
                    "repo": repo,
                    "min_sim": min_similarity,
                    "top_k": top_k,
                },
            )
            rows = result.fetchall()
            return [
                {"issue_number": row[0], "title": row[1], "similarity": float(row[2])}
                for row in rows
            ]
    except Exception:
        _logger.exception("pgvector_search_failed")
        return []
