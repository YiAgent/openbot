# Slice C — Fix workflow end-to-end (part 8: E2E demo + finalization)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Continues from:** `2026-05-20-fix-deepagent-slice-c-part7.md` (use case rewrite).
**Closes:** slice C.

Task C.9 wires the new fix loop into the E2E harness. The existing
demo 03 (which asserts on the old ACK template) is rewritten to use the
real loop. A new demo 10 exercises the tests-failed branch. The
`RecordingGitHubAdapter` gains four new methods (`get_issue`,
`create_branch`, `open_pull_request`, `get_installation_token`) plus a
`pr_creates` recording field. Finally, the slice-C status line in
`docs/superpowers/plans/2026-05-20-review-fix-deepagent.md` flips to
"complete".

---

## Task C.9: E2E — demo 03 rewrite + demo 10 (tests failed)

**Files:**
- Modify: `tests/e2e/conftest.py` (extend `RecordingGitHubAdapter` + add
  `_fake_fix_outcome` monkeypatch + a sandbox factory)
- Modify: `tests/e2e/test_spec_demos.py` (rewrite demo 03, add demo 10)
- Modify: `docs/superpowers/plans/2026-05-20-review-fix-deepagent.md`
  (status line)

### Why two demos (happy path + tests-failed)

The fix loop has two terminal outcomes the user actually observes:

  1. **PR opened** (tests passed) — demo 03 now asserts on the PR
     creation recording, not on the old ACK template.
  2. **Comment posted** (tests failed) — demo 10 asserts the agent
     attempted the fix but the test output was reflected back to the
     user without opening a PR.

Other failure paths (clone failed, push failed, etc.) are already
covered in the use case unit tests (C.8 parametrize). E2E tests carry
the contract that "the pre-flight + use case + GitHub adapter wire up
correctly"; they should not re-test every branch the use case already
tests, but they MUST exercise both terminal observable outcomes.

### Why a separate sandbox factory for E2E

The unit tests in C.8 inject a `_FakeSandbox` directly. E2E tests need
a factory that returns the same kind of fake sandbox so the `async
with ctx.sandbox_factory() as sandbox:` line in `maybe_run_fix` runs
to completion. We don't go through `DaytonaSandboxAdapter` here —
that's covered by adapter unit tests in C.5. The E2E harness has the
job of "wire up the pieces"; the pieces have their own tests.

### Pre-read

  - `tests/e2e/conftest.py` lines 58-200 — current
    `RecordingGitHubAdapter` shape; we'll add four methods + one
    recording field.
  - `tests/e2e/conftest.py` lines 311-334 — existing `_fake_*`
    monkeypatches (`_fake_load_for_repo`, `_fake_chat_reply`,
    `_fake_review_findings`); demo 03's new shape adds
    `_fake_fix_outcome` next to them.
  - `tests/e2e/test_spec_demos.py` lines 110-133 — current demo 03 (the
    one we're rewriting).

### TDD steps

- [ ] **Step 1: Extend `RecordingGitHubAdapter` (conftest.py)**

Edit `tests/e2e/conftest.py`. In the `__init__` body of
`RecordingGitHubAdapter`, append next to `self.pr_reviews`:

```python
        # Slice-C fix loop — recorded write-backs for assertion in demo 03/10.
        self.branch_creates: list[tuple[str, str, str]] = []  # (repo, ref, sha)
        self.pr_creates: list[dict[str, Any]] = []  # POST /pulls
        # Test-controlled issue dict returned by ``get_issue``. Defaults
        # to a minimal happy-path shape so happy-path demos work without
        # extra setup; tests override via ``harness.adapter.fake_issue``.
        self.fake_issue: dict[str, Any] = {
            "title": "Off-by-one in pagination",
            "body": "Last item is dropped when total %% page_size == 0.",
            "base_sha": "abc1234567",
            "default_branch": "main",
            "clone_url": "https://github.com/acme/test-repo.git",
        }
```

Then add four method overrides. Place them after `create_pr_review`
(around line 162) and before `_installation_token`:

```python
    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        """Return the test-controlled fake_issue dict."""
        return dict(self.fake_issue)

    async def create_branch(
        self, event: UnifiedEvent, branch_ref: str, from_sha: str
    ) -> None:
        """Record a branch creation; tests assert via ``branch_creates``."""
        self.branch_creates.append((event.repo, branch_ref, from_sha))

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        """Record the POST /pulls call; demo 03 asserts on the recording."""
        record = {
            "repo": event.repo,
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        self.pr_creates.append(record)
        # GitHub's POST /pulls returns a PR object; the use case reads
        # ``html_url`` to embed in the success comment.
        return {
            "id": 30_000 + len(self.pr_creates),
            "html_url": f"https://github.com/{event.repo}/pull/{30_000 + len(self.pr_creates)}",
            **record,
        }

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Return the same fake token ``_installation_token`` uses."""
        return "fake-installation-token"
```

- [ ] **Step 2: Add a sandbox factory + fix-outcome monkeypatch**

Still in `tests/e2e/conftest.py`. Near the top, after the imports, add
a small fake sandbox that satisfies the use case's contract (`clone`,
`commit_and_push`, `close`). Place it next to `RecordingGitHubAdapter`
or in a small helper section above the harness class:

```python
@dataclass
class FakeSandbox:
    """E2E sandbox stand-in. The use case calls ``clone`` then —
    if tests passed in the (monkeypatched) responder — ``commit_and_push``.
    Tests assert via ``cloned`` and ``pushed``.
    """

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
```

Add a `sandbox` field on `WebhookHarness` (next to `adapter`/`redis`):

```python
    sandbox: FakeSandbox = field(default_factory=FakeSandbox)
```

In the `webhook_harness` fixture body (around line 311-334, after
`_fake_review_findings`), wire the sandbox factory and the fix-outcome
stub:

```python
    from contextlib import asynccontextmanager

    from openbot.domain.fix import FixAttempt as _FixAttempt
    from openbot.domain.fix import FixOutcome as _FixOutcome

    @asynccontextmanager
    async def _sandbox_factory():
        # Single shared FakeSandbox per test so demos can assert on its
        # state. The factory returns the same instance each call (the
        # use case only opens one sandbox per event).
        try:
            yield harness.sandbox
        finally:
            await harness.sandbox.close()

    harness.sandbox_factory_override = _sandbox_factory  # used by dispatch

    async def _fake_fix_outcome(*, sandbox, event, adapter, issue):
        # Default: tests passed. Demos that need the tests-failed branch
        # override ``harness.adapter.fake_issue`` via the existing tunable
        # plus toggle ``harness.fix_outcome_tests_passed = False``.
        return _FixOutcome(
            attempt=_FixAttempt(
                summary=f"DeepAgents fix summary for issue #{event.issue_number}",
                files_changed=("src/api/list.py",),
                tests_passed=harness.fix_outcome_tests_passed,
                test_command="pytest -q",
                test_output=(
                    "3 passed in 0.05s"
                    if harness.fix_outcome_tests_passed
                    else "1 failed, 2 passed in 0.05s\nFAILED tests/test_api.py::test_list"
                ),
                diff="diff --git a/src/api/list.py b/src/api/list.py\n",
            ),
        )

    monkeypatch.setattr(
        "openbot.application.use_cases.fix._generate_fix_outcome",
        _fake_fix_outcome,
    )
```

Add a default on the harness so demos can toggle it:

```python
    fix_outcome_tests_passed: bool = True
    sandbox_factory_override: Any = None
```

In `WebhookHarness.dispatch`, pass the sandbox factory:

```python
        await run_dispatch(
            adapter=self.adapter,
            event=event,
            dispatch=decision,
            session_factory=self.session_factory,
            redis=self.redis,
            rate_limiter=RedisRateLimiter(self.redis),
            sandbox_factory=self.sandbox_factory_override,
        )
```

If `run_dispatch` does not accept `sandbox_factory` yet — that is the
DI wiring step from C.8. Add the parameter to `run_dispatch` and have
it forward into `PreflightContext(sandbox_factory=...)`. The parameter
must default to `None` to keep the other demos working.

- [ ] **Step 3: Rewrite demo 03 to assert on the PR creation**

In `tests/e2e/test_spec_demos.py`, replace the body of
`test_demo_03_bot_assigned_fix_stub` (lines 110-133). Rename it to
`test_demo_03_bot_assigned_fix_opens_pr` to reflect the new contract:

```python
# ─────────────── demo 03: bot-assigned fix opens a PR ───────────────


async def test_demo_03_bot_assigned_fix_opens_pr(
    webhook_harness: WebhookHarness,
) -> None:
    """Issue assigned to the bot → FIX workflow opens a PR.

    The harness monkeypatches ``_generate_fix_outcome`` so DeepAgents
    is never invoked — what we assert is the wiring around it: the
    sandbox is cloned with the installation token, a branch is
    created from the base SHA, the PR is opened with ``Closes #N`` in
    the body, and the user gets a final comment with the PR URL.
    """
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_ASSIGNED,
        delivery_id="d-fix-1",
        issue_number=11,
        raw={"assignee": {"type": "Bot", "login": "openbot[bot]"}},
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-fix-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.FIX for row in rows)

    # Sandbox was cloned with an x-access-token URL pointing at the
    # base SHA from the fake_issue dict.
    assert webhook_harness.sandbox.cloned == [
        (
            "https://x-access-token:fake-installation-token@github.com/acme/test-repo.git",
            "abc1234567",
        ),
    ]

    # Branch was created with the predictable openbot/fix-issue-N-SHORTSHA pattern.
    assert len(webhook_harness.adapter.branch_creates) == 1
    repo, branch_ref, from_sha = webhook_harness.adapter.branch_creates[0]
    assert repo == "acme/test-repo"
    assert branch_ref.startswith("refs/heads/openbot/fix-issue-11-")
    assert "abc1234" in branch_ref
    assert from_sha == "abc1234567"

    # PR was opened with Closes-#N in the body.
    assert len(webhook_harness.adapter.pr_creates) == 1
    pr = webhook_harness.adapter.pr_creates[0]
    assert pr["base"] == "main"
    assert pr["head"].startswith("openbot/fix-issue-11-")
    assert "Closes #11" in pr["body"]
    assert pr["title"].startswith("[OpenBot]")

    # The user-facing comment carries the PR URL.
    assert len(webhook_harness.adapter.replies) == 1
    _, number, body = webhook_harness.adapter.replies[0]
    assert number == 11
    assert "https://github.com/acme/test-repo/pull/" in body
```

- [ ] **Step 4: Add demo 10 (tests failed)**

Append to `tests/e2e/test_spec_demos.py` (after `test_demo_09_*` near
the bottom):

```python
# ─────────────── demo 10: fix attempt with failing tests ───────────────


async def test_demo_10_bot_assigned_fix_tests_failed_yields_comment(
    webhook_harness: WebhookHarness,
) -> None:
    """When the (stubbed) agent reports tests_passed=False, the loop
    must comment with the truncated test output and NOT open a PR.

    This is the second observable outcome of the fix loop. Per-stage
    failure paths (clone failed, push failed, etc.) are covered in
    the use case unit tests (C.8 parametrize) — this demo carries the
    contract that the tests-failed terminal also routes correctly
    through the pre-flight chain + audit pipeline.
    """
    webhook_harness.fix_outcome_tests_passed = False

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_ASSIGNED,
        delivery_id="d-fix-10",
        issue_number=22,
        raw={"assignee": {"type": "Bot", "login": "openbot[bot]"}},
    )
    await webhook_harness.dispatch(event)

    # Workflow still completed (this is a successful agent run with a
    # bad-test outcome — not a workflow error).
    rows = await webhook_harness.audit_rows(delivery_id="d-fix-10")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.FIX for row in rows)

    # Sandbox was cloned but no branch/PR was attempted.
    assert webhook_harness.sandbox.cloned and webhook_harness.sandbox.cloned[0][1] == "abc1234567"
    assert webhook_harness.adapter.branch_creates == []
    assert webhook_harness.adapter.pr_creates == []

    # User got the tests-failed comment with the test output snippet.
    assert len(webhook_harness.adapter.replies) == 1
    _, number, body = webhook_harness.adapter.replies[0]
    assert number == 22
    assert "tests did not pass" in body.lower()
    assert "1 failed" in body
```

- [ ] **Step 5: Verify the demos pass**

```bash
pytest tests/e2e/test_spec_demos.py -v
```

Expected: 10 passed (demo 01–09 unchanged behavior + new demo 10;
demo 03 keeps the same number but tests a different assertion shape).

If any unrelated demo breaks because of the `sandbox_factory`
parameter on `run_dispatch`, the parameter default must be `None` and
the dispatcher must handle `None` by falling through to the existing
`PreflightContext(sandbox_factory=None)` shape — i.e. the new wiring
must be backward-compatible.

- [ ] **Step 6: Update the slice-C status line**

Edit `docs/superpowers/plans/2026-05-20-review-fix-deepagent.md`. Find
the existing slice-tracker section (search for `Slice C`); update the
status from `pending` (or `in progress`) to `complete`, and stamp the
date. For example:

```markdown
- **Slice C — Fix workflow end-to-end:** complete (2026-05-20)
  - C.1 FixAttempt + FixOutcome domain types
  - C.2 Pydantic schema bridge
  - C.3 SandboxPort growth + FakeSandboxAdapter
  - C.4 Channel adapter additions (get_issue, create_branch, open_pull_request, get_installation_token)
  - C.5 DaytonaSandboxAdapter
  - C.6 make_fix_tools factory
  - C.7 DeepAgentsFixResponder
  - C.8 maybe_run_fix end-to-end pipeline
  - C.9 E2E demos (03 rewritten, 10 added)
```

(Adjust the exact wording to match the existing tracker style. The
file may use a checklist (`- [x]`) shape instead — preserve whatever
convention is already in place. The goal is for a future reader
opening the tracker to know slice C is done.)

- [ ] **Step 7: Final full-suite check**

```bash
make check
```

Expected: all green. This is the final gate — if any test outside
slice C breaks, undo the offending change rather than `--no-verify`
the commit.

- [ ] **Step 8: Commit**

```bash
git add tests/e2e/conftest.py \
        tests/e2e/test_spec_demos.py \
        docs/superpowers/plans/2026-05-20-review-fix-deepagent.md
git commit -m "feat(fix): slice C.9 — E2E demos for fix loop (PR + tests-failed)"
```

---

## Slice-C close-out checklist

After the C.9 commit lands, verify:

- [ ] `make check` is green from a fresh checkout.
- [ ] `git log --oneline` since the slice-C kickoff shows nine commits
  (C.1 through C.9), each with the `feat(fix): slice C.N — ...`
  prefix.
- [ ] No commit was made with `--no-verify` (`git log` body should
  not contain a `[skip ci]` or pre-commit-bypass note).
- [ ] No tests under `tests/` assert on prompt wording or agent
  reasoning quality — those belong in `evals/` per PRD §8.3. Spot
  check `tests/infrastructure/agents/test_deepagents_fix.py` and
  `tests/application/use_cases/test_fix.py` for any "the agent should
  reason about X" assertions.
- [ ] Slice-C tracker line in
  `docs/superpowers/plans/2026-05-20-review-fix-deepagent.md` is
  marked complete with date.
- [ ] No code under `openbot/` imports from `evals/`. Run
  `grep -rn 'from evals' openbot` — should return empty.
- [ ] `OPENBOT_SANDBOX_BACKEND` is unchanged from PRD default
  (`daytona`); production sandbox path uses `DaytonaSandboxAdapter`
  from C.5, not the eval-side `evals.sandboxes.factory`.

---

## Notes for reviewers (C.9 only)

1. **Demo 03 changed contract.** Previous demo 03 asserted on the ACK
   template body. The new one asserts on the PR creation + final
   comment URL. This is a deliberate behavior change — the ACK is
   gone (replaced by the real fix loop). Future demos should mirror
   demo 03 shape (assert on the *observable side effect*, not on an
   intermediate template) when they exercise a workflow's terminal
   outcome.

2. **Demo 10 is intentionally narrow.** It does NOT cover the
   per-stage failure templates from C.8. Adding one demo per stage
   would duplicate the use case parametrize. The contract demo 10
   carries is: "the tests-failed terminal routes through pre-flight
   + audit correctly and produces a non-PR observable outcome."

3. **`fix_outcome_tests_passed` is the only test-side toggle.**
   Resist adding more knobs to the harness. If a future demo needs a
   richer fake outcome (e.g. specific files_changed), set
   `harness.adapter.fake_issue` and replace `_fake_fix_outcome`
   inline in that test, rather than expanding the shared fixture.

4. **`pr_creates` recording is the contract.** Any future tests that
   need to assert "a PR was opened" should read
   `harness.adapter.pr_creates`. Do not add a parallel `last_pr` or
   `pr_url` field — one source of truth, list-shaped, in insertion
   order.

---

**Slice C complete.** No further parts. The implementation plan now
spans 8 markdown files (parts 1–8) under
`docs/superpowers/plans/2026-05-20-fix-deepagent-slice-c-part*.md`,
each runnable independently by a fresh subagent via
`superpowers:subagent-driven-development` (recommended) or
`superpowers:executing-plans` (batch + checkpoints).
