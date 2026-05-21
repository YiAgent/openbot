# Slice C — Fix workflow end-to-end (part 4: Daytona sandbox adapter)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Picks up from:** part 3 (C.4 channel adapter additions).
**Continues in:** part 5 (C.6 fix tools), part 6 (C.7–C.8 responder + use case), part 7 (C.9 E2E demo + finalization).

This part lands the production-side implementation of `SandboxPort`
(grown in C.3): `DaytonaSandboxAdapter` at
`openbot/infrastructure/sandboxes/daytona.py`. The C.3 fake handles
unit testing; this Daytona adapter is what runs in production when a
real GitHub webhook fires.

Note: this is the *production fix loop* sandbox port, **not** the same
protocol the eval suite uses (`evals.sandboxes.factory.SandboxBackend`).
They share Daytona as a backend but their interfaces are intentionally
distinct — do NOT import from `evals/sandboxes/factory.py` here.

---

## Task C.5: DaytonaSandboxAdapter (production fix-loop sandbox)

**Files:**
- Create: `openbot/infrastructure/sandboxes/daytona.py`
- Test: `tests/infrastructure/sandboxes/test_daytona.py`
- Modify: `openbot/core/settings.py` (+2 fields: `daytona_api_key`, `daytona_server_url`)
- Modify: `openbot/infrastructure/sandboxes/__init__.py` (+1 export)

### Why a separate module from `evals/sandboxes/daytona_backend.py`

The eval `daytona_backend.py` implements `deepagents.backends.sandbox.BaseSandbox`
(Inspect AI's per-sample tool surface). The production fix loop implements
our own `SandboxPort` (defined in C.3). The two protocols differ in:
- file API shape (`read_file(path) -> str` vs Inspect's
  `FileDownloadResponse` envelope)
- run() return type (`ExecResult` frozen dataclass vs Inspect's
  `ExecuteResponse`)
- lifecycle (production gets cloned-repo seed + push-back; evals get
  pre-cooked image + diff capture)

Trying to share the impl behind one base class would force one side to
accept inappropriate semantics. Keep them separate; share only the
Daytona SDK access pattern.

### Why lazy import + `@lru_cache(maxsize=1)` client

`daytona` is an optional dependency (PRD §3 alternatives doc). Importing
the production sandbox module at app boot must not require the package.
Caching the client avoids re-authenticating on each fix attempt — the
Daytona SDK holds a connection pool internally; rebuilding it per webhook
wastes seconds the agent could be using.

### Why `clone` runs `git` via `sandbox.process.exec`, not an SDK method

Daytona's SDK has no first-class "clone" call. We exec `git clone`
inside the sandbox so the credential never leaves the sandbox boundary
and the SDK only sees opaque shell strings.

### Subprocess safety note

The Daytona SDK's `sandbox.process.exec(command, timeout)` accepts a
shell-string command — we *always* compose those strings with `shlex.quote`
on every interpolated value so untrusted ref names or repo URLs cannot
inject extra shell syntax. None of the calls here use Python's
`subprocess` module or `child_process.exec`; the exec happens inside
the remote Daytona sandbox, not on the OpenBot host.

---

### Step 1: Add Daytona settings to `openbot/core/settings.py`

- [ ] **Step 1.1: Append two new fields** to the `Settings` class body, after the persistence/identity sections. Use the existing `Field(default=None, description=...)` pattern.

```python
    # ─── Sandbox (production fix loop — distinct from eval sandbox) ───
    daytona_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Daytona API key for the production fix-loop sandbox. "
            "Distinct from the eval-suite Daytona key (which is configured "
            "via DAYTONA_API_KEY at the evals/ layer). If unset, the fix "
            "responder fails closed and the use case posts a tailored "
            "comment instead of attempting a fix."
        ),
    )
    daytona_server_url: str | None = Field(
        default=None,
        description=(
            "Override the default Daytona endpoint. Leave unset to use "
            "Daytona's public API."
        ),
    )
```

- [ ] **Step 1.2: Verify settings still loads.**

Run: `uv run python -c "from openbot.core.settings import Settings; s = Settings(); print(s.daytona_api_key, s.daytona_server_url)"`
Expected: `None None` (defaults — no Daytona configured in test env).

---

### Step 2: Write the failing Daytona adapter tests

- [ ] **Step 2.1: Create `tests/infrastructure/sandboxes/test_daytona.py`.**

```python
"""DaytonaSandboxAdapter — production fix-loop sandbox.

Tests mock the daytona SDK via monkeypatch; no real network. The SDK
surface we exercise is intentionally narrow:
  client.create(params) -> sandbox
  client.delete(sandbox) -> None
  sandbox.process.exec(command, timeout) -> response{exit_code, result}
  sandbox.files.upload(path, content) -> None
  sandbox.files.download(path) -> bytes
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter


@pytest.fixture
def fake_sandbox() -> Any:
    """A Daytona sandbox-shaped MagicMock with the methods we call."""
    sb = MagicMock()
    sb.id = "sandbox-abc"
    sb.process = MagicMock()
    sb.files = MagicMock()
    # Default exec returns success
    sb.process.exec.return_value = SimpleNamespace(
        exit_code=0, result="", additional_properties={}
    )
    return sb


@pytest.fixture
def fake_client(fake_sandbox: Any) -> Any:
    client = MagicMock()
    client.create.return_value = fake_sandbox
    client.delete.return_value = None
    return client


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_client: Any) -> DaytonaSandboxAdapter:
    """Construct an adapter with the daytona module patched out."""
    fake_daytona_mod = SimpleNamespace(
        Daytona=lambda config=None: fake_client,
        DaytonaConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        CreateSandboxFromImageParams=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "openbot.infrastructure.sandboxes.daytona._get_daytona_module",
        lambda: fake_daytona_mod,
    )
    return DaytonaSandboxAdapter._for_test(client=fake_client)


# ───── create / close lifecycle ─────


async def test_create_provisions_sandbox(
    monkeypatch: pytest.MonkeyPatch, fake_client: Any
) -> None:
    monkeypatch.setattr(
        "openbot.infrastructure.sandboxes.daytona._get_daytona_module",
        lambda: SimpleNamespace(
            Daytona=lambda config=None: fake_client,
            DaytonaConfig=lambda **kw: SimpleNamespace(**kw),
            CreateSandboxFromImageParams=lambda **kw: SimpleNamespace(**kw),
        ),
    )
    monkeypatch.setenv("OPENBOT_DAYTONA_API_KEY", "test-key")
    adapter = await DaytonaSandboxAdapter.create()
    assert fake_client.create.called
    assert adapter.workspace.startswith("/")
    await adapter.close()
    assert fake_client.delete.called


async def test_close_swallows_delete_errors(
    adapter: DaytonaSandboxAdapter, fake_client: Any
) -> None:
    """close() must never raise — sandbox leaks are logged, not propagated."""
    fake_client.delete.side_effect = RuntimeError("daytona transient 500")
    await adapter.close()  # should not raise


# ───── clone ─────


async def test_clone_execs_git_with_x_access_token_url(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    """Token must be injected into the clone URL — never logged separately."""
    await adapter.clone(
        repo_url="https://github.com/YiAgent/openbot.git",
        ref="main",
        token="ghs_xxx",
    )
    # The exec call list contains the git clone with token-in-URL.
    calls = [str(call.args[0]) for call in fake_sandbox.process.exec.call_args_list]
    assert any("git clone" in c for c in calls)
    assert any("x-access-token:ghs_xxx@github.com/YiAgent/openbot.git" in c for c in calls)


async def test_clone_rejects_non_https_repo_url(adapter: DaytonaSandboxAdapter) -> None:
    """SSH or file URLs would bypass token-in-URL injection — reject loudly."""
    with pytest.raises(ValueError, match="https"):
        await adapter.clone(
            repo_url="git@github.com:YiAgent/openbot.git",
            ref="main",
            token="ghs_xxx",
        )


# ───── run / file IO ─────


async def test_run_normalizes_exec_response_to_exec_result(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    fake_sandbox.process.exec.return_value = SimpleNamespace(
        exit_code=2, result="boom\n", additional_properties={}
    )
    result = await adapter.run(command=["pytest"], timeout_seconds=30)
    assert isinstance(result, ExecResult)
    assert result.exit_code == 2
    # Daytona returns combined stdout+stderr in `result`; we surface it as stdout
    # and leave stderr empty so callers can rely on `(stdout or stderr)` being
    # the human-readable buffer.
    assert result.stdout == "boom\n"
    assert result.stderr == ""
    assert result.timed_out is False


async def test_run_flags_timeout_via_additional_properties(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    """Daytona sets `additional_properties.timed_out` when exec hits the deadline."""
    fake_sandbox.process.exec.return_value = SimpleNamespace(
        exit_code=124, result="", additional_properties={"timed_out": True}
    )
    result = await adapter.run(command=["sleep", "999"], timeout_seconds=1)
    assert result.timed_out is True
    assert result.exit_code == 124
```

- [ ] **Step 2.2: Run the test file** to verify all 6 tests fail with `ModuleNotFoundError`.

Run: `uv run pytest tests/infrastructure/sandboxes/test_daytona.py -v`
Expected: 6 failures, each one `ModuleNotFoundError: No module named 'openbot.infrastructure.sandboxes.daytona'`.

---

### Step 3: Implement `DaytonaSandboxAdapter`

- [ ] **Step 3.1: Create `openbot/infrastructure/sandboxes/daytona.py`.**

```python
"""Daytona implementation of SandboxPort (production fix loop).

Distinct from ``evals/sandboxes/daytona_backend.py`` (which implements
the deepagents BaseSandbox surface used by the eval harness). Both use
the same Daytona SDK; their port shapes differ on purpose — see
``openbot/application/ports/sandbox.py`` for the production contract.

The ``daytona`` Python package is an *optional* dependency: this module
imports the SDK lazily so an OpenBot deployment without a fix loop
(triage / review / chat only) doesn't need it installed.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from openbot.application.ports.sandbox import ExecResult, SandboxPort
from openbot.core.settings import Settings

_logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.11-slim"
_WORKSPACE = "/workspace/repo"
_INSTALL_GIT_SCRIPT = (
    "set -euo pipefail; "
    "if ! command -v git >/dev/null; then "
    "  (apt-get update -qq && apt-get install -y --no-install-recommends "
    "    git ca-certificates >/dev/null 2>&1) || "
    "  (command -v apk >/dev/null && apk add --no-cache git >/dev/null 2>&1) || "
    "  (echo 'WARNING: could not install git' >&2; exit 1); "
    "fi"
)


def _get_daytona_module() -> Any:
    """Import :mod:`daytona` lazily — production fix loop is opt-in."""
    try:
        import daytona  # type: ignore[import-not-found]
    except ImportError as cause:
        raise RuntimeError(
            "daytona SDK is required for the production fix-loop sandbox. "
            "Install via `uv add daytona` and set OPENBOT_DAYTONA_API_KEY."
        ) from cause
    return daytona


@lru_cache(maxsize=1)
def _build_client(api_key: str, server_url: str | None) -> Any:
    """One client per process — pooled connections, single auth handshake."""
    daytona_mod = _get_daytona_module()
    config_kwargs: dict[str, Any] = {"api_key": api_key}
    if server_url:
        config_kwargs["api_url"] = server_url
    config = daytona_mod.DaytonaConfig(**config_kwargs)
    return daytona_mod.Daytona(config)


def _inject_token(repo_url: str, token: str) -> str:
    """Build the x-access-token push URL. HTTPS-only by design.

    Refusing non-HTTPS prevents an SSH or file URL from silently bypassing
    the credential injection (and leaking a clone of a repo we don't own).
    """
    if not repo_url.startswith("https://"):
        raise ValueError(
            f"DaytonaSandboxAdapter.clone requires an https repo_url, got: {repo_url!r}"
        )
    # https://github.com/X/Y.git -> https://x-access-token:TOKEN@github.com/X/Y.git
    return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)


@dataclass
class DaytonaSandboxAdapter(SandboxPort):
    """Per-event production fix-loop sandbox.

    Use via the async-context-manager wrapper produced by the
    ``sandbox_factory`` DI hook (see C.8). Direct construction outside
    tests is discouraged.
    """

    _client: Any
    _sandbox: Any
    workspace: str = _WORKSPACE

    # ── lifecycle ──

    @classmethod
    async def create(cls, *, settings: Settings | None = None) -> DaytonaSandboxAdapter:
        """Provision a fresh Daytona sandbox and return the adapter."""
        s = settings if settings is not None else Settings()
        if s.daytona_api_key is None:
            raise RuntimeError(
                "OPENBOT_DAYTONA_API_KEY is unset — the production fix loop "
                "cannot create a sandbox. Configure via env var or .env."
            )
        client = _build_client(
            s.daytona_api_key.get_secret_value(),
            s.daytona_server_url,
        )
        daytona_mod = _get_daytona_module()
        params = daytona_mod.CreateSandboxFromImageParams(image=_DEFAULT_IMAGE)
        sandbox = await asyncio.to_thread(client.create, params)
        # Ensure git is present and the workspace exists.
        await asyncio.to_thread(sandbox.process.exec, _INSTALL_GIT_SCRIPT, 60)
        await asyncio.to_thread(
            sandbox.process.exec, f"mkdir -p {shlex.quote(_WORKSPACE)}", 10
        )
        return cls(_client=client, _sandbox=sandbox)

    @classmethod
    def _for_test(cls, *, client: Any) -> DaytonaSandboxAdapter:
        """Constructor used by unit tests — bypasses real create()."""
        sandbox = client.create()
        return cls(_client=client, _sandbox=sandbox)

    async def close(self) -> None:
        """Best-effort sandbox teardown — never raises."""
        sb_id = getattr(self._sandbox, "id", "unknown")
        try:
            await asyncio.to_thread(self._client.delete, self._sandbox)
        except Exception:
            _logger.warning(
                "daytona_sandbox_delete_failed",
                extra={"sandbox_id": sb_id},
                exc_info=True,
            )

    # ── git ──

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        url = _inject_token(repo_url, token)
        clone_cmd = (
            f"cd {shlex.quote(self.workspace)} && "
            f"git clone --depth=1 --branch={shlex.quote(ref)} "
            f"{shlex.quote(url)} ."
        )
        # 5 minute timeout — generous for repos up to a few hundred MB.
        response = await asyncio.to_thread(self._sandbox.process.exec, clone_cmd, 300)
        if response.exit_code not in (0, None):
            raise RuntimeError(
                f"daytona git clone failed (exit_code={response.exit_code}): "
                f"{response.result!r}"
            )

    async def git_diff(self) -> str:
        diff_cmd = f"cd {shlex.quote(self.workspace)} && git diff"
        response = await asyncio.to_thread(self._sandbox.process.exec, diff_cmd, 30)
        return response.result or ""

    async def commit_and_push(
        self, *, branch_ref: str, message: str, token: str
    ) -> None:
        # Re-derive the remote URL inside the sandbox so we don't need to
        # carry repo_url through the use case. `git remote get-url origin`
        # returns the token-bearing URL we cloned with — that token may be
        # expired by the time we push, so we rewrite it with the *fresh*
        # token before pushing.
        get_url = (
            f"cd {shlex.quote(self.workspace)} && git remote get-url origin"
        )
        url_resp = await asyncio.to_thread(self._sandbox.process.exec, get_url, 10)
        if url_resp.exit_code not in (0, None):
            raise RuntimeError(f"git remote get-url failed: {url_resp.result!r}")
        old_url = (url_resp.result or "").strip()
        if "@" in old_url and old_url.startswith("https://"):
            tail = old_url.split("@", 1)[1]
            new_url = f"https://x-access-token:{token}@{tail}"
        else:
            new_url = _inject_token(old_url, token)
        push_script = (
            f"cd {shlex.quote(self.workspace)} && "
            "git config user.email 'openbot[bot]@users.noreply.github.com' && "
            "git config user.name 'openbot[bot]' && "
            f"git checkout -b {shlex.quote(branch_ref)} && "
            "git add -A && "
            f"git commit -m {shlex.quote(message)} && "
            f"git push {shlex.quote(new_url)} HEAD:{shlex.quote(branch_ref)}"
        )
        push_resp = await asyncio.to_thread(self._sandbox.process.exec, push_script, 120)
        if push_resp.exit_code not in (0, None):
            raise RuntimeError(
                f"daytona push failed (exit_code={push_resp.exit_code}): "
                f"{push_resp.result!r}"
            )

    # ── file IO ──

    async def read_file(self, path: str) -> str:
        full = f"{self.workspace}/{path}" if not path.startswith("/") else path
        try:
            data = await asyncio.to_thread(self._sandbox.files.download, full)
        except Exception:
            return ""
        if isinstance(data, bytes):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return ""
        return str(data or "")

    async def write_file(self, path: str, content: str) -> None:
        full = f"{self.workspace}/{path}" if not path.startswith("/") else path
        await asyncio.to_thread(
            self._sandbox.files.upload, full, content.encode("utf-8")
        )

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        target = (
            f"{self.workspace}/{path}"
            if not path.startswith("/") and path not in (".", "")
            else self.workspace
        )
        cmd = (
            f"cd {shlex.quote(target)} && "
            f"find . -type f | head -n {int(max)}"
        )
        response = await asyncio.to_thread(self._sandbox.process.exec, cmd, 15)
        if response.exit_code not in (0, None):
            return []
        return [line for line in (response.result or "").splitlines() if line.strip()]

    # ── run ──

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        env_prefix = ""
        if env:
            env_prefix = (
                " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items())
                + " "
            )
        # The argv list arrives shell-quoted, so the composed string is
        # injection-safe even when individual args contain spaces or quotes.
        joined = " ".join(shlex.quote(a) for a in command)
        cmd = f"cd {shlex.quote(self.workspace)} && {env_prefix}{joined}"
        response = await asyncio.to_thread(
            self._sandbox.process.exec, cmd, timeout_seconds
        )
        extras: dict[str, Any] = getattr(response, "additional_properties", {}) or {}
        exit_code = (
            response.exit_code if response.exit_code is not None else int(extras.get("code") or 0)
        )
        return ExecResult(
            stdout=str(response.result or ""),
            stderr="",
            exit_code=int(exit_code),
            timed_out=bool(extras.get("timed_out", False)),
        )


__all__ = ["DaytonaSandboxAdapter"]
```

- [ ] **Step 3.2: Export from the sandboxes package.** Modify `openbot/infrastructure/sandboxes/__init__.py`:

```python
"""Sandbox adapters — production fix loop."""

from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter
from openbot.infrastructure.sandboxes.fake import FakeSandboxAdapter

__all__ = ["DaytonaSandboxAdapter", "FakeSandboxAdapter"]
```

- [ ] **Step 3.3: Run the 6 Daytona tests.**

Run: `uv run pytest tests/infrastructure/sandboxes/test_daytona.py -v`
Expected: 6 passes.

- [ ] **Step 3.4: Confirm the optional-import path.** Module import must succeed even without `daytona` installed; only SDK call paths raise.

Run: `uv run python -c "from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter; print('ok')"`
Expected: `ok` — module import doesn't trigger SDK import.

---

### Step 4: Commit C.5

- [ ] **Step 4.1: Verify the full check passes.**

Run: `make check`
Expected: all green; sandbox test count up by 6.

- [ ] **Step 4.2: Stage and commit.**

```bash
git add openbot/core/settings.py \
        openbot/infrastructure/sandboxes/daytona.py \
        openbot/infrastructure/sandboxes/__init__.py \
        tests/infrastructure/sandboxes/test_daytona.py
git commit -m "feat(sandboxes): add DaytonaSandboxAdapter (slice C.5)

Production implementation of SandboxPort for the fix loop. Lazy
\`daytona\` SDK import so OpenBot deployments without a fix loop don't
need the package installed.

- _get_daytona_module() raises a tailored error if the SDK is missing.
- _build_client() caches one client per process (pooled connections,
  single auth handshake).
- clone() injects the installation token via the
  https://x-access-token:TOKEN@github.com/... pattern; rejects non-HTTPS
  URLs so SSH/file URLs cannot silently bypass credential injection.
- run() normalizes Daytona's ExecuteResponse to our ExecResult
  (combined stdout+stderr in .stdout; .timed_out from additional_properties).
- close() never raises — leak warnings only.

Two new settings fields (daytona_api_key, daytona_server_url) gated by
OPENBOT_ env prefix; the production fix-loop credentials are distinct
from the eval-suite DAYTONA_API_KEY consumed at evals/ layer.

6 unit tests use MagicMock + SimpleNamespace to stand in for the SDK;
no real network."
```

- [ ] **Step 4.3: Verify the commit is clean.**

Run: `git log --oneline -1 && git diff HEAD~1 HEAD --stat`
Expected: one commit listing exactly the 4 files above.

---

## C.5 acceptance checks

- [ ] `make check` green after the commit.
- [ ] `tests/infrastructure/sandboxes/test_daytona.py` has 6 tests; all pass.
- [ ] `DaytonaSandboxAdapter` module imports without `daytona` installed
      (only SDK call paths raise).
- [ ] `_inject_token` rejects non-HTTPS repo URLs (covered by test).
- [ ] No imports from `evals/` anywhere in `openbot/infrastructure/sandboxes/`.
- [ ] `lint-imports` passes.

---

## Heads-up for part 5 (C.6 fix tools)

C.6 builds `make_fix_tools(*, sandbox, event, budget=None)` —
six `StructuredTool`s that close over a `SandboxPort` instance. They
will exercise *both* the C.3 fake (in tests via a `_StubSandbox`) and
the C.5 Daytona adapter (in production). If the `SandboxPort` shape
needs adjustment during C.6 implementation, fix C.3 first and rerun
its tests; C.5's adapter implements the same methods so its tests
will surface the drift.
