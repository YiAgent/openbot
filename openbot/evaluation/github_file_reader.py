"""Lightweight GitHub file reader for the eval harness.

Uses a plain GitHub personal-access token (PAT) to read files from a
specific commit ref.  NOT a GitHub App — no installation-token dance needed.

Designed to be injected into ``EvalChannelAdapter`` so the review agent's
``read_file`` / ``grep_repo`` tool calls hit the real GitHub API rather than
an empty in-memory file map.  One instance per eval sample; create it from
``pr_url`` + ``base_sha`` metadata and pass to ``run_review_sample``.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_HTTP_TIMEOUT = 10.0


@dataclass
class GitHubFileReader:
    """Read files from a GitHub repo at a specific ref.

    Args:
        repo:  GitHub ``"owner/name"`` (e.g. ``"calcom/cal.com"``).
        ref:   Commit SHA or branch name to read files at.
        token: GitHub PAT.  Defaults to ``GITHUB_TOKEN`` env var.
               When empty, all methods return empty results silently.
    """

    repo: str
    ref: str
    token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def read_file(self, path: str) -> str:
        """Return UTF-8 content of ``path`` at ``self.ref``, or ``""`` if absent.

        Returns ``""`` for 404 (file not in repo) and for non-UTF-8 binary files.
        Raises on other HTTP errors (auth failures, server errors).
        """
        if not self.token:
            _logger.warning("GitHubFileReader.read_file: GITHUB_TOKEN unset; returning empty")
            return ""
        url = f"{_API_BASE}/repos/{self.repo}/contents/{path}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(url, params={"ref": self.ref}, headers=self._headers())
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            _logger.warning(
                "github_file_reader_json_error",
                extra={"repo": self.repo, "path": path},
            )
            return ""
        encoded = payload.get("content", "")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            _logger.info(
                "github_file_reader_not_utf8",
                extra={"repo": self.repo, "path": path},
            )
            return ""

    async def grep_repo(
        self,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Search ``self.repo`` for ``pattern``. Returns ``"path: fragment"`` lines.

        Uses GitHub Code Search.  Returns ``[]`` on 404/422 (unindexed repo,
        bad query) — callers should treat an empty list as "no matches found",
        not as "repo absent".  Raises on other HTTP errors.
        """
        if not self.token:
            _logger.warning("GitHubFileReader.grep_repo: GITHUB_TOKEN unset; returning empty")
            return []
        q_parts = [pattern, f"repo:{self.repo}"]
        if path_glob:
            q_parts.append(f"path:{path_glob}")
        q = " ".join(q_parts)
        url = f"{_API_BASE}/search/code?q={quote(q, safe='')}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.text-match+json"},
            )
        if response.status_code in (404, 422):
            _logger.info(
                "github_file_reader_grep_no_results",
                extra={"repo": self.repo, "status": response.status_code},
            )
            return []
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            matches = item.get("text_matches")
            if not isinstance(path, str) or not isinstance(matches, list) or not matches:
                continue
            first = matches[0]
            fragment = (
                first.get("fragment", "").splitlines()[0]
                if isinstance(first, dict) and isinstance(first.get("fragment"), str)
                else ""
            )
            out.append(f"{path}: {fragment}".strip())
            if len(out) >= max_matches:
                break
        return out


__all__ = ["GitHubFileReader"]
