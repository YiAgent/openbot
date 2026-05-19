"""State-machine L2: error and edge-case paths (I-30, I-31x2, I-32, X-01).

Plan IDs reference ``docs/plans/webhook-worker-test-plan.md``.

I-32 note: when ``enqueue`` raises, the webapp falls through to
``BackgroundTasks.add_task(_run_dispatch, ...)``. With ``ASGITransport``,
background tasks ARE executed (Starlette runs them as part of the response
lifecycle). We patch ``openbot.entrypoints.api.app.run_dispatch`` to a no-op so no real
GitHub API or LLM calls occur — we only assert the HTTP layer degrades
gracefully.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.state_machine._payloads import _REPO, issue_body, sign
from tests.state_machine.conftest import SMHarness

_ISSUE_RK = f"github:{_REPO}:issue:42"


# ── I-30: bad HMAC → 401 ──────────────────────────────────────────────────


async def test_bad_signature_401(sm: SMHarness) -> None:
    """I-30: HMAC mismatch → 401, Redis and DB left unchanged."""
    import hmac as hmac_mod
    from hashlib import sha256 as sha256_fn

    body = issue_body("opened", number=42)
    bad_digest = hmac_mod.new(b"wrong-secret", body, sha256_fn).hexdigest()
    bad_headers = {
        "x-hub-signature-256": f"sha256={bad_digest}",
        "x-github-event": "issues",
        "x-github-delivery": "d-30",
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/test",
    }

    resp = await sm.client.post("/webhook/github", content=body, headers=bad_headers)

    assert resp.status_code == 401
    assert await sm.queue_len() == 0


# ── I-31: missing issue.number / installation.id → 202 ignored ────────────


async def test_missing_issue_number(sm: SMHarness) -> None:
    """I-31a: issues.opened without issue.number → dispatch_for returns None → ignored."""
    body = issue_body("opened", number=None)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-31a"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


async def test_missing_installation_id(sm: SMHarness) -> None:
    """I-31b: issues.opened without installation.id → dispatch_for returns None → ignored."""
    body = issue_body("opened", number=42, omit_installation=True)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-31b"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


# ── I-32: Redis enqueue failure → 202 graceful fallback ───────────────────


async def test_redis_enqueue_failure_graceful(
    sm: SMHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-32: enqueue raises → webapp logs + falls through to BackgroundTask → still 202."""

    async def _raise_on_enqueue(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("test-enqueue-failure")

    async def _noop_dispatch(*_a: Any, **_kw: Any) -> None:
        pass

    monkeypatch.setattr("openbot.entrypoints.api.routes.github_webhook.enqueue", _raise_on_enqueue)
    monkeypatch.setattr(
        "openbot.entrypoints.api.routes.github_webhook.run_dispatch", _noop_dispatch
    )

    body = issue_body("opened", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-32"),
    )

    # The webapp must NOT 5xx — graceful degradation.
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    # Nothing was enqueued to the stream.
    assert await sm.queue_len() == 0


# ── X-01: latency gate ────────────────────────────────────────────────────


async def test_webhook_latency_under_100ms(sm: SMHarness) -> None:
    """X-01: happy-path issue.opened round-trip < 100 ms (in-process, no network).

    This gate catches accidental blocking calls or synchronous DB scans.
    It measures Python overhead only — not realistic network latency.
    """
    body = issue_body("opened", number=42)
    headers = sign(body, event="issues", delivery="d-x01")

    start = time.monotonic()
    resp = await sm.client.post("/webhook/github", content=body, headers=headers)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert elapsed_ms < 100, f"webhook took {elapsed_ms:.1f} ms (expected < 100 ms)"
