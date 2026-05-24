"""TaskSpec assembly — pure construction tests.

Tests that ``TaskSpec.from_event_and_dispatch`` maps (event, dispatch)
fields into a well-formed v3 TaskSpec. No I/O, no Redis, no network.
"""

from __future__ import annotations

from openbot.application.router import Dispatch
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.identifiers import derive_task_id
from openbot.domain.workflows import Feature
from openbot.infrastructure.queue.task_spec import TASK_SPEC_VERSION, TaskSpec


def _event(
    kind: EventKind = EventKind.ISSUE_OPENED,
    *,
    actor: str = "octocat",
    actor_type: str = "User",
    repo: str = "owner/repo",
    issue_number: int | None = 42,
    pr_number: int | None = None,
    comment_body: str | None = None,
    installation_id: int = 1,
) -> UnifiedEvent:
    return UnifiedEvent(
        delivery_id="aaaabbbbccccdddd00001111",
        channel="github",
        kind=kind,
        actor=actor,
        actor_type=actor_type,
        repo=repo,
        issue_number=issue_number,
        pr_number=pr_number,
        comment_body=comment_body,
        installation_id=installation_id,
    )


def _dispatch(
    feature: Feature = Feature.TRIAGE,
    *,
    event: UnifiedEvent | None = None,
    intent: str = "start",
    run_id: str = "run-abc123",
    resource_key: str | None = "github:owner/repo:issue:42",
) -> Dispatch:
    ev = event or _event()
    task_id = derive_task_id(ev)
    return Dispatch(
        feature=feature,
        handler=_noop_handler,
        task_id=task_id,
        intent=intent,
        run_id=run_id,
        resource_key=resource_key,
    )


async def _noop_handler(ctx: object) -> None:
    """Placeholder handler — never called in unit tests."""


class TestTaskSpecAssembly:
    def test_spec_version_is_always_3(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        assert spec.spec_version == TASK_SPEC_VERSION
        assert spec.spec_version == 3

    def test_scenario_matches_feature_value(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(Feature.TRIAGE, event=ev))
        assert spec.scenario == "triage"

    def test_review_feature_sets_scenario(self) -> None:
        ev = _event(EventKind.PR_OPENED, pr_number=7, issue_number=None)
        d = _dispatch(Feature.REVIEW, event=ev, resource_key="github:owner/repo:pr:7")
        spec = TaskSpec.from_event_and_dispatch(ev, d)
        assert spec.scenario == "review"

    def test_event_fields_round_trip(self) -> None:
        ev = _event(
            EventKind.ISSUE_COMMENT_CREATED,
            actor="alice",
            comment_body="@openbot summarize",
        )
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(Feature.CHAT, event=ev))
        assert spec.channel == "github"
        assert spec.delivery_id == ev.delivery_id
        assert spec.kind == EventKind.ISSUE_COMMENT_CREATED.value
        assert spec.repo == "owner/repo"
        assert spec.actor == "alice"
        assert spec.comment_body == "@openbot summarize"
        assert spec.installation_id == 1
        assert spec.issue_number == 42

    def test_task_id_is_deterministic(self) -> None:
        ev = _event()
        spec1 = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        spec2 = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        assert spec1.task_id == spec2.task_id
        assert spec1.task_id == derive_task_id(ev)

    def test_intent_forwarded_from_dispatch(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev, intent="supersede"))
        assert spec.intent == "supersede"

    def test_default_intent_is_start(self) -> None:
        ev = _event()
        # Dispatch with intent=None falls back to "start" inside from_event_and_dispatch.
        d = Dispatch(
            feature=Feature.TRIAGE,
            handler=_noop_handler,
            task_id=derive_task_id(ev),
            intent=None,
        )
        spec = TaskSpec.from_event_and_dispatch(ev, d)
        assert spec.intent == "start"

    def test_classifier_skipped_when_no_classifier_output(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev), classifier_output=None)
        assert spec.classifier_skipped is True

    def test_classifier_not_skipped_when_output_provided(self) -> None:
        ev = _event()
        fake_output: dict = {"category": "bug", "priority": "high"}
        spec = TaskSpec.from_event_and_dispatch(
            ev, _dispatch(event=ev), classifier_output=fake_output
        )
        assert spec.classifier_skipped is False
        assert spec.classifier_output == fake_output

    def test_initial_labels_default_empty(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        assert spec.initial_labels == []

    def test_initial_labels_forwarded(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(
            ev, _dispatch(event=ev), initial_labels=["bug", "help wanted"]
        )
        assert spec.initial_labels == ["bug", "help wanted"]

    def test_stages_to_run_default_empty(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        assert spec.stages_to_run == []

    def test_stages_to_run_forwarded(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(
            ev, _dispatch(event=ev), stages_to_run=["triage_classify"]
        )
        assert spec.stages_to_run == ["triage_classify"]

    def test_incremental_flags_default_false(self) -> None:
        ev = _event(EventKind.PR_SYNCHRONIZED, pr_number=7, issue_number=None)
        d = _dispatch(Feature.REVIEW, event=ev)
        spec = TaskSpec.from_event_and_dispatch(ev, d)
        assert spec.is_incremental is False
        assert spec.is_force_push is False

    def test_incremental_flags_forwarded(self) -> None:
        ev = _event(EventKind.PR_SYNCHRONIZED, pr_number=7, issue_number=None)
        d = _dispatch(Feature.REVIEW, event=ev)
        spec = TaskSpec.from_event_and_dispatch(ev, d, is_incremental=True, is_force_push=False)
        assert spec.is_incremental is True
        assert spec.is_force_push is False

    def test_to_json_round_trip(self) -> None:
        """to_json / deserialize_task_spec must produce an equivalent TaskSpec."""
        from openbot.infrastructure.queue.task_spec import deserialize_task_spec

        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev), initial_labels=["bug"])
        blob = spec.to_json()
        restored = deserialize_task_spec(blob)
        assert restored is not None
        assert restored.task_id == spec.task_id
        assert restored.scenario == spec.scenario
        assert restored.initial_labels == ["bug"]
        assert restored.spec_version == TASK_SPEC_VERSION

    def test_to_event_reconstructs_unified_event(self) -> None:
        ev = _event()
        spec = TaskSpec.from_event_and_dispatch(ev, _dispatch(event=ev))
        rebuilt = spec.to_event()
        assert rebuilt.kind == ev.kind
        assert rebuilt.actor == ev.actor
        assert rebuilt.repo == ev.repo
        assert rebuilt.channel == ev.channel
        assert rebuilt.delivery_id == ev.delivery_id
