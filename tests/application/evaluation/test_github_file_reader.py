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


# ── GitHub App auth fallback ──────────────────────────────────────────────────

_APP_ID = "12345"
_INSTALLATION_ID = 99
_INSTALLATION_TOKEN = "ghs_app_installation_token"
_EXPIRES_AT = "2099-01-01T00:00:00Z"


def _app_installation_response():
    return Response(200, json={"id": _INSTALLATION_ID})


def _access_token_response():
    return Response(
        201,
        json={"token": _INSTALLATION_TOKEN, "expires_at": _EXPIRES_AT},
    )


@pytest.mark.asyncio
@respx.mock
async def test_read_file_uses_github_app_token_when_no_pat(monkeypatch) -> None:
    """When GITHUB_TOKEN is unset, read_file falls back to GitHub App auth."""
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", _APP_ID)
    monkeypatch.setenv(
        "OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM",
        _fake_pem(),
    )
    path = "src/foo.ts"
    # Respond to installation lookup + token mint + file fetch
    respx.get(f"{_API}/repos/{_REPO}/installation").mock(return_value=_app_installation_response())
    respx.post(f"{_API}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=_access_token_response()
    )
    respx.get(f"{_API}/repos/{_REPO}/contents/{path}").mock(
        return_value=Response(200, json={"content": _encoded("const x = 1;"), "encoding": "base64"})
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    result = await reader.read_file(path)
    assert result == "const x = 1;"


@pytest.mark.asyncio
@respx.mock
async def test_grep_repo_uses_github_app_token_when_no_pat(monkeypatch) -> None:
    """When GITHUB_TOKEN is unset, grep_repo falls back to GitHub App auth."""
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", _APP_ID)
    monkeypatch.setenv(
        "OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM",
        _fake_pem(),
    )
    respx.get(f"{_API}/repos/{_REPO}/installation").mock(return_value=_app_installation_response())
    respx.post(f"{_API}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=_access_token_response()
    )
    respx.get(url__regex=rf"{_API}/search/code\?.*").mock(
        return_value=Response(
            200, json={"items": [{"path": "a.ts", "text_matches": [{"fragment": "foo bar"}]}]}
        )
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    hits = await reader.grep_repo(pattern="foo")
    assert any("a.ts" in h for h in hits)


@pytest.mark.asyncio
async def test_read_file_returns_empty_when_no_credentials(monkeypatch) -> None:
    """Returns '' when neither GITHUB_TOKEN nor app creds are available."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENBOT_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM", raising=False)
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    result = await reader.read_file("any/path.ts")
    assert result == ""


@pytest.mark.asyncio
@respx.mock
async def test_app_token_is_reused_across_calls(monkeypatch) -> None:
    """GitHub App token minting happens once per GitHubFileReader instance."""
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", _APP_ID)
    monkeypatch.setenv("OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM", _fake_pem())
    installation_route = respx.get(f"{_API}/repos/{_REPO}/installation").mock(
        return_value=_app_installation_response()
    )
    respx.post(f"{_API}/app/installations/{_INSTALLATION_ID}/access_tokens").mock(
        return_value=_access_token_response()
    )
    respx.get(url__regex=rf"{_API}/repos/{_REPO}/contents/.*").mock(
        return_value=Response(200, json={"content": _encoded("x"), "encoding": "base64"})
    )
    reader = GitHubFileReader(repo=_REPO, ref=_REF, token="")
    # Two calls — installation lookup must only happen once
    await reader.read_file("a.ts")
    await reader.read_file("b.ts")
    assert installation_route.call_count == 1


# ── helper ────────────────────────────────────────────────────────────────────


def _fake_pem() -> str:
    """Return a minimal RSA private key PEM for testing (not real crypto)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
