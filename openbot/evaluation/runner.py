"""openbot.evaluation.runner — eval entry points.

Each ``run_*_sample`` function:
  1. Builds a synthetic ``UnifiedEvent`` from the sample's metadata.
  2. Constructs an ``EvalChannelAdapter`` pre-loaded with the sample's
     diff / files / issue data.
  3. Calls the production responder (DeepAgentsReviewResponder, etc.)
     exactly as the use case does.
  4. Returns a typed eval result that scorers can inspect.

This means evals measure the production code path end-to-end, not a
parallel eval-only reimplementation.

Sandbox lifecycle (unified for all four evals):
  All ``run_*_sample`` functions accept an optional ``sandbox_factory``
  callable.  Internally each one:

    1. Opens a sandbox via ``async with sandbox_factory() as sandbox``.
    2. Clones the repo at ``base_sha`` with ``strategy=BLOBLESS``
       (needed because base_sha is a commit SHA, not a branch name).
    3. Wires the sandbox into the agent:
       - fix / repro: passes ``sandbox`` directly to the responder so the
         agent can call ``run_command`` / ``write_file`` etc.
       - review / chat: wraps the sandbox in ``SandboxFileReader`` and
         injects it into ``EvalChannelAdapter.file_reader`` so the agent
         can call ``read_file`` / ``grep_repo`` via the adapter.
         The agent never sees the sandbox; ``SandboxRequirement.FORBIDDEN``
         on the chat agent is not violated.
    4. Lets the context-manager close the sandbox on exit.

  This mirrors what ``dispatcher._run_with_sandbox`` does in production
  — the caller only needs to supply a factory, not manage lifecycle.

  When ``sandbox_factory`` is ``None`` (or missing credentials), review /
  chat fall back to the ``file_reader`` / ``files`` kwargs so they degrade
  gracefully rather than failing hard.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from openbot.domain.checkout import CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.evaluation.adapters import EvalChannelAdapter
from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder
from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder
from openbot.infrastructure.agents.deepagents_repro import DeepAgentsReproResponder
from openbot.infrastructure.agents.deepagents_review import DeepAgentsReviewResponder

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.fix import FixOutcome
    from openbot.domain.repro import ReproOutcome
    from openbot.domain.review import ReviewFindings


@dataclass
class AgentMetadata:
    """Token usage and message history captured from a DeepAgents run."""

    token_usage: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)


# Module-level store: run_id → AgentMetadata.  Solvers read from here
# after calling run_*_sample, since frozen domain objects can't hold attrs.
_agent_metadata_store: dict[str, AgentMetadata] = {}


def _extract_and_store_metadata(responder: Any, run_id: str | None) -> None:
    """Read token usage and messages from a responder's runtime and store."""
    if not run_id:
        return
    runtime = getattr(responder, "_runtime", None)
    if runtime is None:
        return
    last = getattr(runtime, "_last_agent_metadata", {})
    _agent_metadata_store[run_id] = AgentMetadata(
        token_usage=last.get("token_usage", {}),
        messages=last.get("messages", []),
    )


def pop_agent_metadata(run_id: str) -> AgentMetadata:
    """Retrieve and remove agent metadata for a run_id."""
    return _agent_metadata_store.pop(run_id, AgentMetadata())


_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _open_sandbox_if_configured(
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]] | None,
    *,
    clone_url: str,
    base_sha: str,
    clone_token: str,
):
    """Open + clone a sandbox when all args are present; yield ``None`` otherwise.

    Centralises the "do we have a sandbox?" guard for review and chat.
    fix / repro use ``sandbox_factory`` unconditionally (they require it);
    review / chat treat it as optional and fall back to in-memory files.
    """
    if sandbox_factory is not None and base_sha and clone_url:
        async with sandbox_factory() as sandbox:
            await sandbox.clone(
                repo_url=clone_url,
                ref=base_sha,
                token=clone_token,
                strategy=CloneStrategy.BLOBLESS,
            )
            yield sandbox
    else:
        yield None


async def _setup_repo_env(sandbox: SandboxPort) -> None:
    """Install the repo's test dependencies after cloning (best-effort).

    Handles C-extension projects (astropy, numpy, scipy, etc.) whose
    ``pip install -e ".[test]"`` fails under pip's default build-isolation
    because the isolated build env gets a fresh setuptools that may be
    incompatible with the project's build system.

    Strategy:
      1. Read ``pyproject.toml`` [build-system].requires and install
         those build deps into the sandbox (so ``--no-build-isolation``
         can use them).
      2. ``pip install -e ".[test]" --no-build-isolation`` — uses the
         sandbox image's pre-installed build toolchain.
      3. Retry with ``[tests]`` and ``[testing]`` extras.
      4. Fallback: ``pip install pytest`` — bare minimum so the agent
         can at least attempt to run tests.

    Never raises — a failed setup is logged; the agent proceeds and may
    hit missing-import errors at collection time, which is still better
    than burning model-call budget on ``pip install`` inside the agent.
    """
    # ── Step 1: install build-system requirements from pyproject.toml ──
    # This ensures --no-build-isolation has all the build deps available.
    build_deps = await _read_build_requires(sandbox)
    if build_deps:
        result = await sandbox.run(
            command=["pip", "install", "--quiet", *build_deps],
            timeout_seconds=180,
        )
        _logger.debug(
            "repo_env_build_deps count=%d exit=%d",
            len(build_deps),
            result.exit_code,
        )

    # ── Step 2: editable install with test extras (--no-build-isolation) ──
    for extras in (".[test]", ".[tests]", ".[testing]"):
        result = await sandbox.run(
            command=[
                "pip",
                "install",
                "--quiet",
                "-e",
                extras,
                "--no-build-isolation",
            ],
            timeout_seconds=300,
        )
        if result.exit_code == 0:
            _logger.debug("repo_env_setup_ok extras=%s (no-build-isolation)", extras)
            return
        _logger.debug(
            "repo_env_setup_fail extras=%s exit=%d",
            extras,
            result.exit_code,
        )

    # ── Step 3: fallback — at least get pytest ──
    result = await sandbox.run(
        command=["pip", "install", "--quiet", "pytest"],
        timeout_seconds=60,
    )
    _logger.debug("repo_env_setup_fallback exit_code=%d", result.exit_code)


async def _read_build_requires(sandbox: SandboxPort) -> list[str]:
    """Extract [build-system].requires from the repo's pyproject.toml.

    Returns an empty list if pyproject.toml is missing, malformed, or
    the sandbox read fails.  Filters out version pins and environment
    markers so ``pip install`` gets the bare package names — the pinned
    versions in pyproject.toml may conflict with the sandbox's Python
    version or installed packages.
    """
    import tomllib

    try:
        raw = await sandbox.read_file("pyproject.toml")
    except Exception:
        return []
    if not raw or not raw.strip():
        return []

    try:
        data = tomllib.loads(raw)
    except Exception:
        return []

    build_system = data.get("build-system", {})
    requires = build_system.get("requires", [])
    if not isinstance(requires, list):
        return []

    # Strip version specifiers and environment markers:
    #   "setuptools>=42" → "setuptools"
    #   "cython==0.29.22" → "cython"
    #   'importlib-metadata; python_version == "3.7"' → skip
    deps: list[str] = []
    for req in requires:
        if not isinstance(req, str):
            continue
        # Skip env markers (e.g. "; python_version == '3.7'")
        if ";" in req:
            continue
        # Take package name only (before any version specifier)
        match = re.match(r"^([A-Za-z0-9_.-]+)", req)
        if match:
            deps.append(match.group(1))
    return deps


def _make_event(
    *,
    repo: str,
    actor: str,
    kind: EventKind,
    pr_number: int | None = None,
    issue_number: int | None = None,
    comment_body: str | None = None,
    delivery_id: str | None = None,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="eval",
        delivery_id=delivery_id or str(uuid.uuid4()),
        kind=kind,
        repo=repo,
        actor=actor,
        pr_number=pr_number,
        issue_number=issue_number,
        comment_body=comment_body,
    )


async def run_review_sample(
    *,
    repo: str,
    pr_number: int,
    pr_diff: str,
    actor: str = "eval-harness",
    files: dict[str, str] | None = None,
    run_id: str | None = None,
    # ── sandbox path (preferred) ──────────────────────────────────────────────
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]] | None = None,
    base_sha: str = "",
    clone_url: str = "",
    clone_token: str = "",
    # ── legacy file-reader path (fallback when no sandbox configured) ─────────
    file_reader: Any = None,
) -> ReviewFindings:
    """Run the production review responder on a synthetic PR.

    When ``sandbox_factory`` is supplied (together with ``base_sha`` and
    ``clone_url``), the runner opens a Daytona sandbox, clones the repo, wraps
    it in a ``SandboxFileReader``, and injects it into the adapter so the review
    agent's ``read_file`` / ``grep_repo`` tool calls read from the local clone
    rather than hitting the GitHub API.  The sandbox closes after the agent
    finishes.

    When ``sandbox_factory`` is ``None`` (no Daytona configured), falls back
    to ``file_reader`` (e.g. ``GitHubFileReader``) or the in-memory ``files``
    dict — same behavior as before this unified sandbox path was added.

    Args:
        repo:           ``"owner/name"`` — used in the LLM prompt context.
        pr_number:      The PR number (embedded in context; no real API call).
        pr_diff:        The full unified diff for the PR.
        actor:          Reviewer persona (defaults to "eval-harness").
        files:          Optional in-memory repo file map (fallback only).
        run_id:         Optional LangSmith run ID for tracing.
        sandbox_factory: Zero-argument callable returning an async context
            manager that yields a ``SandboxPort``.  Preferred file-access path.
        base_sha:       Commit SHA to clone at (required with sandbox_factory).
        clone_url:      HTTPS clone URL (required with sandbox_factory).
        clone_token:    GitHub token; ``""`` works for public repos.
        file_reader:    Legacy ``GitHubFileReader`` fallback.

    Returns:
        ``ReviewFindings`` — the responder's structured output.
    """
    from openbot.evaluation.sandbox_file_reader import SandboxFileReader

    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.PR_OPENED,
        pr_number=pr_number,
    )
    async with _open_sandbox_if_configured(
        sandbox_factory,
        clone_url=clone_url,
        base_sha=base_sha,
        clone_token=clone_token,
    ) as sandbox:
        resolved_reader = SandboxFileReader(sandbox) if sandbox is not None else file_reader
        adapter = EvalChannelAdapter(
            pr_diff=pr_diff,
            files=files or {},
            file_reader=resolved_reader,
        )
        responder = DeepAgentsReviewResponder()
        result = await responder.review_for_event(
            event,
            adapter=adapter,
            run_id=run_id,
            per_task_cap_usd=Decimal("1.50"),
            session_factory=None,
        )
        _extract_and_store_metadata(responder, run_id)
        return result


async def run_fix_sample(
    *,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    base_sha: str,
    clone_url: str,
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]],
    clone_token: str = "",
    actor: str = "eval-harness",
    files: dict[str, str] | None = None,
    run_id: str | None = None,
) -> FixOutcome:
    """Run the production fix responder on a synthetic GitHub issue.

    Opens a sandbox via ``sandbox_factory``, clones the repo at
    ``base_sha``, then runs the production fix responder — matching
    the dispatcher's ``_run_with_sandbox`` lifecycle in production.

    Args:
        repo:           ``"owner/name"``.
        issue_number:   The issue number.
        issue_title:    Issue title text.
        issue_body:     Issue body text (Markdown).
        base_sha:       The SHA to clone and fix against.
        clone_url:      HTTPS clone URL (e.g. ``https://github.com/X/Y.git``).
        sandbox_factory: Zero-argument callable returning an async context
            manager that yields a ``SandboxPort``.  OpenBot uses
            ``build_sandbox_factory(settings)`` here; callers must not
            call ``sandbox.clone`` themselves — this function owns the
            lifecycle.
        clone_token:    GitHub PAT / installation token.  Pass ``""`` for
            public repos (Daytona accepts unauthenticated HTTPS clones on
            public repositories).
        actor:          Assignee persona.
        files:          Optional in-memory file map (used by
            ``EvalChannelAdapter.read_file`` when no live adapter is set).
        run_id:         Optional LangSmith run ID.

    Returns:
        ``FixOutcome`` — the responder's structured output.
    """
    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.ISSUE_ASSIGNED,
        issue_number=issue_number,
    )
    adapter = EvalChannelAdapter(
        pr_diff="",  # fix loop doesn't read the PR diff
        files=files or {},
        issue={
            "title": issue_title,
            "body": issue_body,
            "comments": [],
            "base_sha": base_sha,
            "default_branch": "main",
            "clone_url": clone_url,
        },
    )
    issue = {
        "title": issue_title,
        "body": issue_body,
        "base_sha": base_sha,
    }

    # Mirror production: open sandbox → clone → agent → close.
    # The context manager (from sandbox_factory) handles close on exit.
    async with sandbox_factory() as sandbox:
        await sandbox.clone(
            repo_url=clone_url,
            ref=base_sha,
            token=clone_token,
            strategy=CloneStrategy.BLOBLESS,
        )
        responder = DeepAgentsFixResponder()
        result = await responder.fix_for_event(
            event,
            adapter=adapter,
            sandbox=sandbox,
            issue=issue,
            run_id=run_id,
        )
        _extract_and_store_metadata(responder, run_id)
        return result


async def run_chat_sample(
    *,
    repo: str,
    user_request: str,
    actor: str = "eval-harness",
    issue_number: int | None = None,
    comment_body: str | None = None,
    run_id: str | None = None,
    # ── sandbox path (preferred) ──────────────────────────────────────────────
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]] | None = None,
    base_sha: str = "",
    clone_url: str = "",
    clone_token: str = "",
) -> str:
    """Run the production chat responder on a synthetic comment.

    When ``sandbox_factory`` is supplied (together with ``base_sha`` and
    ``clone_url``), the runner opens a Daytona sandbox, clones the repo, wraps
    it in a ``SandboxFileReader``, and injects it into the adapter so the chat
    agent's ``read_file`` / ``grep_repo`` / ``list_files`` tool calls read from
    the local clone.  The sandbox closes after the agent finishes.

    Note: ``ChatProfile.sandbox_requirement = FORBIDDEN`` means the sandbox is
    NOT passed to the agent runtime.  It is used only as a file server behind
    the adapter — the agent calls adapter methods, not sandbox methods directly.

    When ``sandbox_factory`` is ``None``, the chat agent runs without file
    access (same behavior as before this change).

    Args:
        repo:           ``"owner/name"``.
        user_request:   The human's message / question.
        actor:          Commenter persona.
        issue_number:   Optional issue number for context.
        comment_body:   Raw comment text (defaults to ``user_request``).
        run_id:         Optional LangSmith run ID.
        sandbox_factory: Zero-argument callable returning an async context
            manager that yields a ``SandboxPort``.
        base_sha:       Commit SHA to clone at (required with sandbox_factory).
        clone_url:      HTTPS clone URL (required with sandbox_factory).
        clone_token:    GitHub token; ``""`` works for public repos.

    Returns:
        The bot's reply string.
    """
    from openbot.evaluation.sandbox_file_reader import SandboxFileReader

    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.ISSUE_COMMENT_CREATED,
        issue_number=issue_number,
        comment_body=comment_body or user_request,
    )
    async with _open_sandbox_if_configured(
        sandbox_factory,
        clone_url=clone_url,
        base_sha=base_sha,
        clone_token=clone_token,
    ) as sandbox:
        adapter: EvalChannelAdapter | None = None
        if sandbox is not None:
            adapter = EvalChannelAdapter(
                pr_diff="",
                file_reader=SandboxFileReader(sandbox),
            )
        responder = DeepAgentsChatResponder()
        answer = await responder.reply_for_event(
            event,
            user_request=user_request,
            run_id=run_id,
            adapter=adapter,
        )
        _extract_and_store_metadata(responder, run_id)
        return answer


async def run_test_generation_sample(
    *,
    instance_id: str,
    repo: str,
    issue_number: int,
    problem_statement: str,
    base_sha: str,
    clone_url: str,
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]],
    clone_token: str = "",
    actor: str = "eval-harness",
    run_id: str | None = None,
) -> ReproOutcome:
    """Run the reproduce agent to write a failing test for SWT-bench.

    The reproduce agent explores the repo, writes a pytest test file that
    triggers the reported failure, runs it to confirm the failure, then
    calls ``git_diff`` to capture the patch.

    The SWT-bench solver maps the return value as:
        ``SwtBenchPrediction.model_patch = outcome.repro_artifact or ""``

    Args:
        instance_id:       SWT-bench instance id (e.g. ``astropy__astropy-12907``).
        repo:              ``"owner/name"``.
        issue_number:      GitHub issue number (may be synthetic for eval).
        problem_statement: Issue body / problem description from the dataset.
        base_sha:          The SHA to clone and test against.
        clone_url:         HTTPS clone URL.
        sandbox_factory:   Required — zero-argument callable returning an async
            context manager yielding a ``SandboxPort``.  The agent needs
            ``write_file`` and ``run_command`` to author and verify the test.
        clone_token:       GitHub PAT / installation token.
        actor:             Assignee persona.
        run_id:            Optional LangSmith run ID.

    Returns:
        ``ReproOutcome`` — ``repro_artifact`` is the git unified diff of the
        test file written; ``repro_artifact_filename`` is the test file path.
        A ``None`` artifact means the agent could not write a reproducing test.
    """
    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.ISSUE_ASSIGNED,
        issue_number=issue_number,
    )
    adapter = EvalChannelAdapter(
        pr_diff="",
        issue={
            "title": f"[SWT-bench] {instance_id}",
            "body": problem_statement,
            "comments": [],
            "base_sha": base_sha,
            "default_branch": "main",
            "clone_url": clone_url,
        },
    )
    issue = {
        "title": f"[SWT-bench] {instance_id}",
        "body": problem_statement,
        "base_sha": base_sha,
    }

    # Sandbox is required: the agent writes a test file and runs it.
    async with sandbox_factory() as sandbox:
        await sandbox.clone(
            repo_url=clone_url,
            ref=base_sha,
            token=clone_token,
            strategy=CloneStrategy.BLOBLESS,
        )
        await _setup_repo_env(sandbox)
        responder = DeepAgentsReproResponder()
        result = await responder.reproduce_for_event(
            event,
            adapter=adapter,
            sandbox=sandbox,
            issue=issue,
            run_id=run_id,
        )
        _extract_and_store_metadata(responder, run_id)
        return result


__all__ = [
    "run_chat_sample",
    "run_fix_sample",
    "run_review_sample",
    "run_test_generation_sample",
]
