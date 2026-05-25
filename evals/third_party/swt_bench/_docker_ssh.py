"""SSH-aware ``docker.from_env()`` shim for remote daemons.

Background
----------
The SWT-bench harness (vendored under :mod:`evals.third_party.swt_bench`)
calls :func:`docker.from_env` to talk to a Docker daemon. When
``DOCKER_HOST`` is ``ssh://<host>``, the Python docker SDK falls back to
paramiko, which only knows how to read **decrypted** keys from
``~/.ssh/`` or from an ssh-agent — it does **not** read the macOS
Keychain, nor does it honour many ``~/.ssh/config`` directives.

On a typical macOS dev box the SSH passphrase lives in Keychain, the
key is encrypted at rest, and ``ssh-add -l`` is empty. ``ssh linux``
works because the macOS ``ssh`` binary integrates with Keychain;
paramiko inside the docker SDK does not, so it raises
``PasswordRequiredException``.

Fix
---
Pass ``use_ssh_client=True`` to :class:`docker.DockerClient` so the SDK
shells out to the system ``ssh`` subprocess instead of paramiko. The
subprocess uses the user's normal SSH stack — Keychain, ssh-agent,
``~/.ssh/config`` host aliases (``ssh://linux`` resolves to
``HostName 10.0.0.3, User wy``), ``ProxyJump``, certificates — none of
which paramiko replicates faithfully.

Usage
-----
Anywhere ``docker.from_env()`` may be called against a remote daemon::

    from evals.third_party.swt_bench._docker_ssh import apply_ssh_patch

    apply_ssh_patch()

This module deliberately lives next to the vendored harness as a
standalone shim, separate from backend modules that might instantiate
``EvalSettings()`` at import time via :func:`get_eval_config`. That
eager config load is fine for production callers but a footgun for the
vendored SWT-bench harness: importing the harness package would
trigger pydantic-settings to read every ``OPENBOT_*`` (and worse,
bare ``PREDICTIONS=…``) env var, which collides with Makefile
variable names. This module has no module-level side effects, so
importing from here stays cheap.

Idempotent. Only patches when ``DOCKER_HOST`` starts with ``ssh://``;
local ``unix://`` and ``tcp://`` setups pass through unchanged so this
is safe to call unconditionally.

For direct ``DockerClient`` construction (e.g. from our own scorers,
not the vendored harness), use :func:`make_client` instead — same
behaviour, more explicit.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import docker

logger = logging.getLogger(__name__)

_PATCHED = False


def _is_ssh_host(base_url: str | None) -> bool:
    return bool(base_url and base_url.startswith("ssh://"))


def apply_ssh_patch() -> None:
    """Monkey-patch :func:`docker.from_env` to set ``use_ssh_client=True``.

    Only takes effect when ``DOCKER_HOST`` is an ``ssh://`` URL — local
    daemons are untouched. Safe to call multiple times.
    """
    global _PATCHED
    if _PATCHED:
        return

    original_from_env = docker.from_env

    def patched_from_env(**kwargs: Any) -> docker.DockerClient:
        if _is_ssh_host(os.environ.get("DOCKER_HOST")):
            kwargs.setdefault("use_ssh_client", True)
            logger.debug(
                "docker.from_env(): DOCKER_HOST=%s, forcing use_ssh_client=True",
                os.environ.get("DOCKER_HOST"),
            )
        return original_from_env(**kwargs)

    docker.from_env = patched_from_env  # type: ignore[assignment]
    _PATCHED = True


def make_client(base_url: str | None = None) -> docker.DockerClient:
    """Construct a :class:`docker.DockerClient` honouring SSH host config.

    Prefer this over :func:`docker.from_env` in code we own — it makes
    the SSH path explicit and skips the monkey-patch dance.
    """
    url = base_url or os.environ.get("DOCKER_HOST") or ""
    if _is_ssh_host(url):
        return docker.DockerClient(base_url=url, use_ssh_client=True)
    return docker.from_env() if not base_url else docker.DockerClient(base_url=base_url)
