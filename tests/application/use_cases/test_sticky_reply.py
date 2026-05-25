"""sticky_reply context manager — unit tests.

Covers the three paths:
1. Happy path: initial POST succeeds, caller calls update() → one PATCH.
2. Initial POST fails: comment_id is None, update() is a no-op.
3. update() fails silently: exception logged, caller not interrupted.
"""

from __future__ import annotations

import pytest

from openbot.application.use_cases._lifecycle import sticky_reply
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d-sticky-1",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo="o/r",
        actor="alice",
        issue_number=42,
        installation_id=1,
    )


@pytest.mark.asyncio
async def test_sticky_reply_happy_path() -> None:
    """Initial placeholder posted, then updated in-place with final message."""
    adapter = FakeChannelAdapter()
    event = _event()

    async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
        assert sticky.comment_id == 1  # FakeChannelAdapter returns len(replies)
        await sticky.update("✅ Done")

    # One POST (initial), one PATCH (update)
    assert len(adapter.replies) == 1
    assert adapter.replies[0][1] == "⏳ Working…"
    assert len(adapter.comment_updates) == 1
    assert adapter.comment_updates[0]["comment_id"] == 1
    assert adapter.comment_updates[0]["message"] == "✅ Done"


@pytest.mark.asyncio
async def test_sticky_reply_initial_post_fails_noop_update() -> None:
    """When the initial POST raises, comment_id is None and update() is a no-op."""

    class BrokenAdapter(FakeChannelAdapter):
        async def reply(self, event, message):  # type: ignore[override]
            raise RuntimeError("GitHub unavailable")

    adapter = BrokenAdapter()
    event = _event()

    async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
        assert sticky.comment_id is None
        # Must not raise even though comment_id is None
        await sticky.update("✅ Done")

    assert adapter.comment_updates == []


@pytest.mark.asyncio
async def test_sticky_reply_update_fails_silently() -> None:
    """When PATCH raises, the exception is swallowed and caller continues."""

    class PatchBrokenAdapter(FakeChannelAdapter):
        async def update_comment(self, event, comment_id, message):  # type: ignore[override]
            raise RuntimeError("PATCH failed")

    adapter = PatchBrokenAdapter()
    event = _event()

    async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
        # update() swallows the PATCH error — caller must not see it
        await sticky.update("✅ Done")

    # Initial POST succeeded
    assert len(adapter.replies) == 1
    # PATCH failed silently — no records, no exception raised to us
    assert adapter.comment_updates == []


@pytest.mark.asyncio
async def test_sticky_reply_no_update_called_is_fine() -> None:
    """Caller is allowed to exit the context without calling update()."""
    adapter = FakeChannelAdapter()
    event = _event()

    async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
        assert sticky.comment_id == 1
        # Intentionally no update call

    # Only the initial POST; no PATCH
    assert len(adapter.replies) == 1
    assert adapter.comment_updates == []


@pytest.mark.asyncio
async def test_sticky_reply_multiple_updates_all_applied() -> None:
    """Each update() call produces one PATCH — e.g. progress steps."""
    adapter = FakeChannelAdapter()
    event = _event()

    async with sticky_reply(adapter, event, initial="⏳ Starting…") as sticky:
        await sticky.update("⏳ Step 1 done…")
        await sticky.update("⏳ Step 2 done…")
        await sticky.update("✅ All done")

    assert len(adapter.replies) == 1
    assert len(adapter.comment_updates) == 3
    assert adapter.comment_updates[-1]["message"] == "✅ All done"


# ── fallback_on_update_error ─────────────────────────────────────────────────
#
# Used by the reproduce responder (spec §A2 / R1.3): when PATCH fails or the
# initial placeholder never landed, post a fresh comment so high-stakes results
# (REPRODUCED outcomes with command + repro script) reach the audit row even
# if the sticky channel is broken.
#
# The fallback is opt-in (default ``False``) so fix.py / chat.py callers keep
# their existing silent-failure semantics — only the reproduce flow wants
# louder degradation.


@pytest.mark.asyncio
async def test_sticky_reply_fallback_posts_new_comment_when_patch_fails() -> None:
    """fallback_on_update_error=True + PATCH fails → fresh reply() carries the message."""

    class PatchBrokenAdapter(FakeChannelAdapter):
        async def update_comment(self, event, comment_id, message):  # type: ignore[override]
            raise RuntimeError("PATCH failed")

    adapter = PatchBrokenAdapter()
    event = _event()

    async with sticky_reply(
        adapter, event, initial="⏳ Working…", fallback_on_update_error=True
    ) as sticky:
        await sticky.update("✅ Done")

    # Two POSTs: the initial placeholder + the fallback message
    assert len(adapter.replies) == 2
    assert adapter.replies[0][1] == "⏳ Working…"
    assert adapter.replies[1][1] == "✅ Done"
    # PATCH attempt was made but raised, so no recorded update
    assert adapter.comment_updates == []


@pytest.mark.asyncio
async def test_sticky_reply_fallback_posts_new_comment_when_initial_failed() -> None:
    """fallback_on_update_error=True + initial POST failed → update() still posts."""

    class FlakyAdapter(FakeChannelAdapter):
        calls: int = 0

        async def reply(self, event, message):  # type: ignore[override]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first reply unavailable")
            return await super().reply(event, message)

    adapter = FlakyAdapter()
    event = _event()

    async with sticky_reply(
        adapter, event, initial="⏳ Working…", fallback_on_update_error=True
    ) as sticky:
        # Initial POST raised → comment_id is None
        assert sticky.comment_id is None
        # update() falls back to a fresh reply()
        await sticky.update("✅ Done")

    # Only the fallback POST landed (initial was rejected by the flaky adapter)
    assert len(adapter.replies) == 1
    assert adapter.replies[0][1] == "✅ Done"


@pytest.mark.asyncio
async def test_sticky_reply_fallback_itself_failing_still_silent() -> None:
    """Even when both PATCH and the fallback reply() raise, caller does not see it."""

    class FullyBrokenAdapter(FakeChannelAdapter):
        post_count: int = 0

        async def reply(self, event, message):  # type: ignore[override]
            self.post_count += 1
            if self.post_count == 1:
                # initial placeholder succeeds so we exercise the PATCH path
                return await super().reply(event, message)
            raise RuntimeError("fallback POST also fails")

        async def update_comment(self, event, comment_id, message):  # type: ignore[override]
            raise RuntimeError("PATCH failed")

    adapter = FullyBrokenAdapter()
    event = _event()

    async with sticky_reply(
        adapter, event, initial="⏳ Working…", fallback_on_update_error=True
    ) as sticky:
        # Must not raise even though every channel is broken
        await sticky.update("✅ Done")

    # Only the initial placeholder landed; PATCH + fallback both raised
    assert len(adapter.replies) == 1
    assert adapter.comment_updates == []


@pytest.mark.asyncio
async def test_sticky_reply_no_fallback_by_default() -> None:
    """Default behavior unchanged: PATCH failure stays silent, no new comment."""

    class PatchBrokenAdapter(FakeChannelAdapter):
        async def update_comment(self, event, comment_id, message):  # type: ignore[override]
            raise RuntimeError("PATCH failed")

    adapter = PatchBrokenAdapter()
    event = _event()

    # No fallback_on_update_error argument → uses False default
    async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
        await sticky.update("✅ Done")

    # Only the initial POST; PATCH failed silently with no fallback
    assert len(adapter.replies) == 1
    assert adapter.comment_updates == []
