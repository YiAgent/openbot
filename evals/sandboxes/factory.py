"""Config-driven factory for OpenBot eval sandboxes.

Eval solvers no longer hard-code a concrete backend. They call
:func:`create_sandbox_for_sample` / :func:`create_bare_sandbox` and the
factory picks the backend at runtime based on configuration:

* ``OPENBOT_SANDBOX_BACKEND`` environment variable
  (``daytona`` | ``modal`` | ``docker``), defaulting to ``daytona``.
* Explicit ``kind=`` kwarg for tests or one-off overrides.

The three concrete backends live in sibling modules and are imported lazily
so a missing optional dependency (``modal``, ``daytona-sdk``) only fails the
codepaths that actually want that backend — not module import.

The factory keeps backend-specific configuration in env vars (image, region,
language) so eval code stays portable; see each backend module for the
recognised env vars.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from deepagents.backends.protocol import ExecuteResponse

from evals.common import config
from evals.common.config import get_eval_config
from evals.sandboxes.repo_setup import DEFAULT_WORKSPACE, RepoSpec

SandboxKind = str

DOCKER: SandboxKind = "docker"
MODAL: SandboxKind = "modal"
DAYTONA: SandboxKind = "daytona"

# Re-exported for callers that want literal strings without reaching into
# ``evals.common.config``. The supported set lives in ``config`` so all
# backend knobs are visible from one place.
_SUPPORTED: tuple[SandboxKind, ...] = config.SANDBOX_BACKENDS_SUPPORTED


@runtime_checkable
class SandboxBackend(Protocol):
    """Structural type implemented by every eval sandbox backend.

    Every concrete backend (Docker, Modal, Daytona) exposes this surface; eval
    solvers depend only on this protocol so they never import a concrete
    class. Mirrors the subset of :class:`deepagents.backends.sandbox.BaseSandbox`
    actually used by eval code, plus repo-specific helpers.
    """

    @property
    def id(self) -> str: ...

    @property
    def workspace(self) -> str: ...

    @property
    def used_sha_fallback(self) -> bool: ...

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse: ...

    async def acapture_diff(self) -> str: ...

    async def aclose(self) -> None: ...


def get_default_sandbox_kind() -> SandboxKind:
    """Return the configured default backend.

    Reads the pydantic-validated value via :func:`get_eval_config`. The
    ``Literal["docker", "modal", "daytona"]`` constraint means an unknown
    string in the env fails loud at startup instead of silently routing
    to a wrong backend mid-eval.
    """
    return get_eval_config().sandbox.backend


def _resolve_kind(kind: SandboxKind | None) -> SandboxKind:
    chosen = (kind or get_default_sandbox_kind()).lower()
    if chosen not in _SUPPORTED:
        raise ValueError(f"Unsupported sandbox backend {chosen!r}; expected one of {_SUPPORTED}.")
    return chosen


async def create_sandbox_for_sample(
    *,
    repo_spec: RepoSpec,
    kind: SandboxKind | None = None,
    **backend_kwargs: Any,
) -> SandboxBackend:
    """Spin up a per-sample sandbox with the repo cloned at ``repo_spec``.

    Backend selection order: ``kind`` arg → ``OPENBOT_SANDBOX_BACKEND`` env →
    :data:`_DEFAULT_KIND`. Unknown extra kwargs are forwarded to the chosen
    backend's ``create_for_sample`` and ignored if the backend doesn't
    recognise them — keeping the call sites uniform.
    """
    chosen = _resolve_kind(kind)
    if chosen == DOCKER:
        from evals.sandboxes.docker_backend import DockerSandboxBackend

        return await DockerSandboxBackend.create_for_sample(repo_spec=repo_spec, **backend_kwargs)
    if chosen == MODAL:
        from evals.sandboxes.modal_backend import ModalSandboxBackend

        return await ModalSandboxBackend.create_for_sample(repo_spec=repo_spec, **backend_kwargs)
    from evals.sandboxes.daytona_backend import DaytonaSandboxBackend

    return await DaytonaSandboxBackend.create_for_sample(repo_spec=repo_spec, **backend_kwargs)


async def create_bare_sandbox(
    *,
    workspace: str = DEFAULT_WORKSPACE,
    kind: SandboxKind | None = None,
    **backend_kwargs: Any,
) -> SandboxBackend:
    """Spin up an empty sandbox without a repo clone.

    Used by review samples that have no repo identity (synthetic findings,
    prompt-injection corpus). The agent still gets a writeable ``workspace``
    inside the sandbox; ``acapture_diff`` will return an empty diff.
    """
    chosen = _resolve_kind(kind)
    if chosen == DOCKER:
        from evals.sandboxes.docker_backend import DockerSandboxBackend

        return await DockerSandboxBackend.create_bare(workspace=workspace, **backend_kwargs)
    if chosen == MODAL:
        from evals.sandboxes.modal_backend import ModalSandboxBackend

        return await ModalSandboxBackend.create_bare(workspace=workspace, **backend_kwargs)
    from evals.sandboxes.daytona_backend import DaytonaSandboxBackend

    return await DaytonaSandboxBackend.create_bare(workspace=workspace, **backend_kwargs)
