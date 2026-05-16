"""DeepAgents ``BaseSandbox`` implementation backed by a local Docker container.

Replaces the earlier Modal-cloud backend (Modal pricing was incompatible with
the eval cadence). Each eval sample spins up a private container, clones the
target repository at the pinned commit into ``/workspace``, and tears it down
when the solver returns. The container is fully ephemeral — auto-removed on
``aclose`` regardless of success or failure.

Why ``docker-py`` over ``aiodocker``: docker-py is the official Docker SDK
for Python (maintained by Docker, Inc.), so the API contract is stable and
matches the daemon's REST surface byte-for-byte. It's synchronous; we wrap
calls in :func:`asyncio.to_thread` so the deepagents async path stays
non-blocking. The added thread-hop is negligible next to the network/disk
work Docker is doing anyway.

This module never runs the official evaluation harnesses (SWE-bench /
SWT-bench Docker grids) — it only hosts the agent. Scoring is downstream
via the official harness fed our ``predictions.jsonl``.

Lifecycle::

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="astropy/astropy", base_commit="abcd…"),
    )
    try:
        agent = build_baseline_agent(..., backend=backend)
        result = await agent.ainvoke(...)
        patch = await backend.acapture_diff()
    finally:
        await backend.aclose()
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import shlex
import uuid
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from evals.sandboxes.repo_setup import (
    _SHA_FALLBACK_MARKER,
    DEFAULT_WORKSPACE,
    RepoSpec,
    capture_diff_script,
    repo_setup_script,
)

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from docker.models.containers import Container  # noqa: F401

logger = logging.getLogger(__name__)

# Default image. ``python:3.11-slim`` is small (~120 MB) and we apt-install
# ``git`` + ``ca-certificates`` on first use; alternative bases that ship git
# pre-built (e.g. ``buildpack-deps:bookworm-scm``) are ~500 MB and not worth
# the extra pull on a fresh machine. Pin to a digest in CI when reproducibility
# matters.
_DEFAULT_IMAGE = "python:3.11-slim"

# Defense-in-depth: even though ``image`` is solver-controlled (never sample-
# controlled), validate the tag against the upstream Docker reference grammar
# before passing it to the daemon. Catches accidental injection if a future
# caller wires sample metadata to the image arg (semgrep CWE-250).
# Pattern is a conservative subset of github.com/distribution/reference: only
# lowercase tags, no ``$`` / spaces / shell metacharacters.
_IMAGE_REF_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[a-zA-Z0-9._-]+)?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)


def _validate_image(image: str) -> str:
    """Return ``image`` if it matches the allowed reference grammar; else raise."""
    if not isinstance(image, str) or not _IMAGE_REF_RE.match(image):
        raise ValueError(
            f"Refusing to launch container with image {image!r} — does not "
            f"match the allowed Docker reference grammar."
        )
    return image


# Apt step that runs once per container immediately after start. The base
# image doesn't ship git; this adds ~10s on the first sample of the session
# and is essentially free thereafter thanks to apt's package cache.
_INSTALL_GIT_SCRIPT = (
    "set -euo pipefail; "
    "if ! command -v git >/dev/null; then "
    "  apt-get update -qq && apt-get install -y --no-install-recommends "
    "    git ca-certificates >/dev/null; "
    "fi"
)

# Per-command timeout. Mirrors the previous Modal default + matches Inspect's
# bash-session minimum.
_DEFAULT_RUN_TIMEOUT_S = 600


def _docker() -> Any:
    """Import :mod:`docker` lazily and surface a clear error if it's missing.

    We don't want a missing optional dep to cascade through the eval package
    on dev machines that aren't running Docker.
    """
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError as cause:  # pragma: no cover — exercised on minimal envs
        raise RuntimeError(
            "docker-py is required for DockerSandboxBackend. "
            "Install via `uv add docker` and make sure the Docker daemon is "
            "running (Docker Desktop, Colima, or any docker-engine).\n"
            "If you use Colima or a non-default socket, set "
            "DOCKER_HOST=unix://$HOME/.colima/default/docker.sock (or your "
            "active context's endpoint — see `docker context ls`)."
        ) from cause
    return docker


class DockerSandboxBackend(BaseSandbox):
    """DeepAgents-compatible sandbox backed by a single Docker container.

    Construction is async (image pull + container start hit the daemon over
    a Unix socket); call :meth:`create_for_sample` rather than the
    constructor directly. The container is owned for the lifetime of one
    eval sample and torn down via :meth:`aclose`.

    Threading model: deepagents calls into the backend through both
    synchronous helpers (``read`` / ``write`` / …) and async ones
    (``aread`` / …). All Docker traffic goes through ``asyncio.to_thread``,
    so neither path blocks the event loop.
    """

    def __init__(
        self,
        *,
        container: Any,
        workspace: str = DEFAULT_WORKSPACE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> None:
        self._container = container
        self._workspace = workspace
        self._run_timeout = run_timeout
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._closed = False
        # Set by ``create_for_sample`` when the requested SHA wasn't reachable
        # and we fell back to the default branch HEAD.
        self._used_sha_fallback: bool = False

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    async def create_bare(
        cls,
        *,
        workspace: str = DEFAULT_WORKSPACE,
        image: str = _DEFAULT_IMAGE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> DockerSandboxBackend:
        """Spin up an empty container with ``workspace`` pre-created.

        Used by tasks that don't need a pre-cloned repo (synthetic review
        samples, prompt-injection corpus). Caller is responsible for
        closing the backend via :meth:`aclose`.
        """
        backend = await cls._start_container(image=image, run_timeout=run_timeout)
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
        image: str = _DEFAULT_IMAGE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
    ) -> DockerSandboxBackend:
        """Spin up a container, clone the repo at ``repo_spec.base_commit``.

        Args:
            repo_spec: Identity of the repo checkout for this sample.
            image: Docker image tag. Defaults to ``python:3.11-slim``;
                ``git`` is apt-installed on first command. Override with a
                pre-baked image to skip the install latency.
            run_timeout: Default timeout for individual shell-run calls.

        Raises:
            RuntimeError: If git install or repo clone fails. We don't fall
                back silently — a missing base commit means the sample is
                unrunnable and should be flagged as errored.
        """
        backend = await cls._start_container(image=image, run_timeout=run_timeout)
        backend._workspace = repo_spec.workspace

        # Install git first (image-cached on subsequent samples).
        resp = await backend.aexecute(_INSTALL_GIT_SCRIPT, timeout=300)
        if resp.exit_code != 0:
            await backend.aclose()
            raise RuntimeError(f"Failed to install git in container:\n{resp.output}")

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

    # ── container start (shared between factories) ─────────────────────────

    @classmethod
    async def _start_container(
        cls,
        *,
        image: str,
        run_timeout: int,
    ) -> DockerSandboxBackend:
        """Pull (if missing) + start a fresh container with PID-1 sleep."""

        # Validate up-front so the error surfaces before we hit the daemon.
        validated_image = _validate_image(image)

        def _do_start() -> Any:
            docker = _docker()
            client = docker.from_env()
            # ``images.get`` is a cheap local lookup; ``pull`` only fires on
            # first use of an image. We don't reuse running containers across
            # samples — clean slate per sample is the contract.
            try:
                client.images.get(validated_image)
            except docker.errors.ImageNotFound:
                logger.info("pulling docker image %s …", validated_image)
                client.images.pull(validated_image)
            # ``validated_image`` is checked by ``_validate_image`` above —
            # only matches the Docker reference grammar (lowercase tag, no
            # shell metacharacters, no ``$``/quoting). Callers never pass
            # sample-controlled data here; the arg is solver-configured.
            run_kwargs = {
                "image": validated_image,
                "command": ["sleep", "infinity"],
                "detach": True,
                "tty": True,
                # we delete in aclose; auto_remove races with running shells
                "auto_remove": False,
                "name": f"openbot-agent-{uuid.uuid4().hex[:12]}",
                "network_mode": "bridge",
            }
            return client.containers.run(**run_kwargs)  # nosemgrep

        container = await asyncio.to_thread(_do_start)
        return cls(container=container, run_timeout=run_timeout)

    # ── identity ───────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        # ``Container.id`` is the long SHA; short form is friendlier in
        # LangSmith / Inspect traces.
        cid = getattr(self._container, "id", "") or ""
        return cid[:12] if cid else "docker-sandbox"

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def used_sha_fallback(self) -> bool:
        """True iff repo setup landed on default-branch HEAD, not the SHA.

        Indicates the requested ``base_commit`` was unreachable (e.g.
        squash-merged PR base now GC'd from main). The agent still has a
        readable repo at ``/workspace``, but file contents reflect a
        slightly later commit than the diff.
        """
        return self._used_sha_fallback

    # ── core: async run ────────────────────────────────────────────────────

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Run ``bash -lc command`` inside the container.

        Mirrors the Modal backend semantics: we conditionally ``cd`` into
        ``workspace`` so user commands without an explicit ``cd`` start at
        the repo root, but the conditional lets the initial repo-setup
        script (run *before* ``workspace`` exists) succeed.
        """
        if self._closed:
            raise RuntimeError("DockerSandboxBackend already closed")
        _ = timeout if timeout is not None else self._run_timeout
        ws_q = shlex.quote(self._workspace)
        prefixed = f"if [ -d {ws_q} ]; then cd {ws_q}; fi\n{command}"

        def _do_run() -> tuple[int, str]:
            # docker-py's exec API is synchronous and blocks until the
            # command completes. Per-call timeouts require the lower-level
            # daemon API; we rely on container-level limits + the
            # surrounding solver's overall budget instead.
            result = self._container.exec_run(
                cmd=["bash", "-lc", prefixed],
                demux=True,
                tty=False,
            )
            exit_code = int(result.exit_code or 0)
            if isinstance(result.output, tuple):
                stdout_b, stderr_b = result.output
            else:
                stdout_b, stderr_b = result.output, None
            out = (stdout_b or b"").decode("utf-8", errors="replace")
            err = (stderr_b or b"").decode("utf-8", errors="replace") if stderr_b else ""
            combined = f"{out}\n{err}".strip() if err else out
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
                # Worker-thread path: submit back to the parent loop.
                return asyncio.run_coroutine_threadsafe(coro, self._loop).result()
            raise RuntimeError(
                "DockerSandboxBackend.execute() called from inside the parent "
                "event loop — would deadlock. Use aexecute() from async code."
            )
        return asyncio.run(coro)

    # ── upload / download via base64 ───────────────────────────────────────

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
        """Return the agent's edits as a unified diff vs the base commit.

        Output is exactly what ``predictions.jsonl`` wants in ``model_patch``:
        a single ``git diff`` chunk with no terminal newline normalisation.
        Empty string means the agent made no changes.
        """
        resp = await self.aexecute(capture_diff_script(self._workspace), timeout=120)
        if resp.exit_code != 0:
            raise RuntimeError(f"git diff failed inside Docker sandbox:\n{resp.output}")
        return resp.output

    # ── teardown ───────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _do_close() -> None:
            try:
                self._container.stop(timeout=2)
            except Exception:
                logger.warning("Container stop failed", exc_info=True)
            try:
                self._container.remove(force=True)
            except Exception:
                logger.warning("Container remove failed", exc_info=True)

        await asyncio.to_thread(_do_close)

    async def __aenter__(self) -> DockerSandboxBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
