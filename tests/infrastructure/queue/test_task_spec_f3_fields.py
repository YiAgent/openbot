# tests/infrastructure/queue/test_task_spec_f3_fields.py
"""TaskSpec F3 extension: classifier_output, is_incremental, is_force_push."""

from __future__ import annotations

import json

from openbot.infrastructure.queue.task_spec import TaskSpec, deserialize_task_spec


def _base_kwargs() -> dict:
    return {
        "spec_version": 3,
        "task_id": "t1",
        "run_id": "r1",
        "prev_run_id": None,
        "resource_key": "github:org/repo:issue:1",
        "event_seq": 0,
        "intent": "start",
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "spec_built_at": "2026-01-01T00:00:00+00:00",
        "scenario": "triage",
        "channel": "github",
        "delivery_id": "del-1",
        "kind": "issue.opened",
        "repo": "org/repo",
        "actor": "alice",
        "actor_type": "User",
        "issue_number": 1,
        "pr_number": None,
        "comment_body": None,
        "installation_id": 100,
        "raw": {},
        "check_run_id": None,
        "decision_trace": [],
        "classifier_skipped": True,
        "stages_to_run": [],
        "initial_labels": [],
    }


def test_new_fields_default_to_none_false() -> None:
    spec = TaskSpec(**_base_kwargs())
    assert spec.classifier_output is None
    assert spec.is_incremental is False
    assert spec.is_force_push is False


def test_new_fields_roundtrip() -> None:
    spec = TaskSpec(
        **_base_kwargs(),
        classifier_output={"type": "bug"},
        is_incremental=True,
        is_force_push=False,
    )
    restored = deserialize_task_spec(spec.to_json())
    assert restored is not None
    assert restored.classifier_output == {"type": "bug"}
    assert restored.is_incremental is True


def test_old_v3_json_without_new_fields() -> None:
    """Old v3 JSON missing new keys → defaults applied (backward-compat)."""
    blob = json.dumps(_base_kwargs())
    spec = deserialize_task_spec(blob)
    assert spec is not None
    assert spec.classifier_output is None
    assert spec.is_incremental is False


def test_from_event_and_dispatch_defaults() -> None:
    from openbot.application.router import dispatch_for
    from openbot.domain.events import EventKind, UnifiedEvent

    event = UnifiedEvent(
        channel="github",
        delivery_id="del-2",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=5,
        pr_number=None,
        installation_id=100,
        comment_body=None,
        raw={"issue": {"number": 5, "labels": []}},
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None

    spec = TaskSpec.from_event_and_dispatch(event, dispatch)
    assert spec.classifier_output is None
    assert spec.is_incremental is False
    assert spec.classifier_skipped is True  # no output → skipped


def test_from_event_and_dispatch_with_classifier_output() -> None:
    from openbot.application.router import dispatch_for
    from openbot.domain.events import EventKind, UnifiedEvent

    event = UnifiedEvent(
        channel="github",
        delivery_id="del-3",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=6,
        pr_number=None,
        installation_id=100,
        comment_body=None,
        raw={"issue": {"number": 6, "labels": []}},
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None

    output = {"type": "feature", "severity_guess": "low"}
    spec = TaskSpec.from_event_and_dispatch(
        event,
        dispatch,
        classifier_output=output,
        is_incremental=True,
        is_force_push=False,
    )
    assert spec.classifier_output == output
    assert spec.is_incremental is True
    assert spec.classifier_skipped is False  # output present → not skipped
