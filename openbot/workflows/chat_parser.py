"""@openbot chat command parser — PRD §4.4 / harness spec §3 M11.

A *thin* parser. It doesn't interpret natural language — that's the
chat LLM's job. This module's responsibility is to:

  - Confirm the comment is a real `@openbot` mention (vs. `@openbot-dev`,
    `someone said @openbot stop`, an empty comment, etc.).
  - Classify the immediate command word as a known structural intent:
    ``is_cancel`` (handled by CancelCommentMiddleware), ``is_help``
    (canned response), or neither (LLM intent).
  - Hand back the post-mention body so the LLM-driven path sees a
    cleaned-up request without the ``@openbot `` prefix.

It does NOT decide whether to ACK / refuse / answer — that's the
chat workflow's job. Slice C only wires the cancel-vs-help-vs-LLM
branches into `maybe_run_chat`; the actual LLM call lands later.

Safety:

  - Strips Unicode format controls (``Cf`` category — zero-width space,
    RTL override, etc.) so ``@openbot​stop`` doesn't bypass detection.
  - Caps body length at 2048 chars before regex match (PRD §4.8
    prompt-injection: a 50-MB paste must not stall the regex engine).
  - Compiled, anchored, length-bounded regex (``re.match``, not
    ``re.search``) — only matches at the start of the comment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

# Same cap CancelCommentMiddleware uses — keep both in sync.
_BODY_MAX_CHARS: Final = 4096
# Captured-group cap (2 KB) is well above any realistic chat command
# (a question fits in ~200 chars). The outer body cap (4 KB) protects
# the regex engine itself; this cap protects the LLM prompt downstream.
#
# Deliberately no `\s*$` anchor: we want to classify `@openbot stop`
# correctly even if the user pastes 50 MB of garbage after it. The
# captured group is bounded; anything past 2 KB is silently dropped.
_MENTION_RE: Final = re.compile(
    r"^@(?:openbot|yibots)(?=\s|$)(?:\s+(.{1,2048}))?",
    re.IGNORECASE | re.DOTALL,
)

# Structural keywords. Anything else is treated as "freeform" and
# handed to the LLM. Keeping this list small + closed prevents
# `@openbot please stop spamming me` from accidentally tripping cancel
# (the keyword must be the FIRST whitespace-delimited token).
_CANCEL_KEYWORDS: Final = frozenset({"stop", "cancel", "停", "取消"})
# Accept both ASCII `?` and the CJK fullwidth variant (U+FF1F) —
# Chinese keyboards default to the fullwidth form. The `noqa: RUF001`
# is correct here: the fullwidth char is the *intent*, not a typo.
_HELP_KEYWORDS: Final = frozenset({"help", "?", "？", "帮助"})  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class ChatCommand:
    """Parsed mention.

    ``body_after_mention`` is the captured group with structural
    keywords stripped — i.e. what the LLM should receive. For
    ``@openbot help me reproduce``, body_after_mention is
    ``"help me reproduce"``; for ``@openbot stop``, it's ``"stop"``
    (the workflow will short-circuit on ``is_cancel`` and ignore body).
    """

    raw_mention: str
    body_after_mention: str
    is_cancel: bool
    is_help: bool

    @property
    def is_freeform(self) -> bool:
        """Convenience — body is meant for the LLM."""
        return not self.is_cancel and not self.is_help


def parse(comment_body: str | None) -> ChatCommand | None:
    """Return a ChatCommand if the comment is an ``@openbot`` mention.

    Returns ``None`` (not raise) for:

      - empty / None bodies
      - lookalike logins (``@openbot-dev``)
      - mentions not at the start of the message (``hey @openbot stop``)
      - bodies that exceed the post-strip cap

    The Router's chat-prefix check is the first line of defense; this
    parser is the second. It's safe for any non-trusted comment to
    pass through — the worst case is `None`.
    """
    if not comment_body:
        return None

    # Strip zero-width / RTL-override style controls so visually
    # indistinguishable variants can't bypass detection.
    cleaned = "".join(ch for ch in comment_body if unicodedata.category(ch) != "Cf")
    if len(cleaned) > _BODY_MAX_CHARS:
        cleaned = cleaned[:_BODY_MAX_CHARS]

    match = _MENTION_RE.match(cleaned.strip())
    if match is None:
        return None

    captured = (match.group(1) or "").strip()
    if not captured:
        # Bare `@openbot` ping — no command word. Treat as freeform
        # request with empty body; the LLM path will reply with a
        # generic acknowledgement.
        return ChatCommand(
            raw_mention=cleaned, body_after_mention="", is_cancel=False, is_help=False
        )

    first_word, _, _ = captured.partition(" ")
    # CJK keywords aren't ASCII-lowered the same way, but our keyword
    # sets are stored in canonical form (stop/cancel/help/帮助 etc.)
    # and `.lower()` is a safe no-op for CJK codepoints, so a single
    # cast is enough.
    first_lower = first_word.lower()

    return ChatCommand(
        raw_mention=cleaned,
        body_after_mention=captured,
        is_cancel=first_lower in _CANCEL_KEYWORDS,
        is_help=first_lower in _HELP_KEYWORDS,
    )


__all__ = ["ChatCommand", "parse"]
