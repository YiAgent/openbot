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
import atexit
import base64
import binascii
import contextlib
import logging
import os
import re
import shlex
import subprocess
import uuid
import weakref
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from evals.common.config import get_eval_config
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

# Default image, shared across docker / modal / daytona backends — see
# :class:`evals.common.config.SandboxSettings`. ``python:3.11-slim`` is
# small (~120 MB) and we apt-install ``git`` + ``ca-certificates`` on
# first use; alternative bases that ship git pre-built (e.g.
# ``buildpack-deps:bookworm-scm``) are ~500 MB and not worth the extra
# pull on a fresh machine. Pin to a digest in CI when reproducibility
# matters. Read at module-import time because backends never want
# mid-run image rotation; tests that flip backends use a fresh process.
_DEFAULT_IMAGE = get_eval_config().sandbox.default_image

# Defense-in-depth: even though ``image`` is solver-controlled (never sample-
# controlled), validate the tag against the upstream Docker reference grammar
# before passing it to the daemon. Catches accidental injection if a future
# caller wires sample metadata to the image arg (semgrep CWE-250).
#
# Component separator follows the spec at github.com/distribution/reference:
#   separator = ``_`` | ``.`` | ``__`` | one-or-more ``-``
# Real SWE-bench images use ``__`` (``astropy__astropy-12907``), which the
# previous single-char pattern silently rejected.
_IMAGE_SEPARATOR = r"(?:[._]|__|-+)"
_IMAGE_COMPONENT = rf"[a-z0-9]+(?:{_IMAGE_SEPARATOR}[a-z0-9]+)*"
_IMAGE_REF_RE = re.compile(
    rf"^{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
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

# Per-command timeout, shared across sandbox backends. Mirrors the
# previous Modal default + matches Inspect's bash-session minimum.
_DEFAULT_RUN_TIMEOUT_S = get_eval_config().sandbox.default_run_timeout_s

# Identifying label applied to every container we create. Used by:
#  * the startup orphan sweep (kills containers left over from a previous
#    crashed run before they pile up), and
#  * the per-process `atexit` hook (force-removes anything still alive when
#    the eval process exits, even on SIGTERM / pytest cancel).
# We never match on container *name* (the user could rename them) and we
# never touch containers without this label — that would risk hitting the
# user's own dev containers.
_LABEL_KEY = "openbot.eval.sandbox"
_LABEL_VALUE = "1"
_LABEL_SESSION_KEY = "openbot.eval.session"
# One session id per Python process. Lets us scope the `atexit` cleanup to
# *our* containers without nuking containers a sibling process is actively
# using (e.g. parallel `make smoke-fix` + `make smoke-test`).
_SESSION_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Container ids alive in this process. We use a set rather than a WeakSet of
# backends because the atexit hook needs to survive past Python object GC.
_LIVE_CONTAINER_IDS: set[str] = set()

# Image refs this process pulled (i.e. weren't in the local docker image cache
# at the time we asked for them). Only these are eligible for cleanup-on-close
# / atexit reaping — we never touch images the user had pre-cached, even if
# we ran a container on them.
_IMAGES_PULLED_BY_US: set[str] = set()


def _shell_rm_container(container_id: str) -> None:
    """Best-effort force-remove via the docker CLI.

    Called from ``atexit`` and ``weakref.finalize``, both of which may run
    after the asyncio loop has closed — so we cannot use docker-py's async
    paths or our own ``aexecute``. The CLI is the only thing guaranteed to
    work at interpreter shutdown. Stderr is discarded; the container might
    already be gone (race with normal aclose), which is fine.
    """
    # Docker daemon down, CLI missing, or container already cleaned — nothing
    # we can do at exit time. The orphan sweep on next startup will mop up.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )


def _shell_rmi_image(image: str) -> None:
    """Best-effort image removal via the docker CLI.

    Mirrors :func:`_shell_rm_container` — used from ``atexit`` where the
    docker-py client may be unusable. ``-f`` removes the image even if a
    stopped container still references it, but won't reach into a different
    image's layer tree. Failures (image already gone, in use by a running
    container we don't own, daemon down) are intentionally silent.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["docker", "rmi", "-f", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )


def _atexit_sweep() -> None:
    """Force-remove every container + pulled image left over at process exit.

    Runs unconditionally at interpreter shutdown — catches SIGTERM, uncaught
    exceptions, and `sys.exit()` paths that bypassed solver-level finally
    blocks. Safe to call repeatedly; ids that were already cleaned just no-op.

    Images are reaped *after* containers because docker refuses to remove an
    image while any container (even an exited one) references it. We never
    touch the shared default image, even if we pulled it — re-pulling
    ``python:3.11-slim`` between runs would waste ~120 MB of bandwidth on
    every cold start.
    """
    for cid in list(_LIVE_CONTAINER_IDS):
        _shell_rm_container(cid)
    _LIVE_CONTAINER_IDS.clear()
    for image in list(_IMAGES_PULLED_BY_US):
        if image == _DEFAULT_IMAGE:
            continue
        _shell_rmi_image(image)
    _IMAGES_PULLED_BY_US.clear()


atexit.register(_atexit_sweep)


def _sweep_orphans_from_prior_runs() -> None:
    """Force-remove containers labelled by *previous* eval processes.

    Runs once per process, lazily, the first time we start a container.
    Filters strictly on our label, so user-owned containers are safe. We
    explicitly skip containers tagged with the current session id, because
    a sibling eval process may have legitimately started those.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={_LABEL_KEY}={_LABEL_VALUE}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Docker daemon unreachable — defer; the real start_container call
        # will surface a clearer error.
        return
    if result.returncode != 0:
        return
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not ids:
        return
    # Inspect each candidate's session label and skip any that match the
    # current process — those belong to us and are still in use.
    keep_alive: set[str] = set()
    try:
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{.Id}} {{index .Config.Labels "' + _LABEL_SESSION_KEY + '"}}',
                *ids,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if inspect.returncode == 0:
            for line in inspect.stdout.splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) == 2 and parts[1] == _SESSION_ID:
                    keep_alive.add(parts[0])
    except (OSError, subprocess.SubprocessError):
        pass
    to_remove = [cid for cid in ids if cid not in keep_alive]
    if not to_remove:
        return
    logger.info(
        "removing %d orphan sandbox container(s) from prior eval runs",
        len(to_remove),
    )
    subprocess.run(
        ["docker", "rm", "-f", *to_remove],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )


_orphan_sweep_done = False


def _ensure_orphan_sweep() -> None:
    """Idempotent wrapper — sweep once per process."""
    global _orphan_sweep_done
    if _orphan_sweep_done:
        return
    _orphan_sweep_done = True
    _sweep_orphans_from_prior_runs()


def _shell_rm_container_and_forget(container_id: str) -> None:
    """Finalizer payload: remove container and drop it from the live set.

    Module-level so :func:`weakref.finalize` can hold the reference without
    keeping the backend instance alive (a bound method would defeat the
    point of finalize).
    """
    _LIVE_CONTAINER_IDS.discard(container_id)
    _shell_rm_container(container_id)


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
        image: str = _DEFAULT_IMAGE,
        cleanup_image_on_close: bool = False,
    ) -> None:
        self._container = container
        self._workspace = workspace
        self._run_timeout = run_timeout
        # Used by ``aclose`` to decide whether to ``docker rmi`` the image
        # after the container is gone. Never enabled for the shared default
        # base — that would force re-pull on every subsequent sample.
        self._image = image
        self._cleanup_image_on_close = (
            cleanup_image_on_close and image != _DEFAULT_IMAGE and image in _IMAGES_PULLED_BY_US
        )
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._closed = False
        # Set by ``create_for_sample`` when the requested SHA wasn't reachable
        # and we fell back to the default branch HEAD.
        self._used_sha_fallback: bool = False

        # Track this container in the process-global set and register a
        # weakref finalizer. Two layered safety nets:
        #   * if aclose() runs → finalizer cancels itself (clean path)
        #   * if backend is GC'd without aclose → finalizer force-removes
        #   * if process exits without GC → atexit sweeps _LIVE_CONTAINER_IDS
        cid = getattr(container, "id", None)
        if cid:
            _LIVE_CONTAINER_IDS.add(cid)
            self._finalizer: weakref.finalize | None = weakref.finalize(
                self, _shell_rm_container_and_forget, cid
            )
        else:
            self._finalizer = None

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    async def create_bare(
        cls,
        *,
        workspace: str = DEFAULT_WORKSPACE,
        image: str = _DEFAULT_IMAGE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
        cleanup_image_on_close: bool = True,
    ) -> DockerSandboxBackend:
        """Spin up an empty container with ``workspace`` pre-created.

        Used by tasks that don't need a pre-cloned repo (synthetic review
        samples, prompt-injection corpus). Caller is responsible for
        closing the backend via :meth:`aclose`.

        ``cleanup_image_on_close`` defaults to ``True``: if this process
        pulled a non-default image to start the container, the image is
        ``docker rmi``'d on close. The shared default image is always
        preserved (re-pulling it for every sample is wasteful).
        """
        backend = await cls._start_container(
            image=image,
            run_timeout=run_timeout,
            cleanup_image_on_close=cleanup_image_on_close,
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
        image: str = _DEFAULT_IMAGE,
        run_timeout: int = _DEFAULT_RUN_TIMEOUT_S,
        cleanup_image_on_close: bool = True,
    ) -> DockerSandboxBackend:
        """Spin up a container, clone the repo at ``repo_spec.base_commit``.

        Args:
            repo_spec: Identity of the repo checkout for this sample.
            image: Docker image tag. Defaults to ``python:3.11-slim``;
                ``git`` is apt-installed on first command. Override with a
                pre-baked image to skip the install latency.
            run_timeout: Default timeout for individual shell-run calls.
            cleanup_image_on_close: When ``True`` (default), ``docker rmi``
                the image on :meth:`aclose` *iff* this process pulled it
                and it isn't the shared default base. Per-sample images
                (e.g. ``ghcr.io/epoch-research/swe-bench.eval.…``) are
                multi-GB; without this cleanup, a 50-sample run leaves
                ~150 GB of unreferenced image layers on disk. The shared
                default base is never removed — re-pulling on every
                subsequent sample would burn more time and bandwidth than
                the disk savings are worth.

        Raises:
            RuntimeError: If git install or repo clone fails. We don't fall
                back silently — a missing base commit means the sample is
                unrunnable and should be flagged as errored.
        """
        backend = await cls._start_container(
            image=image,
            run_timeout=run_timeout,
            cleanup_image_on_close=cleanup_image_on_close,
        )
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
        cleanup_image_on_close: bool = False,
    ) -> DockerSandboxBackend:
        """Pull (if missing) + start a fresh container with PID-1 sleep."""

        # Validate up-front so the error surfaces before we hit the daemon.
        validated_image = _validate_image(image)
        # One-shot orphan cleanup before the first container of this process.
        # Cheap (single `docker ps` call) and idempotent.
        _ensure_orphan_sweep()

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
                # Mark *this* image as ours to clean up. If the image was
                # already locally cached, the user (or a previous tool)
                # paid for it — leave it alone.
                _IMAGES_PULLED_BY_US.add(validated_image)
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
                # Labels enable the orphan sweep and atexit cleanup to
                # safely identify our containers without touching anything
                # the user owns. ``session`` lets sibling eval processes
                # coexist (sweep skips other live sessions).
                "labels": {
                    _LABEL_KEY: _LABEL_VALUE,
                    _LABEL_SESSION_KEY: _SESSION_ID,
                },
            }
            return client.containers.run(**run_kwargs)  # nosemgrep

        container = await asyncio.to_thread(_do_start)
        return cls(
            container=container,
            run_timeout=run_timeout,
            image=validated_image,
            cleanup_image_on_close=cleanup_image_on_close,
        )

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
        cid = getattr(self._container, "id", None)

        def _do_close() -> None:
            try:
                # Slightly longer than before — 2s wasn't always enough for
                # in-flight pytest subprocesses to wind down; 5s still
                # caps total teardown well under the eval per-sample budget.
                self._container.stop(timeout=5)
            except Exception:
                logger.warning("Container stop failed", exc_info=True)
            try:
                # ``v=True`` also reaps any anonymous volumes the container
                # accumulated (apt cache, pip cache); otherwise dangling
                # volumes pile up on disk and `docker system df` grows.
                self._container.remove(force=True, v=True)
            except Exception:
                logger.warning("Container remove failed", exc_info=True)

        await asyncio.to_thread(_do_close)

        # Clean exit: drop tracking + cancel the finalize fallback so it
        # doesn't fire later trying to remove an already-gone container.
        if cid:
            _LIVE_CONTAINER_IDS.discard(cid)
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None

        # Image cleanup runs *after* the container is gone — docker refuses
        # to remove an image referenced by any container, even stopped ones.
        # ``force=False`` so a sibling backend in another asyncio task that
        # happens to share the same image (rare but legal) isn't disturbed;
        # we'll fall through to the atexit sweep instead.
        if self._cleanup_image_on_close:
            await asyncio.to_thread(self._maybe_remove_image)

    def _maybe_remove_image(self) -> None:
        """Best-effort ``docker rmi`` of the image this backend used.

        Called from :meth:`aclose` only when the image is eligible
        (non-default, pulled by us). Tolerates the "image in use" case
        without noise — the atexit sweep will retry on process exit.
        """
        try:
            docker = _docker()
            client = docker.from_env()
            client.images.remove(self._image, force=False, noprune=False)
            _IMAGES_PULLED_BY_US.discard(self._image)
            logger.info("removed docker image %s after sample", self._image)
        except Exception as exc:
            # ImageNotFound (already gone), APIError "image is being used by
            # stopped container <id>" (sibling sample), or daemon hiccup —
            # log at debug; force=True via the atexit shell rmi will mop up
            # if we're truly done with it.
            logger.debug("skip docker rmi %s: %s", self._image, exc, exc_info=False)

    async def __aenter__(self) -> DockerSandboxBackend:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()
