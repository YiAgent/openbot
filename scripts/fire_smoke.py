#!/usr/bin/env python3
"""Smoke-test webhook sender for L4 E2E validation.

Fires signed GitHub-shape webhook payloads at a running OpenBot server
(local or remote) and reports the server's response.  Use this to
manually verify the full webhook → state machine → queue chain works
end-to-end without needing a real GitHub App to trigger events.

Usage
-----
    # Fire all smoke scenarios at local dev server:
    uv run python scripts/fire_smoke.py --all

    # Fire a specific scenario:
    uv run python scripts/fire_smoke.py issue-opened
    uv run python scripts/fire_smoke.py pr-opened
    uv run python scripts/fire_smoke.py pr-synchronize
    uv run python scripts/fire_smoke.py chat-mention
    uv run python scripts/fire_smoke.py concurrent-sync   # fires 2 syncs in parallel

    # Fire at a remote server (e.g. Heroku staging):
    uv run python scripts/fire_smoke.py --all --target https://openbot-staging.herokuapp.com

    # Use a custom webhook secret:
    uv run python scripts/fire_smoke.py --all --secret my-webhook-secret

Environment
-----------
    OPENBOT_GITHUB_WEBHOOK_SECRET   — webhook secret (overrides --secret)
    FIRE_SMOKE_TARGET               — server URL (overrides --target)

Exit codes
----------
    0  — all fired scenarios returned HTTP 2xx
    1  — at least one scenario returned a non-2xx response
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from typing import Any

import httpx

_logger = logging.getLogger("fire_smoke")

_DEFAULT_TARGET = "http://localhost:8000/webhook/github"
_DEFAULT_SECRET = "openbot-smoke-test-secret"

# ── Stable test fixtures ──────────────────────────────────────────────────────

_REPO = "smoke-test-org/smoke-test-repo"
_INSTALLATION_ID = 99_999_999

_HUMAN_SENDER = {"login": "smoke-alice", "id": 10_001, "type": "User"}

_REPO_OBJECT = {
    "id": 1,
    "full_name": _REPO,
    "name": "smoke-test-repo",
    "owner": {"login": "smoke-test-org", "id": 5_001},
    "private": False,
    "default_branch": "main",
}

_INSTALLATION_OBJECT = {"id": _INSTALLATION_ID}


# ── Signature helpers ─────────────────────────────────────────────────────────


def _sign(body: bytes, *, secret: str) -> str:
    """Return ``sha256=<hex>`` HMAC-SHA256 signature for ``body``."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(
    body: bytes,
    *,
    event: str,
    delivery_id: str,
    secret: str,
) -> dict[str, str]:
    """Build the minimal header set GitHub sends on every webhook delivery."""
    return {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/smoke0001",
        "x-github-event": event,
        "x-github-delivery": delivery_id,
        "x-hub-signature-256": _sign(body, secret=secret),
    }


def _make(
    payload: dict[str, Any], *, event: str, delivery_id: str, secret: str
) -> tuple[dict, bytes]:
    """Return (headers, body) ready for an httpx POST."""
    body = json.dumps(payload).encode()
    return _headers(body, event=event, delivery_id=delivery_id, secret=secret), body


# ── Payload builders ──────────────────────────────────────────────────────────


def _issue_opened(
    *,
    number: int = 1,
    delivery_id: str = "smoke-issue-open-1",
    secret: str,
) -> tuple[dict, bytes]:
    payload = {
        "action": "opened",
        "issue": {
            "number": number,
            "title": "Smoke test issue",
            "body": "This is a smoke test issue.",
            "state": "open",
            "user": _HUMAN_SENDER,
            "labels": [],
            "assignees": [],
        },
        "sender": _HUMAN_SENDER,
        "repository": _REPO_OBJECT,
        "installation": _INSTALLATION_OBJECT,
    }
    return _make(payload, event="issues", delivery_id=delivery_id, secret=secret)


def _pr_opened(
    *,
    number: int = 10,
    head_sha: str = "aaabbbccc000",
    delivery_id: str = "smoke-pr-open-1",
    secret: str,
) -> tuple[dict, bytes]:
    payload = {
        "action": "opened",
        "pull_request": {
            "number": number,
            "title": "Smoke test PR",
            "body": "Smoke test PR body.",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {
                "sha": head_sha,
                "ref": "feature/smoke",
                "repo": {"full_name": _REPO, "fork": False},
            },
            "base": {
                "sha": "deadbeef",
                "ref": "main",
                "repo": {"full_name": _REPO},
            },
            "user": _HUMAN_SENDER,
        },
        "sender": _HUMAN_SENDER,
        "repository": _REPO_OBJECT,
        "installation": _INSTALLATION_OBJECT,
    }
    return _make(payload, event="pull_request", delivery_id=delivery_id, secret=secret)


def _pr_synchronize(
    *,
    number: int = 10,
    head_sha: str,
    delivery_id: str,
    secret: str,
) -> tuple[dict, bytes]:
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": number,
            "title": "Smoke test PR",
            "body": "Smoke test PR body.",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {
                "sha": head_sha,
                "ref": "feature/smoke",
                "repo": {"full_name": _REPO, "fork": False},
            },
            "base": {
                "sha": "deadbeef",
                "ref": "main",
                "repo": {"full_name": _REPO},
            },
            "user": _HUMAN_SENDER,
        },
        "sender": _HUMAN_SENDER,
        "repository": _REPO_OBJECT,
        "installation": _INSTALLATION_OBJECT,
    }
    return _make(payload, event="pull_request", delivery_id=delivery_id, secret=secret)


def _chat_mention(
    *,
    issue_number: int = 1,
    comment: str = "@openbot what is the status of this issue?",
    delivery_id: str = "smoke-chat-1",
    secret: str,
) -> tuple[dict, bytes]:
    payload = {
        "action": "created",
        "issue": {
            "number": issue_number,
            "title": "Smoke test issue",
            "state": "open",
            "user": _HUMAN_SENDER,
            "pull_request": None,
        },
        "comment": {
            "id": 99_001,
            "body": comment,
            "user": _HUMAN_SENDER,
        },
        "sender": _HUMAN_SENDER,
        "repository": _REPO_OBJECT,
        "installation": _INSTALLATION_OBJECT,
    }
    return _make(payload, event="issue_comment", delivery_id=delivery_id, secret=secret)


def _bad_signature(
    *,
    delivery_id: str = "smoke-bad-sig-1",
    secret: str,
) -> tuple[dict, bytes]:
    """Returns a payload with a deliberately wrong signature (expect 401)."""
    h, body = _issue_opened(delivery_id=delivery_id, secret=secret)
    h["x-hub-signature-256"] = "sha256=" + "0" * 64  # wrong sig
    return h, body


# ── Single scenario fire ──────────────────────────────────────────────────────

_RESULT_OK = "✅"
_RESULT_FAIL = "❌"


async def _fire(
    client: httpx.AsyncClient,
    target: str,
    scenario: str,
    headers: dict[str, str],
    body: bytes,
    *,
    expect_status: int = 202,
    verbose: bool = False,
) -> bool:
    """POST one payload, log the outcome, return True if expected."""
    t0 = time.monotonic()
    try:
        resp = await client.post(target, content=body, headers=headers, timeout=10.0)
        elapsed_ms = (time.monotonic() - t0) * 1000
        ok = resp.status_code == expect_status
        emoji = _RESULT_OK if ok else _RESULT_FAIL
        delivery = headers.get("x-github-delivery", "?")[:12]
        _logger.info(
            "%s %-28s delivery=%-12s → HTTP %d  (%.0f ms)",
            emoji,
            scenario,
            delivery,
            resp.status_code,
            elapsed_ms,
        )
        if verbose or not ok:
            try:
                _logger.info("    body: %s", resp.json())
            except Exception:
                _logger.info("    body: %s", resp.text[:200])
        return ok
    except httpx.RequestError as exc:
        _logger.error("❌ %-28s NETWORK ERROR: %s", scenario, exc)
        return False


# ── Scenario catalogue ────────────────────────────────────────────────────────


async def run_issue_opened(
    client: httpx.AsyncClient, target: str, secret: str, verbose: bool
) -> bool:
    h, b = _issue_opened(
        number=1, delivery_id=f"smoke-issue-open-{int(time.time())}", secret=secret
    )
    return await _fire(client, target, "issue-opened", h, b, verbose=verbose)


async def run_pr_opened(client: httpx.AsyncClient, target: str, secret: str, verbose: bool) -> bool:
    h, b = _pr_opened(
        number=20,
        head_sha="smoke001aaaa",
        delivery_id=f"smoke-pr-open-{int(time.time())}",
        secret=secret,
    )
    return await _fire(client, target, "pr-opened", h, b, verbose=verbose)


async def run_pr_synchronize(
    client: httpx.AsyncClient, target: str, secret: str, verbose: bool
) -> bool:
    ts = int(time.time())
    h, b = _pr_synchronize(
        number=20, head_sha=f"smoke-sync-{ts}", delivery_id=f"smoke-pr-sync-{ts}", secret=secret
    )
    return await _fire(client, target, "pr-synchronize", h, b, verbose=verbose)


async def run_chat_mention(
    client: httpx.AsyncClient, target: str, secret: str, verbose: bool
) -> bool:
    h, b = _chat_mention(
        issue_number=1,
        comment="@openbot smoke test — what is the status of this repo?",
        delivery_id=f"smoke-chat-{int(time.time())}",
        secret=secret,
    )
    return await _fire(client, target, "chat-mention", h, b, verbose=verbose)


async def run_bad_signature(
    client: httpx.AsyncClient, target: str, secret: str, verbose: bool
) -> bool:
    """Expect 401 — validates the webhook signature gate."""
    h, b = _bad_signature(delivery_id=f"smoke-bad-sig-{int(time.time())}", secret=secret)
    return await _fire(
        client, target, "bad-signature (expect 401)", h, b, expect_status=401, verbose=verbose
    )


async def run_concurrent_sync(
    client: httpx.AsyncClient, target: str, secret: str, verbose: bool
) -> bool:
    """Fire two PR synchronize events for the same PR concurrently.

    Expected: both return 202 accepted.  Only one should end up as
    RUNNING in the DB (the other is SUPERSEDE'd).  This validates the
    resource_lock + CAS serialisation under concurrent load — a key L4
    property that smee.io cannot exercise with real GitHub because GitHub
    delivers webhooks serially, not in parallel.
    """
    ts = int(time.time())
    h_a, b_a = _pr_synchronize(
        number=21,
        head_sha=f"smoke-concurrent-A-{ts}",
        delivery_id=f"smoke-conc-A-{ts}",
        secret=secret,
    )
    h_b, b_b = _pr_synchronize(
        number=21,
        head_sha=f"smoke-concurrent-B-{ts}",
        delivery_id=f"smoke-conc-B-{ts}",
        secret=secret,
    )
    # Fire both simultaneously.
    ok_a, ok_b = await asyncio.gather(
        _fire(client, target, "concurrent-sync-A", h_a, b_a, verbose=verbose),
        _fire(client, target, "concurrent-sync-B", h_b, b_b, verbose=verbose),
    )
    return ok_a and ok_b


# ── Scenario registry ─────────────────────────────────────────────────────────

_SCENARIOS: dict[str, Any] = {
    "issue-opened": run_issue_opened,
    "pr-opened": run_pr_opened,
    "pr-synchronize": run_pr_synchronize,
    "chat-mention": run_chat_mention,
    "bad-signature": run_bad_signature,
    "concurrent-sync": run_concurrent_sync,
}


# ── Main ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fire signed webhook payloads at an OpenBot server for smoke testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "scenario",
        nargs="?",
        choices=list(_SCENARIOS),
        help="Which scenario to fire. Omit when using --all.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Run every scenario in sequence.",
    )
    p.add_argument(
        "--target",
        default=os.environ.get("FIRE_SMOKE_TARGET", _DEFAULT_TARGET),
        metavar="URL",
        help=f"Webhook endpoint to target (default: {_DEFAULT_TARGET})",
    )
    p.add_argument(
        "--secret",
        default=os.environ.get("OPENBOT_GITHUB_WEBHOOK_SECRET", _DEFAULT_SECRET),
        metavar="SECRET",
        help="Webhook HMAC secret (must match OPENBOT_GITHUB_WEBHOOK_SECRET on the server)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full response bodies.",
    )
    return p.parse_args(argv)


async def _main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.scenario and not args.all:
        _logger.error("specify a scenario name or use --all")
        _logger.info("available scenarios: %s", ", ".join(_SCENARIOS))
        sys.exit(1)

    scenarios_to_run = list(_SCENARIOS) if args.all else [args.scenario]

    _logger.info("target: %s", args.target)
    _logger.info("running %d scenario(s):", len(scenarios_to_run))

    all_ok = True
    async with httpx.AsyncClient() as client:
        for name in scenarios_to_run:
            fn = _SCENARIOS[name]
            ok = await fn(client, args.target, args.secret, args.verbose)
            if not ok:
                all_ok = False

    if all_ok:
        _logger.info("\n🎉 all smoke scenarios passed")
        sys.exit(0)
    else:
        _logger.error("\n💥 one or more smoke scenarios failed — check output above")
        sys.exit(1)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
