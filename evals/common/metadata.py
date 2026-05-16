"""Run-level metadata collector — eval PRD §10.1.

`collect_run_metadata(suite_name, mode, dataset_version, dataset_sha256)` returns
a dict whose keys exactly match `REQUIRED_RUN_FIELD_NAMES`. Pass the dict
straight into `evals.common.langsmith.log_run_metadata()`.

Auto-detected fields (with safe fallbacks for the v0.1 skeleton):
  - git_sha           : `git rev-parse HEAD` (raises if not in a git repo)
  - runner_version    : `inspect_ai.__version__`
  - prompt_version    : `openbot.prompts.__version__`   (fallback "unknown")
  - workflow_version  : `openbot.workflows.__version__` (fallback "unknown")
  - judge_prompt_version : evals.common.judges.JUDGE_PROMPT_VERSION (fallback 0; E1-T05 ships the real one)
  - started_at        : datetime.now(timezone.utc).isoformat()
  - triggered_by      : "github_actor" if GITHUB_ACTIONS=true else env(OPENBOT_EVAL_TRIGGER) or "local"

Caller-supplied fields:
  - suite_name, mode      : positional args
  - dataset_version       : keyword (per-suite, lives in manifest)
  - dataset_sha256        : keyword (per-suite, lives in manifest)
  - solver_id             : keyword (provider id, e.g. deepagents_baseline)
  - solver_family         : keyword (baseline | production)
  - model_id / judge_model_id / sandbox_backend / suite_version : keyword with defaults
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from typing import Any

from evals.common._metadata_spec import REQUIRED_RUN_FIELD_NAMES

_DEFAULT_MODEL_ID = "anthropic/claude-opus-4-7"
_DEFAULT_SANDBOX_BACKEND = "modal"


def _git_sha() -> str:
    """Return current commit SHA. Raises CalledProcessError if not in a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _runner_version() -> str:
    """inspect-ai version (PRD §10.1 `runner_version`)."""
    from inspect_ai import __version__ as inspect_ai_version

    return inspect_ai_version


def _try_module_version(modname: str) -> str:
    """Best-effort `<modname>.__version__` lookup; "unknown" if module not importable."""
    try:
        mod = __import__(modname, fromlist=["__version__"])
    except ImportError:
        return "unknown"
    return getattr(mod, "__version__", "unknown")


def _judge_prompt_version() -> int:
    """Read from evals.common.judges (E1-T05). Fallback 0 in the skeleton."""
    try:
        from evals.common.judges import JUDGE_PROMPT_VERSION
    except ImportError:
        return 0
    return JUDGE_PROMPT_VERSION


def _detect_trigger() -> str:
    """Map env to PRD §10.1 `triggered_by` enum: github_actor | local | cron."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github_actor"
    explicit = os.environ.get("OPENBOT_EVAL_TRIGGER")
    if explicit in {"github_actor", "local", "cron"}:
        return explicit
    return "local"


def collect_run_metadata(
    suite_name: str,
    mode: str,
    *,
    dataset_version: str,
    dataset_sha256: str,
    solver_id: str,
    solver_family: str,
    model_id: str = _DEFAULT_MODEL_ID,
    judge_model_id: str = _DEFAULT_MODEL_ID,
    sandbox_backend: str = _DEFAULT_SANDBOX_BACKEND,
    suite_version: int = 1,
) -> dict[str, Any]:
    """Build a complete PRD §10.1 metadata dict. CWD-independent.

    Returns a dict whose `.keys()` ⊇ `REQUIRED_RUN_FIELD_NAMES`. Caller pipes
    it into `evals.common.langsmith.log_run_metadata()`.
    """
    metadata: dict[str, Any] = {
        "suite_name": suite_name,
        "suite_version": suite_version,
        "dataset_version": dataset_version,
        "dataset_sha256": dataset_sha256,
        "git_sha": _git_sha(),
        "prompt_version": _try_module_version("openbot.prompts"),
        "workflow_version": _try_module_version("openbot.workflows"),
        "solver_id": solver_id,
        "solver_family": solver_family,
        "model_id": model_id,
        "judge_model_id": judge_model_id,
        "judge_prompt_version": _judge_prompt_version(),
        "sandbox_backend": sandbox_backend,
        "runner_version": _runner_version(),
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(),
        "triggered_by": _detect_trigger(),
    }
    # Belt-and-braces: contract check at construction time so a future field
    # added to PRD §10.1 fails *here*, before hitting `log_run_metadata`.
    missing = REQUIRED_RUN_FIELD_NAMES - metadata.keys()
    if missing:
        raise RuntimeError(
            f"collect_run_metadata out of sync with PRD §10.1: missing {sorted(missing)}"
        )
    return metadata
