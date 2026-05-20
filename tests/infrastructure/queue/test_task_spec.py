# tests/infrastructure/queue/test_task_spec.py
"""TaskSpec v3 — schema, serialization, event reconstruction."""

from __future__ import annotations

import json

from openbot.infrastructure.queue.task_spec import (
    TASK_SPEC_VERSION,
    TaskSpec,
    deserialize_task_spec,
)


def _spec(**kw: object) -> TaskSpec:
    base: dict = {
        "spec_version": 3,
        "task_id": "t-1",
        "run_id": "r-1",
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
        "actor_type": None,
        "issue_number": 1,
        "pr_number": None,
        "comment_body": None,
        "installation_id": 42,
        "raw": {},
        "check_run_id": None,
        "decision_trace": [],
        "classifier_skipped": True,
        "stages_to_run": [],
        "initial_labels": [],
    }
    base.update(kw)
    return TaskSpec(**base)  # type: ignore[arg-type]


def test_version_constant() -> None:
    assert TASK_SPEC_VERSION == 3


def test_round_trip() -> None:
    spec = _spec()
    blob = spec.to_json()
    assert json.loads(blob)["spec_version"] == 3
    restored = deserialize_task_spec(blob)
    assert restored is not None
    assert restored.task_id == "t-1"
    assert restored.classifier_skipped is True


def test_deserialize_bad_json_returns_none() -> None:
    assert deserialize_task_spec("not-json") is None


def test_deserialize_wrong_version_returns_none() -> None:
    assert deserialize_task_spec(json.dumps({"spec_version": 99})) is None


def test_v2_payload_rejected() -> None:
    """A v2 QueuePayload must NOT be accepted as TaskSpec."""
    assert deserialize_task_spec(json.dumps({"version": 2, "task_id": "x"})) is None


def test_to_event_reconstructs() -> None:
    from openbot.domain.events import EventKind

    event = _spec().to_event()
    assert event.repo == "org/repo"
    assert event.kind is EventKind.ISSUE_OPENED


def test_initial_labels_present() -> None:
    spec = _spec(initial_labels=["cancel-openbot", "bug"])
    assert "cancel-openbot" in spec.initial_labels
