"""`make doctor` — first-run self-check for an OpenBot deployment.

PRD §7 promises a "30-minute setup". This script answers the question
"did setup work?" without requiring the user to read every log line.

Checks (run in order; first failure exits non-zero):

  1. **ENV completeness** — required ``OPENBOT_*`` variables are set;
     the GitHub App PEM is readable and chmod 0600 (private key spill
     prevention; PRD §13 #5 + §8.4 lock).
  2. **Postgres reachable** — connect with ``OPENBOT_POSTGRES_URL`` and
     ``SELECT 1``.
  3. **Redis reachable** — ``PING``.
  4. **Schema present** — ``audit_log`` + ``cost_meter`` tables exist
     (we ``SELECT 1 FROM <t> LIMIT 0`` so empty tables still pass).
  5. **Signed webhook round-trip** — compute HMAC-SHA-256 over a synthetic
     ``ping`` payload with ``OPENBOT_GITHUB_WEBHOOK_SECRET``, POST it to
     ``/webhook/github``, expect ``accepted`` or ``ignored``. Skipped
     with a friendly message when the server isn't running.

Secrets are never echoed. Only "present/absent" / "ok/fail" is printed.

Exit codes:
  0 — every check passed (or the optional ones were cleanly skipped)
  1 — a required check failed (or an unexpected exception during a check)

Usage::

    uv run python scripts/doctor.py [--skip-webhook] [--port 8080]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Stylistic choice: doctor output is a tiny CLI dashboard. Plain stdout +
# exit codes (no logging module) — keeps the surface readable for users
# who pipe it through `tee` or `grep` for CI.


# ───────────────────────── presentation ─────────────────────────


def _emit(prefix: str, msg: str) -> None:
    """One-line dashboard row."""
    print(f"{prefix} {msg}", flush=True)


def _ok(msg: str) -> None:
    _emit("✓", msg)


def _fail(msg: str) -> None:
    _emit("✗", msg)


def _skip(msg: str) -> None:
    _emit("↷", msg)


# ───────────────────────── check 1 · ENV ─────────────────────────


_REQUIRED_ENV: tuple[str, ...] = (
    "OPENBOT_GITHUB_WEBHOOK_SECRET",
    "OPENBOT_GITHUB_APP_ID",
    "OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH",
    "OPENBOT_POSTGRES_URL",
    "OPENBOT_REDIS_URL",
)


def check_env(settings_env: dict[str, str]) -> bool:
    """Return True iff every required env var is set and the PEM is safe."""
    missing = [name for name in _REQUIRED_ENV if not settings_env.get(name)]
    if missing:
        _fail(f"env: missing {', '.join(missing)}")
        return False

    # PEM existence + permissions. We never read the file's contents.
    pem_path = Path(settings_env["OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH"]).expanduser()
    if not pem_path.exists():
        _fail(f"env: PEM not found at {pem_path}")
        return False
    if not pem_path.is_file():
        _fail(f"env: PEM path is not a regular file: {pem_path}")
        return False
    # 0o600 (rw-------) is the documented standard for SSH-style keys.
    # On non-POSIX filesystems mode bits may be meaningless; skip the
    # check with a soft warning so Windows / WSL users aren't stuck.
    mode = pem_path.stat().st_mode & 0o777
    if mode != 0o600:
        if sys.platform == "win32":
            _skip(f"env: PEM mode {oct(mode)} (skipping POSIX 0600 check on Windows)")
        else:
            _fail(f"env: PEM at {pem_path} is mode {oct(mode)}; expected 0o600")
            return False

    _ok("env: required OPENBOT_* set, PEM 0600")
    return True


# ───────────────────────── check 2 · Postgres ─────────────────────────


async def check_postgres(postgres_url: str) -> bool:
    """Open a fresh connection, run ``SELECT 1``, close. No pool reuse."""
    try:
        # Import here so a missing optional dep doesn't crash check 1.
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError as exc:
        _fail(f"postgres: sqlalchemy not installed ({exc})")
        return False

    engine = create_async_engine(postgres_url, future=True)
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        _fail(f"postgres: connect failed ({type(exc).__name__}: {exc})")
        return False
    finally:
        await engine.dispose()

    _ok("postgres: SELECT 1 returned")
    return True


# ───────────────────────── check 3 · Redis ─────────────────────────


async def check_redis(redis_url: str) -> bool:
    try:
        import redis.asyncio as redis_async
    except ImportError as exc:
        _fail(f"redis: redis-py not installed ({exc})")
        return False

    client = redis_async.from_url(redis_url, decode_responses=True)
    try:
        pong = await client.ping()
    except Exception as exc:
        _fail(f"redis: PING failed ({type(exc).__name__}: {exc})")
        return False
    finally:
        await client.aclose()

    if not pong:
        _fail("redis: PING returned falsy")
        return False
    _ok("redis: PING ok")
    return True


# ───────────────────────── check 4 · schema ─────────────────────────


async def check_schema(postgres_url: str) -> bool:
    """Verify ``audit_log`` + ``cost_meter`` exist (independent of row count).

    Uses the ORM ``Base.metadata`` reflection point rather than building
    SQL strings: the table list is sourced from the SQLAlchemy declarative
    metadata (compile-time symbols, not user input), and ``Inspector``
    issues parameterized catalog queries internally. This sidesteps the
    "f-string into ``text()``" pattern that's a well-known SQLi smell.
    """
    try:
        from sqlalchemy import inspect
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError as exc:
        _fail(f"schema: sqlalchemy not installed ({exc})")
        return False

    # Importing the ORM models registers them on `Base.metadata`. Doctor
    # is a CLI script — circular-import risk is zero here.
    from openbot.infrastructure.persistence.models import AuditLog, CostMeter

    required_tables = (AuditLog.__tablename__, CostMeter.__tablename__)

    engine = create_async_engine(postgres_url, future=True)
    missing: list[str] = []
    try:
        async with engine.connect() as conn:
            # `run_sync` lets the synchronous Inspector API run against
            # the async engine without us hand-building catalog queries.
            existing = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        missing = [t for t in required_tables if t not in existing]
    finally:
        await engine.dispose()

    if missing:
        _fail(f"schema: missing tables: {', '.join(missing)} — run `alembic upgrade head`")
        return False
    _ok("schema: audit_log + cost_meter present")
    return True


# ───────────────────────── check 5 · webhook round-trip ────────────────────────


@dataclass(frozen=True, slots=True)
class _WebhookProbe:
    secret: bytes
    target_url: str


def _sign(secret: bytes, body: bytes) -> str:
    """`sha256=<hex>` — same format GitHub puts on the ``X-Hub-Signature-256`` header."""
    mac = hmac.new(secret, body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def check_webhook_round_trip(probe: _WebhookProbe) -> bool:
    """POST a synthetic `ping` event with a real HMAC. Skip on connect error."""
    try:
        import httpx
    except ImportError as exc:
        _fail(f"webhook: httpx not installed ({exc})")
        return False

    body = json.dumps(
        {"zen": "Keep it logically awesome.", "hook_id": 1, "hook": {"type": "App"}},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _sign(probe.secret, body)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": f"doctor-{int(time.time())}",
        "User-Agent": "openbot-doctor/0.1",
    }

    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            response = await client.post(probe.target_url, content=body, headers=headers)
        except httpx.ConnectError:
            _skip(f"webhook: server not running at {probe.target_url} (start `make dev`)")
            # Skipped cleanly — not a hard failure.
            return True
        except httpx.HTTPError as exc:
            _fail(f"webhook: POST failed ({type(exc).__name__}: {exc})")
            return False

    if response.status_code != 202:
        _fail(f"webhook: expected 202, got {response.status_code}: {response.text[:120]}")
        return False
    try:
        body_json = response.json()
    except ValueError:
        _fail(f"webhook: response not JSON ({response.text[:120]})")
        return False
    status = body_json.get("status")
    if status not in {"accepted", "ignored", "queued"}:
        _fail(f"webhook: unexpected status={status!r}")
        return False
    _ok(f"webhook: signed POST returned 202 status={status!r}")
    return True


# ───────────────────────── orchestration ─────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    env = dict(os.environ)

    print("OpenBot doctor — running self-check…", flush=True)
    print("(secrets are never echoed; only presence is checked)\n", flush=True)

    if not check_env(env):
        return 1

    postgres_url = env["OPENBOT_POSTGRES_URL"]
    redis_url = env["OPENBOT_REDIS_URL"]

    # Run Postgres + Redis in parallel — independent checks, no shared state.
    pg_ok, redis_ok = await asyncio.gather(
        check_postgres(postgres_url),
        check_redis(redis_url),
    )
    if not (pg_ok and redis_ok):
        return 1

    if not await check_schema(postgres_url):
        return 1

    if args.skip_webhook:
        _skip("webhook: --skip-webhook passed")
    else:
        secret = env["OPENBOT_GITHUB_WEBHOOK_SECRET"].encode("utf-8")
        target = f"http://{args.host}:{args.port}/webhook/github"
        ok = await check_webhook_round_trip(_WebhookProbe(secret=secret, target_url=target))
        if not ok:
            return 1

    print("\nAll checks passed ✓", flush=True)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openbot-doctor",
        description="OpenBot first-run self-check (PRD §7).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Webhook host for the round-trip check (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OPENBOT_DOCTOR_PORT", "8080")),
        help="Webhook port for the round-trip check (default: 8080).",
    )
    parser.add_argument(
        "--skip-webhook",
        action="store_true",
        help="Skip the signed-webhook round-trip; useful for headless CI.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Synchronous wrapper around the async checks. Returns process exit code."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


def _entrypoint() -> Any:
    """`uv run python scripts/doctor.py` lands here."""
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover — CLI entry
    _entrypoint()
