"""Lightweight GitHub file reader for the eval harness.

Token resolution precedence:
  1. ``token`` constructor arg / ``GITHUB_TOKEN`` env var (PAT).
  2. GitHub App credentials: ``OPENBOT_GITHUB_APP_ID`` +
     ``OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM`` — mints an installation token for
     the target repo on first use and caches it for the instance lifetime.
  3. Empty string (no reads — all methods return empty results silently).

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
_GITHUB_API_VERSION = "2022-11-28"


async def _mint_installation_token(repo: str) -> str:
    """Mint a GitHub App installation token for ``repo``.

    Reads ``OPENBOT_GITHUB_APP_ID`` and ``OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM``
    from the environment.  Returns ``""`` when either is absent, or when the
    repo has no recorded installation for this App.
    """
    app_id_str = os.environ.get("OPENBOT_GITHUB_APP_ID", "")
    pem_str = os.environ.get("OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM", "")
    if not app_id_str or not pem_str:
        return ""
    try:
        from openbot.infrastructure.adapters.github_auth import GitHubAppAuth

        auth = GitHubAppAuth(
            app_id=int(app_id_str),
            private_key_pem=pem_str.encode() if isinstance(pem_str, str) else pem_str,
        )
        jwt_token = auth.app_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_API_BASE}/repos/{repo}/installation",
                headers=headers,
            )
        if resp.status_code != 200:
            _logger.info(
                "github_file_reader_no_installation",
                extra={"repo": repo, "status": resp.status_code},
            )
            return ""
        installation_id = resp.json().get("id")
        if not installation_id:
            return ""
        tok = await auth.installation_token(int(installation_id))
        return tok.token
    except Exception as exc:
        _logger.warning("github_file_reader_app_auth_failed: %s", exc)
        return ""


@dataclass
class GitHubFileReader:
    """Read files from a GitHub repo at a specific ref.

    Args:
        repo:  GitHub ``"owner/name"`` (e.g. ``"calcom/cal.com"``).
        ref:   Commit SHA or branch name to read files at.
        token: GitHub PAT.  Defaults to ``GITHUB_TOKEN`` env var.
               When empty, falls back to GitHub App auth (see module docstring).
    """

    repo: str
    ref: str
    token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    # Lazily resolved effective token (PAT wins; App token is fallback).
    _effective_token: str = field(init=False, default="", repr=False)
    _token_resolved: bool = field(init=False, default=False, repr=False)

    async def _ensure_token(self) -> str:
        """Return the effective bearer token, resolving once per instance."""
        if self._token_resolved:
            return self._effective_token
        self._token_resolved = True
        if self.token:
            self._effective_token = self.token
        else:
            self._effective_token = await _mint_installation_token(self.repo)
            if not self._effective_token:
                _logger.warning(
                    "GitHubFileReader: no credentials for repo=%r. "
                    "Set GITHUB_TOKEN in Doppler (a classic PAT with no "
                    "extra scopes reads any public repo at 5000 req/hr). "
                    "The OpenBot GitHub App only covers repos where it is "
                    "installed — external public benchmark repos like "
                    "calcom/cal.com require a PAT.",
                    self.repo,
                )
        return self._effective_token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }

    async def read_file(self, path: str) -> str:
        """Return UTF-8 content of ``path`` at ``self.ref``, or ``""`` if absent.

        Returns ``""`` for 404 (file not in repo) and for non-UTF-8 binary files.
        Raises on other HTTP errors (auth failures, server errors).
        """
        token = await self._ensure_token()
        if not token:
            _logger.warning("GitHubFileReader.read_file: no credentials available; returning empty")
            return ""
        # Strip leading slash — the agent may send "/packages/foo.ts" but the
        # Contents API path must be repo-relative without a leading slash.
        # A leading slash produces a double-slash URL that GitHub 301-redirects
        # to the numeric-id form; httpx does not follow redirects by default.
        clean_path = path.lstrip("/")
        url = f"{_API_BASE}/repos/{self.repo}/contents/{clean_path}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params={"ref": self.ref}, headers=self._headers(token))
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
        token = await self._ensure_token()
        if not token:
            _logger.warning("GitHubFileReader.grep_repo: no credentials available; returning empty")
            return []
        q_parts = [pattern, f"repo:{self.repo}"]
        if path_glob:
            q_parts.append(f"path:{path_glob}")
        q = " ".join(q_parts)
        url = f"{_API_BASE}/search/code?q={quote(q, safe='')}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    **self._headers(token),
                    "Accept": "application/vnd.github.text-match+json",
                },
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
