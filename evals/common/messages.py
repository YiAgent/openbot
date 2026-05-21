"""LangChain message text extraction utilities shared across eval solvers."""

from __future__ import annotations

from typing import Any


def extract_message_text(message: Any) -> str:
    """Extract plain text from a LangChain message (str or list-of-blocks)."""
    text = message.content if hasattr(message, "content") else str(message)
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return str(text)


def join_message_texts(messages: list[Any]) -> str:
    """Concatenate all message texts for offline debugging / safety scans."""
    parts = [extract_message_text(msg).strip() for msg in messages]
    return "\n\n".join(part for part in parts if part)
