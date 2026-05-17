"""GitHub App authentication — JWT + installation access tokens.

PRD §4.8 + §13 #5: each user builds their own App. Two-token flow:

  1. **App JWT** (~10 min, RS256-signed with the App's private key)
     identifies the App itself; only used to mint installation tokens.
  2. **Installation access token** (~1 h, returned from `/app/installations/.../access_tokens`)
     identifies the App *as a particular installation*. This is what gets
     attached to every repo-scoped API call (comment, label, etc.).

Tokens are cached in-process per installation and refreshed 5 min before
expiry to absorb clock skew. Never log them.

GitHub docs:
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx
import jwt

_API_BASE_DEFAULT: Final = "https://api.github.com"
_JWT_LIFETIME_SECONDS: Final = 9 * 60  # GitHub max is 10min; leave 60s headroom
_JWT_IAT_BACKDATE_SECONDS: Final = 60  # 60s clock-skew tolerance
_TOKEN_REFRESH_SLACK_SECONDS: Final = 5 * 60  # refresh 5min before expiry
_HTTP_TIMEOUT_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """Short-lived (~1h) token scoped to one App installation."""

    token: str
    expires_at: datetime  # tz-aware UTC
    installation_id: int

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now < self.expires_at - timedelta(seconds=_TOKEN_REFRESH_SLACK_SECONDS)


class GitHubAppAuth:
    """Mints and caches installation tokens.

    Construct once at startup with the App's id + private key bytes (PEM).
    All methods are coroutine-safe (single asyncio.Lock per instance).
    """

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: bytes,
        api_base: str = _API_BASE_DEFAULT,
        http: httpx.AsyncClient | None = None,
        user_agent: str = "OpenBot",
    ) -> None:
        if app_id <= 0:
            raise ValueError("app_id must be a positive integer")
        if not private_key_pem.startswith(b"-----BEGIN"):
            raise ValueError("private_key_pem does not look like a PEM file")

        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
        self._owns_http = http is None
        self._user_agent = user_agent
        self._cache: dict[int, InstallationToken] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_pem_file(
        cls, *, app_id: int, private_key_path: Path | str, **kwargs: object
    ) -> GitHubAppAuth:
        """Load the PEM from disk. PRD §7: setup.sh wizard writes the path into .env."""
        path = Path(private_key_path)
        pem = path.read_bytes()
        # type: ignore[arg-type] — kwargs flows through verbatim
        return cls(app_id=app_id, private_key_pem=pem, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_pem_or_path(
        cls,
        *,
        app_id: int,
        private_key_pem: bytes | str | None,
        private_key_path: Path | str | None,
        **kwargs: object,
    ) -> GitHubAppAuth:
        """Construct from inline PEM bytes/str, falling back to a filesystem path.

        Inline PEM wins when both are set — the Heroku / Render style where
        no persistent disk exists and the secret rides in an env var. Local
        dev keeps using the file path written by ``setup.sh`` (PRD §7).

        Raises ValueError when neither source is provided so callers don't
        accidentally instantiate an auth object that will 500 on first JWT.
        """
        if private_key_pem is not None:
            data = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
            return cls(app_id=app_id, private_key_pem=data, **kwargs)  # type: ignore[arg-type]
        if private_key_path is not None:
            return cls.from_pem_file(app_id=app_id, private_key_path=private_key_path, **kwargs)
        raise ValueError("either private_key_pem or private_key_path must be set")

    def app_jwt(self, *, now: int | None = None) -> str:
        """Mint a short-lived JWT identifying the App (not any installation).

        Returns a fresh JWT each call — token is cheap to mint and binding it
        to install-token requests keeps the cache logic simple.
        """
        now = now if now is not None else int(time.time())
        payload = {
            "iat": now - _JWT_IAT_BACKDATE_SECONDS,
            "exp": now + _JWT_LIFETIME_SECONDS,
            # PyJWT 2.10+ requires `iss` to be a string. GitHub accepts both,
            # so we always serialize as string for forward compatibility.
            "iss": str(self._app_id),
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> InstallationToken:
        """Return a fresh installation token, hitting cache when possible."""
        if installation_id <= 0:
            raise ValueError("installation_id must be a positive integer")

        cached = self._cache.get(installation_id)
        if cached and cached.is_fresh():
            return cached

        async with self._lock:
            # Re-check inside the lock — another coroutine may have just refreshed.
            cached = self._cache.get(installation_id)
            if cached and cached.is_fresh():
                return cached

            token = await self._mint(installation_id)
            self._cache[installation_id] = token
            return token

    async def _mint(self, installation_id: int) -> InstallationToken:
        url = f"{self._api_base}/app/installations/{installation_id}/access_tokens"
        response = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self._user_agent,
            },
        )
        response.raise_for_status()
        data = response.json()
        return InstallationToken(
            token=data["token"],
            expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")),
            installation_id=installation_id,
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP client if we own it."""
        if self._owns_http:
            await self._http.aclose()
