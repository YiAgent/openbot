"""Unit tests for openbot.evaluation.github_file_reader.GitHubFileReader.

All HTTP calls are intercepted via respx so no real network traffic is sent.
"""

from __future__ import annotations

import base64

import pytest
import respx
from httpx import Response

from openbot.evaluation.github_file_reader import GitHubFileReader

_REPO = "calcom/cal.com"
_REF = "ba9688a04a8398c9a8332ee7061bfae2f2efd524"
_API = "https://api.github.com"


def _encoded(content: str) -> str:
    """Base64-encode the way GitHub Contents API does (with newline padding)."""
    return base64.b64encode(content.encode()).decode()


# ── read_file ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_read_file_returns_decoded_content() -> None:
    """read_file decodes a base64 GitHub Contents API response."""
    path = "packages/lib/calendarClient.ts"
    respx.get(f"{_API}/repos/{_REPO}/contents/{path}").mock(
        return_value=Response(
            200,
            json={"content": _encoded("export const foo = 1;\n"), "encoding": "base64"},
        )
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    result = await reader.read_file(path)
    assert result == "export const foo = 1;\n"


@pytest.mark.asyncio
@respx.mock
async def test_read_file_sends_ref_as_query_param() -> None:
    """read_file passes ?ref=<sha> so the file is read at the base commit."""
    path = "src/utils.ts"
    captured: dict = {}

    def handler(request):
        captured["ref"] = request.url.params.get("ref")
        return Response(200, json={"content": _encoded("x"), "encoding": "base64"})

    respx.get(f"{_API}/repos/{_REPO}/contents/{path}").mock(side_effect=handler)
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    await reader.read_file(path)
    assert captured["ref"] == _REF


@pytest.mark.asyncio
@respx.mock
async def test_read_file_returns_empty_on_404() -> None:
    """read_file returns \"\" when GitHub returns 404 (file absent)."""
    path = "nonexistent.ts"
    respx.get(f"{_API}/repos/{_REPO}/contents/{path}").mock(
        return_value=Response(404, json={"message": "Not Found"})
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    result = await reader.read_file(path)
    assert result == ""


@pytest.mark.asyncio
async def test_read_file_returns_empty_without_token() -> None:
    """read_file short-circuits and returns \"\" when no GITHUB_TOKEN is set."""
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    result = await reader.read_file("any/path.ts")
    assert result == ""


# ── grep_repo ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_grep_repo_returns_path_fragment_hits() -> None:
    """grep_repo parses GitHub Code Search items into path: fragment lines."""
    respx.get(url__regex=rf"{_API}/search/code\?.*").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "path": "packages/trpc/server/routers/viewer/bookings.tsx",
                        "text_matches": [{"fragment": "  forEach(async booking => {"}],
                    },
                    {
                        "path": "packages/features/handleCancelBooking.ts",
                        "text_matches": [{"fragment": "  forEach(async (booking) => {"}],
                    },
                ]
            },
        )
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    hits = await reader.grep_repo(pattern="forEach")
    assert len(hits) == 2
    assert "bookings.tsx: " in hits[0]
    assert "handleCancelBooking.ts: " in hits[1]


@pytest.mark.asyncio
async def test_grep_repo_returns_empty_without_token() -> None:
    """grep_repo short-circuits and returns [] when no GITHUB_TOKEN is set."""
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    result = await reader.grep_repo(pattern="anything")
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_grep_repo_returns_empty_on_422() -> None:
    """grep_repo returns [] on 422 (unindexed repo / bad query) — non-fatal."""
    respx.get(url__regex=rf"{_API}/search/code\?.*").mock(
        return_value=Response(422, json={"message": "Validation Failed"})
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    result = await reader.grep_repo(pattern="foo")
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_grep_repo_respects_max_matches() -> None:
    """grep_repo honours the max_matches cap."""
    items = [{"path": f"file{i}.ts", "text_matches": [{"fragment": "match"}]} for i in range(10)]
    respx.get(url__regex=rf"{_API}/search/code\?.*").mock(
        return_value=Response(200, json={"items": items})
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="ghp_test")
    result = await reader.grep_repo(pattern="match", max_matches=3)
    assert len(result) == 3
