"""GitHub VCR config used by tests/real_service/github/.

Centralises secret redaction + match policy so individual test files
don't reinvent it. The pytest_recording plugin picks this up via the
vcr_config fixture (see tests/real_service/github/conftest.py).
"""

from __future__ import annotations

import re
from typing import Any

REDACT_HEADERS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-github-delivery",
    "x-hub-signature",
    "x-hub-signature-256",
)

_REDACT_BODY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'"token":\s*"[^"]+"'), '"token": "REDACTED"'),
    (re.compile(r'"private_key":\s*"[^"]+"'), '"private_key": "REDACTED"'),
)


def redact_response_body(response: dict[str, Any]) -> dict[str, Any]:
    """Strip secret-like substrings from VCR response bodies in-place."""
    body = response.get("body", {}).get("string", b"")
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
        for pat, repl in _REDACT_BODY_PATTERNS:
            text = pat.sub(repl, text)
        response["body"]["string"] = text.encode("utf-8")
    return response


def github_vcr_config() -> dict[str, Any]:
    """Return a vcrpy config dict suitable for the @pytest.mark.vcr fixture."""
    return {
        "filter_headers": list(REDACT_HEADERS),
        "filter_query_parameters": ["access_token"],
        "before_record_response": redact_response_body,
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        "record_mode": "none",  # default replay-only; CLI overrides at record time
        "decode_compressed_response": True,
    }


__all__ = ["REDACT_HEADERS", "github_vcr_config", "redact_response_body"]
