"""Docker-backed sandbox layer for deepagents — completely separate from
the Inspect AI evaluation/scoring sandboxes.

Two responsibilities live here:

- :mod:`evals.sandboxes.docker_backend` — a deepagents ``BaseSandbox``
  implementation that drives a local Docker container via docker-py.
  This is where the *agent* executes (read/edit/grep/glob/run-test).
- :mod:`evals.sandboxes.repo_setup` — SWE-bench-style ``git clone @ commit``
  bootstrap that pre-populates ``/workspace`` inside the container before
  the agent gets control.

The eval-side official Docker harness (SWE-bench / SWT-bench) is **not**
invoked from this package. Solvers here produce structured predictions
(see :mod:`evals.common.predictions`); the user is expected to feed those
predictions to the upstream evaluation harness offline.
"""

from evals.sandboxes.docker_backend import DockerSandboxBackend
from evals.sandboxes.repo_setup import RepoSpec, repo_setup_script

__all__ = ["DockerSandboxBackend", "RepoSpec", "repo_setup_script"]
