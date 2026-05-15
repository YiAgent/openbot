"""Unit tests for evals.common.judges — PRD §10.3 governance gate."""

from __future__ import annotations

import hashlib

from evals.common import judges


def test_default_model_locked_to_opus47() -> None:
    """PRD §17 #5 locks the default judge model."""
    assert judges.DEFAULT_JUDGE_MODEL_ID == "anthropic/claude-opus-4-7"


def test_prompt_version_is_positive_int() -> None:
    assert isinstance(judges.JUDGE_PROMPT_VERSION, int)
    assert judges.JUDGE_PROMPT_VERSION >= 1


def test_prompt_hash_matches_live_body() -> None:
    """The pinned `_JUDGE_PROMPT_HASH` must equal the SHA256 of the live body.

    **If this test fails after editing `_JUDGE_PROMPT_BODY`:**
      1. Bump `JUDGE_PROMPT_VERSION` by 1.
      2. Replace `_JUDGE_PROMPT_HASH` with the value printed in the
         assertion error below.
      3. Append a row to `docs/eval/judge-version-log.md` with the rationale
         and before/after release-suite scores (PRD §10.3).
    """
    live = hashlib.sha256(judges._JUDGE_PROMPT_BODY.encode("utf-8")).hexdigest()
    assert live == judges._JUDGE_PROMPT_HASH, (
        f"judge prompt body drifted without version bump.\n"
        f"  pinned hash : {judges._JUDGE_PROMPT_HASH}\n"
        f"  live   hash : {live}\n"
        f"  current version: {judges.JUDGE_PROMPT_VERSION}\n"
        f"Bump JUDGE_PROMPT_VERSION and update _JUDGE_PROMPT_HASH; "
        f"see docs/eval/judge-version-log.md."
    )


def test_judge_dataclass_is_frozen() -> None:
    """Judge is immutable per run so a half-modified instance can't leak into scoring."""
    j = judges.Judge()
    import dataclasses

    assert dataclasses.is_dataclass(j)
    # Mutation must raise — frozen dataclass enforces it.
    try:
        j.model_id = "anthropic/claude-opus-4-8"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("Judge should be frozen but mutation succeeded")


def test_fingerprint_is_stable_under_no_change() -> None:
    j1 = judges.Judge()
    j2 = judges.Judge()
    assert j1.fingerprint() == j2.fingerprint()


def test_fingerprint_changes_when_prompt_body_changes() -> None:
    """Different prompt body → different fingerprint, regardless of version match."""
    j_default = judges.Judge()
    j_drifted = judges.Judge(prompt_body=judges._JUDGE_PROMPT_BODY + "\nextra line\n")
    assert j_default.fingerprint() != j_drifted.fingerprint()


def test_fingerprint_carries_model_id_and_version() -> None:
    j = judges.Judge()
    model_id, version, body_sha = j.fingerprint()
    assert model_id == judges.DEFAULT_JUDGE_MODEL_ID
    assert version == judges.JUDGE_PROMPT_VERSION
    assert len(body_sha) == 64  # SHA-256 hex


def test_drift_detection_for_prompt_change_without_version_bump(monkeypatch) -> None:
    """Simulate a developer editing the prompt but forgetting to bump version.

    AC: judge prompt change without version bump must be detected.
    """
    altered = judges._JUDGE_PROMPT_BODY + "\nsneaky addition\n"
    monkeypatch.setattr(judges, "_JUDGE_PROMPT_BODY", altered)
    live = hashlib.sha256(judges._JUDGE_PROMPT_BODY.encode("utf-8")).hexdigest()
    # The drift signal: live hash diverges from the pinned hash → the pin test
    # would fail in CI, forcing a version bump.
    assert live != judges._JUDGE_PROMPT_HASH
