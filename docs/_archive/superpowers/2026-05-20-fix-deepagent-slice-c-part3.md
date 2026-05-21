# Slice C — Fix workflow end-to-end (part 3: channel adapter additions)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Picks up from:** part 1 (C.1 domain + C.2 schema), part 2 (C.3 sandbox port + fake).
**Continues in:** part 4 (C.5 Daytona + C.6 fix tools), part 5 (C.7 responder + C.8 use case + C.9 E2E).

This part adds the four channel-adapter methods the fix use case needs to
read the issue, create a fix branch, open the PR, and hand the sandbox a
push token:

| Port method | GitHub REST endpoint(s) | Caller |
|---|---|---|
| `get_issue(event, issue_number)` | `GET /repos/{r}/issues/{n}`, `GET /repos/{r}/issues/{n}/comments`, `GET /repos/{r}` | use case (C.8) |
| `create_branch(event, branch_ref, from_sha)` | `POST /repos/{r}/git/refs` | use case (C.8) |
| `open_pull_request(event, *, title, body, head, base)` | `POST /repos/{r}/pulls` | use case (C.8) |
| `get_installation_token(event)` | reuses `_installation_token` internally | use case (C.8) |

The use case needs the raw token string so it can hand it to the sandbox
for `git push https://x-access-token:{token}@github.com/...`. The
adapter's existing `_authed_json` resolves the same token internally for
its own REST calls; exposing a thin port method on top of that keeps the
use case agnostic of `InstallationToken` (the dataclass returned by
`openbot.infrastructure.adapters.github_auth.GitHubAppAuth.installation_token`).

---

## Task C.4: ChannelAdapterPort + GitHubAdapter additions

**Files:**
- Modify: `openbot/application/ports/channel_adapter.py` — add 4 abstract methods at the end of the `ChannelAdapterPort` Protocol.
- Modify: `openbot/infrastructure/adapters/github.py` — add 4 implementations before the `_authed_json` helper (lines ~556).
- Modify: `tests/infrastructure/adapters/test_github.py` — add 8 tests using the existing `adapter_factory` fixture.
- Modify: `tests/_fakes/channel_adapter.py` — add 4 stub implementations with recording fields.

### Why on the channel-adapter port (not on a new "git" port)

The fix loop reads an issue (channel concept), opens a PR (channel concept),
and asks the channel for a push token (channel concept). Splitting these
across a separate `GitPort` would force `maybe_run_fix` to take two
dependencies that always travel together. The slice B port already owns
`reply` / `add_label` / `create_pr_review`, so PR creation lives next to
them naturally.

### Why `get_issue` is one batched call, not three separate methods

Three round-trips on every fix attempt — issue body, comments, default
branch — would be needless latency. The single port method bundles them
behind one boundary; the GitHub adapter makes three HTTP calls under the
hood, but callers see one coherent issue snapshot.

### Why `get_installation_token` returns a raw `str`, not `InstallationToken`

The use case only needs the bearer string for URL interpolation. Returning
`InstallationToken` would force the application layer to import an
infrastructure type, which import-linter would reject. The token's
expiry is irrelevant to the use case — `_installation_token` already
caches and refreshes it inside the adapter.

---

### Step 1: Write the failing port tests + adapter tests

- [ ] **Step 1.1: Add port-shape contract checks** to `tests/infrastructure/adapters/test_github.py` near the end of the existing write-back block.

Append after the last existing test (use the same `adapter_factory` fixture, `_event`, `_FakeAuth`, and `_INSTALL_TOKEN` already defined in the file):

```python
# ───── get_issue ─────


async def test_get_issue_batches_three_calls(adapter_factory: Any) -> None:
    """Issue body + comments + default-branch repo metadata fetched in one batch."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/issues/42") and not url.endswith("/comments"):
            return httpx.Response(
                200,
                json={
                    "title": "Bug: pagination off-by-one",
                    "body": "Page 2 returns rows 9-17 instead of 10-19.",
                },
            )
        if url.endswith("/issues/42/comments"):
            return httpx.Response(
                200,
                json=[
                    {"user": {"login": "alice"}, "body": "Repro on main."},
                    {"user": {"login": "bob"}, "body": "Looks like LIMIT/OFFSET swap."},
                ],
            )
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(
                200,
                json={"default_branch": "main", "clone_url": "https://github.com/YiAgent/openbot.git"},
            )
        if url.endswith("/repos/YiAgent/openbot/git/ref/heads/main"):
            return httpx.Response(
                200,
                json={"object": {"sha": "deadbeefcafebabe1234567890abcdef12345678"}},
            )
        raise AssertionError(f"unexpected url: {url}")

    adapter, captured = adapter_factory(handler, auth=_FakeAuth())
    issue = await adapter.get_issue(_event(issue_number=42), 42)

    assert issue["title"] == "Bug: pagination off-by-one"
    assert issue["body"].startswith("Page 2")
    assert issue["comments"] == [
        {"author": "alice", "body": "Repro on main."},
        {"author": "bob", "body": "Looks like LIMIT/OFFSET swap."},
    ]
    assert issue["default_branch"] == "main"
    assert issue["clone_url"] == "https://github.com/YiAgent/openbot.git"
    assert issue["base_sha"] == "deadbeefcafebabe1234567890abcdef12345678"
    # 4 requests: issue, comments, repo metadata, default-branch ref
    assert len(captured) == 4


async def test_get_issue_raises_on_404(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Not Found"}),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.get_issue(_event(issue_number=999), 999)


async def test_get_issue_handles_empty_body_and_no_comments(adapter_factory: Any) -> None:
    """Issue with `body: null` and zero comments still produces a valid dict."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/issues/7") and not url.endswith("/comments"):
            return httpx.Response(200, json={"title": "thin", "body": None})
        if url.endswith("/issues/7/comments"):
            return httpx.Response(200, json=[])
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(
                200,
                json={"default_branch": "trunk", "clone_url": "https://github.com/YiAgent/openbot.git"},
            )
        if "/git/ref/heads/trunk" in url:
            return httpx.Response(200, json={"object": {"sha": "abc1234567890"}})
        raise AssertionError(f"unexpected url: {url}")

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    issue = await adapter.get_issue(_event(issue_number=7), 7)

    assert issue["title"] == "thin"
    assert issue["body"] == ""  # null body normalized to empty string
    assert issue["comments"] == []
    assert issue["default_branch"] == "trunk"


# ───── create_branch ─────


async def test_create_branch_posts_git_refs(adapter_factory: Any) -> None:
    """POST /repos/{r}/git/refs with body {ref, sha}."""
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(
            201, json={"ref": "refs/heads/openbot/fix-issue-42-deadbee", "object": {"sha": "deadbee"}}
        ),
        auth=_FakeAuth(),
    )

    await adapter.create_branch(
        _event(),
        "openbot/fix-issue-42-deadbee",
        from_sha="deadbeefcafebabe1234567890abcdef12345678",
    )

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/git/refs"
    body = json.loads(req.content)
    # GitHub expects the *full ref path*, not the short name.
    assert body == {
        "ref": "refs/heads/openbot/fix-issue-42-deadbee",
        "sha": "deadbeefcafebabe1234567890abcdef12345678",
    }


async def test_create_branch_raises_on_422_conflict(adapter_factory: Any) -> None:
    """422 = branch already exists. Caller surfaces a tailored comment."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(
            422, json={"message": "Reference already exists"}
        ),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.create_branch(_event(), "openbot/fix-issue-42-deadbee", from_sha="deadbee")
    assert exc_info.value.response.status_code == 422


# ───── open_pull_request ─────


async def test_open_pull_request_posts_pulls(adapter_factory: Any) -> None:
    """draft=False is implicit (GitHub default) — fix loop never opens drafts."""
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(
            201,
            json={
                "number": 123,
                "html_url": "https://github.com/YiAgent/openbot/pull/123",
                "head": {"ref": "openbot/fix-issue-42-deadbee"},
            },
        ),
        auth=_FakeAuth(),
    )

    pr = await adapter.open_pull_request(
        _event(),
        title="Fix #42: pagination off-by-one",
        body="Closes #42\n\nSwapped LIMIT and OFFSET.",
        head="openbot/fix-issue-42-deadbee",
        base="main",
    )

    assert pr["html_url"] == "https://github.com/YiAgent/openbot/pull/123"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/pulls"
    body = json.loads(req.content)
    assert body == {
        "title": "Fix #42: pagination off-by-one",
        "body": "Closes #42\n\nSwapped LIMIT and OFFSET.",
        "head": "openbot/fix-issue-42-deadbee",
        "base": "main",
    }
    # draft must not be in the payload — never set explicitly, never True.
    assert "draft" not in body


async def test_open_pull_request_raises_on_422_no_diff(adapter_factory: Any) -> None:
    """No-commit branch → 422 "No commits between base and head".
    Use case surfaces this as the "tests passed but no changes" path."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(
            422,
            json={"message": "Validation Failed", "errors": [{"message": "No commits between main and ..."}]},
        ),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.open_pull_request(
            _event(), title="t", body="b", head="x", base="main"
        )
    assert exc_info.value.response.status_code == 422


# ───── get_installation_token ─────


async def test_get_installation_token_returns_raw_string(adapter_factory: Any) -> None:
    """The application layer never sees InstallationToken — only the raw bearer."""
    auth = _FakeAuth()
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=auth)

    token = await adapter.get_installation_token(_event())

    assert token == _INSTALL_TOKEN
    assert auth.calls == [_INSTALL_ID]
```

- [ ] **Step 1.2: Run the new tests to verify they fail with `AttributeError`.**

Run: `uv run pytest tests/infrastructure/adapters/test_github.py -k "get_issue or create_branch or open_pull_request or get_installation_token" -v`
Expected: 8 failures, each one `AttributeError: 'GitHubAdapter' object has no attribute '...'`.

---

### Step 2: Grow `ChannelAdapterPort`

- [ ] **Step 2.1: Add 4 methods to the port** at `openbot/application/ports/channel_adapter.py`.

Append at the end of the `ChannelAdapterPort` Protocol class body (after `create_pr_review`):

```python
    async def get_issue(
        self,
        event: UnifiedEvent,
        issue_number: int,
    ) -> dict[str, Any]:
        """Return a normalized snapshot of an issue.

        Shape (all keys always present):
            {
              "title": str,
              "body": str,                # "" if GitHub returned null
              "comments": list[dict],     # [{"author": str, "body": str}, ...]
              "base_sha": str,            # default branch HEAD
              "default_branch": str,
              "clone_url": str,
            }

        Implementations batch the underlying calls (issue + comments +
        repo metadata + branch ref). Raises ``httpx.HTTPStatusError`` on
        any non-2xx (the use case catches the 404 case to skip silently).
        """
        ...

    async def create_branch(
        self,
        event: UnifiedEvent,
        branch_ref: str,
        from_sha: str,
    ) -> None:
        """Create a new branch ``branch_ref`` pointing at ``from_sha``.

        ``branch_ref`` is the short ref (e.g. ``openbot/fix-issue-42-deadbee``);
        implementations prepend ``refs/heads/`` for GitHub's git-refs endpoint.

        Raises ``httpx.HTTPStatusError(422)`` if the branch already exists —
        the use case surfaces that as "open fix attempt already pending".
        """
        ...

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        """Open a non-draft pull request from ``head`` into ``base``.

        Returns the GitHub PR object (``number``, ``html_url``, ``head``, ...).
        Always non-draft (PRD §13 #2 — fix loop never opens speculative PRs).
        Raises on HTTP error so the use case can post a tailored comment.
        """
        ...

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Return a short-lived push token for the event's installation.

        The use case interpolates this into the clone/push URL:
        ``https://x-access-token:{token}@github.com/{repo}.git``. Token is
        opaque, single-use from the use case's perspective; adapter caches
        and refreshes it internally.

        Raises if the adapter was constructed without auth (e.g., webhook-
        only mode without a GitHub App).
        """
        ...
```

- [ ] **Step 2.2: Verify the port file still imports cleanly.**

Run: `uv run python -c "from openbot.application.ports.channel_adapter import ChannelAdapterPort; print(ChannelAdapterPort)"`
Expected: prints the protocol class, no errors.

---

### Step 3: Implement on `GitHubAdapter`

- [ ] **Step 3.1: Add the four methods** to `openbot/infrastructure/adapters/github.py`, inserted just before `_authed_json` (the existing private helper section). Order: `get_issue`, `create_branch`, `open_pull_request`, `get_installation_token`.

```python
    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        """Return a normalized issue snapshot for the fix loop.

        Three GitHub calls (issue, comments, repo metadata) + one for the
        branch ref so we know the SHA to fork the fix branch from. Issued
        sequentially through ``_authed_json`` so the existing retry +
        rate-limit-headroom logging fires for each.
        """
        base = f"{self._api_base}/repos/{event.repo}"
        issue = await self._authed_json("GET", f"{base}/issues/{issue_number}", event)
        comments_raw = await self._authed_json(
            "GET", f"{base}/issues/{issue_number}/comments?per_page=100", event
        )
        repo_meta = await self._authed_json("GET", base, event)
        default_branch = (
            str(repo_meta.get("default_branch") or "main")
            if isinstance(repo_meta, dict)
            else "main"
        )
        ref = await self._authed_json(
            "GET", f"{base}/git/ref/heads/{default_branch}", event
        )
        base_sha = (
            str(ref["object"]["sha"])
            if isinstance(ref, dict) and isinstance(ref.get("object"), dict)
            else ""
        )

        comments: list[dict[str, str]] = []
        if isinstance(comments_raw, list):
            for c in comments_raw:
                if not isinstance(c, dict):
                    continue
                user = c.get("user") if isinstance(c.get("user"), dict) else {}
                comments.append(
                    {
                        "author": str(user.get("login") or "unknown"),
                        "body": str(c.get("body") or ""),
                    }
                )

        return {
            "title": str(issue.get("title") or "") if isinstance(issue, dict) else "",
            "body": str(issue.get("body") or "") if isinstance(issue, dict) else "",
            "comments": comments,
            "base_sha": base_sha,
            "default_branch": default_branch,
            "clone_url": (
                str(repo_meta.get("clone_url") or f"https://github.com/{event.repo}.git")
                if isinstance(repo_meta, dict)
                else f"https://github.com/{event.repo}.git"
            ),
        }

    async def create_branch(
        self,
        event: UnifiedEvent,
        branch_ref: str,
        from_sha: str,
    ) -> None:
        """POST /repos/{r}/git/refs — prepend ``refs/heads/`` (GitHub requires it)."""
        url = f"{self._api_base}/repos/{event.repo}/git/refs"
        await self._authed_json(
            "POST",
            url,
            event,
            json_body={"ref": f"refs/heads/{branch_ref}", "sha": from_sha},
        )

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        """POST /repos/{r}/pulls — never sends ``draft`` (default False)."""
        url = f"{self._api_base}/repos/{event.repo}/pulls"
        return await self._authed_json(
            "POST",
            url,
            event,
            json_body={"title": title, "body": body, "head": head, "base": base},
        )

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Expose the raw bearer for sandbox push URLs (see port docstring)."""
        token = await self._installation_token(event)
        return str(token.token)
```

- [ ] **Step 3.2: Run the 8 new tests.**

Run: `uv run pytest tests/infrastructure/adapters/test_github.py -k "get_issue or create_branch or open_pull_request or get_installation_token" -v`
Expected: 8 passes.

- [ ] **Step 3.3: Verify the full existing adapter test suite still passes.**

Run: `uv run pytest tests/infrastructure/adapters/test_github.py -v`
Expected: all green (previous tests + 8 new).

---

### Step 4: Extend `FakeChannelAdapter` with recording fields

The fakes layer needs stubs so the use-case tests (C.8) can assert
on what got called. Mirror the existing pattern (recording lists +
`@dataclass(field(default_factory=...))`).

- [ ] **Step 4.1: Add 5 fields and 4 method stubs** to `tests/_fakes/channel_adapter.py`.

In the `@dataclass class FakeChannelAdapter:` body, after the existing
`pr_reviews` field, add:

```python
    # ── Slice C fix-loop recording fields ──
    issue_lookups: list[tuple[str | None, int]] = field(default_factory=list)
    branch_creates: list[tuple[str | None, str, str]] = field(default_factory=list)
    pr_creates: list[dict[str, Any]] = field(default_factory=list)
    token_lookups: list[str | None] = field(default_factory=list)
    fake_issue: dict[str, Any] = field(
        default_factory=lambda: {
            "title": "stub issue",
            "body": "stub body",
            "comments": [],
            "base_sha": "0" * 40,
            "default_branch": "main",
            "clone_url": "https://github.com/example/repo.git",
        }
    )
    fake_pr: dict[str, Any] = field(
        default_factory=lambda: {
            "number": 1,
            "html_url": "https://github.com/example/repo/pull/1",
        }
    )
    fake_installation_token: str = "fake-install-token"
```

Then at the bottom of the class (after `create_pr_review`), add:

```python
    async def get_issue(
        self, event: UnifiedEvent, issue_number: int
    ) -> dict[str, Any]:
        self.issue_lookups.append((event.resource_key, issue_number))
        # Return a *copy* so test mutations don't poison the shared default.
        return dict(self.fake_issue)

    async def create_branch(
        self, event: UnifiedEvent, branch_ref: str, from_sha: str
    ) -> None:
        self.branch_creates.append((event.resource_key, branch_ref, from_sha))

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        record = {
            "resource_key": event.resource_key,
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        self.pr_creates.append(record)
        return dict(self.fake_pr)

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        self.token_lookups.append(event.resource_key)
        return self.fake_installation_token
```

- [ ] **Step 4.2: Verify the fake still type-matches the port.**

Run: `uv run python -c "from tests._fakes.channel_adapter import FakeChannelAdapter; from openbot.application.ports.channel_adapter import ChannelAdapterPort; assert isinstance(FakeChannelAdapter(), ChannelAdapterPort)"`
Expected: no output (silent success — `runtime_checkable` protocol passes).

If `isinstance` reports False, run `mypy tests/_fakes/channel_adapter.py` to see which method's signature drifted.

---

### Step 5: Wire-up checks

- [ ] **Step 5.1: Run the slice-B regression suite.**

Run: `uv run pytest tests/infrastructure/adapters tests/_fakes -v`
Expected: all green. The 8 new tests sit alongside the existing
write-back tests; FakeChannelAdapter still satisfies the protocol.

- [ ] **Step 5.2: Run import-linter to confirm no hexagonal contract drift.**

Run: `uv run lint-imports`
Expected: all contracts pass. The new port methods live in
`openbot/application/ports/`, impls in `openbot/infrastructure/adapters/`.

- [ ] **Step 5.3: Full make check.**

Run: `make check`
Expected: fmt + lint + tests all green. New test count = previous + 8.

---

### Step 6: Commit C.4

- [ ] **Step 6.1: Stage and commit.**

```bash
git add openbot/application/ports/channel_adapter.py \
        openbot/infrastructure/adapters/github.py \
        tests/infrastructure/adapters/test_github.py \
        tests/_fakes/channel_adapter.py
git commit -m "feat(channel-adapter): add get_issue / create_branch / open_pull_request / get_installation_token (slice C.4)

Slice C step 4 of 9. Lays the channel surface the fix use case needs:

- get_issue(event, n) → normalized {title, body, comments, base_sha,
  default_branch, clone_url} batched across 4 REST calls.
- create_branch(event, ref, from_sha) → POST /git/refs with full
  refs/heads/ prefix. 422 on conflict surfaces to caller for tailored
  comment.
- open_pull_request(event, *, title, body, head, base) → POST /pulls,
  never draft (PRD §13 #2 — no speculative PRs).
- get_installation_token(event) → raw bearer string for sandbox
  x-access-token push URL; application layer never sees InstallationToken.

FakeChannelAdapter gains matching recording fields (issue_lookups,
branch_creates, pr_creates, token_lookups) so use-case tests can assert
side-effects. 8 new httpx.MockTransport tests bring the adapter total
to {previous + 8} with no real network."
```

- [ ] **Step 6.2: Verify the commit is clean.**

Run: `git log --oneline -1 && git diff HEAD~1 HEAD --stat`
Expected: one commit listing exactly the 4 modified files above.

---

## C.4 acceptance checks

- [ ] `make check` green.
- [ ] `tests/infrastructure/adapters/test_github.py` has 8 new tests; all pass.
- [ ] `FakeChannelAdapter` satisfies `ChannelAdapterPort` at runtime
      (`isinstance(..., ChannelAdapterPort)` is True).
- [ ] No reference to `InstallationToken` outside `openbot/infrastructure/`.
- [ ] `lint-imports` reports zero violations.
- [ ] Adapter never sends `draft=True` (assertion in test).

---

## Heads-up for part 4 (C.5 + C.6)

Part 4 builds on the port shape locked here:

- The Daytona adapter (C.5) consumes `clone_url` and the installation
  token (via the use case, not directly — adapter only sees the URL
  passed to `clone`).
- The fix tools (C.6) close over the sandbox port, not the channel
  adapter. The channel adapter is the use case's concern.

If anything in the port shape changes during implementation, fix C.4
first (and re-run its tests) before touching C.5.
