"""Daytona implementation of SandboxPort (production fix loop).

Distinct from ``evals/sandboxes/daytona_backend.py`` (which implements
the deepagents BaseSandbox surface used by the eval harness). Both use
the same Daytona SDK; their port shapes differ on purpose — see
``openbot/application/ports/sandbox.py`` for the production contract.

The ``daytona`` Python package is an *optional* dependency: this module
imports the SDK lazily so an OpenBot deployment without a fix loop
(triage / review / chat only) doesn't need it installed.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from openbot.application.ports.sandbox import ExecResult, SandboxPort
from openbot.core.settings import Settings

_logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.11-slim"
_WORKSPACE = "/workspace/repo"
_INSTALL_GIT_SCRIPT = (
    "set -euo pipefail; "
    "if ! command -v git >/dev/null; then "
    "  (apt-get update -qq && apt-get install -y --no-install-recommends "
    "    git ca-certificates >/dev/null 2>&1) || "
    "  (command -v apk >/dev/null && apk add --no-cache git >/dev/null 2>&1) || "
    "  (echo 'WARNING: could not install git' >&2; exit 1); "
    "fi"
)


def _get_daytona_module() -> Any:
    """Import :mod:`daytona` lazily — production fix loop is opt-in."""
    try:
        import daytona  # type: ignore[import-not-found]
    except ImportError as cause:
        raise RuntimeError(
            "daytona SDK is required for the production fix-loop sandbox. "
            "Install via `uv add daytona` and set OPENBOT_DAYTONA_API_KEY."
        ) from cause
    return daytona


@lru_cache(maxsize=1)
def _build_client(api_key: str, server_url: str | None) -> Any:
    """One client per process — pooled connections, single auth handshake."""
    daytona_mod = _get_daytona_module()
    config_kwargs: dict[str, Any] = {"api_key": api_key}
    if server_url:
        config_kwargs["api_url"] = server_url
    config = daytona_mod.DaytonaConfig(**config_kwargs)
    return daytona_mod.Daytona(config)


def _inject_token(repo_url: str, token: str) -> str:
    """Build the x-access-token push URL. HTTPS-only by design.

    Refusing non-HTTPS prevents an SSH or file URL from silently bypassing
    the credential injection (and leaking a clone of a repo we don't own).
    """
    if not repo_url.startswith("https://"):
        raise ValueError(
            f"DaytonaSandboxAdapter.clone requires an https repo_url, got: {repo_url!r}"
        )
    # https://github.com/X/Y.git -> https://x-access-token:TOKEN@github.com/X/Y.git
    return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)


@dataclass
class DaytonaSandboxAdapter(SandboxPort):
    """Per-event production fix-loop sandbox.

    Use via the async-context-manager wrapper produced by the
    ``sandbox_factory`` DI hook (see C.8). Direct construction outside
    tests is discouraged.
    """

    _client: Any
    _sandbox: Any
    workspace: str = _WORKSPACE

    # ── lifecycle ──

    @classmethod
    async def create(cls, *, settings: Settings | None = None) -> DaytonaSandboxAdapter:
        """Provision a fresh Daytona sandbox and return the adapter."""
        s = settings if settings is not None else Settings()
        if s.daytona_api_key is None:
            raise RuntimeError(
                "OPENBOT_DAYTONA_API_KEY is unset — the production fix loop "
                "cannot create a sandbox. Configure via env var or .env."
            )
        client = _build_client(
            s.daytona_api_key.get_secret_value(),
            s.daytona_server_url,
        )
        daytona_mod = _get_daytona_module()
        params = daytona_mod.CreateSandboxFromImageParams(image=_DEFAULT_IMAGE)
        sandbox = await asyncio.to_thread(client.create, params)
        # Ensure git is present and the workspace exists.
        await asyncio.to_thread(sandbox.process.exec, _INSTALL_GIT_SCRIPT, 60)
        await asyncio.to_thread(sandbox.process.exec, f"mkdir -p {shlex.quote(_WORKSPACE)}", 10)
        return cls(_client=client, _sandbox=sandbox)

    @classmethod
    def _for_test(cls, *, client: Any) -> DaytonaSandboxAdapter:
        """Constructor used by unit tests — bypasses real create()."""
        sandbox = client.create()
        return cls(_client=client, _sandbox=sandbox)

    async def close(self) -> None:
        """Best-effort sandbox teardown — never raises."""
        sb_id = getattr(self._sandbox, "id", "unknown")
        try:
            await asyncio.to_thread(self._client.delete, self._sandbox)
        except Exception:
            _logger.warning(
                "daytona_sandbox_delete_failed",
                extra={"sandbox_id": sb_id},
                exc_info=True,
            )

    # ── git ──

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        url = _inject_token(repo_url, token)
        clone_cmd = (
            f"cd {shlex.quote(self.workspace)} && "
            f"git clone --depth=1 --branch={shlex.quote(ref)} "
            f"{shlex.quote(url)} ."
        )
        # 5 minute timeout — generous for repos up to a few hundred MB.
        response = await asyncio.to_thread(self._sandbox.process.exec, clone_cmd, 300)
        if response.exit_code not in (0, None):
            raise RuntimeError(
                f"daytona git clone failed (exit_code={response.exit_code}): {response.result!r}"
            )

    async def git_diff(self) -> str:
        diff_cmd = f"cd {shlex.quote(self.workspace)} && git diff"
        response = await asyncio.to_thread(self._sandbox.process.exec, diff_cmd, 30)
        return response.result or ""

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        # Re-derive the remote URL inside the sandbox so we don't need to
        # carry repo_url through the use case. `git remote get-url origin`
        # returns the token-bearing URL we cloned with — that token may be
        # expired by the time we push, so we rewrite it with the *fresh*
        # token before pushing.
        get_url = f"cd {shlex.quote(self.workspace)} && git remote get-url origin"
        url_resp = await asyncio.to_thread(self._sandbox.process.exec, get_url, 10)
        if url_resp.exit_code not in (0, None):
            raise RuntimeError(f"git remote get-url failed: {url_resp.result!r}")
        old_url = (url_resp.result or "").strip()
        if "@" in old_url and old_url.startswith("https://"):
            tail = old_url.split("@", 1)[1]
            new_url = f"https://x-access-token:{token}@{tail}"
        else:
            new_url = _inject_token(old_url, token)
        push_script = (
            f"cd {shlex.quote(self.workspace)} && "
            "git config user.email 'openbot[bot]@users.noreply.github.com' && "
            "git config user.name 'openbot[bot]' && "
            f"git checkout -b {shlex.quote(branch_ref)} && "
            "git add -A && "
            f"git commit -m {shlex.quote(message)} && "
            f"git push {shlex.quote(new_url)} HEAD:{shlex.quote(branch_ref)}"
        )
        push_resp = await asyncio.to_thread(self._sandbox.process.exec, push_script, 120)
        if push_resp.exit_code not in (0, None):
            raise RuntimeError(
                f"daytona push failed (exit_code={push_resp.exit_code}): {push_resp.result!r}"
            )

    # ── file IO ──

    async def read_file(self, path: str) -> str:
        full = f"{self.workspace}/{path}" if not path.startswith("/") else path
        try:
            data = await asyncio.to_thread(self._sandbox.files.download, full)
        except Exception:
            return ""
        if isinstance(data, bytes):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return ""
        return str(data or "")

    async def write_file(self, path: str, content: str) -> None:
        full = f"{self.workspace}/{path}" if not path.startswith("/") else path
        await asyncio.to_thread(self._sandbox.files.upload, full, content.encode("utf-8"))

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        target = (
            f"{self.workspace}/{path}"
            if not path.startswith("/") and path not in (".", "")
            else self.workspace
        )
        cmd = f"cd {shlex.quote(target)} && find . -type f | head -n {int(max)}"
        response = await asyncio.to_thread(self._sandbox.process.exec, cmd, 15)
        if response.exit_code not in (0, None):
            return []
        return [line for line in (response.result or "").splitlines() if line.strip()]

    # ── run ──

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        env_prefix = ""
        if env:
            env_prefix = (
                " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items()) + " "
            )
        # The argv list arrives shell-quoted, so the composed string is
        # injection-safe even when individual args contain spaces or quotes.
        joined = " ".join(shlex.quote(a) for a in command)
        cmd = f"cd {shlex.quote(self.workspace)} && {env_prefix}{joined}"
        response = await asyncio.to_thread(self._sandbox.process.exec, cmd, timeout_seconds)
        extras: dict[str, Any] = getattr(response, "additional_properties", {}) or {}
        exit_code = (
            response.exit_code if response.exit_code is not None else int(extras.get("code") or 0)
        )
        return ExecResult(
            stdout=str(response.result or ""),
            stderr="",
            exit_code=int(exit_code),
            timed_out=bool(extras.get("timed_out", False)),
        )


__all__ = ["DaytonaSandboxAdapter"]
