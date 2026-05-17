"""Modal-cloud implementation of the eval sandbox backend.

Mirrors :mod:`evals.sandboxes.docker_backend` but runs the per-sample
container on Modal's serverless infrastructure instead of a local Docker
daemon. The advantages — true parallelism, no local resource cost — only
pay off for cohort-sized eval runs; for a smoke run on a workstation the
docker backend is usually faster. Backend selection happens in
:mod:`evals.sandboxes.factory` so eval code doesn't care which one it gets.

The Modal Python SDK is an *optional* dependency: importing this module is
cheap, but :meth:`ModalSandboxBackend.create_for_sample` raises a clear
error if ``modal`` is missing or the user hasn't run ``modal token new``.
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

# All defaults flow through :class:`evals.common.config.SandboxSettings`
# so the docker / daytona / modal trio stays in sync. Locals kept for
# in-module readability — resolved at import time, since backends don't
# rotate images mid-run.
_DEFAULT_IMAGE = get_eval_config().sandbox.default_image
_DEFAULT_APP_NAME = get_eval_config().sandbox.modal_app
_DEFAULT_RUN_TIMEOUT_S = get_eval_config().sandbox.default_run_timeout_s

# Same install-git preamble the docker backend uses. Modal launches off the
# stock ``python:3.11-slim`` image so git isn't preinstalled; apt's package
# cache makes this cheap on repeat samples.
_INSTALL_GIT_SCRIPT = (
    "set -euo pipefail; "
    "if ! command -v git >/dev/null; then "
    "  apt-get update -qq && apt-get install -y --no-install-recommends "
    "    git ca-certificates >/dev/null; "
    "fi"
)


def _modal_module() -> Any:
    """Import :mod:`modal` lazily with a clear error if it's missing."""
    try:
        import modal  # type: ignore[import-not-found]
    except ImportError as cause:
        raise RuntimeError(
            "modal is required for ModalSandboxBackend. "
            "Install via `uv add modal` and run `modal token new` to "
            "authenticate. To switch to a different backend, set "
            "OPENBOT_SANDBOX_BACKEND=docker (or =daytona)."
        ) from cause
    return modal


class ModalSandboxBackend(BaseSandbox):
    """DeepAgents-compatible sandbox backed by one Modal sandbox per sample."""

    def __init__(
        self,
        *,
        sandbox: Any,
        app: Any,
        workspace: str = DEFAULT_WORKSPACE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> None:
        self._sandbox = sandbox
        self._app = app
        self._workspace = workspace
        self._run_timeout = run_timeout
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._closed = False
        self._used_sha_fallback: bool = False
        self._id = getattr(sandbox, "object_id", None) or f"modal-{uuid.uuid4().hex[:12]}"

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    async def create_bare(
        cls,
        *,
        workspace: str = DEFAULT_WORKSPACE,
        image: str | None = None,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
        app_name: str | None = None,
    ) -> ModalSandboxBackend:
        """Spin up an empty Modal sandbox with ``workspace`` pre-created."""
        backend = await cls._start_sandbox(
            image=image,
            run_timeout=run_timeout,
            app_name=app_name,
        )
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
        app_name: str | None = None,
    ) -> ModalSandboxBackend:
        """Spin up a Modal sandbox and clone the repo at ``repo_spec``."""
        backend = await cls._start_sandbox(
            image=image,
            run_timeout=run_timeout,
            app_name=app_name,
        )
        backend._workspace = repo_spec.workspace

        resp = await backend.aexecute(_INSTALL_GIT_SCRIPT, timeout=300)
        if resp.exit_code != 0:
            await backend.aclose()
            raise RuntimeError(f"Failed to install git in Modal sandbox:\n{resp.output}")

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
        app_name: str | None,
    ) -> ModalSandboxBackend:
        """Look up (or create) the Modal app and start a sandbox on it."""

        chosen_image = image or os.environ.get(config.MODAL_IMAGE_ENV) or _DEFAULT_IMAGE
        chosen_app = app_name or os.environ.get(config.MODAL_APP_ENV) or _DEFAULT_APP_NAME

        def _do_start() -> tuple[Any, Any]:
            modal = _modal_module()
            app = modal.App.lookup(chosen_app, create_if_missing=True)
            mimage = modal.Image.from_registry(chosen_image)
            sandbox = modal.Sandbox.create(
                "sleep",
                "infinity",
                image=mimage,
                app=app,
                timeout=max(run_timeout * 4, 3600),
            )
            return sandbox, app

        sandbox, app = await asyncio.to_thread(_do_start)
        return cls(
            sandbox=sandbox,
            app=app,
            run_timeout=run_timeout,
        )

    # ── identity ───────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self._id[:32]

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
            raise RuntimeError("ModalSandboxBackend already closed")
        _ = timeout if timeout is not None else self._run_timeout
        ws_q = shlex.quote(self._workspace)
        prefixed = f"if [ -d {ws_q} ]; then cd {ws_q}; fi\n{command}"

        def _do_run() -> tuple[int, str]:
            proc = self._sandbox.exec("bash", "-lc", prefixed)
            stdout = proc.stdout.read() or ""
            stderr = proc.stderr.read() or ""
            exit_code = int(proc.wait() or 0)
            combined = f"{stdout}\n{stderr}".strip() if stderr else stdout
            return exit_code, combined

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
                "ModalSandboxBackend.execute() called from inside the parent "
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
            raise RuntimeError(f"git diff failed inside Modal sandbox:\n{resp.output}")
        return resp.output

    # ── teardown ───────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _do_close() -> None:
            try:
                self._sandbox.terminate()
            except Exception:
                logger.warning("Modal sandbox terminate failed", exc_info=True)

        await asyncio.to_thread(_do_close)

    async def __aenter__(self) -> ModalSandboxBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
