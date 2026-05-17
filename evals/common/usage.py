"""Provider-usage aggregation shared by all eval solvers.

LangChain attaches ``usage_metadata`` to *each* AI message in the agent's
message log — i.e. one entry per underlying LLM call. A deepagents agent
typically makes 20-50 tool-call rounds per sample, so the per-sample token
total is the **sum** across those messages, not the figure on any single
message.

Why this matters: LangSmith's trace view sums child-run usage automatically,
producing the "correct" total (hundreds of thousands of tokens for a typical
fix sample). The experiment dashboard, however, posts whatever we stuff into
``state.metadata["provider_usage"]`` as the sample's authoritative usage
payload. Returning only the last AI message's usage (the previous behavior)
under-reported by 1/N — the trace would show 300k tokens while the
experiment column read ~10-20k. Aggregating here brings the two views back
into agreement.
"""

from __future__ import annotations

from typing import Any

# Accepted alias keys: LangChain's modern schema uses ``input_tokens`` /
# ``output_tokens``; some older providers (and ``response_metadata.usage``)
# still surface OpenAI's ``prompt_tokens`` / ``completion_tokens`` shape.
# We accept both on input but normalise to the modern names on output.
_INPUT_ALIASES = ("input_tokens", "prompt_tokens")
_OUTPUT_ALIASES = ("output_tokens", "completion_tokens")


def _coerce_usage_dict(message: Any) -> dict[str, Any] | None:
    """Pull a usage dict off ``message`` if either common location has one."""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        return usage
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        nested = response_metadata.get("usage")
        if isinstance(nested, dict):
            return nested
    return None


def _first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int):
            return value
    return None


def _accumulate_int_dict(dest: dict[str, int], source: dict[str, Any] | None) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if isinstance(value, int):
            dest[key] = dest.get(key, 0) + value


def aggregate_provider_usage(messages: list[Any]) -> dict[str, Any] | None:
    """Sum LangChain usage_metadata across every AI message.

    Returns ``None`` if no message carried a usage payload (e.g. the agent
    crashed before any LLM call completed). The returned dict mirrors the
    LangChain ``usage_metadata`` shape — ``input_tokens`` / ``output_tokens``
    / ``total_tokens`` plus optional ``input_token_details`` /
    ``output_token_details`` sub-dicts (cache_read, cache_creation,
    reasoning, …) summed per inner key. ``total_cost`` is also summed when
    present so the experiment column reflects the full sample's spend.
    """
    input_total = 0
    output_total = 0
    total_total = 0
    saw_total = False
    cost_total = 0.0
    saw_cost = False
    input_details: dict[str, int] = {}
    output_details: dict[str, int] = {}
    saw_any = False

    for message in messages:
        usage = _coerce_usage_dict(message)
        if usage is None:
            continue
        saw_any = True

        inp = _first_int(usage, _INPUT_ALIASES)
        out = _first_int(usage, _OUTPUT_ALIASES)
        tot = usage.get("total_tokens")

        if inp is not None:
            input_total += inp
        if out is not None:
            output_total += out
        if isinstance(tot, int):
            total_total += tot
            saw_total = True
        elif inp is not None and out is not None:
            # Older shapes sometimes omit ``total_tokens``; reconstruct so
            # the experiment view doesn't drop the per-call contribution.
            total_total += inp + out
            saw_total = True

        cost = usage.get("total_cost")
        if isinstance(cost, int | float):
            cost_total += float(cost)
            saw_cost = True

        _accumulate_int_dict(input_details, usage.get("input_token_details"))
        _accumulate_int_dict(output_details, usage.get("output_token_details"))

    if not saw_any:
        return None

    result: dict[str, Any] = {
        "input_tokens": input_total,
        "output_tokens": output_total,
    }
    if saw_total:
        result["total_tokens"] = total_total
    if saw_cost:
        result["total_cost"] = cost_total
    if input_details:
        result["input_token_details"] = input_details
    if output_details:
        result["output_token_details"] = output_details
    return result
