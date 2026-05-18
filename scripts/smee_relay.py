#!/usr/bin/env python3
"""smee.io → local webhook relay for L4 E2E testing.

Usage
-----
    # Relay to local dev server (default port 8000):
    uv run python scripts/smee_relay.py https://smee.io/<channel-id>

    # Relay to a custom local port:
    uv run python scripts/smee_relay.py https://smee.io/<channel-id> --target http://localhost:9000/webhook/github

    # Create a new smee channel automatically:
    uv run python scripts/smee_relay.py --new

    # Verbose mode (print each event summary):
    uv run python scripts/smee_relay.py https://smee.io/<channel-id> -v

What it does
------------
smee.io receives GitHub webhook POSTs and broadcasts them over SSE
(Server-Sent Events).  Each SSE ``data:`` field is a flat JSON object
that merges the webhook body with the GitHub delivery headers.

This relay:
  1. Subscribes to the smee.io channel via SSE (GET + streaming).
  2. Splits each event into headers (``x-*``, ``content-type``) + body.
  3. POSTs body + headers to the local FastAPI endpoint.
  4. Logs the local server's response code.
  5. Reconnects with exponential backoff on disconnect.

The local server must have OPENBOT_GITHUB_WEBHOOK_SECRET set to the
same secret configured in the GitHub App webhook settings.

Exit codes
----------
  0  — killed by signal (normal)
  1  — channel URL invalid or unreachable
  2  — smee channel not found (404)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

_logger = logging.getLogger("smee_relay")
_DEFAULT_TARGET = "http://localhost:8000/webhook/github"
# smee.io SSE reconnect backoff: start 1s, cap 60s.
_BACKOFF_INIT = 1.0
_BACKOFF_MAX = 60.0


# ── SSE parsing ──────────────────────────────────────────────────────────────


async def _sse_events(
    client: httpx.AsyncClient,
    url: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed smee.io SSE data objects (reconnects on drop).

    smee.io sends events as:
        event: message
        data: <json-object>

    We only care about ``data:`` lines (ignore ``event:``, ``:`` comments,
    and ``id:`` lines).
    """
    backoff = _BACKOFF_INIT
    while True:
        try:
            _logger.info("connecting to smee channel…")
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                timeout=None,  # SSE is a long-lived connection
            ) as response:
                if response.status_code == 404:
                    _logger.error("smee channel not found (404): %s", url)
                    sys.exit(2)
                response.raise_for_status()
                _logger.info("connected — waiting for events (ctrl-c to stop)")
                backoff = _BACKOFF_INIT  # reset on successful connect

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError as exc:
                        _logger.warning("skipped malformed SSE data: %s (%s)", raw[:80], exc)

        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
            _logger.warning("SSE connection dropped: %s — reconnecting in %.1fs", exc, backoff)
        except asyncio.CancelledError:
            return

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)


# ── Event forwarding ─────────────────────────────────────────────────────────

# Keys from smee that are HTTP headers, not webhook body fields.
_HEADER_PREFIXES = ("x-",)
_HEADER_EXACT = {"content-type", "user-agent"}


def _split_smee_data(
    data: dict[str, Any],
) -> tuple[dict[str, str], bytes]:
    """Return (headers, body_bytes) from a flat smee event object.

    smee.io merges the GitHub webhook body fields with the delivery
    headers into one JSON object.  We partition them:
      - ``x-*`` keys  →  headers (GitHub signature, event, delivery id)
      - ``content-type``, ``user-agent``  →  headers
      - everything else  →  webhook body (re-serialized as JSON)
    """
    headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}

    for key, value in data.items():
        is_header = any(key.startswith(p) for p in _HEADER_PREFIXES) or key in _HEADER_EXACT
        if is_header:
            headers[key] = str(value)
        else:
            body_fields[key] = value

    body_bytes = json.dumps(body_fields).encode()
    # Smee sends content-type from GitHub, but we override to ensure JSON.
    headers.setdefault("content-type", "application/json")
    return headers, body_bytes


async def _forward(
    client: httpx.AsyncClient,
    target: str,
    data: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    """POST one smee event to the local webhook endpoint."""
    headers, body = _split_smee_data(data)

    event_type = headers.get("x-github-event", "unknown")
    delivery = headers.get("x-github-delivery", "?")
    action = data.get("action", "")

    try:
        t0 = time.monotonic()
        resp = await client.post(target, content=body, headers=headers, timeout=10.0)
        elapsed_ms = (time.monotonic() - t0) * 1000

        status_emoji = "✅" if resp.status_code < 400 else "❌"
        summary = (
            f"{status_emoji} {event_type}.{action}"
            f"  delivery={delivery[:8]}"
            f"  → {resp.status_code}"
            f"  ({elapsed_ms:.0f}ms)"
        )
        if verbose:
            _logger.info(summary)
            try:
                _logger.info("  response: %s", resp.json())
            except Exception:
                _logger.info("  response body: %s", resp.text[:200])
        else:
            _logger.info(summary)

    except httpx.RequestError as exc:
        _logger.error(
            "failed to POST %s.%s to %s: %s",
            event_type,
            action,
            target,
            exc,
        )


# ── New channel creation ──────────────────────────────────────────────────────


async def _create_channel() -> str:
    """GET https://smee.io/new (follow redirect) to obtain a fresh channel URL."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get("https://smee.io/new", timeout=10.0)
        # After the redirect chain, the final URL is the new channel.
        final_url = str(resp.url)
        if final_url != "https://smee.io/new" and final_url.startswith("https://smee.io/"):
            return final_url
        raise RuntimeError(
            f"failed to create smee channel: HTTP {resp.status_code} url={final_url}"
        )


# ── Main relay loop ───────────────────────────────────────────────────────────


async def relay(
    channel_url: str,
    target: str = _DEFAULT_TARGET,
    *,
    verbose: bool = False,
) -> None:
    """Subscribe to ``channel_url`` and forward every event to ``target``."""
    async with httpx.AsyncClient() as sse_client, httpx.AsyncClient() as post_client:
        async for data in _sse_events(sse_client, channel_url):
            await _forward(post_client, target, data, verbose=verbose)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Forward smee.io webhooks to your local OpenBot server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "channel",
        nargs="?",
        help="smee.io channel URL (e.g. https://smee.io/abc123). Omit to use SMEE_CHANNEL env var.",
    )
    p.add_argument(
        "--new",
        action="store_true",
        help="Create a new smee.io channel and print its URL, then start relaying.",
    )
    p.add_argument(
        "--target",
        default=_DEFAULT_TARGET,
        metavar="URL",
        help=f"Local webhook endpoint (default: {_DEFAULT_TARGET})",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full request/response details for each event.",
    )
    return p.parse_args(argv)


async def _main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve channel URL.
    channel_url: str | None = args.channel

    if args.new:
        _logger.info("creating new smee.io channel…")
        channel_url = await _create_channel()
        print(f"\n  smee channel URL: {channel_url}\n")
        print("  Set this as your GitHub App's webhook URL, then configure:")
        print(f"  SMEE_CHANNEL={channel_url}")
        print()

    if not channel_url:
        import os

        channel_url = os.environ.get("SMEE_CHANNEL")

    if not channel_url:
        print(
            "error: provide a smee channel URL as argument, set SMEE_CHANNEL env var, or use --new",
            file=sys.stderr,
        )
        sys.exit(1)

    if not channel_url.startswith("https://smee.io/"):
        print(f"error: expected a smee.io URL, got: {channel_url}", file=sys.stderr)
        sys.exit(1)

    _logger.info("relay: %s → %s", channel_url, args.target)
    _logger.info("press ctrl-c to stop")

    try:
        await relay(channel_url, args.target, verbose=args.verbose)
    except KeyboardInterrupt:
        _logger.info("stopped")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
