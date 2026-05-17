"""Eval sandbox layer — pluggable backends behind a single factory.

Eval solvers obtain a sandbox via :func:`create_sandbox_for_sample` /
:func:`create_bare_sandbox`. Backend selection is config-driven (env var
``OPENBOT_SANDBOX_BACKEND`` ∈ {``daytona``, ``modal``, ``docker``},
default ``daytona``) so swapping the runtime never touches solver code.

This package never invokes the official SWE-bench / SWT-bench Docker
harnesses — solvers emit structured predictions
(:mod:`evals.common.predictions`) that the user feeds to the upstream
harness offline.

Modules:

* :mod:`evals.sandboxes.factory` — config-driven dispatch + shared
  ``SandboxBackend`` Protocol.
* :mod:`evals.sandboxes.docker_backend` — local Docker container backend.
* :mod:`evals.sandboxes.modal_backend` — Modal-cloud backend.
* :mod:`evals.sandboxes.daytona_backend` — Daytona workspace backend.
* :mod:`evals.sandboxes.repo_setup` — ``git clone @ commit`` bootstrap shared
  by every backend.
"""

from evals.sandboxes.docker_backend import DockerSandboxBackend
from evals.sandboxes.factory import (
    DAYTONA,
    DOCKER,
    MODAL,
    SandboxBackend,
    SandboxKind,
    create_bare_sandbox,
    create_sandbox_for_sample,
    get_default_sandbox_kind,
)
from evals.sandboxes.repo_setup import RepoSpec, repo_setup_script

__all__ = [
    "DAYTONA",
    "DOCKER",
    "MODAL",
    "DockerSandboxBackend",
    "RepoSpec",
    "SandboxBackend",
    "SandboxKind",
    "create_bare_sandbox",
    "create_sandbox_for_sample",
    "get_default_sandbox_kind",
    "repo_setup_script",
]
