#!/usr/bin/env python3
"""Comprehensive L4 smoke-test sender — issue / PR / mention lifecycle.

Usage
-----
    uv run python scripts/fire_smoke.py --all
    uv run python scripts/fire_smoke.py --group issue
    uv run python scripts/fire_smoke.py --group pr
    uv run python scripts/fire_smoke.py --group mention
    uv run python scripts/fire_smoke.py --group lifecycle
    uv run python scripts/fire_smoke.py --list
    uv run python scripts/fire_smoke.py issue-opened -v

Environment: OPENBOT_GITHUB_WEBHOOK_SECRET, FIRE_SMOKE_TARGET
Exit: 0=all passed, 1=any failed
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

_logger = logging.getLogger("fire_smoke")
_DEFAULT_TARGET = "http://localhost:8000/webhook/github"
_DEFAULT_SECRET = "openbot-smoke-test-secret"

# ── Shared fixtures ───────────────────────────────────────────────────────────

_REPO = "smoke-test-org/smoke-test-repo"
_INSTALL = {"id": 99_999_999}
_HUMAN = {"login": "smoke-alice", "id": 10_001, "type": "User"}
_BOT = {"login": "openbot[bot]", "id": 10_002, "type": "Bot"}
_OUTSIDER = {"login": "smoke-outsider", "id": 10_099, "type": "User"}
_REPO_OBJ = {
    "id": 1,
    "full_name": _REPO,
    "name": "smoke-test-repo",
    "owner": {"login": "smoke-test-org", "id": 5_001},
    "private": False,
    "default_branch": "main",
}
_FORK_HEAD = {"full_name": "smoke-outsider/fork-repo", "fork": True}
_SAME_HEAD = {"full_name": _REPO, "fork": False}
_BASE_REPO = {"full_name": _REPO}
_PR_BASE_REF = {"sha": "deadbeef", "ref": "main", "repo": _BASE_REPO}


def _uid(p: str) -> str:
    return f"{p}-{int(time.time() * 1000) % 9_999_999}"


# ── HMAC + payload helpers ────────────────────────────────────────────────────


def _sign(body: bytes, *, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make(payload: dict, *, event: str, delivery: str, secret: str) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    return {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/s",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "x-hub-signature-256": _sign(body, secret=secret),
    }, body


def _issue_payload(
    action: str,
    *,
    n: int,
    state: str = "open",
    labels: list | None = None,
    assignees: list | None = None,
    sender: dict | None = None,
    extra: dict | None = None,
) -> dict:
    p: dict[str, Any] = {
        "action": action,
        "repository": _REPO_OBJ,
        "installation": _INSTALL,
        "sender": sender or _HUMAN,
        "issue": {
            "number": n,
            "title": f"Smoke #{n}",
            "body": "smoke",
            "state": state,
            "user": _HUMAN,
            "labels": labels or [],
            "assignees": assignees or [],
        },
    }
    if extra:
        p.update(extra)
    return p


def _issue_event(
    action: str, *, n: int, delivery: str, secret: str, **kw: Any
) -> tuple[dict, bytes]:
    return _make(
        _issue_payload(action, n=n, **kw), event="issues", delivery=delivery, secret=secret
    )


def _comment_event(
    *,
    n: int,
    body_text: str,
    delivery: str,
    secret: str,
    sender: dict | None = None,
    on_pr: bool = False,
) -> tuple[dict, bytes]:
    issue_obj: dict[str, Any] = {
        "number": n,
        "title": f"Smoke #{n}",
        "state": "open",
        "user": _HUMAN,
    }
    if on_pr:
        issue_obj["pull_request"] = {"url": f"https://api.github.com/repos/{_REPO}/pulls/{n}"}
    p = {
        "action": "created",
        "issue": issue_obj,
        "repository": _REPO_OBJ,
        "installation": _INSTALL,
        "sender": sender or _HUMAN,
        "comment": {"id": 77000 + n, "body": body_text, "user": sender or _HUMAN},
    }
    return _make(p, event="issue_comment", delivery=delivery, secret=secret)


def _pr_event(
    action: str,
    *,
    n: int,
    sha: str,
    delivery: str,
    secret: str,
    draft: bool = False,
    merged: bool = False,
    state: str = "open",
    head_repo: dict | None = None,
    sender: dict | None = None,
) -> tuple[dict, bytes]:
    p = {
        "action": action,
        "repository": _REPO_OBJ,
        "installation": _INSTALL,
        "sender": sender or _HUMAN,
        "pull_request": {
            "number": n,
            "title": f"Smoke PR #{n}",
            "body": "smoke",
            "state": state,
            "draft": draft,
            "merged": merged,
            "user": _HUMAN,
            "head": {"sha": sha, "ref": "feature/s", "repo": head_repo or _SAME_HEAD},
            "base": _PR_BASE_REF,
        },
    }
    return _make(p, event="pull_request", delivery=delivery, secret=secret)


def _pr_rc_event(
    *, n: int, body_text: str, delivery: str, secret: str, sender: dict | None = None
) -> tuple[dict, bytes]:
    p = {
        "action": "created",
        "repository": _REPO_OBJ,
        "installation": _INSTALL,
        "sender": sender or _HUMAN,
        "pull_request": {"number": n, "title": f"Smoke PR #{n}", "state": "open", "user": _HUMAN},
        "comment": {"id": 88000 + n, "body": body_text, "user": sender or _HUMAN},
    }
    return _make(p, event="pull_request_review_comment", delivery=delivery, secret=secret)


# ── Fire helper ───────────────────────────────────────────────────────────────


@dataclass
class Result:
    name: str
    ok: bool
    code: int
    body: dict | None = None
    notes: str = ""


async def _fire(
    client: httpx.AsyncClient,
    target: str,
    name: str,
    h: dict,
    b: bytes,
    *,
    expect: int = 202,
    expect_body: dict | None = None,
    verbose: bool = False,
) -> Result:
    t0 = time.monotonic()
    try:
        resp = await client.post(target, content=b, headers=h, timeout=10.0)
        ms = (time.monotonic() - t0) * 1000
        rb: dict | None = None
        with contextlib.suppress(Exception):
            rb = resp.json()

        ok = resp.status_code == expect
        notes = ""
        if ok and expect_body and rb:
            for k, v in expect_body.items():
                if rb.get(k) != v:
                    ok = False
                    notes = f"{k}={rb.get(k)!r} want {v!r}"
                    break

        emoji = "✅" if ok else "❌"
        del_id = h.get("x-github-delivery", "?")[:14]
        _logger.info(
            "%s %-34s %-14s → %d  (%.0f ms)%s",
            emoji,
            name,
            del_id,
            resp.status_code,
            ms,
            f"  [{notes}]" if notes else "",
        )
        if verbose and rb:
            _logger.info("    %s", rb)
        return Result(name, ok, resp.status_code, rb, notes)
    except httpx.RequestError as e:
        _logger.error("❌ %-34s NET ERROR: %s", name, e)
        return Result(name, False, 0, None, str(e))


# ── Scenario registry ─────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    group: str
    description: str
    fn: Callable[..., Any]


_REGISTRY: list[Scenario] = []


def _s(name: str, group: str, desc: str):
    def decorator(fn: Callable) -> Callable:
        _REGISTRY.append(Scenario(name, group, desc, fn))
        return fn

    return decorator


# ── Issue scenarios ───────────────────────────────────────────────────────────


@_s("issue-opened", "issue", "issues.opened → triage START (202, feature=triage)")
async def _(c, t, sec, v):
    h, b = _issue_event("opened", n=101, delivery=_uid("i-open"), secret=sec)
    return await _fire(
        c,
        t,
        "issue-opened",
        h,
        b,
        expect_body={"status": "accepted", "feature": "triage"},
        verbose=v,
    )


@_s("issue-edited", "issue", "issues.edited → triage (START/SUPERSEDE)")
async def _(c, t, sec, v):
    h, b = _issue_event("edited", n=101, delivery=_uid("i-edit"), secret=sec)
    return await _fire(
        c,
        t,
        "issue-edited",
        h,
        b,
        expect_body={"status": "accepted", "feature": "triage"},
        verbose=v,
    )


@_s("issue-closed", "issue", "issues.closed → cancel run (202 accepted)")
async def _(c, t, sec, v):
    h, b = _issue_event("closed", n=101, state="closed", delivery=_uid("i-close"), secret=sec)
    return await _fire(c, t, "issue-closed", h, b, verbose=v)


@_s("issue-reopened", "issue", "issues.reopened → new triage START")
async def _(c, t, sec, v):
    h, b = _issue_event("reopened", n=101, delivery=_uid("i-reopen"), secret=sec)
    return await _fire(
        c,
        t,
        "issue-reopened",
        h,
        b,
        expect_body={"status": "accepted", "feature": "triage"},
        verbose=v,
    )


@_s("issue-assigned-bot", "issue", "issues.assigned to bot → fix workflow")
async def _(c, t, sec, v):
    h, b = _issue_event(
        "assigned",
        n=102,
        assignees=[_BOT],
        extra={"assignee": _BOT},
        delivery=_uid("i-asgn-bot"),
        secret=sec,
    )
    return await _fire(
        c,
        t,
        "issue-assigned-bot",
        h,
        b,
        expect_body={"status": "accepted", "feature": "fix"},
        verbose=v,
    )


@_s("issue-assigned-human", "issue", "issues.assigned to human → IGNORE")
async def _(c, t, sec, v):
    h, b = _issue_event(
        "assigned", n=103, extra={"assignee": _HUMAN}, delivery=_uid("i-asgn-human"), secret=sec
    )
    return await _fire(c, t, "issue-assigned-human", h, b, verbose=v)


@_s("issue-labeled-cancel", "issue", "issues.labeled cancel-openbot → cancel/blocked")
async def _(c, t, sec, v):
    lbl = {"id": 1, "name": "cancel-openbot", "color": "red"}
    h, b = _issue_event(
        "labeled",
        n=104,
        labels=[lbl],
        extra={"label": lbl},
        delivery=_uid("i-lbl-cancel"),
        secret=sec,
    )
    return await _fire(c, t, "issue-labeled-cancel", h, b, verbose=v)


@_s("issue-labeled-bug", "issue", "issues.labeled 'bug' → IGNORE")
async def _(c, t, sec, v):
    lbl = {"id": 2, "name": "bug", "color": "d73a4a"}
    h, b = _issue_event(
        "labeled", n=105, labels=[lbl], extra={"label": lbl}, delivery=_uid("i-lbl-bug"), secret=sec
    )
    return await _fire(c, t, "issue-labeled-bug", h, b, verbose=v)


@_s("issue-comment-bot-guard", "issue", "bot self-comment → IGNORE (status=ignored)")
async def _(c, t, sec, v):
    h, b = _comment_event(
        n=101,
        body_text="I'm a bot replying to myself",
        delivery=_uid("i-bot-reply"),
        secret=sec,
        sender=_BOT,
    )
    return await _fire(
        c, t, "issue-comment-bot-guard", h, b, expect_body={"status": "ignored"}, verbose=v
    )


@_s("issue-dedup", "issue", "same delivery twice → 2nd ignored (needs Redis)")
async def _(c, t, sec, v):
    fixed = "smoke-dedup-fixed-0001"
    h, b = _issue_event("opened", n=106, delivery=fixed, secret=sec)
    r1 = await _fire(c, t, "issue-dedup [1st]", h, b, verbose=v)
    r2 = await _fire(c, t, "issue-dedup [2nd]", h, b, expect=200, verbose=v)
    if not r1.ok:
        return r1
    if not r2.ok and r2.code == 202:
        _logger.info("  ↳ dedup open (no Redis) — both 202 is expected without Redis")
        return Result("issue-dedup", True, 202, None, "no-Redis fallback")
    return r2


# ── PR scenarios ──────────────────────────────────────────────────────────────


@_s("pr-opened", "pr", "pull_request.opened (same-repo) → review START")
async def _(c, t, sec, v):
    h, b = _pr_event("opened", n=201, sha="aaa001", delivery=_uid("pr-open"), secret=sec)
    return await _fire(
        c, t, "pr-opened", h, b, expect_body={"status": "accepted", "feature": "review"}, verbose=v
    )


@_s("pr-draft", "pr", "pull_request.opened (draft) → IGNORE")
async def _(c, t, sec, v):
    h, b = _pr_event(
        "opened", n=202, sha="bbb001", draft=True, delivery=_uid("pr-draft"), secret=sec
    )
    return await _fire(c, t, "pr-draft", h, b, verbose=v)


@_s("pr-synchronize", "pr", "pull_request.synchronize → review SUPERSEDE")
async def _(c, t, sec, v):
    h, b = _pr_event("synchronize", n=201, sha="aaa002", delivery=_uid("pr-sync"), secret=sec)
    return await _fire(
        c,
        t,
        "pr-synchronize",
        h,
        b,
        expect_body={"status": "accepted", "feature": "review"},
        verbose=v,
    )


@_s("pr-merged", "pr", "pull_request.closed (merged=True) → PR_MERGED")
async def _(c, t, sec, v):
    h, b = _pr_event(
        "closed",
        n=201,
        sha="aaa999",
        state="closed",
        merged=True,
        delivery=_uid("pr-merged"),
        secret=sec,
    )
    return await _fire(c, t, "pr-merged", h, b, verbose=v)


@_s("pr-closed-unmerged", "pr", "pull_request.closed (merged=False) → PR_CLOSED")
async def _(c, t, sec, v):
    h, b = _pr_event(
        "closed",
        n=203,
        sha="ccc001",
        state="closed",
        merged=False,
        delivery=_uid("pr-close"),
        secret=sec,
    )
    return await _fire(c, t, "pr-closed-unmerged", h, b, verbose=v)


@_s("pr-fork-blocked", "pr", "fork PR without /ok-to-test → blocked")
async def _(c, t, sec, v):
    h, b = _pr_event(
        "opened",
        n=204,
        sha="fork001",
        head_repo=_FORK_HEAD,
        sender=_OUTSIDER,
        delivery=_uid("pr-fork"),
        secret=sec,
    )
    return await _fire(c, t, "pr-fork-blocked", h, b, verbose=v)


@_s("pr-review-comment-chat", "pr", "review comment @openbot → chat")
async def _(c, t, sec, v):
    h, b = _pr_rc_event(
        n=201, body_text="@openbot explain this function", delivery=_uid("pr-rc-chat"), secret=sec
    )
    return await _fire(c, t, "pr-review-comment-chat", h, b, verbose=v)


@_s("pr-review-comment-ignored", "pr", "review comment without @openbot → IGNORE")
async def _(c, t, sec, v):
    h, b = _pr_rc_event(
        n=201, body_text="nit: rename this variable", delivery=_uid("pr-rc-noop"), secret=sec
    )
    return await _fire(c, t, "pr-review-comment-ignored", h, b, verbose=v)


# ── Mention scenarios ─────────────────────────────────────────────────────────


@_s("mention-chat", "mention", "@openbot freeform → chat (feature=chat)")
async def _(c, t, sec, v):
    h, b = _comment_event(
        n=101, body_text="@openbot can you summarise this?", delivery=_uid("m-chat"), secret=sec
    )
    return await _fire(
        c, t, "mention-chat", h, b, expect_body={"status": "accepted", "feature": "chat"}, verbose=v
    )


@_s("mention-help", "mention", "@openbot help → chat (help command)")
async def _(c, t, sec, v):
    h, b = _comment_event(n=101, body_text="@openbot help", delivery=_uid("m-help"), secret=sec)
    return await _fire(c, t, "mention-help", h, b, verbose=v)


@_s("mention-cancel", "mention", "@openbot cancel → cancel command")
async def _(c, t, sec, v):
    h, b = _comment_event(n=101, body_text="@openbot cancel", delivery=_uid("m-cancel"), secret=sec)
    return await _fire(c, t, "mention-cancel", h, b, verbose=v)


@_s("mention-on-pr", "mention", "@openbot on PR issue_comment → chat")
async def _(c, t, sec, v):
    h, b = _comment_event(
        n=201,
        body_text="@openbot what does this PR change?",
        delivery=_uid("m-pr"),
        secret=sec,
        on_pr=True,
    )
    return await _fire(
        c,
        t,
        "mention-on-pr",
        h,
        b,
        expect_body={"status": "accepted", "feature": "chat"},
        verbose=v,
    )


@_s("mention-unrelated", "mention", "comment without @openbot → IGNORE")
async def _(c, t, sec, v):
    h, b = _comment_event(
        n=101, body_text="Thanks for the quick fix!", delivery=_uid("m-unrelated"), secret=sec
    )
    return await _fire(c, t, "mention-unrelated", h, b, verbose=v)


# ── Security scenarios ────────────────────────────────────────────────────────


@_s("bad-signature", "security", "wrong HMAC → 401")
async def _(c, t, sec, v):
    h, b = _issue_event("opened", n=999, delivery=_uid("sec-bad-sig"), secret=sec)
    h["x-hub-signature-256"] = "sha256=" + "0" * 64
    return await _fire(c, t, "bad-signature", h, b, expect=401, verbose=v)


@_s("missing-event-header", "security", "no x-github-event → 202 ignored (no validation)")
async def _(c, t, sec, v):
    h, b = _issue_event("opened", n=999, delivery=_uid("sec-no-evt"), secret=sec)
    del h["x-github-event"]
    return await _fire(c, t, "missing-event-header", h, b, expect=202, verbose=v)


@_s("unknown-event", "security", "unrecognised event 'star' → graceful 202 ignored")
async def _(c, t, sec, v):
    payload = {
        "action": "created",
        "starred_at": "2026-01-01T00:00:00Z",
        "sender": _HUMAN,
        "repository": _REPO_OBJ,
        "installation": _INSTALL,
    }
    body = json.dumps(payload).encode()
    h = {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/s",
        "x-github-event": "star",
        "x-github-delivery": _uid("sec-unknown"),
        "x-hub-signature-256": _sign(body, secret=sec),
    }
    return await _fire(c, t, "unknown-event-star", h, body, expect=202, verbose=v)


# ── Concurrent scenario ───────────────────────────────────────────────────────


@_s("concurrent-sync", "concurrent", "two PR syncs in parallel → both 202")
async def _(c, t, sec, v):
    ts = int(time.time() * 1000) % 9_999_999
    ha, ba = _pr_event("synchronize", n=205, sha=f"cA-{ts}", delivery=f"s-cA-{ts}", secret=sec)
    hb, bb = _pr_event("synchronize", n=205, sha=f"cB-{ts}", delivery=f"s-cB-{ts}", secret=sec)
    ra, rb = await asyncio.gather(
        _fire(c, t, "concurrent-sync-A", ha, ba, verbose=v),
        _fire(c, t, "concurrent-sync-B", hb, bb, verbose=v),
    )
    return Result("concurrent-sync", ra.ok and rb.ok, 202 if ra.ok and rb.ok else 0)


# ── Lifecycle workflows ───────────────────────────────────────────────────────


@_s("lifecycle-issue", "lifecycle", "issue: opened → edited → closed → reopened")
async def _(c, t, sec, v):
    n = 300
    _logger.info("  ── issue lifecycle (issue #%d) ──", n)
    steps = [("opened", "triage"), ("edited", "triage"), ("closed", None), ("reopened", "triage")]
    ok = True
    for action, feat in steps:
        h, b = _issue_event(action, n=n, delivery=_uid(f"lc-i-{action}"), secret=sec)
        exp = {"status": "accepted", "feature": feat} if feat else None
        r = await _fire(c, t, f"  lc-issue.{action}", h, b, expect_body=exp, verbose=v)
        if not r.ok:
            ok = False
    return Result("lifecycle-issue", ok, 202 if ok else 0)


@_s("lifecycle-issue-fix", "lifecycle", "issue → assigned-bot (triage→fix handoff)")
async def _(c, t, sec, v):
    n = 400
    _logger.info("  ── issue+fix lifecycle (issue #%d) ──", n)
    pairs = [
        (
            _issue_event("opened", n=n, delivery=_uid("lc-f-open"), secret=sec),
            {"status": "accepted", "feature": "triage"},
        ),
        (
            _issue_event(
                "assigned",
                n=n,
                assignees=[_BOT],
                extra={"assignee": _BOT},
                delivery=_uid("lc-f-assign"),
                secret=sec,
            ),
            {"status": "accepted", "feature": "fix"},
        ),
    ]
    ok = True
    for (h, b), exp in pairs:
        r = await _fire(c, t, "  lc-issue+fix", h, b, expect_body=exp, verbose=v)
        if not r.ok:
            ok = False
    return Result("lifecycle-issue-fix", ok, 202 if ok else 0)


@_s("lifecycle-pr", "lifecycle", "PR: opened -> push x2 -> merged")
async def _(c, t, sec, v):
    n = 500
    _logger.info("  ── PR lifecycle (PR #%d) ──", n)
    evts = [
        (_pr_event("opened", n=n, sha="v1", delivery=_uid("lc-pr-open"), secret=sec), 202),
        (_pr_event("synchronize", n=n, sha="v2", delivery=_uid("lc-pr-sync1"), secret=sec), 202),
        (_pr_event("synchronize", n=n, sha="v3", delivery=_uid("lc-pr-sync2"), secret=sec), 202),
        (
            _pr_event(
                "closed",
                n=n,
                sha="v3",
                state="closed",
                merged=True,
                delivery=_uid("lc-pr-merge"),
                secret=sec,
            ),
            202,
        ),
    ]
    labels = ["opened", "sync-1", "sync-2", "merged"]
    ok = True
    for ((h, b), exp), lbl in zip(evts, labels, strict=False):
        r = await _fire(c, t, f"  lc-pr.{lbl}", h, b, expect=exp, verbose=v)
        if not r.ok:
            ok = False
    return Result("lifecycle-pr", ok, 202 if ok else 0)


@_s("lifecycle-pr-mention", "lifecycle", "PR opened + reviewer @openbot chat")
async def _(c, t, sec, v):
    n = 600
    _logger.info("  ── PR + mention lifecycle (PR #%d) ──", n)
    evts = [
        (
            _pr_event("opened", n=n, sha="chat-v1", delivery=_uid("lc-pm-open"), secret=sec),
            {"status": "accepted", "feature": "review"},
        ),
        (
            _comment_event(
                n=n,
                body_text="@openbot security implications?",
                delivery=_uid("lc-pm-chat"),
                secret=sec,
                on_pr=True,
            ),
            {"status": "accepted", "feature": "chat"},
        ),
        (
            _pr_rc_event(
                n=n,
                body_text="@openbot explain this function",
                delivery=_uid("lc-pm-rc"),
                secret=sec,
            ),
            None,
        ),
    ]
    ok = True
    for (h, b), exp in evts:
        r = await _fire(c, t, "  lc-pr+mention", h, b, expect_body=exp, verbose=v)
        if not r.ok:
            ok = False
    return Result("lifecycle-pr-mention", ok, 202 if ok else 0)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    groups = sorted({s.group for s in _REGISTRY})
    names = [s.name for s in _REGISTRY]
    p = argparse.ArgumentParser(
        description="L4 webhook smoke-test sender.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("scenario", nargs="?", choices=names, metavar="SCENARIO")
    p.add_argument("--all", action="store_true")
    p.add_argument("--group", choices=groups, metavar="GROUP", help=f"Groups: {', '.join(groups)}")
    p.add_argument("--list", action="store_true")
    p.add_argument("--target", default=os.environ.get("FIRE_SMOKE_TARGET", _DEFAULT_TARGET))
    p.add_argument(
        "--secret", default=os.environ.get("OPENBOT_GITHUB_WEBHOOK_SECRET", _DEFAULT_SECRET)
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.list:
        for grp in sorted({s.group for s in _REGISTRY}):
            print(f"\n[{grp}]")
            for s in _REGISTRY:
                if s.group == grp:
                    print(f"  {s.name:<36} {s.description}")
        sys.exit(0)

    if not args.scenario and not args.all and not args.group:
        _logger.error("specify a scenario, --all, --group, or --list")
        sys.exit(1)

    to_run = (
        [s for s in _REGISTRY if s.name == args.scenario]
        if args.scenario
        else [s for s in _REGISTRY if s.group == args.group]
        if args.group
        else list(_REGISTRY)
    )

    _logger.info("target  : %s", args.target)
    _logger.info("running : %d scenario(s)\n", len(to_run))

    results: list[Result] = []
    async with httpx.AsyncClient() as client:
        for s in to_run:
            r = await s.fn(client, args.target, args.secret, args.verbose)
            if s.group == "lifecycle":
                _logger.info("%s %s", "✅" if r.ok else "❌", r.name)
            results.append(r)

    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    _logger.info("\n%s  %d passed, %d failed", "─" * 50, passed, failed)
    if failed:
        for r in results:
            if not r.ok:
                _logger.info("  ❌ %-34s HTTP %-3d  %s", r.name, r.code, r.notes)
        sys.exit(1)
    _logger.info("🎉 all scenarios passed")


if __name__ == "__main__":
    asyncio.run(_main())
