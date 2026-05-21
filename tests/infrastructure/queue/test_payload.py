"""QueuePayload — JSON round-trip + UnifiedEvent reconstruction."""

from __future__ import annotations

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.queue import QueuePayload, deserialize_payload
from openbot.infrastructure.queue.payload import PAYLOAD_VERSION


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="deliv-1",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=7,
        installation_id=99,
        raw={"action": "opened", "issue": {"number": 7}},
    )


def test_round_trip_preserves_event_fields() -> None:
    payload = QueuePayload.from_event(_event(), feature=Feature.TRIAGE, task_id="t-1234")
    blob = payload.to_json()
    restored = deserialize_payload(blob)
    assert restored is not None
    event = restored.to_event()
    assert event.channel == "github"
    assert event.delivery_id == "deliv-1"
    assert event.kind is EventKind.ISSUE_OPENED
    assert event.repo == "org/r"
    assert event.actor == "alice"
    assert event.issue_number == 7
    assert event.installation_id == 99
    assert event.raw == {"action": "opened", "issue": {"number": 7}}
    assert restored.feature == "triage"
    assert restored.task_id == "t-1234"


def test_unknown_event_kind_falls_back_to_unknown() -> None:
    """A payload from a future router with a new EventKind must not crash
    an old worker — it should resolve to UNKNOWN so the worker still
    drains the entry."""
    payload = QueuePayload.from_event(_event(), feature=Feature.TRIAGE, task_id="t")
    # Tamper the kind to simulate a forward-incompatible payload.
    blob = payload.to_json().replace('"issue.opened"', '"issue.transferred"')
    restored = deserialize_payload(blob)
    assert restored is not None
    event = restored.to_event()
    assert event.kind is EventKind.UNKNOWN


def test_deserialize_returns_none_on_invalid_json() -> None:
    assert deserialize_payload("{not json") is None


def test_deserialize_returns_none_on_version_mismatch() -> None:
    payload = QueuePayload.from_event(_event(), feature=Feature.TRIAGE, task_id="t")
    blob = payload.to_json().replace(f'"version":{PAYLOAD_VERSION}', '"version":999')
    assert deserialize_payload(blob) is None


def test_deserialize_returns_none_on_schema_drift() -> None:
    """Missing required field → None, not crash."""
    blob = '{"version":1,"channel":"github"}'
    assert deserialize_payload(blob) is None


def test_payload_handles_bytes_blob() -> None:
    """Some Redis clients return values as bytes; deserialize accepts."""
    payload = QueuePayload.from_event(_event(), feature=Feature.TRIAGE, task_id="t")
    restored = deserialize_payload(payload.to_json().encode("utf-8"))
    assert restored is not None


def test_payload_is_frozen() -> None:
    import pytest

    payload = QueuePayload.from_event(_event(), feature=Feature.TRIAGE, task_id="t")
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        payload.task_id = "tampered"  # type: ignore[misc]


def test_payload_preserves_unicode_in_raw() -> None:
    """CJK in issue title/body must survive the JSON round-trip."""
    event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="bob",
        actor_type="User",
        issue_number=1,
        installation_id=1,
        raw={"issue": {"title": "无法启动 — 测试中文"}},
    )
    payload = QueuePayload.from_event(event, feature=Feature.TRIAGE, task_id="t")
    restored = deserialize_payload(payload.to_json())
    assert restored is not None
    assert restored.raw["issue"]["title"] == "无法启动 — 测试中文"
