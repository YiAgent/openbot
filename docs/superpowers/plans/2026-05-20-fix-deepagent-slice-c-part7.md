# Slice C — Fix workflow end-to-end (part 7: use case rewrite)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Continues from:** `2026-05-20-fix-deepagent-slice-c-part6.md` (`DeepAgentsFixResponder`).
**Continues to:** `2026-05-20-fix-deepagent-slice-c-part8.md` (E2E demo + finalization).

Task C.8 replaces the ACK stub in `openbot/application/use_cases/fix.py`
with the full pipeline: fetch issue → open sandbox → clone → run
responder → branch+push+PR (tests passed) or comment (anything else).
Also adds the `sandbox_factory` field on `PreflightContext`.

---

## Task C.8: Use case — rewrite `maybe_run_fix`

**Files:**
- Rewrite: `openbot/application/use_cases/fix.py`
- Modify: `openbot/application/middleware/preflight.py` (+1 field on `PreflightContext`)
- Rewrite: `tests/application/use_cases/test_fix.py`

The use case orchestrates five dependencies (channel adapter, sandbox
port, responder, audit lifecycle, `@traceable`) across seven failure
paths. Each failure path gets a tailored GitHub comment so the issue
reporter knows what happened. The single seam — `_generate_fix_outcome`
— is what E2E tests in C.9 monkeypatch. Mirror
`_generate_review_findings` in `openbot/application/use_cases/review.py`
(slice B).

**`sandbox_factory` on `PreflightContext`:** the entrypoint already
builds the context per webhook delivery; threading the factory through
it keeps the use case free of `Daytona`/`Modal`/`Docker` knowledge (PRD
lock: sandbox is pluggable). `None` means "sandbox not configured" —
the use case comments instead of raising. Type:
`Callable[[], AbstractAsyncContextManager[SandboxPort]] | None`. All
shell-like work flows through `SandboxPort.run([...])` argv form — no
subprocess work in this file.

**Pre-read:** `openbot/application/use_cases/review.py` (same shape:
audit lifecycle wrapping, structured try/except per stage, single
seam). `_lifecycle.audit_lifecycle` and `_tracing.traceable` are
unchanged from slice B.

### TDD steps

- [ ] **Step 1: Add `sandbox_factory` to `PreflightContext`**

Edit `openbot/application/middleware/preflight.py`. In the
`TYPE_CHECKING:` block, add:

```python
    from contextlib import AbstractAsyncContextManager

    from openbot.application.ports.sandbox import SandboxPort
```

`Callable` is already imported in that block. Add the field on
`PreflightContext` (after `rate_limiter`, before `cache` so the loose
`cache` field remains last):

```python
    # Per-event sandbox factory — slice C. ``None`` means the sandbox
    # backend is not configured; the fix use case treats that as a
    # graceful comment ("sandbox unavailable") rather than raising.
    # Returns an async context manager so ``close()`` runs on every
    # exit, including the failure paths.
    sandbox_factory: Callable[[], AbstractAsyncContextManager[SandboxPort]] | None = None
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/application/use_cases/test_fix.py
"""Use case tests for ``maybe_run_fix`` (slice C end-to-end)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.use_cases import fix as fix_module
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome


def _event(**overrides: Any) -> UnifiedEvent:
    defaults: dict[str, Any] = {
        "delivery_id": "del-1",
        "channel": "github",
        "kind": EventKind.ISSUE_ASSIGNED,
        "repo": "o/r",
        "actor": "alice",
        "installation_id": 999,
        "issue_number": 7,
        "pr_number": None,
        "event_id": "evt-1",
        "action": "assigned",
    }
    defaults.update(overrides)
    return UnifiedEvent(**defaults)


def _attempt(**overrides: Any) -> FixAttempt:
    defaults: dict[str, Any] = {
        "summary": "fix off-by-one",
        "files_changed": ("src/api/list.py",),
        "tests_passed": True,
        "test_command": "pytest -q",
        "test_output": "3 passed",
        "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
    }
    defaults.update(overrides)
    return FixAttempt(**defaults)


def _issue() -> dict[str, Any]:
    return {
        "title": "Off-by-one",
        "body": "details",
        "base_sha": "abc1234567",
        "default_branch": "main",
        "clone_url": "https://github.com/o/r.git",
    }


@dataclass
class _FakeSandbox:
    cloned: list[tuple[str, str | None]] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    async def clone(self, repo_url: str, *, ref: str | None = None) -> None:
        self.cloned.append((repo_url, ref))

    async def commit_and_push(
        self, *, branch: str, message: str, remote_url: str
    ) -> None:
        self.pushed.append((branch, message, remote_url))

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def _sandbox_cm(sandbox: _FakeSandbox):
    try:
        yield sandbox
    finally:
        await sandbox.close()


def _adapter(**method_overrides: Any) -> MagicMock:
    """Happy-path adapter mock; pass kwargs to override a method
    (e.g. ``get_issue=AsyncMock(side_effect=RuntimeError())``).
    """
    a = MagicMock()
    a.get_issue = AsyncMock(return_value=_issue())
    a.get_installation_token = AsyncMock(return_value="tok123")
    a.create_branch = AsyncMock()
    a.open_pull_request = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/9"},
    )
    a.reply = AsyncMock(return_value={"id": 1})
    for name, value in method_overrides.items():
        setattr(a, name, value)
    return a


def _ctx(*, event: UnifiedEvent | None = None, adapter: Any, sandbox_factory: Any) -> Any:
    from openbot.application.middleware.preflight import PreflightContext
    from openbot.application.router import Dispatch
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.domain.workflows import Feature

    return PreflightContext(
        event=event or _event(),
        dispatch=Dispatch(feature=Feature.FIX, _reason="test"),
        config=EffectiveConfig.empty(),  # existing helper used by test_review.py
        adapter=adapter,
        session_factory=None,
        redis=None,
        sandbox_factory=sandbox_factory,
    )


# ---------- Skip paths ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("field_to_drop", ["issue_number", "installation_id"])
async def test_skips_when_required_event_field_missing(field_to_drop: str):
    adapter = _adapter()
    ctx = _ctx(
        event=_event(**{field_to_drop: None}),
        adapter=adapter,
        sandbox_factory=lambda: _sandbox_cm(_FakeSandbox()),
    )

    await fix_module.maybe_run_fix(ctx)

    adapter.reply.assert_not_called()
    adapter.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_comments_when_sandbox_factory_missing():
    adapter = _adapter()
    ctx = _ctx(adapter=adapter, sandbox_factory=None)

    await fix_module.maybe_run_fix(ctx)

    assert "sandbox is not configured" in adapter.reply.call_args.args[1].lower()


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_fetches_issue_clones_and_opens_pr(monkeypatch):
    sandbox = _FakeSandbox()
    adapter = _adapter()

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    # Token injected into HTTPS URL; clone uses base SHA.
    assert sandbox.cloned == [
        ("https://x-access-token:tok123@github.com/o/r.git", "abc1234567"),
    ]
    assert sandbox.closed is True

    # Branch name pattern + short SHA.
    branch_ref = adapter.create_branch.call_args.args[1]
    assert branch_ref.startswith("refs/heads/openbot/fix-issue-7-")
    assert "abc1234" in branch_ref

    # PR body closes the issue; head/base wired from issue dict.
    pr_kwargs = adapter.open_pull_request.call_args.kwargs
    assert pr_kwargs["base"] == "main"
    assert pr_kwargs["head"].startswith("openbot/fix-issue-7-")
    assert "Closes #7" in pr_kwargs["body"]
    assert sandbox.pushed and sandbox.pushed[0][0].startswith("openbot/fix-issue-7-")

    # Final comment carries the PR URL.
    assert "https://github.com/o/r/pull/9" in adapter.reply.call_args.args[1]


@pytest.mark.asyncio
async def test_audit_lifecycle_records_pr_url_on_success(monkeypatch):
    sandbox = _FakeSandbox()
    adapter = _adapter()
    captured: list[str] = []

    @asynccontextmanager
    async def fake_audit_lifecycle(ctx, *, workflow):
        rec = type("R", (), {"outcome": ""})()
        try:
            yield rec
        finally:
            captured.append(rec.outcome)

    monkeypatch.setattr(fix_module, "audit_lifecycle", fake_audit_lifecycle)

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    assert captured == ["pr_opened:https://github.com/o/r/pull/9"]


# ---------- Tests-failed path (carries truncation check) ----------


@pytest.mark.asyncio
async def test_comments_with_truncated_output_when_tests_failed(monkeypatch):
    sandbox = _FakeSandbox()
    adapter = _adapter()
    huge = "X" * 50_000

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(
            attempt=_attempt(tests_passed=False, test_output=huge),
        )

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    adapter.create_branch.assert_not_called()
    adapter.open_pull_request.assert_not_called()
    posted = adapter.reply.call_args.args[1]
    assert "tests did not pass" in posted.lower()
    assert "truncated" in posted.lower()
    assert len(posted) < 10_000  # well under GitHub's 65k cap


# ---------- Failure paths (parametrized over the stage that explodes) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_phrase", "expect_pr_attempt"),
    [
        ("get_issue", "could not read", False),
        ("clone", "clone", False),
        ("agent", "agent failed", False),
        ("create_branch", "branch", False),
        ("push", "push", False),
        ("open_pull_request", "could not open", True),
    ],
)
async def test_failure_in_stage_yields_tailored_comment(
    monkeypatch, stage: str, expected_phrase: str, expect_pr_attempt: bool,
):
    """One parametrized case per stage. Setup is shared — only the
    failing dependency varies, plus the user-visible comment phrase.
    """
    sandbox: _FakeSandbox = _FakeSandbox()
    adapter_overrides: dict[str, Any] = {}

    if stage == "get_issue":
        adapter_overrides["get_issue"] = AsyncMock(side_effect=RuntimeError("404"))
    elif stage == "clone":
        class ExplodingClone(_FakeSandbox):
            async def clone(self, *args, **kwargs) -> None:
                raise RuntimeError("clone refused")

        sandbox = ExplodingClone()
    elif stage == "agent":
        async def fake_generate(*, sandbox, event, adapter, issue):
            raise RuntimeError("agent imploded")

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)
    elif stage == "create_branch":
        adapter_overrides["create_branch"] = AsyncMock(
            side_effect=RuntimeError("ref exists"),
        )
    elif stage == "push":
        class PushFail(_FakeSandbox):
            async def commit_and_push(self, *args, **kwargs) -> None:
                raise RuntimeError("auth failed")

        sandbox = PushFail()
    elif stage == "open_pull_request":
        adapter_overrides["open_pull_request"] = AsyncMock(
            side_effect=RuntimeError("validation_failed"),
        )

    if stage != "agent":
        async def fake_generate(*, sandbox, event, adapter, issue):
            return FixOutcome(attempt=_attempt(tests_passed=True))

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    adapter = _adapter(**adapter_overrides)
    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    # Sandbox CM cleanup ran unless we never entered it (get_issue path).
    if stage == "get_issue":
        assert sandbox.cloned == []
    else:
        assert sandbox.closed is True

    if not expect_pr_attempt:
        adapter.open_pull_request.assert_not_called()

    assert expected_phrase in adapter.reply.call_args.args[1].lower()
```

- [ ] **Step 3: Verify the tests fail**

```bash
pytest tests/application/use_cases/test_fix.py -v
```

Expected: most tests fail (the ACK stub has none of these surfaces).

- [ ] **Step 4: Write the rewritten use case**

```python
# openbot/application/use_cases/fix.py
"""Issue → PR fix workflow — PRD §4.3 end-to-end pipeline.

Flow:
  1. Fetch the issue body and base commit from GitHub.
  2. Open a sandbox via ``ctx.sandbox_factory()``.
  3. Clone the repo with an installation-scoped token.
  4. Run ``DeepAgentsFixResponder`` to produce a ``FixOutcome``.
  5. Tests passed → push branch, open PR, comment with PR URL.
     Tests failed → comment with the truncated test output.
     Any step raised → comment with the corresponding error template.

The whole pipeline runs inside ``audit_lifecycle`` so the workflow
phase transitions get logged uniformly with review/triage. No path
raises out of this function — every failure becomes a comment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openbot.application.use_cases._lifecycle import audit_lifecycle
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.fix import FixOutcome
from openbot.domain.workflows import Workflow
from openbot.infrastructure.agents import DeepAgentsFixResponder

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)

# GitHub comments hard-cap at 65_536 chars. 4_000 keeps headers + URLs
# + diff snippets fitting when the agent emits verbose test output.
_MAX_TEST_OUTPUT_CHARS = 4_000

_NO_SANDBOX = (
    ":robot: OpenBot can't run the fix loop: the sandbox is not configured "
    "on this deployment. The maintainer needs to set "
    "`OPENBOT_DAYTONA_API_KEY` (or another sandbox backend) before "
    "automated fixes will work."
)
_ISSUE_READ_FAIL = (
    ":warning: OpenBot could not read this issue from GitHub. The fix loop "
    "was skipped. The error has been logged."
)
_CLONE_FAIL = (
    ":warning: OpenBot opened a sandbox but could not clone the repository. "
    "The fix loop was skipped. The error has been logged."
)
_AGENT_FAIL = (
    ":warning: OpenBot's fix agent failed before producing a result. "
    "The error has been logged; please re-assign the issue to retry."
)
_BRANCH_CONFLICT = (
    ":warning: OpenBot finished the fix locally but could not create the "
    "branch on GitHub (it may already exist). The error has been logged."
)
_PUSH_FAIL = (
    ":warning: OpenBot finished the fix locally but could not push the "
    "branch to GitHub. The error has been logged."
)
_OPEN_PR_FAIL = (
    ":warning: OpenBot pushed the fix branch but could not open the pull "
    "request. The error has been logged."
)
_TESTS_FAILED_HEADER = (
    ":x: OpenBot attempted a fix but the tests did not pass.\n\n"
    "**Summary:** {summary}\n"
    "**Command:** `{cmd}`\n\n"
    "<details><summary>Test output</summary>\n\n```\n{output}\n```\n</details>"
)
_PR_OPENED = (
    ":sparkles: OpenBot opened a fix for issue #{issue}: {url}\n\n"
    "**Summary:** {summary}\n"
    "**Files changed:** {files}\n"
    "**Test command:** `{cmd}`"
)


def _truncate(text: str, *, limit: int = _MAX_TEST_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n\n[truncated — {dropped} bytes of test output omitted]"


def _inject_token(clone_url: str, token: str) -> str:
    """Embed the installation token in an HTTPS clone URL.

    GitHub accepts ``https://x-access-token:<TOKEN>@host/owner/repo.git``
    as a credential carrier. Refuse non-HTTPS — the token is a bearer
    secret and inserting it into ``ssh://`` or ``file://`` leaks it.
    """
    if not clone_url.startswith("https://"):
        raise ValueError(f"fix_clone_url_not_https:{clone_url[:32]}")
    return f"https://x-access-token:{token}@{clone_url[len('https://'):]}"


def _short_sha(sha: str) -> str:
    return sha[:7] if sha else "nosha"


def _branch_name(*, issue_number: int, base_sha: str) -> str:
    return f"openbot/fix-issue-{issue_number}-{_short_sha(base_sha)}"


def _pr_title(issue_title: str, issue_number: int) -> str:
    head = (issue_title or "").strip().replace("\n", " ")
    if not head:
        head = f"Fix for issue #{issue_number}"
    return f"[OpenBot] {head}"[:240]  # GitHub caps PR titles at 256.


def _pr_body(*, attempt_summary: str, issue_number: int, test_command: str) -> str:
    return (
        f"Closes #{issue_number}.\n\n"
        f"**Summary:** {attempt_summary}\n"
        f"**Test command:** `{test_command}`\n\n"
        "_Generated by OpenBot. Reviewer should still read the diff before merging._"
    )


def _files_changed_str(files: tuple[str, ...]) -> str:
    if not files:
        return "(none reported)"
    if len(files) <= 5:
        return ", ".join(f"`{f}`" for f in files)
    head = ", ".join(f"`{f}`" for f in files[:5])
    return f"{head}, …and {len(files) - 5} more"


async def _generate_fix_outcome(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    issue: dict[str, Any],
) -> FixOutcome:
    """Module-level seam — E2E tests monkeypatch this to skip DeepAgents.

    Mirrors ``_generate_review_findings`` in ``review.py``. Keeping the
    responder construction inside a top-level coroutine means tests can
    replace just this one function without touching the orchestration
    code that wraps it.
    """
    responder = DeepAgentsFixResponder()
    return await responder.fix_for_event(
        event, adapter=adapter, sandbox=sandbox, issue=issue,
    )


@_traceable(run_type="chain", name="fix")
async def maybe_run_fix(ctx: PreflightContext) -> None:
    event = ctx.event
    if event.issue_number is None or event.installation_id is None:
        _logger.info(
            "fix_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    adapter = ctx.adapter
    issue_number = event.issue_number

    if ctx.sandbox_factory is None:
        await _safe_reply(adapter, event, _NO_SANDBOX)
        return

    async with audit_lifecycle(ctx, workflow=Workflow.FIX) as audit:
        try:
            issue = await adapter.get_issue(event, issue_number)
        except Exception:
            _logger.exception("fix_get_issue_failed", extra=_log_extra(event))
            await _safe_reply(adapter, event, _ISSUE_READ_FAIL)
            audit.outcome = "get_issue_failed"
            return

        clone_url = str(issue.get("clone_url", ""))
        base_sha = str(issue.get("base_sha", ""))
        default_branch = str(issue.get("default_branch", "main"))

        try:
            token = await adapter.get_installation_token(event)
            authed_url = _inject_token(clone_url, token)
        except Exception:
            _logger.exception("fix_token_failed", extra=_log_extra(event))
            await _safe_reply(adapter, event, _CLONE_FAIL)
            audit.outcome = "token_failed"
            return

        async with ctx.sandbox_factory() as sandbox:
            try:
                await sandbox.clone(authed_url, ref=base_sha)
            except Exception:
                _logger.exception("fix_clone_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _CLONE_FAIL)
                audit.outcome = "clone_failed"
                return

            try:
                outcome = await _generate_fix_outcome(
                    sandbox=sandbox,
                    event=event,
                    adapter=adapter,
                    issue=issue,
                )
            except Exception:
                _logger.exception("fix_agent_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _AGENT_FAIL)
                audit.outcome = "agent_failed"
                return

            if not outcome.attempt.tests_passed:
                await _safe_reply(
                    adapter,
                    event,
                    _TESTS_FAILED_HEADER.format(
                        summary=outcome.attempt.summary,
                        cmd=outcome.attempt.test_command,
                        output=_truncate(outcome.attempt.test_output),
                    ),
                )
                audit.outcome = "tests_failed"
                return

            branch = _branch_name(issue_number=issue_number, base_sha=base_sha)
            branch_ref = f"refs/heads/{branch}"

            try:
                await adapter.create_branch(event, branch_ref, base_sha)
            except Exception:
                _logger.exception("fix_create_branch_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _BRANCH_CONFLICT)
                audit.outcome = "create_branch_failed"
                return

            try:
                await sandbox.commit_and_push(
                    branch=branch,
                    message=f"openbot: fix #{issue_number}",
                    remote_url=authed_url,
                )
            except Exception:
                _logger.exception("fix_push_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _PUSH_FAIL)
                audit.outcome = "push_failed"
                return

            try:
                pr = await adapter.open_pull_request(
                    event,
                    title=_pr_title(str(issue.get("title", "")), issue_number),
                    body=_pr_body(
                        attempt_summary=outcome.attempt.summary,
                        issue_number=issue_number,
                        test_command=outcome.attempt.test_command,
                    ),
                    head=branch,
                    base=default_branch,
                )
            except Exception:
                _logger.exception("fix_open_pr_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _OPEN_PR_FAIL)
                audit.outcome = "open_pr_failed"
                return

            pr_url = str(pr.get("html_url", ""))
            await _safe_reply(
                adapter,
                event,
                _PR_OPENED.format(
                    issue=issue_number,
                    url=pr_url,
                    summary=outcome.attempt.summary,
                    files=_files_changed_str(outcome.attempt.files_changed),
                    cmd=outcome.attempt.test_command,
                ),
            )
            audit.outcome = f"pr_opened:{pr_url}"


async def _safe_reply(
    adapter: ChannelAdapterPort, event: UnifiedEvent, message: str
) -> None:
    """Post a comment, swallowing any errors. The use case contract is
    "the fix workflow never raises out of pre-flight." Comment posting
    is best-effort UX — substantive work and audit are already recorded.
    """
    try:
        await adapter.reply(event, message)
    except Exception:
        _logger.exception("fix_reply_failed", extra=_log_extra(event))


def _log_extra(event: UnifiedEvent) -> dict[str, Any]:
    return {
        "delivery_id": event.delivery_id,
        "repo": event.repo,
        "issue_number": event.issue_number,
    }


__all__ = ["maybe_run_fix"]
```

- [ ] **Step 5: Verify the tests pass**

```bash
pytest tests/application/use_cases/test_fix.py -v
```

Expected: 11 passed (5 non-parametrized tests + 2 `field_to_drop`
expansions + 6 `stage` expansions = 11 total).

- [ ] **Step 6: Wire `sandbox_factory` in DI**

Find where `PreflightContext` is constructed for fix dispatches —
`grep -rn 'PreflightContext(' openbot/entrypoints openbot/application`.
Pass:

```python
sandbox_factory=lambda: DaytonaSandboxAdapter.create(settings=settings),
```

…for the fix path; leave `None` for non-fix dispatches. If wiring is
not yet in place, leave a `# TODO(C.9 DI)` marker so C.9 picks it up.
The unit tests above do not require DI wiring — they pass a factory
directly into `PreflightContext`.

- [ ] **Step 7: `make check`**

```bash
make check
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add openbot/application/use_cases/fix.py \
        openbot/application/middleware/preflight.py \
        tests/application/use_cases/test_fix.py
git commit -m "feat(fix): slice C.8 — end-to-end fix loop with sandbox + PR"
```

---

## Notes for reviewers (C.8 only)

1. **One seam, not many.** `_generate_fix_outcome` is the single
   patch-point E2E tests use. Adding per-stage seams would double the
   test surface for no gain.

2. **Per-stage templates, not a generic "fix failed".** When the user
   sees the comment on their issue they need to know what to do next:
   re-run? add credentials? open a maintainer issue? Per-stage
   templates are deliberate — small upfront cost, large ongoing UX win.

3. **Audit `outcome` strings are log-stable.** `pr_opened:<url>`,
   `tests_failed`, `clone_failed`, etc. are scraped by Prometheus
   dashboards through the audit pipeline. Don't change the prefixes
   without coordinating with ops.

4. **No prompt-quality assertions in `tests/`.** The tests above
   assert control flow and user-visible comment phrases. They do NOT
   assert anything about agent reasoning or prompt wording —
   CLAUDE.md §forbidden routes those to `evals/`.

---

**Continue with `2026-05-20-fix-deepagent-slice-c-part8.md`** for task C.9
(E2E demo 08, `RecordingGitHubAdapter` pr_creates field, and the
slice-C status line update in
`docs/superpowers/plans/2026-05-20-review-fix-deepagent.md`).
