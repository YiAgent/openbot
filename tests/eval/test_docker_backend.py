"""Tests for the deepagents Docker sandbox backend.

docker-py is faked out with tiny stand-ins so unit tests stay hermetic — we
only care that :class:`DockerSandboxBackend` wires arguments correctly,
honours the workspace, surfaces failures, and tears the container down on
close.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.sandboxes import RepoSpec
from evals.sandboxes import docker_backend as docker_backend_mod
from evals.sandboxes.docker_backend import (
    _IMAGE_REF_RE,
    DockerSandboxBackend,
    _validate_image,
)
from evals.sandboxes.repo_setup import (
    capture_diff_script,
    repo_setup_script,
)

# ─── repo_setup unit tests ────────────────────────────────────────────────


def test_repo_setup_script_uses_tiered_sha_fetch_with_head_fallback() -> None:
    spec = RepoSpec(repo="astropy/astropy", base_commit="abc123", workspace="/workspace")
    script = repo_setup_script(spec)
    assert "git init" in script
    assert "git remote add origin https://github.com/astropy/astropy.git" in script
    # shlex.quote leaves alphanumeric SHAs unquoted; check the bare form.
    assert "git fetch --depth=1 origin abc123" in script
    assert "git fetch origin abc123" in script
    assert "git fetch --depth=1 origin HEAD" in script
    assert "OPENBOT_REPO_SETUP_FALLBACK" in script
    assert "/workspace" in script
    assert "set -euo pipefail" in script.splitlines()[0]


def test_capture_diff_script_includes_add_intent_to_add() -> None:
    script = capture_diff_script("/workspace")
    assert "git add -N" in script
    assert "git diff --no-color HEAD" in script
    assert "/workspace" in script


# ─── image-reference grammar ──────────────────────────────────────────────


def test_image_grammar_accepts_canonical_tags() -> None:
    for ok in (
        "python:3.11-slim",
        "ubuntu:24.04",
        "ghcr.io/owner/name:latest",
        "registry.example.com/team/app:1.2.3",
        "alpine:3.20@sha256:" + "0" * 64,
    ):
        assert _IMAGE_REF_RE.match(ok), ok


def test_image_grammar_rejects_shell_metacharacters() -> None:
    for bad in (
        "python:3.11; rm -rf /",
        "$(curl evil.com)",
        "python:3.11 --rm",
        "",
        "Python:UPPER",  # registry refs are lowercase
        "python:3.11`echo hi`",
    ):
        with pytest.raises(ValueError):
            _validate_image(bad)


# ─── DockerSandboxBackend with a tiny in-process fake of docker-py ───────


class _FakeExecResult:
    """Mimics :class:`docker.types.ExecResult`."""

    def __init__(self, exit_code: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.exit_code = exit_code
        # docker-py with demux=True returns (stdout, stderr) tuple.
        self.output: tuple[bytes, bytes | None] = (stdout, stderr if stderr else None)


class _FakeContainer:
    """Records every command sent through :meth:`exec_run` for assertions."""

    def __init__(self, *, scripted: list[tuple[int, str]] | None = None) -> None:
        self.commands: list[str] = []
        self.stopped = False
        self.removed = False
        self.id = "c0ffee1234567890abcdef"
        # FIFO of (exit_code, stdout) tuples consumed in order.
        self._scripted = list(scripted or [])

    def exec_run(self, *, cmd: list[str], demux: bool, tty: bool) -> _FakeExecResult:
        # ``cmd`` is ``["bash", "-lc", "<command>"]``.
        full = cmd[2] if len(cmd) >= 3 else ""
        self.commands.append(full)
        if self._scripted:
            rc, out = self._scripted.pop(0)
        else:
            rc, out = 0, ""
        return _FakeExecResult(rc, stdout=out.encode("utf-8"))

    def stop(self, *, timeout: int = 0) -> None:
        self.stopped = True

    def remove(self, *, force: bool = False, v: bool = False) -> None:
        self.removed = True
        self.volumes_removed = v


class _FakeImageStore:
    def __init__(self, *, missing: bool = False) -> None:
        self._missing = missing
        self.pull_calls: list[str] = []
        self.remove_calls: list[tuple[str, bool]] = []

    def get(self, name: str) -> object:
        if self._missing:
            from docker import errors  # type: ignore[import-not-found]

            raise errors.ImageNotFound(name)
        return object()

    def pull(self, name: str) -> object:
        self.pull_calls.append(name)
        return object()

    def remove(self, name: str, *, force: bool = False, noprune: bool = False) -> None:
        self.remove_calls.append((name, force))


class _FakeContainerStore:
    def __init__(self, *, scripted: list[tuple[int, str]] | None = None) -> None:
        self._scripted = scripted
        self.last: _FakeContainer | None = None

    def run(self, **kwargs: Any) -> _FakeContainer:
        self.last = _FakeContainer(scripted=self._scripted)
        return self.last


class _FakeClient:
    def __init__(self, *, image_missing: bool, scripted: list[tuple[int, str]] | None) -> None:
        self.images = _FakeImageStore(missing=image_missing)
        self.containers = _FakeContainerStore(scripted=scripted)


def _fake_docker(
    *,
    image_missing: bool = False,
    scripted: list[tuple[int, str]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    holder: dict[str, Any] = {"client": None}

    class _ModuleNS:
        @staticmethod
        def from_env() -> _FakeClient:
            # Cache the client so pull / remove calls made from different
            # code paths (start_container, aclose's _maybe_remove_image)
            # all see the same state.
            if holder["client"] is None:
                holder["client"] = _FakeClient(image_missing=image_missing, scripted=scripted)
            return holder["client"]

        class errors:
            # docker-py exposes errors.ImageNotFound — import the real
            # class so ``except docker.errors.ImageNotFound`` in our
            # backend's pull path resolves correctly.
            try:
                from docker.errors import ImageNotFound  # type: ignore[import-not-found]
            except ImportError:

                class ImageNotFound(Exception):  # type: ignore[no-redef]
                    pass

    return _ModuleNS, holder


@pytest.mark.asyncio
async def test_create_for_sample_clones_and_returns_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_docker, holder = _fake_docker()
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    spec = RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace")
    backend = await DockerSandboxBackend.create_for_sample(repo_spec=spec)

    container: _FakeContainer = holder["client"].containers.last
    assert container is not None
    # Setup script + apt-install both run; check the clone URL leaked through.
    assert any("x/y.git" in cmd for cmd in container.commands), container.commands
    assert backend.id == "c0ffee123456"
    await backend.aclose()
    assert container.stopped is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_create_for_sample_pulls_image_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_docker, holder = _fake_docker(image_missing=True)
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    spec = RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace")
    backend = await DockerSandboxBackend.create_for_sample(repo_spec=spec)

    client: _FakeClient = holder["client"]
    assert client.images.pull_calls == ["python:3.11-slim"]
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_for_sample_raises_when_clone_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First call = git install (rc=0). Second call = repo setup (rc=128).
    fake_docker, holder = _fake_docker(scripted=[(0, ""), (128, "fatal: bad sha")])
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    spec = RepoSpec(repo="x/y", base_commit="bad", workspace="/workspace")
    with pytest.raises(RuntimeError, match="Failed to clone"):
        await DockerSandboxBackend.create_for_sample(repo_spec=spec)
    assert holder["client"].containers.last.removed is True


@pytest.mark.asyncio
async def test_acapture_diff_returns_git_diff_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff_body = "diff --git a/foo b/foo\n+x\n"
    # apt-install (ok), repo setup (ok), capture diff (ok with body).
    fake_docker, _holder = _fake_docker(scripted=[(0, ""), (0, ""), (0, diff_body)])
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace"),
    )
    out = await backend.acapture_diff()
    assert out == diff_body
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_bare_makes_workspace_and_skips_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_docker, holder = _fake_docker()
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_bare(workspace="/workspace")
    container: _FakeContainer = holder["client"].containers.last
    # aexecute wraps the command as `cd <ws> && (<cmd>)`, so look for the
    # mkdir fragment inside the composed command line.
    assert any("mkdir -p" in cmd and "/workspace" in cmd for cmd in container.commands)
    assert not any("git clone" in cmd for cmd in container.commands)
    await backend.aclose()


@pytest.mark.asyncio
async def test_aexecute_after_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_docker, _holder = _fake_docker()
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)
    backend = await DockerSandboxBackend.create_bare()
    await backend.aclose()
    with pytest.raises(RuntimeError, match="already closed"):
        await backend.aexecute("echo hi")


# ─── image cleanup on close ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pulled_images() -> Any:
    """Isolate ``_IMAGES_PULLED_BY_US`` between tests.

    The set is module-global by design (atexit needs it to outlive any
    individual backend instance), so without this fixture a test that
    pulls an image bleeds eligibility into later tests.
    """
    docker_backend_mod._IMAGES_PULLED_BY_US.clear()
    yield
    docker_backend_mod._IMAGES_PULLED_BY_US.clear()


@pytest.mark.asyncio
async def test_aclose_leaves_default_shared_image_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``python:3.11-slim`` is shared across samples.

    Removing it after every sample would force a re-pull on the next one —
    the cleanup exists to reclaim per-sample-image disk waste, not to
    thrash the shared base.
    """
    fake_docker, holder = _fake_docker(image_missing=True)
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace"),
    )
    await backend.aclose()

    client: _FakeClient = holder["client"]
    assert client.images.pull_calls == ["python:3.11-slim"]
    assert client.images.remove_calls == [], "default shared base must not be removed on aclose"


@pytest.mark.asyncio
async def test_aclose_removes_per_sample_image_we_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-default image we pulled (~3 GB swe-bench eval image) is reaped.

    Mirrors the production pain point: a 50-sample fix run leaves ~150 GB
    of layer data on disk if every per-sample image lingers.
    """
    per_sample = "ghcr.io/epoch-research/swe-bench.eval.arm64.astropy__astropy-12907:latest"
    fake_docker, holder = _fake_docker(image_missing=True)
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace"),
        image=per_sample,
    )
    await backend.aclose()

    client: _FakeClient = holder["client"]
    assert client.images.pull_calls == [per_sample]
    assert client.images.remove_calls == [(per_sample, False)]
    assert per_sample not in docker_backend_mod._IMAGES_PULLED_BY_US


@pytest.mark.asyncio
async def test_aclose_skips_image_we_did_not_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the image was already in the local cache, leave it alone.

    The user (or some other tooling) paid for the pull and may depend on
    the image staying around. We only reap what we fetched ourselves.
    """
    per_sample = "ghcr.io/epoch-research/swe-bench.eval.arm64.astropy__astropy-12907:latest"
    fake_docker, holder = _fake_docker(image_missing=False)
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace"),
        image=per_sample,
    )
    await backend.aclose()

    client: _FakeClient = holder["client"]
    assert client.images.pull_calls == []
    assert client.images.remove_calls == []


@pytest.mark.asyncio
async def test_aclose_skips_image_when_cleanup_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``cleanup_image_on_close=False`` overrides the default."""
    per_sample = "registry.example.com/team/sample-img:abc"
    fake_docker, holder = _fake_docker(image_missing=True)
    monkeypatch.setattr(docker_backend_mod, "_docker", lambda: fake_docker)

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo="x/y", base_commit="sha", workspace="/workspace"),
        image=per_sample,
        cleanup_image_on_close=False,
    )
    await backend.aclose()

    client: _FakeClient = holder["client"]
    assert client.images.pull_calls == [per_sample]
    assert client.images.remove_calls == []
    # Image stays tracked so the atexit sweep can still reap it if the
    # process exits without anyone reusing the image.
    assert per_sample in docker_backend_mod._IMAGES_PULLED_BY_US
