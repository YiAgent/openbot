"""Inspect solver wrapping the durable `deepagents_baseline` review provider.

PRD §4.1 reserves a future `openbot_prod` provider that will call
`openbot.application.workflows.review.run(...)` once the production workflow exists. This
module intentionally keeps the deepagents path as a long-lived comparator so
future evals can show where OpenBot itself beats a credible baseline.

Both providers must preserve the same input/output contract:
  - Input  : PR diff (str)
  - Output : list[Finding] where Finding = {file, line: int | None, body, severity}

The Inspect AI `@solver` shim at the bottom is the entry point used by
`evals/tasks/review_martian.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from deepagents.backends.protocol import BackendProtocol

from evals.agents.baseline import build_run_config, resolve_model
from evals.agents.review import (
    ReviewResponseModel,
    build_review_agent,
    build_review_user_message,
)
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import (
    RepoSpec,
    SandboxBackend,
    create_bare_sandbox,
    create_sandbox_for_sample,
)


class Finding(TypedDict):
    """Output shape — also referenced by `evals.scorers.review_overlap.Finding`."""

    file: str
    line: int | None
    body: str
    severity: Literal["low", "medium", "high"]


def _normalize_severity(value: Any) -> Literal["low", "medium", "high"]:
    s = str(value).lower().strip()
    if s in {"low", "medium", "high"}:
        return s  # type: ignore[return-value]
    return "medium"


def _coerce_findings(raw: Any) -> list[Finding]:
    """Best-effort: pull a `findings` list out of whatever the agent returned."""
    if isinstance(raw, dict) and "findings" in raw:
        items = raw["findings"]
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                {
                    "file": str(item["file"]),
                    "line": int(item["line"]) if item.get("line") is not None else None,
                    "body": str(item["body"]),
                    "severity": _normalize_severity(item.get("severity", "medium")),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the LAST JSON object from `text` — agents sometimes prepend prose."""
    matches = re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, flags=re.DOTALL)
    for blob in reversed(matches):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# Usage aggregation lives in evals.common.usage — sums across all AI
# messages, matching LangSmith's trace-side aggregation.


@dataclass(frozen=True)
class ReviewResult:
    """Both the raw agent reply and the structured findings parsed from it.

    The raw text matters: the safety scorer (E2-T13) must scan the **whole**
    response for canaries / forbidden patterns. If we only kept the parsed
    findings, an attacker could leak a canary in prefatory prose and pass.

    ``structured_present`` records whether the agent actually emitted a
    schema-conforming payload (via deepagents' ``structured_response`` OR a
    JSON object embedded in prose). When ``False`` but ``raw_text`` carries
    substantive analysis, the caller runs the force-tool retry — that's the
    specific failure mode we observed on review (long prose enumeration of
    real defects, structured-output tool never called).
    """

    raw_text: str
    findings: list[Finding] = field(default_factory=list)
    provider_usage: dict[str, Any] | None = None
    structured_present: bool = False


def _findings_from_structured(payload: Any) -> list[Finding] | None:
    """Pull normalized findings out of deepagents' ``structured_response``.

    deepagents binds ``response_format=`` to the agent's terminal step via
    langchain's ``AutoStrategy`` — the parsed Pydantic instance lives at
    ``result["structured_response"]``. Returns ``None`` when the agent didn't
    emit one (e.g. legacy stub agents in unit tests), so callers fall back to
    the regex extractor without crashing.
    """
    if payload is None:
        return None
    if isinstance(payload, ReviewResponseModel):
        as_dict = payload.model_dump()
    elif isinstance(payload, dict):
        as_dict = payload
    else:
        # Some langchain shapes return arbitrary objects; try `.model_dump()`
        # before giving up so we don't lose a valid scorecard to a type check.
        dump = getattr(payload, "model_dump", None)
        if not callable(dump):
            return None
        as_dict = dump()
    return _coerce_findings(as_dict)


def _collect_raw_text(messages: list[Any]) -> str:
    """Concatenate every assistant message into one string.

    The safety scorer (E2-T13) scans the **whole** agent reply for canaries
    and forbidden patterns. With ``response_format=`` enabled, the final
    schema-binding step is its own assistant turn, so an attacker who plants
    a canary in an earlier turn (e.g. a chain-of-thought leak) would slip
    past a scan that only looked at ``messages[-1]``. Joining every AI text
    turn keeps the safety scorer's surface a strict superset of what the
    previous "last message only" path saw.
    """
    parts: list[str] = []
    for msg in messages:
        if getattr(msg, "type", None) != "ai":
            continue
        content = msg.content if hasattr(msg, "content") else str(msg)
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        parts.append(str(content))
    return "\n".join(parts)


def _parse_agent_result(result: Any) -> ReviewResult:
    """Shared parser for both closed-form and sandbox-mode agent results."""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    last_msg = messages[-1] if messages else None
    text = _collect_raw_text(messages) if messages else ""
    if not text and last_msg is not None:
        # Defensive: legacy stub agents may not set a `type` attr on messages.
        fallback = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
        if isinstance(fallback, list):
            fallback = "\n".join(b.get("text", "") for b in fallback if isinstance(b, dict))
        text = str(fallback)

    structured = result.get("structured_response") if isinstance(result, dict) else None
    findings = _findings_from_structured(structured)
    structured_present = findings is not None
    if findings is None:
        # Stub-agent / legacy path — extract JSON from the raw reply.
        obj = _extract_json_object(text)
        findings = _coerce_findings(obj) if obj else []
        # JSON-in-prose counts as schema-equivalent: agent did emit the
        # findings shape, just inline instead of via the structured tool.
        if obj is not None:
            structured_present = True

    return ReviewResult(
        raw_text=text,
        findings=findings,
        provider_usage=aggregate_provider_usage(messages),
        structured_present=structured_present,
    )


def review_diff(diff: str, *, model: str | None = None) -> ReviewResult:
    """Run the deep agent on a PR diff, return raw text + normalized findings.

    Pure function (no inspect-ai imports) so it's directly callable in tests.

    The preconfigured review agent lives under ``evals.agents.review``. This
    helper keeps the legacy direct-call test surface.
    """
    # Closed-form review: no backend, no tools. The agent reads the diff and
    # emits findings — it never touches a shell. The shared baseline factory
    # still applies (so the HarnessProfile that drops `write_todos` and
    # `task` matches what the SWE-bench solver uses).
    resolved_model = resolve_model(override=model)

    agent = build_review_agent(model=resolved_model, sandbox_mode=False)
    user_msg = build_review_user_message(diff=diff, sandbox_mode=False)
    raw_result = agent.invoke({"messages": [{"role": "user", "content": user_msg}]})
    # The baseline wrapper's structured_finalizer guarantees
    # ``structured_response`` whenever the agent emitted any prose, so we
    # check both gates here. Empty trace → wrapper already raised
    # AgentTerminationError; clean wrapper return → schema is present.
    assert_clean_termination(
        raw_result,
        requires_structured_response=True,
        structured_response_type=ReviewResponseModel,
    )
    return _parse_agent_result(raw_result)


_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/\d+")


def _resolve_owner_repo(repo_field: Any, pr_url: Any) -> str | None:
    """Derive a canonical ``owner/name`` slug for cloning.

    Why this exists: the Martian review dataset stores ``repo`` as a
    cosmetic slug (``cal_dot_com``) that is **not** a real GitHub path.
    The real ``owner/name`` lives in ``pr_url``. We prefer ``repo`` only
    when it already looks GitHub-shaped; otherwise parse the PR URL.
    Returns ``None`` when neither yields a usable slug (caller falls back
    to a bare sandbox).
    """
    if isinstance(repo_field, str) and "/" in repo_field and ".." not in repo_field:
        return repo_field
    if isinstance(pr_url, str):
        m = _PR_URL_RE.search(pr_url)
        if m:
            return f"{m['owner']}/{m['name']}"
    return None


# ─── Inspect AI @solver shim ────────────────────────────────────────────────


def deepagents_baseline_review_solver(
    *,
    model: str | None = None,
    backend: BackendProtocol | None = None,
    use_sandbox: bool = True,
):  # type: ignore[no-untyped-def]
    """Inspect AI ``@solver`` — deepagents review with sandbox + gh access.

    Mirrors the swe_fix / swe_test pattern: the agent runs under an Inspect
    per-sample sandbox via :class:`InspectSandboxBackend`, so deepagents
    auto-attaches its file-aware toolset (``ls`` / ``read_file`` / ``grep``
    / ``glob`` / ``write_file`` / ``edit_file`` / ``execute``). ``gh`` and
    ``git`` are reachable through ``execute`` — the system prompt teaches
    the agent which ``gh`` subcommands to reach for. The PR ``repo`` /
    ``pr_url`` / ``pr_title`` from the sample metadata are surfaced in the
    user message so the agent has canonical identifiers without needing to
    parse the diff header.

    Args:
        model: Optional explicit ``provider:model`` id. Falls back to
            the shared ``OPENBOT_DEEPAGENTS_MODEL`` config, then to the
            baseline fallback.
        backend: Override sandbox backend (testing hook). Defaults to a
            fresh :class:`InspectSandboxBackend` per sample.
        use_sandbox: When ``False``, runs the closed-form ``review_diff``
            path (no backend, no tools). Useful as a regression fallback
            when no sandbox is configured on the Task.

    State shape after this solver runs:
      - ``state.output.completion`` ← normalized findings JSON (stable
        export / LangSmith surface).
      - ``state.metadata["agent_raw_output"]`` ← full raw agent text
        (for future safety / canary scans).
      - ``state.metadata["candidate_findings"]`` ← parsed findings list
        (for the review_overlap scorer).
      - ``state.metadata["candidate_findings_json"]`` ← JSON-serialized
        findings (legacy / trace export).
    """
    from inspect_ai.solver import Generate, Solver, TaskState, solver

    resolved_model = resolve_model(override=model)

    @solver
    def _solver() -> Solver:
        async def _run(state: TaskState, _generate: Generate) -> TaskState:
            diff = state.input_text
            if not use_sandbox:
                result = review_diff(diff, model=resolved_model)
            else:
                md = state.metadata or {}
                repo = _resolve_owner_repo(md.get("repo"), md.get("pr_url"))
                # ``upstream_commit`` is martian-CRB's own snapshot SHA (provenance
                # of the golden comments), NOT the PR's base commit on the target
                # repo. Using it for ``git fetch`` guarantees the SHA-fallback path
                # fires and the agent ends up reading the wrong code. Trust only
                # ``base_sha`` (the dataset builder writes it via the GitHub PR API);
                # missing ``base_sha`` ⇒ bare sandbox (no repo) is more honest than a
                # silently-wrong checkout.
                base_sha = md.get("base_sha")
                if backend is not None:
                    effective_backend = backend
                elif repo and base_sha:
                    # Production path: clone the repo at the PR base commit
                    # so the agent can chase callers / type defs / tests
                    # beyond what the diff shows. Backend kind is config-
                    # driven (OPENBOT_SANDBOX_BACKEND); the solver doesn't
                    # care which one returned.
                    effective_backend = await create_sandbox_for_sample(
                        repo_spec=RepoSpec(repo=str(repo), base_commit=str(base_sha)),
                    )
                else:
                    # Bare fallback — sample has no repo identity (synthetic
                    # tests, prompt-injection corpus, etc.). Agent still gets
                    # a shell + scratch /workspace; diff in input is enough.
                    effective_backend = await create_bare_sandbox()
                # Track whether *we* own the backend, so we can close it on
                # exit (caller-supplied backends stay alive — caller-managed).
                owns_backend = backend is None
                # try/finally must wrap everything after the backend is
                # constructed — if agent build or config wiring throws,
                # we still need to tear the container down. Previously the
                # try started only around ainvoke, leaking on early errors.
                try:
                    agent = build_review_agent(
                        model=resolved_model,
                        backend=effective_backend,
                    )
                    user_msg = build_review_user_message(
                        diff=diff,
                        repo=md.get("repo"),
                        pr_url=md.get("pr_url"),
                        pr_title=md.get("pr_title"),
                        base_sha=base_sha,
                    )
                    sample_label = str(state.sample_id) if state.sample_id is not None else "anon"
                    ls_config = build_run_config(
                        sample_id=sample_label,
                        dataset_version=md.get("dataset_version", "martian_2026w20"),
                        solver_family=md.get("solver_family", "deepagents_baseline"),
                        model=resolved_model,
                        git_sha=md.get("git_sha"),
                        extra_metadata={
                            "repo": md.get("repo"),
                            "pr_url": md.get("pr_url"),
                        },
                    )
                    if isinstance(effective_backend, SandboxBackend):
                        state.metadata["modal_sandbox_id"] = effective_backend.id
                        state.metadata["modal_sha_fallback"] = effective_backend.used_sha_fallback
                    raw_result = await agent.ainvoke(
                        {"messages": [{"role": "user", "content": user_msg}]},
                        config=ls_config,
                    )
                    # Structured output is enforced by the baseline
                    # wrapper's post-loop finalizer call — by the time we
                    # get here, ``structured_response`` is populated (or
                    # the wrapper already raised AgentTerminationError on
                    # an empty trace). We still gate on the contract so
                    # any future regression is loud.
                    assert_clean_termination(
                        raw_result,
                        requires_structured_response=True,
                        structured_response_type=ReviewResponseModel,
                    )
                    result = _parse_agent_result(raw_result)
                finally:
                    if owns_backend and isinstance(effective_backend, SandboxBackend):
                        await effective_backend.aclose()

            state.metadata["candidate_findings"] = result.findings
            state.metadata["candidate_findings_json"] = json.dumps(
                {"findings": result.findings}, ensure_ascii=False
            )
            state.metadata["agent_raw_output"] = result.raw_text
            if result.provider_usage is not None:
                state.metadata["provider_usage"] = result.provider_usage
            # Keep the completion field stable and structured for eval exports
            # and LangSmith experiment rows; raw prose lives in metadata.
            state.output.completion = state.metadata["candidate_findings_json"]
            return state

        return _run

    return _solver()
