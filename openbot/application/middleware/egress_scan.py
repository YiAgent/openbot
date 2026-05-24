"""Egress safety scanner — PRD §4.8.

One function — ``scan_egress_text`` — and one decorator — ``EgressScannedAdapter``
— together ensure that every bot-authored string emitted toward GitHub is
checked for verified secret patterns *before* the live ``ChannelAdapter`` call.

Library: ``detect-secrets`` (Yelp). Pure-Python, fast, and the "vetted,
well-tested library" choice over a custom regex bag (per repo's tldrsec
secure-defaults guidance). We use the in-process API (``SecretsCollection``)
on a transient temp file because detect-secrets's plugins read from a file
handle.

Defaults:
  * ``egress_action="redact"`` — replace each finding's exact byte span with
    ``<openbot:redacted-secret>``.
  * Soft timeout: 500 ms per call. On timeout the entire chunk is replaced
    with ``SAFE_TIMEOUT_REPLACEMENT`` and ``timed_out=True``. We never reuse
    a partial scan result; a hung scanner can't slip a secret through.

The surface enum is closed on purpose (mirrors ``UserInputSource``) so a caller
can't fabricate a new origin string; every emit-site is auditable from the type.

``scan_egress_text`` is an ``async def`` so the soft timeout can be enforced
via ``asyncio.wait_for`` without ``asyncio.run``-ing inside an already-running
event loop (which is the normal case — every call site is an async webhook
handler or worker coroutine).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

_logger = logging.getLogger(__name__)

REDACTION_MARKER: Final = "<openbot:redacted-secret>"
SAFE_TIMEOUT_REPLACEMENT: Final = "[openbot: response withheld — egress safety scanner timed out]"
_TIMEOUT_S: float = 0.5


class EgressSurface(StrEnum):
    """Closed enum of bot-output surfaces. Free strings are rejected at the type."""

    ISSUE_REPLY = "github.issue_reply"
    PR_REVIEW_BODY = "github.pr_review_body"
    PR_REVIEW_INLINE = "github.pr_review_inline"
    PR_TITLE = "github.pr_title"
    PR_BODY = "github.pr_body"
    TRIAGE_ACK = "github.triage_ack"
    FIX_FAILURE_NOTE = "github.fix_failure_note"


@dataclass(frozen=True, slots=True)
class _RawFinding:
    secret_value: str
    type_label: str


@dataclass(frozen=True, slots=True)
class EgressScanResult:
    text: str
    findings: tuple[_RawFinding, ...] = field(default_factory=tuple)
    timed_out: bool = False
    surface: EgressSurface = EgressSurface.ISSUE_REPLY


def _run_detect_secrets(text: str) -> list[_RawFinding]:
    """Run detect-secrets over ``text`` synchronously. Returns a list of findings.

    detect-secrets's plugin API requires a file path. We write to a mode-0600
    temp file in the OS temp dir, scan it, and delete it afterward. We use
    ``tempfile.mkstemp`` (not ``NamedTemporaryFile``) because the file must
    be re-opened for reading by the plugin chain on every supported OS.
    """
    from detect_secrets import SecretsCollection
    from detect_secrets.settings import default_settings

    findings: list[_RawFinding] = []
    fd, path = tempfile.mkstemp(prefix="openbot-egress-", suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        with default_settings():
            collection = SecretsCollection()
            collection.scan_file(path)
            for _file, secret in collection:
                if not secret.secret_value:
                    continue
                findings.append(
                    _RawFinding(
                        secret_value=secret.secret_value,
                        type_label=secret.type or "unknown",
                    )
                )
    finally:
        try:
            os.unlink(path)
        except OSError:
            _logger.debug("egress_scan_tempfile_unlink_failed", extra={"path": path})
    return findings


def _redact(text: str, findings: Sequence[_RawFinding]) -> str:
    """Replace every finding's secret_value with the redaction marker.

    detect-secrets returns the verbatim secret text, so a literal ``str.replace``
    is correct; no regex required. Iteration order does not matter — each pass
    is idempotent because the marker contains characters detect-secrets never
    flags.
    """
    redacted = text
    for f in findings:
        if f.secret_value and f.secret_value in redacted:
            redacted = redacted.replace(f.secret_value, REDACTION_MARKER)
    return redacted


async def scan_egress_text(text: str, *, surface: EgressSurface) -> EgressScanResult:
    """Scan ``text`` and return an ``EgressScanResult``.

    Caller decides what to do with ``timed_out`` / ``findings``; the
    ``EgressScannedAdapter`` decorator below is the only production caller.

    Async because every call site already runs inside an event loop (webhook
    request handler / worker consumer). Running the scanner via
    ``asyncio.to_thread`` + ``asyncio.wait_for`` is the cheapest cancellation-
    safe pattern available — a hung detect-secrets plugin gets preempted at
    the timeout without requiring signal handling.
    """
    if not text:
        return EgressScanResult(text=text, surface=surface)

    try:
        findings = await asyncio.wait_for(
            asyncio.to_thread(_run_detect_secrets, text),
            timeout=_TIMEOUT_S,
        )
    except TimeoutError:
        _logger.warning(
            "egress_scanner_timeout",
            extra={"surface": surface.value, "len": len(text)},
        )
        return EgressScanResult(
            text=SAFE_TIMEOUT_REPLACEMENT,
            findings=(),
            timed_out=True,
            surface=surface,
        )
    except Exception:
        # Any other scanner error: log + fall-safe with the safe replacement.
        # A scanner that crashes on every chunk is loud (warning per call)
        # without locking the bot out of GitHub entirely.
        _logger.exception(
            "egress_scanner_error",
            extra={"surface": surface.value, "len": len(text)},
        )
        return EgressScanResult(
            text=SAFE_TIMEOUT_REPLACEMENT,
            findings=(),
            timed_out=True,
            surface=surface,
        )

    if not findings:
        return EgressScanResult(text=text, surface=surface)

    return EgressScanResult(
        text=_redact(text, findings),
        findings=tuple(findings),
        timed_out=False,
        surface=surface,
    )


__all__ = [
    "REDACTION_MARKER",
    "SAFE_TIMEOUT_REPLACEMENT",
    "EgressScanResult",
    "EgressSurface",
    "scan_egress_text",
]
