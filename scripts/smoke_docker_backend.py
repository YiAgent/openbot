"""End-to-end smoke test for :class:`DockerSandboxBackend`.

Runs a real Docker cycle:

  1. Spin up a container with the default image.
  2. Clone a tiny public repo at a pinned commit.
  3. Exercise the deepagents backend's ``aexecute`` / read paths.
  4. Edit a file in-place, capture the diff, assert it parses as a
     unified diff.
  5. Tear the container down.

Free (uses local Docker), unlike the previous Modal smoke. Invoke when
validating backend changes:

    uv run python scripts/smoke_docker_backend.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from evals.sandboxes import DockerSandboxBackend, RepoSpec

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smoke")

# Tiny public repo with a known good commit — picked for size (< 1 MiB) and
# stability. A heavier repo would be a better stress test but pushes the
# smoke run past ~30 s.
_REPO = "octocat/Hello-World"
_BASE = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"  # README at master tip


async def main() -> int:
    log.info("creating Docker sandbox for %s@%s …", _REPO, _BASE[:7])
    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo=_REPO, base_commit=_BASE),
    )
    log.info("container id = %s, workspace = %s", backend.id, backend.workspace)

    try:
        ls_out = await backend.aexecute("ls -1")
        log.info("ls (rc=%s):\n%s", ls_out.exit_code, ls_out.output.strip() or "<empty>")
        assert ls_out.exit_code == 0, ls_out.output

        cat_out = await backend.aexecute("cat README")
        log.info("README first line: %s", cat_out.output.splitlines()[:1])

        edit_out = await backend.aexecute("printf '%s\\n' '# openbot smoke marker' >> README")
        assert edit_out.exit_code == 0, edit_out.output

        diff = await backend.acapture_diff()
        log.info("captured diff (%d bytes):\n%s", len(diff), diff[:400])
        assert "openbot smoke marker" in diff, "expected marker missing from diff"
        assert diff.startswith("diff --git "), "diff doesn't look like unified diff"

        log.info("smoke run passed")
        return 0
    finally:
        log.info("removing container %s", backend.id)
        await backend.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
