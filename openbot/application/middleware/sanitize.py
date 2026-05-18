"""SanitizeInputs middleware — entry-side byte gate (chain position #1).

Spec anchor: docs/_archive/prd-history/openbot-harness-spec.md §3 M8
(entry-side companion to the LLM-side ``wrap_user_input``). The harness
spec is archived; current chain layout lives in
docs/specs/2026-05-17-webhook-worker-layering-design.md §3.2 D2.

This is a **byte-level** gate that runs first in the pre-flight chain.
It operates on the immutable ``UnifiedEvent`` fields carrying
user-controlled text (``comment_body`` today; ``title`` / ``body`` live
under ``event.raw`` and are sanitized at point-of-use by the LLM-side
``wrap_user_input`` instead). A sanitized copy is stashed on
``ctx.cache[_SANITIZED_EVENT_KEY]`` for downstream middlewares and the
workflow handler to consume via the ``sanitized_event(ctx)`` helper.

Three responsibilities:

  1. **Strip invisible Unicode.** ``Cf`` (format — zero-width, RTL
     override, BOM) and ``Cc`` (other controls, except ``\\n`` ``\\t``)
     are removed. Otherwise ``@openbot​stop`` with a ZWSP slips past
     ``CancelCommentMiddleware`` and a RTL override breaks log/tooling
     readers downstream.

  2. **Length-cap.** Single-field cap (``max_field_length``) truncates;
     total-length cap (``max_total_length``) BLOCKS with
     ``reason="input_too_large"`` and phase REJECTED. The total is
     computed AFTER sanitization so an attacker padding with zero-width
     bytes doesn't bloat the real budget.

  3. **Actor login regex.** Anything user-controlled that ends up in a
     Redis key (rate-limit / cancel-thread counters) must match
     ``^[A-Za-z0-9-]{1,39}$`` — the GitHub login grammar. A handcrafted
     actor with ``:`` or ``\\n`` would split into a different Redis key
     and either bypass the cap or trample neighbor keys.

Distinct-from-`wrap_user_input` rationale: ``wrap_user_input`` defends
the **prompt-structure** boundary (HTML-escape + tagged block); this
defends the **byte-format** boundary at chain entry. Both layers stay
because they cover non-overlapping attack vectors.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from openbot.application.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.persistence.models import WorkflowPhase

if TYPE_CHECKING:
    from openbot.events import UnifiedEvent

_logger = logging.getLogger("openbot.middleware.sanitize")

# 8 KB / field — well above realistic chat comments but bounds the
# per-field tokenization cost downstream. Tunable per-instance via the
# dataclass field.
_DEFAULT_MAX_FIELD_LENGTH: Final = 8_000
# 64 KB total — roughly GitHub's per-comment ceiling. A single webhook
# payload over this is either misconfigured or adversarial.
_DEFAULT_MAX_TOTAL_LENGTH: Final = 64_000

# GitHub login grammar (PRD §5.1 / spec §9.1 lock). Mirrors the literal
# in ``openbot.middleware.rate_limit`` rather than importing it; a
# circular slice-A/B import isn't worth one regex.
_ACTOR_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,39}$")

# Public cache key. Exported via ``sanitized_event(ctx)`` so callers
# don't have to know the literal — keeps the rename surface to one
# place if the cache schema ever changes.
_SANITIZED_EVENT_KEY: Final = "sanitize_inputs.event"

# UnifiedEvent fields we sanitize. Only ``comment_body`` is on the
# struct today; future fields (e.g. an authored title) can join here
# without changing the loop logic.
_TEXT_FIELDS: Final = ("comment_body",)


def _sanitize_string(text: str) -> str:
    """Drop format/control characters; preserve ``\\n`` and ``\\t`` only.

    Uses ``unicodedata.category`` so we don't have to enumerate every
    invisible codepoint (U+200B-200F, U+202A-202E, U+2060, U+FEFF, …).
    The general-category-based filter is more durable than a static
    deny-list — new format codepoints in future Unicode releases are
    handled without changes here.
    """
    cleaned: list[str] = []
    for ch in text:
        if ch in ("\n", "\t"):
            cleaned.append(ch)
            continue
        category = unicodedata.category(ch)
        # ``Cf`` = format (ZWSP, RLO, BOM, …) — invisible by design,
        # zero semantic role in a comment body.
        # ``Cc`` = other control (NUL, ESC, …) — only ``\\n`` ``\\t``
        # carry meaning; drop the rest.
        if category in ("Cf", "Cc"):
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def sanitized_event(ctx: PreflightContext) -> UnifiedEvent:
    """Return the sanitized copy of ``ctx.event``, falling back to the original.

    Downstream callers always read through this helper, never
    ``ctx.event`` directly. When ``SanitizeInputsMiddleware`` ran first
    (the only supported order) the sanitized version is on the cache;
    otherwise we hand back the raw event so unit tests that bypass the
    chain still get a value.
    """
    return ctx.cache.get(_SANITIZED_EVENT_KEY, ctx.event)


@dataclass(frozen=True, slots=True)
class SanitizeInputsMiddleware:
    """Chain position #1 — invisible-byte filter + actor + size caps."""

    name: str = "sanitize_inputs"
    max_field_length: int = _DEFAULT_MAX_FIELD_LENGTH
    max_total_length: int = _DEFAULT_MAX_TOTAL_LENGTH

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        event = ctx.event

        # ── 1) actor regex gate ──
        # Run before other work — cheapest check, and rejecting an
        # invalid actor here prevents the rate-limit middleware from
        # ever using it as a Redis key segment.
        if event.actor and not _ACTOR_RE.match(event.actor):
            _logger.warning(
                "sanitize_actor_invalid",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    # ``repr`` exposes escaped control chars (``\\x00``,
                    # ``\\u202e``) so an audit reader can tell why we
                    # rejected without the raw byte sequence ending up
                    # in a terminal that interprets it.
                    "actor_repr": repr(event.actor)[:64],
                },
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="actor_invalid",
                audit_phase=WorkflowPhase.REJECTED,
                # No reply — posting back to a forged actor login is
                # itself a privilege-escalation vector (force-send to
                # any GitHub login the attacker chose).
            )

        # ── 2) per-field sanitize + per-field truncate ──
        # We never mutate the frozen ``event`` in place; build a dict
        # of new values and use ``dataclasses.replace`` once at the
        # end. The dict starts empty so we only allocate a new event
        # when something actually changed.
        new_fields: dict[str, str] = {}
        truncated: list[str] = []
        # Effective value per field — used both for the dataclass
        # replace step and the total-length budget below.
        effective: dict[str, str] = {}
        for field_name in _TEXT_FIELDS:
            raw = getattr(event, field_name)
            if raw is None:
                continue
            assert isinstance(raw, str)
            cleaned = _sanitize_string(raw)
            if len(cleaned) > self.max_field_length:
                cleaned = cleaned[: self.max_field_length]
                truncated.append(field_name)
            effective[field_name] = cleaned
            if cleaned != raw:
                new_fields[field_name] = cleaned

        # ── 3) total-length DoS gate ──
        # Sum after sanitize/truncate so the budget is the visible
        # length, not the attacker's padded length.
        total = sum(len(value) for value in effective.values())
        if total > self.max_total_length:
            _logger.warning(
                "sanitize_input_too_large",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "total_chars": total,
                    "limit": self.max_total_length,
                },
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="input_too_large",
                audit_phase=WorkflowPhase.REJECTED,
            )

        # ── 4) Stash sanitized event for downstream ──
        # Always set the cache key (even if unchanged) so callers
        # using ``sanitized_event(ctx)`` see consistent behavior.
        sanitized = replace(event, **new_fields) if new_fields else event
        ctx.cache[_SANITIZED_EVENT_KEY] = sanitized

        if truncated:
            _logger.info(
                "sanitize_field_truncated",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "fields": truncated,
                    "limit": self.max_field_length,
                },
            )

        return MiddlewareDecision.proceed()


__all__ = ["SanitizeInputsMiddleware", "sanitized_event"]
