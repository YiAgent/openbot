"""Daytona implementation of the eval sandbox backend.

Same surface area as :mod:`evals.sandboxes.docker_backend` and
:mod:`evals.sandboxes.modal_backend`, but the per-sample sandbox lives on
Daytona's managed sandbox platform. Selected via
``OPENBOT_SANDBOX_BACKEND=daytona`` (the project default) so eval code
stays portable.

The ``daytona`` Python package is an optional dependency. Importing this
module is cheap; the SDK is only loaded when a Daytona sandbox is
actually created.

Recognised configuration env vars:

* ``DAYTONA_API_KEY`` — auth key for the Daytona API
* ``DAYTONA_API_URL`` — override the default Daytona endpoint
* ``DAYTONA_TARGET`` — region / target (e.g. ``us``, ``eu``)
* ``OPENBOT_DAYTONA_IMAGE`` — image for the per-sample sandbox
  (default ``python:3.11-slim`` for parity with the docker backend)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import shlex
import uuid
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from evals.common import config
from evals.common.config import get_eval_config
from evals.sandboxes.repo_setup import (
    _SHA_FALLBACK_MARKER,
    DEFAULT_WORKSPACE,
    RepoSpec,
    capture_diff_script,
    repo_setup_script,
)

logger = logging.getLogger(__name__)

# Shared docker/modal/daytona defaults — see
# :class:`evals.common.config.SandboxSettings`.
_DEFAULT_IMAGE = get_eval_config().sandbox.default_image
_DEFAULT_RUN_TIMEOUT_S = get_eval_config().sandbox.default_run_timeout_s

# Daytona's stock images don't always include git; we shell-install on
# first use so any base image works.
_INSTALL_GIT_SCRIPT = (
    "set -euo pipefail; "
    "if ! command -v git >/dev/null; then "
    "  apt-get update -qq && apt-get install -y --no-install-recommends "
    "    git ca-certificates >/dev/null 2>&1 || "
    "  (command -v apk >/dev/null && apk add --no-cache git >/dev/null 2>&1) || "
    "  echo 'WARNING: could not install git via apt or apk'; "
    "fi"
)


def _daytona_module() -> Any:
    """Import :mod:`daytona` lazily with a clear error if it's missing."""
    try:
        import daytona  # type: ignore[import-not-found]
    except ImportError as cause:
        raise RuntimeError(
            "daytona is required for DaytonaSandboxBackend. "
            "Install via `uv add daytona` and set DAYTONA_API_KEY."
        ) from cause
    return daytona


def _build_client() -> Any:
    """Construct a configured :class:`daytona.Daytona` client."""
    daytona_mod = _daytona_module()
    config_kwargs: dict[str, Any] = {}
    api_key = os.environ.get("DAYTONA_API_KEY")
    api_url = os.environ.get("DAYTONA_API_URL")
    target = os.environ.get("DAYTONA_TARGET")
    if api_key:
        config_kwargs["api_key"] = api_key
    if api_url:
        config_kwargs["api_url"] = api_url
    if target:
        config_kwargs["target"] = target
    config = daytona_mod.DaytonaConfig(**config_kwargs) if config_kwargs else None
    return daytona_mod.Daytona(config)


def _create_sandbox(client: Any, *, image: str, run_timeout: int) -> Any:
    """Create a Daytona sandbox from the given Docker image."""
    daytona_mod = _daytona_module()
    params = daytona_mod.CreateSandboxFromImageParams(image=image)
    # The ``timeout`` knob here is the *boot* timeout (build + start);
    # per-command timeouts are set on each ``process.exec`` call.
    return client.create(params, timeout=max(run_timeout, 600))


def _run_command(sandbox: Any, command: str, *, timeout: int) -> tuple[int, str]:
    """Run ``command`` in the sandbox; return (exit_code, combined output)."""
    response = sandbox.process.exec(command, timeout=timeout)
    exit_code = response.exit_code
    if exit_code is None:
        extras = getattr(response, "additional_properties", None) or {}
        exit_code = int(extras.get("code") or 0)
    result = response.result or ""
    if not isinstance(result, str):
        result = str(result)
    return int(exit_code), result


def _remove_sandbox(client: Any, sandbox: Any) -> None:
    """Tear a Daytona sandbox down: stop first (best-effort), then delete.

    Order matters: some control-plane configurations refuse ``delete`` on a
    sandbox still in ``STARTED`` state. We stop first as a no-op cushion and
    then issue the authoritative ``delete``. The delete error is **not**
    swallowed — leaking sandboxes silently eats the account's disk quota
    (each per-sample sandbox holds a multi-hundred-MB repo clone), which
    cascades into a "30 GiB exceeded" reject on the next ``create_for_sample``
    call. Better to surface the failure now and fix the root cause than to
    discover it as instant-empty samples 50 runs from now.
    """
    sb_id = getattr(sandbox, "id", "unknown")
    try:
        sandbox.stop(timeout=10)
    except Exception:
        logger.debug("Daytona sandbox.stop(%s) failed before delete", sb_id, exc_info=True)
    try:
        client.delete(sandbox)
    except Exception:
        logger.warning(
            "Daytona sandbox %s teardown FAILED — will leak disk quota until "
            "manually deleted via `openbot.evals.sandboxes.daytona_backend."
            "purge_leaked_sandboxes()` or the Daytona dashboard.",
            sb_id,
            exc_info=True,
        )


def purge_leaked_sandboxes(*, max_pages: int = 5, dry_run: bool = False) -> list[str]:
    """Delete every sandbox the API lists for this account; return their ids.

    Operational tool, not part of the per-sample teardown path. Use from an
    ad-hoc script (or `python -m`) when a crashed eval run leaks sandboxes
    that eat the disk quota.

    Set ``dry_run=True`` to list without deleting. Iterates pages until the
    API runs out or ``max_pages`` is hit (safety net against pagination
    cycles on accounts with thousands of sandboxes).
    """
    client = _build_client()
    deleted: list[str] = []
    for page_num in range(1, max_pages + 1):
        page = client.list(page=page_num, limit=100)
        if not page.items:
            break
        for sandbox in page.items:
            sb_id = getattr(sandbox, "id", "unknown")
            if dry_run:
                logger.info("[dry-run] would delete %s (state=%s)", sb_id, sandbox.state)
                deleted.append(sb_id)
                continue
            try:
                sandbox.stop(timeout=10)
            except Exception:
                logger.debug("stop %s failed", sb_id, exc_info=True)
            try:
                client.delete(sandbox)
                deleted.append(sb_id)
                logger.info("deleted %s", sb_id)
            except Exception:
                logger.warning("delete %s failed", sb_id, exc_info=True)
    return deleted


class DaytonaSandboxBackend(BaseSandbox):
    """DeepAgents-compatible sandbox backed by one Daytona sandbox per sample."""

    def __init__(
        self,
        *,
        client: Any,
        sandbox_handle: Any,
        workspace: str = DEFAULT_WORKSPACE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._sandbox_handle = sandbox_handle
        self._workspace = workspace
        self._run_timeout = run_timeout
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._closed = False
        self._used_sha_fallback: bool = False
        self._id = (
            getattr(sandbox_handle, "id", None)
            or getattr(sandbox_handle, "name", None)
            or f"daytona-{uuid.uuid4().hex[:12]}"
        )

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    async def create_bare(
        cls,
        *,
        workspace: str = DEFAULT_WORKSPACE,
        image: str | None = None,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> DaytonaSandboxBackend:
        backend = await cls._start_sandbox(image=image, run_timeout=run_timeout)
        backend._workspace = workspace
        resp = await backend.aexecute(
            f"{_INSTALL_GIT_SCRIPT}; mkdir -p {shlex.quote(workspace)}",
            timeout=300,
        )
        if resp.exit_code != 0:
            await backend.aclose()
            raise RuntimeError(f"Failed to prepare bare workspace {workspace}:\n{resp.output}")
        return backend

    @classmethod
    async def create_for_sample(
        cls,
        *,
        repo_spec: RepoSpec,
        image: str | None = None,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> DaytonaSandboxBackend:
        backend = await cls._start_sandbox(image=image, run_timeout=run_timeout)
        backend._workspace = repo_spec.workspace

        resp = await backend.aexecute(_INSTALL_GIT_SCRIPT, timeout=300)
        if resp.exit_code != 0:
            await backend.aclose()
            raise RuntimeError(f"Failed to install git in Daytona sandbox:\n{resp.output}")

        setup = repo_setup_script(repo_spec)
        resp = await backend.aexecute(setup, timeout=600)
        if resp.exit_code != 0:
            await backend.aclose()
            raise RuntimeError(
                f"Failed to clone {repo_spec.repo}@{repo_spec.base_commit} into "
                f"{repo_spec.workspace}:\n{resp.output}"
            )
        backend._used_sha_fallback = _SHA_FALLBACK_MARKER in resp.output
        return backend

    # ── sandbox start ──────────────────────────────────────────────────────

    @classmethod
    async def _start_sandbox(
        cls,
        *,
        image: str | None,
        run_timeout: int,
    ) -> DaytonaSandboxBackend:
        chosen_image = image or os.environ.get(config.DAYTONA_IMAGE_ENV) or _DEFAULT_IMAGE

        def _do_start() -> tuple[Any, Any]:
            client = _build_client()
            sandbox = _create_sandbox(client, image=chosen_image, run_timeout=run_timeout)
            return client, sandbox

        client, sandbox_handle = await asyncio.to_thread(_do_start)
        return cls(
            client=client,
            sandbox_handle=sandbox_handle,
            run_timeout=run_timeout,
        )

    # ── identity ───────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return str(self._id)[:32]

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def used_sha_fallback(self) -> bool:
        return self._used_sha_fallback

    # ── core: async run ────────────────────────────────────────────────────

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if self._closed:
            raise RuntimeError("DaytonaSandboxBackend already closed")
        effective_timeout = timeout if timeout is not None else self._run_timeout
        ws_q = shlex.quote(self._workspace)
        prefixed = f"if [ -d {ws_q} ]; then cd {ws_q}; fi\n{command}"

        def _do_run() -> tuple[int, str]:
            return _run_command(self._sandbox_handle, prefixed, timeout=effective_timeout)

        exit_code, combined = await asyncio.to_thread(_do_run)
        return ExecuteResponse(output=combined, exit_code=exit_code, truncated=False)

    # ── sync bridge ────────────────────────────────────────────────────────

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        coro = self.aexecute(command, timeout=timeout)
        if self._loop is not None and self._loop.is_running():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run_coroutine_threadsafe(coro, self._loop).result()
            raise RuntimeError(
                "DaytonaSandboxBackend.execute() called from inside the parent "
                "event loop — would deadlock. Use aexecute() from async code."
            )
        return asyncio.run(coro)

    # ── upload / download via base64 over exec ────────────────────────────

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse] = []
        for path, content in files:
            b64 = base64.b64encode(content).decode("ascii")
            cmd = (
                f"set -e; mkdir -p $(dirname {shlex.quote(path)}); "
                f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(path)}"
            )
            resp = self.execute(cmd)
            if resp.exit_code != 0:
                results.append(
                    FileUploadResponse(path=path, error=resp.output.strip() or "write failed")
                )
            else:
                results.append(FileUploadResponse(path=path))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            cmd = shlex.join(["base64", "-w0", path])
            resp = self.execute(cmd)
            if resp.exit_code != 0:
                results.append(FileDownloadResponse(path=path, error="file_not_found"))
                continue
            try:
                content = base64.b64decode(resp.output.strip(), validate=False)
            except (ValueError, binascii.Error):
                results.append(FileDownloadResponse(path=path, error="invalid_path"))
                continue
            results.append(FileDownloadResponse(path=path, content=content))
        return results

    # ── patch capture ──────────────────────────────────────────────────────

    async def acapture_diff(self) -> str:
        resp = await self.aexecute(capture_diff_script(self._workspace), timeout=120)
        if resp.exit_code != 0:
            raise RuntimeError(f"git diff failed inside Daytona sandbox:\n{resp.output}")
        return resp.output

    # ── teardown ───────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _do_close() -> None:
            _remove_sandbox(self._client, self._sandbox_handle)

        await asyncio.to_thread(_do_close)

    async def __aenter__(self) -> DaytonaSandboxBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
