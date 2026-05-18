# Phase 2c — Remaining Ports + Empty Ignore List (Tasks 2.8 – 2.11)

> Continues from [`phase-2b`](2026-05-18-hexagonal-restructure-phase-2b-state-ports.md). Final phase before [`Phase 3`](2026-05-18-hexagonal-restructure-phase-3-tests.md).

**Goal of Phase 2c:** introduce the last four Ports (`RateLimiterPort`, `ConfigLoaderPort`, `LLMPort`, `SandboxPort`) and verify the `.importlinter` `ignore_imports` list has shrunk to **only the allowed `application.ports.dedup -> infrastructure.persistence.dedup` Port→leaf-enum exception** documented in Phase 2a Task 2.1.

`LLMPort` and `SandboxPort` are defined but have NO Phase-2 consumer — they exist as a contract so the future agent-slice plugs in cleanly. Each gets a Protocol + Fake + contract test but no consumer-switch step.

4 tasks, 4 commits. Total of 12 commits across Phase 2 (2.0 + 2.1 – 2.11).

---

## Task 2.8: `RateLimiterPort`

**Spec mapping:** rate-limit middleware currently calls `redis.zadd` inline. The Port abstracts the "check + record" pair so the middleware doesn't import Redis.

**Files:**
- Create: `openbot/application/ports/rate_limiter.py`
- Create: `openbot/infrastructure/persistence/rate_limiter_redis.py`
- Modify: `openbot/application/middleware/rate_limit.py` — call via Port
- Modify: `openbot/entrypoints/api/app.py` — attach
- Modify: `openbot/application/ports/__init__.py` — re-export
- Modify: `.importlinter` — remove any narrow `middleware.rate_limit -> persistence.*` ignore added in Phase 2b
- Create: `tests/_fakes/rate_limiter.py`
- Create: `tests/application/ports/test_rate_limiter_port_contract.py`

- [ ] **Step 1: Inspect the current rate-limit middleware**

```bash
sed -n '1,80p' openbot/application/middleware/rate_limit.py
```

Identify the function that takes a Redis client and decides allow/deny. Typical shape:

```python
async def check(redis, key, *, limit: int, window_seconds: int) -> bool
```

- [ ] **Step 2: Write `application/ports/rate_limiter.py`**

```python
"""RateLimiterPort — sliding-window allow/deny."""
from __future__ import annotations

from typing import Protocol


class RateLimiterPort(Protocol):
    """Sliding-window rate limiter keyed by an arbitrary string.

    Returns True if the request is allowed; False if it exceeded the limit
    for `window_seconds`. Fail-open semantics on backend error (consistent
    with WebhookDedup behavior).
    """

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool: ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.rate_limiter import RateLimiterPort  # noqa: F401
```

- [ ] **Step 3: Write `infrastructure/persistence/rate_limiter_redis.py`**

```python
"""Redis-backed RateLimiterPort using a ZSET sliding window."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import redis.asyncio as redis_async

if TYPE_CHECKING:
    from openbot.application.ports.rate_limiter import RateLimiterPort

_logger = logging.getLogger("openbot.rate_limiter")


class RedisRateLimiter:
    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        if self._redis is None:
            return True  # fallback-open

        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_seconds * 1000
        try:
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {str(now_ms): now_ms})
            pipe.zcard(key)
            pipe.expire(key, window_seconds)
            results = await pipe.execute()
            count = results[2]
            return count <= limit
        except Exception:
            _logger.exception("rate_limiter_redis_error_fail_open", extra={"key": key})
            return True  # fail-open


if TYPE_CHECKING:
    _witness: "RateLimiterPort" = RedisRateLimiter(redis=None)
```

If the existing middleware uses a different algorithm (token bucket, etc.), mirror that algorithm here. The point is to encapsulate the Redis calls, not to redesign the limiter.

- [ ] **Step 4: Switch the middleware**

Edit `openbot/application/middleware/rate_limit.py`:
- Constructor accepts `rate_limiter: RateLimiterPort` instead of a Redis client.
- Every call site of `redis.zadd(...)` (or the inline check) becomes `await self._rate_limiter.check(key, limit=..., window_seconds=...)`.
- Remove every `import redis` / `import redis.asyncio` from the middleware file.

Update construction in `entrypoints/api/app.py`:

```python
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter
# ...
app.state.rate_limiter = RedisRateLimiter(redis)
```

- [ ] **Step 5: Delete the ignore line**

Edit `.importlinter` — remove the narrow ignore for `middleware.rate_limit` if it was added in Phase 2b. The broader `middleware.* -> persistence.*` ignore should already be gone after Phase 2b Task 2.7.

- [ ] **Step 6: Write `tests/_fakes/rate_limiter.py`**

```python
"""FakeRateLimiter — counts calls per key, deterministic allow/deny."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class FakeRateLimiter:
    """Per-key call counter; returns False after the per-key allowance is exhausted."""

    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    async def check(
        self, key: str, *, limit: int, window_seconds: int
    ) -> bool:
        self.calls.append((key, limit, window_seconds))
        self.counts[key] += 1
        return self.counts[key] <= limit
```

- [ ] **Step 7: Write the contract test**

Create `tests/application/ports/test_rate_limiter_port_contract.py`:

```python
"""Contract test — FakeRateLimiter enforces limit."""
from __future__ import annotations

import pytest

from tests._fakes.rate_limiter import FakeRateLimiter


@pytest.mark.asyncio
async def test_allow_under_limit_deny_over() -> None:
    rl = FakeRateLimiter()
    assert await rl.check("k", limit=2, window_seconds=60) is True
    assert await rl.check("k", limit=2, window_seconds=60) is True
    assert await rl.check("k", limit=2, window_seconds=60) is False
    assert rl.counts == {"k": 3}
```

- [ ] **Step 8: Run tests**

```bash
make check
```
Expected: 551 passed. `lint-imports` green.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce RateLimiterPort + RedisRateLimiter adapter"
```

---

## Task 2.9: `ConfigLoaderPort`

**Spec mapping:** `infrastructure.config_loader.load_for_repo(repo_full_name) -> EffectiveConfig`. The Port wraps the call so consumers depend on the contract, not on the YAML loader.

**Files:**
- Create: `openbot/application/ports/config_loader.py`
- Modify: `openbot/infrastructure/config_loader.py` — add adapter class
- Modify: `openbot/entrypoints/api/app.py` — attach
- Modify: any handler/middleware that calls `load_for_repo` directly — switch to Port
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/config_loader.py`
- Create: `tests/application/ports/test_config_loader_port_contract.py`

- [ ] **Step 1: Write `application/ports/config_loader.py`**

```python
"""ConfigLoaderPort — per-repo effective config resolution."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openbot.domain.config_schema import EffectiveConfig


class ConfigLoaderPort(Protocol):
    """Resolve the effective config for one repo."""

    async def load_for_repo(self, repo_full_name: str) -> "EffectiveConfig":
        """Returns the merged config — defaults + repo overrides."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.config_loader import ConfigLoaderPort  # noqa: F401
```

- [ ] **Step 2: Add adapter class in `infrastructure/config_loader.py`**

After the existing `load_for_repo` function:

```python
class YamlConfigLoader:
    """ConfigLoaderPort impl backed by the YAML loader above."""

    async def load_for_repo(self, repo_full_name: str) -> EffectiveConfig:
        # `load_for_repo` may be sync; await-wrap or call directly per its
        # current signature. If sync, leave the wrapper async for Port shape.
        return load_for_repo(repo_full_name)


if TYPE_CHECKING:
    from openbot.application.ports.config_loader import ConfigLoaderPort

    _witness: "ConfigLoaderPort" = YamlConfigLoader()
```

If today's `load_for_repo` is already async, just `return await load_for_repo(repo_full_name)`. Match its actual signature.

- [ ] **Step 3: Switch consumers**

```bash
grep -rn "load_for_repo" openbot/ tests/ | grep -v __pycache__ | grep -v "infrastructure/config_loader.py"
```

Every callsite — likely in middleware or handlers — switches to `await deps.config_loader.load_for_repo(repo)` or `await request.app.state.config_loader.load_for_repo(repo)`.

In `entrypoints/api/app.py` lifespan:

```python
from openbot.infrastructure.config_loader import YamlConfigLoader
# ...
app.state.config_loader = YamlConfigLoader()
```

- [ ] **Step 4: Write `tests/_fakes/config_loader.py`**

```python
"""FakeConfigLoader — returns programmable EffectiveConfig per repo."""
from __future__ import annotations

from dataclasses import dataclass, field

from openbot.domain.config_schema import EffectiveConfig


@dataclass
class FakeConfigLoader:
    by_repo: dict[str, EffectiveConfig] = field(default_factory=dict)
    default: EffectiveConfig | None = None
    calls: list[str] = field(default_factory=list)

    async def load_for_repo(self, repo_full_name: str) -> EffectiveConfig:
        self.calls.append(repo_full_name)
        if repo_full_name in self.by_repo:
            return self.by_repo[repo_full_name]
        if self.default is None:
            raise KeyError(f"no fake config for {repo_full_name}")
        return self.default
```

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_config_loader_port_contract.py`. Build a minimal `EffectiveConfig` — read `openbot/domain/config_schema.py` to confirm required fields:

```python
"""Contract test — FakeConfigLoader returns programmed configs."""
from __future__ import annotations

import pytest

from openbot.domain.config_schema import EffectiveConfig
from tests._fakes.config_loader import FakeConfigLoader


def _minimal_config() -> EffectiveConfig:
    # Update kwargs to match EffectiveConfig's required fields.
    # All sub-configs (BudgetConfig, etc.) have sensible defaults in the
    # dataclass — pass only the ones without defaults.
    return EffectiveConfig()


@pytest.mark.asyncio
async def test_returns_programmed_config() -> None:
    cfg = _minimal_config()
    loader = FakeConfigLoader(by_repo={"owner/repo": cfg})
    assert await loader.load_for_repo("owner/repo") is cfg
    assert loader.calls == ["owner/repo"]


@pytest.mark.asyncio
async def test_missing_repo_raises_without_default() -> None:
    loader = FakeConfigLoader()
    with pytest.raises(KeyError):
        await loader.load_for_repo("owner/missing")
```

- [ ] **Step 6: Run tests**

```bash
make check
```
Expected: 552 passed. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce ConfigLoaderPort + YamlConfigLoader adapter"
```

---

## Task 2.10: `LLMPort`

**Spec mapping:** `litellm.acompletion(...)` is currently called directly from `infrastructure/llm/complete.py`. The Port defines the shape that the future agent slice will depend on. **No consumer switch in Phase 2** — workflows are still ACK-only stubs.

**Files:**
- Create: `openbot/application/ports/llm.py`
- Modify: `openbot/infrastructure/llm/complete.py` — add adapter class + witness
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/llm.py`
- Create: `tests/application/ports/test_llm_port_contract.py`

- [ ] **Step 1: Write `application/ports/llm.py`**

```python
"""LLMPort — single-call completion contract.

Defined for the agent slice; no Phase-2 consumer wires through it yet.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class LLMPort(Protocol):
    """One-shot chat completion."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant text. Raises on transport error."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.llm import LLMPort  # noqa: F401
```

- [ ] **Step 2: Add adapter in `infrastructure/llm/complete.py`**

```python
class LiteLLMCompleter:
    """Concrete LLMPort backed by litellm.acompletion."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        # Reuse the existing module-level `complete` function (or whatever the
        # current entry point is) — wrap only its kwargs to match the Port.
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await acompletion(**kwargs)
        return response["choices"][0]["message"]["content"]


if TYPE_CHECKING:
    from openbot.application.ports.llm import LLMPort

    _witness: "LLMPort" = LiteLLMCompleter()
```

`acompletion` here is whatever `litellm` symbol the current code imports. If `complete.py` already has a function-shaped facade, just call into it from `LiteLLMCompleter.complete`.

- [ ] **Step 3: Write `tests/_fakes/llm.py`**

```python
"""FakeLLM — programmable LLMPort that records every call."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeLLM:
    response: str = ""
    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return self.response
```

- [ ] **Step 4: Write the contract test**

Create `tests/application/ports/test_llm_port_contract.py`:

```python
"""Contract test — FakeLLM records calls and returns programmed responses."""
from __future__ import annotations

import pytest

from tests._fakes.llm import FakeLLM


@pytest.mark.asyncio
async def test_complete_returns_default_response() -> None:
    llm = FakeLLM(response="hello")
    out = await llm.complete(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert out == "hello"
    assert llm.calls[0]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_queued_responses_drain_in_order() -> None:
    llm = FakeLLM(responses=["a", "b"])
    assert await llm.complete(model="m", messages=[]) == "a"
    assert await llm.complete(model="m", messages=[]) == "b"
```

- [ ] **Step 5: Run tests**

```bash
make check
```
Expected: 553 passed. `lint-imports` green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce LLMPort + LiteLLMCompleter adapter (no Phase-2 consumer)"
```

---

## Task 2.11: `SandboxPort` + empty-ignore-list checkpoint

**Spec mapping:** the future agent slice will execute tool calls in an isolated sandbox. The Port is defined now so DeepAgents can plug in without revisiting middleware. **No Phase-2 consumer.**

Locked boundary reminder: PRD §3 keeps `evals.sandboxes.factory` under `evals/`, not under `openbot/`. `SandboxPort` is a Hexagonal contract for the agent slice; it does NOT shadow or replace `evals.sandboxes.factory`. If agent code ever needs eval sandboxes, it imports from `evals.sandboxes.factory` directly through this Port — the Port is the contract, the eval factory is one possible adapter.

**Files:**
- Create: `openbot/application/ports/sandbox.py`
- Create: `openbot/infrastructure/agents/__init__.py` (already exists as placeholder from Phase 1 — leave empty)
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/sandbox.py`
- Create: `tests/application/ports/test_sandbox_port_contract.py`
- Modify: `.importlinter` — verify the `ignore_imports` list contains ONLY the allowed Port→leaf-enum line

- [ ] **Step 1: Write `application/ports/sandbox.py`**

```python
"""SandboxPort — isolated execution surface for agent tools.

Defined for the agent slice. The first impl will live under
`infrastructure/agents/` and adapt the chosen runtime (DeepAgents +
Daytona/Modal/Docker, per locked-boundary §3).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SandboxPort(Protocol):
    """Run one command in an isolated environment, return its result."""

    async def run(
        self,
        *,
        command: list[str],
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Return {'stdout': str, 'stderr': str, 'exit_code': int, 'timed_out': bool}."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.sandbox import SandboxPort  # noqa: F401
```

- [ ] **Step 2: Write `tests/_fakes/sandbox.py`**

```python
"""FakeSandbox — programmable SandboxPort that records every command."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSandbox:
    default_result: dict[str, Any] = field(
        default_factory=lambda: {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
        }
    )
    results: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[list[str], Mapping[str, str] | None, int]] = field(
        default_factory=list
    )

    async def run(
        self,
        *,
        command: list[str],
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        self.calls.append((command, env, timeout_seconds))
        if self.results:
            return self.results.pop(0)
        return dict(self.default_result)
```

- [ ] **Step 3: Write the contract test**

Create `tests/application/ports/test_sandbox_port_contract.py`:

```python
"""Contract test — FakeSandbox records commands and returns programmed results."""
from __future__ import annotations

import pytest

from tests._fakes.sandbox import FakeSandbox


@pytest.mark.asyncio
async def test_run_returns_default_result() -> None:
    sb = FakeSandbox()
    out = await sb.run(command=["echo", "hi"])
    assert out["exit_code"] == 0
    assert sb.calls == [(["echo", "hi"], None, 60)]


@pytest.mark.asyncio
async def test_queued_result_drains_first() -> None:
    sb = FakeSandbox(results=[{"stdout": "x", "stderr": "", "exit_code": 1, "timed_out": False}])
    out = await sb.run(command=["true"])
    assert out["exit_code"] == 1
    out2 = await sb.run(command=["true"])
    assert out2["exit_code"] == 0  # back to default
```

- [ ] **Step 4: Verify the `.importlinter` ignore list is now minimal**

Read `.importlinter`. The `ignore_imports` section MUST be exactly:

```
ignore_imports =
    openbot.application.ports.dedup -> openbot.infrastructure.persistence.dedup
```

Any other line is a leftover. To find leftovers:

```bash
uv run lint-imports --verbose
```

If `lint-imports` is green with only the dedup exception, the layer contract is fully enforced. If it flags additional imports, do one of:

1. The flagged import is a TRUE Phase-2 leftover → file a Phase-2 follow-up task; do NOT add a new ignore.
2. The flagged import is a Port → leaf-enum exception we should accept → add a comment in `.importlinter` justifying it AND extend the ignore list.

The 11-Port catalogue is complete only when (1) finds zero genuine leftovers.

- [ ] **Step 5: Run tests**

```bash
make check
```
Expected: 554 passed (543 baseline + 11 Port contract tests). `lint-imports` green with ONLY the documented exception.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce SandboxPort; close the importlinter ignore list"
```

---

## Phase 2 Acceptance (covers 2a + 2b + 2c)

- [ ] `application/ports/` contains 11 Protocol files.
- [ ] `application/dispatcher_deps.py` exists with all 11 fields typed.
- [ ] Every Port has a `tests/_fakes/<name>.py` + `tests/application/ports/test_<name>_port_contract.py` (11 contract tests, all passing).
- [ ] `entrypoints/api/app.py` builds Port-typed collaborators in `lifespan` and attaches them to `app.state.*`.
- [ ] `entrypoints/api/routes/github_webhook.py` reads only `request.app.state.*` — no direct `openbot.infrastructure.*` imports beyond DTOs (`QueuePayload`).
- [ ] `.importlinter` `ignore_imports` contains exactly one line — the documented `application.ports.dedup -> infrastructure.persistence.dedup` Port→leaf-enum exception.
- [ ] `make check` reports **554 passed**.
- [ ] `git log --oneline` shows 12 atomic commits across Tasks 2.0 – 2.11.

**Open the Phase 2 PR (12 commits across Phase 2a + 2b + 2c).** Stop here. Wait for CI green and code review before starting [Phase 3](2026-05-18-hexagonal-restructure-phase-3-tests.md).
