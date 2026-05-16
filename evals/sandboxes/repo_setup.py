"""SWE-bench-style repo bootstrap for the Modal agent sandbox.

The agent runs inside a Modal sandbox where the target repository has been
cloned at a pinned commit into ``/workspace``. Each eval sample supplies a
:class:`RepoSpec` (``repo`` slug + ``base_commit``); :func:`repo_setup_script`
turns that into a self-contained bash script the Modal sandbox runs once on
startup.

Why a script rather than Python orchestration: Modal's ``Sandbox.exec`` runs
shell commands and we want the whole clone-and-checkout flow inside the
sandbox's network namespace anyway (avoids local clones + uploads). The
script uses ``set -e`` so a failed clone aborts startup loudly.

Patch artefacts (``model_patch`` for SWE-bench, ``test_patch`` for SWT-bench)
are produced by capturing ``git diff`` against the base commit *after* the
agent has finished editing — see ``capture_diff`` below.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

# Mirrors SWE-bench's ``/testbed`` convention by intent — the agent receives
# this path through the system prompt, so changing it has cross-task impact.
DEFAULT_WORKSPACE = "/workspace"


@dataclass(frozen=True)
class RepoSpec:
    """Identity of a repository checkout inside the agent sandbox.

    ``repo``: GitHub ``owner/name`` slug (e.g. ``"astropy/astropy"``).
    ``base_commit``: SHA-1 to check out before the agent starts editing.
    ``workspace``: absolute path inside the sandbox where the repo lives.
    """

    repo: str
    base_commit: str
    workspace: str = DEFAULT_WORKSPACE


_SHA_FALLBACK_MARKER = "OPENBOT_REPO_SETUP_FALLBACK"


def repo_setup_script(spec: RepoSpec) -> str:
    """Return a bash script that prepares ``spec.repo`` at ``spec.base_commit``.

    Tiered strategy (must work for both SWE-bench-style reachable SHAs and
    review-eval ``upstream_commit`` values that may be GC'd from main):

      1. Init + add ``origin`` remote (no clone yet — we don't know which
         path will succeed).
      2. ``git fetch --depth=1 origin <sha>`` — GitHub allows fetching any
         reachable SHA via ``uploadpack.allowReachableSHA1InWant`` (the
         github.com default), even when the SHA isn't a branch tip.
      3. If that fails, drop ``--depth=1`` and try again (some older SHAs
         need history).
      4. If the SHA is fully unreachable (e.g. squash-merged PR, stale
         dataset snapshot), shallow-fetch the default branch HEAD instead
         and print :data:`_SHA_FALLBACK_MARKER`. The agent still gets a
         readable repo for chasing callers / type defs / tests — the
         exact commit is approximate.

    Either way, the final state is a detached HEAD on ``openbot-agent`` so
    ``git diff`` later remains well-defined.

    Shell-injection note: ``repo`` / ``base_commit`` come from curated
    datasets, but we still quote via :func:`shlex.quote`.
    """
    repo_url = f"https://github.com/{spec.repo}.git"
    ws_q = shlex.quote(spec.workspace)
    url_q = shlex.quote(repo_url)
    sha_q = shlex.quote(spec.base_commit)
    return "\n".join(
        [
            "set -euo pipefail",
            f"rm -rf {ws_q}",
            f"mkdir -p {ws_q}",
            f"cd {ws_q}",
            "git init -q",
            f"git remote add origin {url_q}",
            # Tier 1+2: shallow then full SHA fetch. Tier 3: HEAD fallback.
            "fetched_target=''",
            f"if git fetch --depth=1 origin {sha_q} 2>/dev/null; then",
            f"  fetched_target={sha_q}",
            f"elif git fetch origin {sha_q} 2>/dev/null; then",
            f"  fetched_target={sha_q}",
            "else",
            f'  echo "{_SHA_FALLBACK_MARKER}: SHA {spec.base_commit} unreachable, falling back to default branch HEAD" >&2',
            "  git fetch --depth=1 origin HEAD",
            "  fetched_target=FETCH_HEAD",
            "fi",
            'git checkout --detach "$fetched_target"',
            "git switch -c openbot-agent",
            "git config user.email openbot@example.invalid",
            "git config user.name openbot-agent",
        ]
    )


def capture_diff_script(workspace: str = DEFAULT_WORKSPACE) -> str:
    """Bash one-liner that prints the agent's diff vs the base commit.

    Runs ``git add -N`` to mark new untracked files so they show up in
    ``git diff`` (otherwise SWE-bench-style patches miss new test files —
    the common SWT-Bench failure mode). The output is the raw unified
    diff, ready to drop into a ``predictions.jsonl`` ``model_patch`` field.
    """
    ws_q = shlex.quote(workspace)
    return f"cd {ws_q} && git add -N . >/dev/null 2>&1 || true; git diff --no-color HEAD"
