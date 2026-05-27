"""Chat eval solver — thin Inspect AI adapter calling the openbot.evaluation facade.

This solver calls ``openbot.evaluation.run_chat_sample`` which exercises the real
production chat responder.

File-fetching design (unified sandbox pattern):
  When the sample metadata contains ``repo`` and ``commit_id`` (SWE-QA-Pro shape),
  the solver passes ``sandbox_factory`` + ``clone_url`` + ``base_sha`` to
  ``run_chat_sample``.  The runner opens a Daytona sandbox, clones the repo at
  ``commit_id``, and wraps it in a ``SandboxFileReader`` injected into the adapter
  so the chat agent's ``read_file`` / ``grep_repo`` / ``list_files`` tool calls read
  from the local clone.

  Note: ``ChatProfile.sandbox_requirement = FORBIDDEN`` means the sandbox is NOT
  passed to the agent runtime.  It is used only as a file server behind the adapter.
  The agent calls adapter methods; it never interacts with the sandbox directly.

  When no sandbox is configured (DAYTONA_API_KEY not set) or the sample lacks
  ``commit_id``, the chat agent answers without file access (same as before).
"""

from __future__ import annotations

import logging

from openbot.application.sandbox_factory_deps import build_sandbox_factory
from openbot.core.settings import Settings
from openbot.evaluation import run_chat_sample

logger = logging.getLogger(__name__)


def openbot_chat_solver(*, model: str | None = None):
    """Inspect ``@solver`` driving the openbot chat responder on a SWE-QA-Pro sample."""
    try:
        from inspect_ai.solver import solver
    except ImportError:

        def solver(fn):  # type: ignore[misc]
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            md = state.metadata or {}
            sample_id = str(state.sample_id) if state.sample_id is not None else "anon"
            repo = str(md.get("repo", ""))
            question = state.input_text or ""

            # SWE-QA-Pro provides repo + commit_id; use them to clone a sandbox
            # so the agent can read actual repo files rather than answering blind.
            commit_id = str(md.get("commit_id", ""))
            clone_url = f"https://github.com/{repo}.git" if repo else ""
            settings = Settings()
            sandbox_factory = build_sandbox_factory(settings)
            if sandbox_factory is None:
                logger.warning(
                    "chat_solver: no sandbox configured (DAYTONA_API_KEY not set); "
                    "chat agent will answer without file access."
                )
            elif not commit_id:
                logger.warning(
                    "chat_solver: sample %s has no commit_id; "
                    "chat agent will answer without file access.",
                    sample_id,
                )
                sandbox_factory = None

            answer = await run_chat_sample(
                repo=repo,
                user_request=question,
                run_id=sample_id,
                sandbox_factory=sandbox_factory,
                base_sha=commit_id,
                clone_url=clone_url,
            )

            state.output.completion = answer
            return state

        return _run

    return _solver()


# Backward-compat alias
openbot_swe_qa_solver = openbot_chat_solver

__all__ = ["openbot_chat_solver", "openbot_swe_qa_solver"]
