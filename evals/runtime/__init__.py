"""Runtime support for OpenBot eval suites.

Importing this package eagerly redirects LangChain auto-traces from the prod
project (``LANGSMITH_PROJECT``) to the eval-only project
(``LANGSMITH_PROJECT_EVAL``). LangChain reads ``LANGSMITH_PROJECT`` from the
environment at callback time, so the redirect must happen before any solver
issues an LLM call. The first import of any ``evals.tasks.*`` module pulls
this in, which is good enough — Inspect AI loads the task module before the
solver runs.

No-op if ``LANGSMITH_API_KEY`` or ``LANGSMITH_PROJECT_EVAL`` is unset (offline
dev / unit tests / CI without LangSmith credentials).
"""

from __future__ import annotations

import os

from evals.runtime.config import LANGSMITH_EVAL_PROJECT_ENV


def _route_traces_to_eval_project() -> None:
    if not os.environ.get("LANGSMITH_API_KEY"):
        return
    project = os.environ.get(LANGSMITH_EVAL_PROJECT_ENV)
    if project:
        os.environ["LANGSMITH_PROJECT"] = project


_route_traces_to_eval_project()

# Importing for side effect: ``@hooks`` registers the LangSmith Experiment
# anchor hook in Inspect AI's global registry. Must happen before any task
# module loads, so it lives at package init time. Hook itself is a noop
# unless ``LANGSMITH_API_KEY`` is set (see ``langsmith_hook._LangSmithExperimentHook.enabled``).
from evals.runtime import langsmith_hook  # noqa: E402, F401
