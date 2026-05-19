# L4 End-to-End Testing Guide

> **Layer**: L4 — Real GitHub + smee.io → local OpenBot server  
> **Automation**: Manual / pre-release validation  
> **Who runs this**: Maintainers before any production deployment

---

## Overview

L4 tests exercise the **full event path** without any mocking:

```
GitHub.com                smee.io              Local server
  │                          │                      │
  │── webhook POST ──────────▶│── SSE forward ──────▶│
  │                          │                      │── verify HMAC
  │                          │                      │── parse event
  │                          │                      │── resource_lock
  │                          │                      │── DB transition
  │                          │                      │── Redis XADD
  │                          │                      │── 202 accepted
  │                          │                      │
  │                                                 │── worker XREADGROUP
  │                                                 │── run_dispatch
  │◀── GitHub API: post comment ───────────────────│
```

| Layer | Tests | Automation |
|-------|-------|------------|
| L1 Unit | `tests/state/`, `tests/workflows/` | CI, always |
| L2 State Machine | `tests/state_machine/` | CI, always |
| L3 Integration | `tests/integration/` | CI, on-demand |
| **L4 E2E** | **this guide + `scripts/fire_smoke.py`** | **Manual, pre-release** |

> **Smoke without real GitHub**: `scripts/fire_smoke.py` fires signed payloads at the local server and validates HTTP responses — useful for CI pipelines where a real GitHub App is unavailable. See [§4 Smoke Without GitHub](#4-smoke-without-github).

---

## 1. Prerequisites

### 1.1 Required accounts / tokens

| What | Where to get it |
|------|-----------------|
| GitHub account | github.com |
| A **test repository** | Create a throwaway repo (`smoke-test-org/openbot-e2e`) |
| smee.io channel | Auto-created by `scripts/smee_relay.py --new` (free, no login) |
| GitHub App | See [§2](#2-github-app-setup) |

### 1.2 Local stack

```bash
# Terminal 1 — local databases
docker compose up redis postgres   # or use Upstash + Neon for hosted

# Terminal 2 — OpenBot server
cp .env.example .env               # fill in secrets (see §2)
uv run make dev                    # starts FastAPI on :8000 with autoreload

# Terminal 3 — smee relay
export SMEE_CHANNEL=https://smee.io/<your-channel-id>
uv run python scripts/smee_relay.py $SMEE_CHANNEL -v

# Terminal 4 — worker (optional: test queue processing)
uv run python -m openbot.worker
```

### 1.3 `.env` minimum for L4

```env
OPENBOT_GITHUB_WEBHOOK_SECRET=<your-app-webhook-secret>
OPENBOT_GITHUB_APP_ID=<numeric-app-id>
OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH=./secrets/github-app-private.pem
OPENBOT_REDIS_URL=redis://localhost:6379/0
OPENBOT_POSTGRES_URL=postgresql+asyncpg://openbot:openbot@localhost:5432/openbot
```

---

## 2. GitHub App Setup

> **One-time setup** — save the credentials in your password manager.

### 2.1 Create the App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **GitHub App name**: `OpenBot (dev)` or similar
   - **Homepage URL**: `http://localhost:8000`
   - **Webhook URL**: paste your smee.io channel URL (from `scripts/smee_relay.py --new`)
   - **Webhook secret**: generate a random string, save it as `OPENBOT_GITHUB_WEBHOOK_SECRET`
3. **Permissions** (Repository):
   - Issues: Read & Write
   - Pull requests: Read & Write
   - Contents: Read-only (for config.yaml loading)
4. **Subscribe to events**:
   - Issues, Pull requests, Issue comments, Pull request review comments
5. Click **Create GitHub App**

### 2.2 Collect credentials

After creation:
1. Note the **App ID** → `OPENBOT_GITHUB_APP_ID`
2. Scroll to **Private keys** → **Generate a private key** → save to `secrets/github-app-private.pem`
3. Go to **Install App** → install on your test repository

### 2.3 Verify the App is receiving events

```bash
# In your smee relay terminal, trigger a test event:
# Create a new issue in the test repo → you should see:
#   ✅ issues.opened  delivery=abc12345  → 202  (23ms)
```

---

## 3. Manual Test Scenarios

Run these in order. Each scenario lists:
- **Trigger**: what to do in GitHub
- **Expected relay log**: what `smee_relay.py -v` should print
- **Expected bot behavior**: what the bot should post/do
- **Verification**: how to confirm correctness

---

### S-01: Issue Triage ACK

**Trigger**: Create a new issue in the test repository.

**Expected relay log**:
```
✅ issues.opened  delivery=<id>  → 202  (<N>ms)
   response: {"status": "accepted", "feature": "triage", ...}
```

**Expected bot behavior** (within 5–30 s, worker must be running):
- Bot posts a comment: *"OpenBot received this issue…"*

**Verification**:
```
✅ HTTP 202 with status=accepted
✅ Bot comment appears on the issue within ~30s
✅ No duplicate comments on page refresh
```

---

### S-02: PR Review ACK (same-repo, non-draft)

**Trigger**: Open a new PR from a branch in the test repository (not a fork, not draft).

**Expected relay log**:
```
✅ pull_request.opened  delivery=<id>  → 202  (<N>ms)
   response: {"status": "accepted", "feature": "review", ...}
```

**Expected bot behavior**:
- Bot posts a comment: *"OpenBot received this PR…"*

**Verification**:
```
✅ HTTP 202 with status=accepted and feature=review
✅ Bot comment appears on the PR
```

---

### S-03: Supersede on Push (PR synchronize)

**Trigger**: Push two commits to the PR branch quickly (within 5 s of each other).

**Expected relay log** (two events arrive sequentially — smee serializes):
```
✅ pull_request.synchronize  delivery=<id-A>  → 202  (<N>ms)
   response: {"status": "accepted", ...}
✅ pull_request.synchronize  delivery=<id-B>  → 202  (<N>ms)
   response: {"status": "accepted", ...}
```

**Expected bot behavior**:
- Only **one** review comment appears (the second sync supersedes the first run).
- No duplicate comments from the first (cancelled) run.

**Verification**:
```
✅ Both 202 accepted (no errors)
✅ Exactly one bot comment visible on the PR after both runs complete
✅ Redis PEL empty (verify with: redis-cli XPENDING openbot.application.workflows openbot.application.workflows:group - +)
```

---

### S-04: @openbot Chat Mention

**Trigger**: Comment `@openbot what is this PR doing?` on any open issue or PR.

**Expected relay log**:
```
✅ issue_comment.created  delivery=<id>  → 202  (<N>ms)
   response: {"status": "accepted", "feature": "chat", ...}
```

**Expected bot behavior**:
- Bot replies within ~30 s.

**Verification**:
```
✅ HTTP 202 with feature=chat
✅ Bot comment appears referencing the @mention
```

---

### S-05: Idempotent Dedup (GitHub resend simulation)

**Trigger**: Use `curl` to re-send an already-delivered webhook with the same `X-GitHub-Delivery` header:

```bash
# Capture a delivery ID from the smee relay log (e.g. "abc12345678")
DELIVERY_ID="<paste-delivery-id-here>"
WEBHOOK_SECRET="<your-secret>"

# Re-send the exact same delivery:
# (modify scripts/fire_smoke.py to use the same delivery_id — or replay smee)
uv run python scripts/fire_smoke.py issue-opened \
  --target http://localhost:8000/webhook/github
# Then immediately re-send with the same delivery_id by running again.
```

**Expected behavior**:
- First: `202 {"status": "accepted"}`
- Second (same delivery_id): `200 {"status": "ignored", "reason": "duplicate"}`
- No second DB row or queue entry created.

**Verification**:
```
✅ Exactly one entry in Redis stream for that resource
✅ Second response has status=ignored, reason=duplicate
```

---

### S-06: Worker Crash Recovery (I-33 real-world)

**Trigger**:
1. Start a long-running issue (fire `issue-opened`).
2. Kill the worker process **mid-processing** (`ctrl-c` in Terminal 4).
3. Restart the worker.

**Expected behavior**:
- The entry is reclaimed from the PEL by the restarted worker.
- Bot completes its comment.

**Verification**:
```bash
# Check PEL before restart:
redis-cli XPENDING openbot.application.workflows openbot.application.workflows:group - + 10
# Should show 1 entry owned by the crashed consumer.

# After restart (60s delay in production; see _PENDING_IDLE_MS):
# PEL should become empty again.
redis-cli XPENDING openbot.application.workflows openbot.application.workflows:group - + 10
# Should show 0 pending entries.
```

---

### S-07: Bot Self-Reply Guard (M-31)

**Trigger**: Have the bot (or a user named `openbot[bot]`) comment on an issue.

**Expected behavior**:
- The bot's own comment does **not** trigger another workflow run.
- `relay log`: `→ 200  response: {"status": "ignored", "reason": "bot_actor"}`

**Verification**:
```
✅ No second bot comment appears
✅ Response has status=ignored
```

---

### S-08: Webhook Signature Rejection (I-30)

**Trigger**: Send a request with a wrong signature:

```bash
uv run python scripts/fire_smoke.py bad-signature \
  --target http://localhost:8000/webhook/github
```

**Expected**:
```
✅ bad-signature (expect 401)   → HTTP 401  (<N>ms)
```

**Verification**:
```
✅ HTTP 401 (not 500)
✅ No DB row created, no Redis entry
```

---

## 4. Smoke Without GitHub

For CI pipelines or quick local checks where a real GitHub App is unavailable, use `fire_smoke.py` to fire signed payloads directly at the local server:

```bash
# Fire all scenarios (server must be running, secret must match):
export OPENBOT_GITHUB_WEBHOOK_SECRET=your-dev-secret
uv run python scripts/fire_smoke.py --all \
  --target http://localhost:8000/webhook/github \
  --secret "$OPENBOT_GITHUB_WEBHOOK_SECRET"

# Expected output (all ✅):
# 11:22:33  ✅ issue-opened              delivery=smoke-issue-  → HTTP 202  (12ms)
# 11:22:33  ✅ pr-opened                 delivery=smoke-pr-ope  → HTTP 202  (8ms)
# 11:22:33  ✅ pr-synchronize            delivery=smoke-pr-syn  → HTTP 202  (7ms)
# 11:22:33  ✅ chat-mention              delivery=smoke-chat-1  → HTTP 202  (9ms)
# 11:22:33  ✅ bad-signature (expect 401) delivery=smoke-bad-s  → HTTP 401  (3ms)
# 11:22:33  ✅ concurrent-sync-A         delivery=smoke-conc-A  → HTTP 202  (11ms)
# 11:22:33  ✅ concurrent-sync-B         delivery=smoke-conc-B  → HTTP 202  (10ms)
# 11:22:33
# 11:22:33  🎉 all smoke scenarios passed
```

The smoke script validates the **receive path** (HMAC, parse, state machine, enqueue) but not the **write-back path** (GitHub API comments). For write-back validation, real GitHub App credentials are required.

---

## 5. Release Checklist

Before every production deployment, complete these steps in order:

```
Pre-flight
[ ] All automated tests pass: make check
[ ] Integration tests pass: uv run pytest tests/integration/ -v
[ ] Smoke script passes: uv run python scripts/fire_smoke.py --all

GitHub App E2E (requires test repo + smee relay)
[ ] S-01 Issue triage ACK
[ ] S-02 PR review ACK
[ ] S-03 Supersede on push (no duplicate comments)
[ ] S-04 @openbot chat mention
[ ] S-07 Bot self-reply guard
[ ] S-08 Signature rejection

Deploy to staging
[ ] Deploy to staging environment
[ ] Re-run smoke script against staging: uv run python scripts/fire_smoke.py --all --target https://openbot-staging.example.com
[ ] Monitor Papertrail/Upstash for 5 min — no unexpected 5xx

Deploy to production
[ ] Deploy to production
[ ] Fire one real issue in the production test repo → verify bot responds
[ ] Monitor dashboards for 15 min
```

---

## 6. Troubleshooting

### smee relay shows events but server returns 401

The webhook secret in `.env` doesn't match the GitHub App's webhook secret.
```bash
# Verify the secret is loaded:
uv run python -c "from openbot.core.settings import get_settings; s = get_settings(); print(bool(s.github_webhook_secret))"
```

### smee relay shows no events after creating issue

1. Check the GitHub App is installed on the test repository.
2. Verify the smee channel URL in the App's webhook settings matches the one the relay is listening to.
3. Check the App's **Recent Deliveries** tab in GitHub App settings for failed attempts.

### Worker exits without processing

Check `OPENBOT_REDIS_URL` is set and reachable:
```bash
redis-cli -u $OPENBOT_REDIS_URL ping  # should return PONG
```

### PEL stuck after worker crash

If `_PENDING_IDLE_MS` is the production default (60 s), the entry stays in PEL for 60 s before a new worker can claim it. Wait 60 s and restart the worker, or temporarily reduce `_PENDING_IDLE_MS` in `.env` for testing.

---

## 7. Useful Redis Debugging Commands

```bash
# Watch the stream in real time:
redis-cli XREAD BLOCK 0 STREAMS openbot.application.workflows $

# Check pending entries (PEL):
redis-cli XPENDING openbot.application.workflows openbot.application.workflows:group - + 10

# Check DLQ entries:
redis-cli XRANGE openbot.application.workflows:dead - + COUNT 10

# Count active cancel flags:
redis-cli KEYS "openbot:run_cancel:*" | wc -l

# Flush everything (dev only!):
redis-cli FLUSHDB
```

---

## 8. smee Relay Reference

```
usage: smee_relay.py [-h] [--new] [--target URL] [-v] [channel]

positional arguments:
  channel        smee.io channel URL (e.g. https://smee.io/abc123)
                 Omit to use SMEE_CHANNEL env var.

options:
  --new          Create a new smee.io channel then start relaying.
  --target URL   Local webhook endpoint (default: http://localhost:8000/webhook/github)
  -v, --verbose  Print full request/response details for each event.
```

```
usage: fire_smoke.py [-h] [--all] [--target URL] [--secret SECRET] [-v]
                     [{issue-opened,pr-opened,pr-synchronize,chat-mention,
                       bad-signature,concurrent-sync}]

Environment variables:
  OPENBOT_GITHUB_WEBHOOK_SECRET   webhook HMAC secret (overrides --secret)
  FIRE_SMOKE_TARGET               server URL (overrides --target)
```
