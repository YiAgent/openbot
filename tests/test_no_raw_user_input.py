"""Static defense against prompt injection — harness spec §3 M8 Acceptance #3.

What this test catches
======================

A developer writing one of these into a workflow / LLM module:

    messages = [{"role": "user", "content": event.comment_body}]
    prompt = f"Review this:\\n{event.comment_body}"
    body = "User said: " + event.body

Each of the above feeds raw user-controlled text directly into a
prompt context, bypassing the HTML-escape + tagged-block wrapping
provided by ``openbot.infrastructure.llm.sanitize.wrap_user_input``. The wrapper
exists so the model treats user content as data, not as instructions.

Heuristic
=========

For every ``event.{comment_body|title|body}`` attribute access or
``event.raw[...]`` subscript inside files under ``openbot/application/workflows/``
or ``openbot/infrastructure/llm/``, we flag if it appears inside one of three
"prompt-construction shapes":

  1. an f-string (``ast.JoinedStr``),
  2. string concatenation (``BinOp`` with operator ``Add``),
  3. a dict literal value (``ast.Dict.values``) — the shape used for
     OpenAI / Anthropic ``messages`` list entries.

Everything else (parser arguments, log ``extra`` dicts, control-flow
conditionals, local-variable bindings) is allowed. The test is
intentionally narrow: we want zero false positives on the existing
code while catching every plausible new prompt-injection vector.

Whitelisted sink
================

``wrap_user_input(...)`` is the documented escape hatch. Even when
its argument node would otherwise match — e.g. you write
``wrap_user_input(f"prefix {event.comment_body}")`` — we still flag
because the *inner* f-string already constructs an unwrapped string.
That's intentional: the safe form is
``f"prefix {wrap_user_input(event.comment_body, source=...)}"``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS: tuple[Path, ...] = (
    # Phase-1 restructure: real code moved to application/workflows/.
    # Keep old path so any future files there are also scanned.
    _REPO_ROOT / "openbot" / "workflows",
    _REPO_ROOT / "openbot" / "application" / "workflows",
    _REPO_ROOT / "openbot" / "infrastructure" / "llm",
)

# Fields on ``UnifiedEvent`` that carry user-controlled text (PRD §4.8
# threat surface). ``actor`` is intentionally NOT in this set —
# ``SanitizeInputsMiddleware`` regex-validates it at chain entry, and
# callers safely interpolate it into ACK templates.
_RAW_FIELDS: frozenset[str] = frozenset({"comment_body", "title", "body"})


def _python_files(roots: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files.extend(p for p in root.rglob("*.py") if not p.name.startswith("_test"))
    return files


def _is_event_raw_field_access(node: ast.AST) -> bool:
    """``event.comment_body`` / ``event.title`` / ``event.body``."""
    if isinstance(node, ast.Attribute) and node.attr in _RAW_FIELDS:
        value = node.value
        return isinstance(value, ast.Name) and value.id == "event"
    return False


def _is_event_raw_subscript(node: ast.AST) -> bool:
    """``event.raw[...]`` or ``event.raw.get(...)`` — untyped payload."""
    if isinstance(node, ast.Subscript):
        value = node.value
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "raw"
            and isinstance(value.value, ast.Name)
            and value.value.id == "event"
        )
    return False


def _violations_in_tree(tree: ast.Module, *, source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, snippet)`` tuples for each prompt-injection-shaped use."""
    violations: list[tuple[int, str]] = []

    # Build a child→parent map so we can walk upward without re-scanning.
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def _flag(node: ast.AST, reason: str) -> None:
        line = source.splitlines()[node.lineno - 1].strip() if node.lineno else ""
        violations.append((node.lineno or 0, f"{reason}: {line}"))

    for node in ast.walk(tree):
        is_field = _is_event_raw_field_access(node)
        is_subscript = _is_event_raw_subscript(node)
        if not (is_field or is_subscript):
            continue

        # Walk upward — first dangerous shape we hit decides the verdict.
        cur: ast.AST | None = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, ast.JoinedStr):
                _flag(node, "raw user field inside f-string")
                break
            if isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
                # Only flag string concat; numeric Add can't happen on
                # str-typed fields but be explicit anyway.
                _flag(node, "raw user field in BinOp(Add) — string concat")
                break
            if isinstance(cur, ast.Dict):
                # Dict values are the OpenAI/Anthropic messages shape.
                # If the field appears in a dict value (not key), flag.
                if any(v is not None and _contains(v, node) for v in cur.values):
                    _flag(node, "raw user field as dict value (messages-shape)")
                break
            # Stop at function/class scope — beyond is unrelated.
            if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
                break
            cur = parents.get(id(cur))

    return violations


def _contains(haystack: ast.AST, needle: ast.AST) -> bool:
    """True iff ``needle`` is anywhere within the ``haystack`` subtree."""
    return any(n is needle for n in ast.walk(haystack))


@pytest.mark.parametrize(
    "path", _python_files(_SCAN_DIRS), ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_no_raw_event_field_in_prompt_shape(path: Path) -> None:
    """Each file in scope must be free of unwrapped user-content uses."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations = _violations_in_tree(tree, source=source)
    if violations:
        msgs = "\n".join(
            f"  {path.relative_to(_REPO_ROOT)}:{line}  — {snippet}" for line, snippet in violations
        )
        pytest.fail(
            "Raw user-controlled event fields reach LLM-prompt construction "
            "without passing through `wrap_user_input(...)` (spec §3 M8). "
            f"Wrap the value before interpolating it.\n{msgs}"
        )


def test_scan_actually_runs_on_known_files() -> None:
    """Belt-and-suspenders: empty file list would silently pass everything."""
    files = _python_files(_SCAN_DIRS)
    rel = {str(p.relative_to(_REPO_ROOT)) for p in files}
    # At minimum, the four workflow stubs and the sanitize module
    # must be in scope. If this set shrinks, the test is no longer
    # protecting what its docstring claims.
    must_include = {
        # Phase-1 restructure: real code moved to application/workflows/.
        "openbot/application/workflows/chat.py",
        "openbot/application/workflows/triage.py",
        "openbot/application/workflows/review.py",
        "openbot/application/workflows/fix.py",
        "openbot/infrastructure/llm/sanitize.py",
    }
    missing = must_include - rel
    assert not missing, f"AST lint scan missed expected files: {missing}"


def test_lint_flags_a_synthetic_violation() -> None:
    """Self-check: handcrafted f-string with a raw field IS flagged."""
    src = (
        "async def bad(event):\n"
        '    prompt = f"User said: {event.comment_body}"\n'
        "    return prompt\n"
    )
    tree = ast.parse(src)
    violations = _violations_in_tree(tree, source=src)
    assert violations, "lint failed to flag an obvious f-string violation"
    assert "f-string" in violations[0][1]


def test_lint_does_not_flag_wrapped_use() -> None:
    """Self-check: passing through wrap_user_input keeps the check quiet
    when no JoinedStr / BinOp+str / Dict-value shape wraps it."""
    src = (
        "async def ok(event):\n"
        "    safe = wrap_user_input(event.comment_body, source=ISSUE_BODY)\n"
        "    return safe\n"
    )
    tree = ast.parse(src)
    violations = _violations_in_tree(tree, source=src)
    assert not violations, f"lint false-positive on wrapped use: {violations}"


def test_lint_does_not_flag_control_flow() -> None:
    """Self-check: `if event.comment_body and not ...` patterns are control flow."""
    src = (
        "async def gate(event):\n"
        "    if not event.comment_body:\n"
        "        return None\n"
        "    body = event.comment_body or ''\n"
        "    return body\n"
    )
    tree = ast.parse(src)
    violations = _violations_in_tree(tree, source=src)
    assert not violations, f"lint false-positive on control flow: {violations}"
