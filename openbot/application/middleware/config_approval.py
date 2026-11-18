"""Config approval gate — PRD v0.2 §3.2.

When a PR actually modifies `.openbot/config.yaml`, the bot checks for a
`config-approved` label. Without it, the bot comments and blocks. Detection
is based on the PR **diff** (the real changed files), not on free-text
mentions in the title/body — otherwise the gate is trivially bypassed by
simply not naming the file.

When the touched lines include high-risk keys (budget / safety / channels /
cancel / rate_limit / egress caps), they're surfaced in the block comment so
the reviewer knows why approval is required.

This is a preflight-level check — it runs before the workflow starts,
same as budget/rate-limit/cancel gates.
"""

from __future__ import annotations

import logging

from openbot.application.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
    WorkflowPhase,
)

_logger = logging.getLogger(__name__)

# High-risk YAML keys. When a changed line in the config diff starts with one
# of these keys, the change is flagged in the block comment. The gate blocks
# on ANY config.yaml change regardless (fail closed); this list only enriches
# the reason surfaced to the reviewer.
_HIGH_RISK_KEYS = (
    "budget",
    "safety",
    "channels",
    "cancel",
    "rate_limit",
    "egress_action",
    "global_hard_kill_usd",
    "monthly_soft_cap_usd",
    "per_task",
)

_APPROVAL_LABEL = "config-approved"
_CONFIG_PATH = ".openbot/config.yaml"


def _diff_touches_config(diff: str) -> bool:
    """True if the unified diff modifies ``.openbot/config.yaml``."""
    for line in diff.splitlines():
        if line.startswith(("diff --git", "+++ ", "--- ")) and _CONFIG_PATH in line:
            return True
    return False


def _changed_high_risk_keys(diff: str) -> list[str]:
    """Return high-risk YAML keys appearing on added/removed lines inside the
    ``.openbot/config.yaml`` section of the diff.

    Best-effort: scans the file section between its ``diff --git`` header and
    the next file header, inspecting ``+``/``-`` content lines (ignoring the
    ``+++``/``---`` file markers) for a leading ``key:`` token.
    """
    found: set[str] = set()
    in_config = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_config = _CONFIG_PATH in line
            continue
        if not in_config:
            continue
        if line.startswith(("+++", "---")):
            continue
        if not line.startswith(("+", "-")):
            continue
        stripped = line[1:].strip()
        key = stripped.split(":", 1)[0].strip()
        if key in _HIGH_RISK_KEYS:
            found.add(key)
    return sorted(found)


class ConfigApprovalGate:
    """Block config-change PRs that lack the approval label."""

    name = "config_approval"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        """Block PRs that modify .openbot/config.yaml without the approval label."""
        event = ctx.event

        # Only applies to PRs.
        if not event.pr_number:
            return MiddlewareDecision.proceed()

        # Inspect the actual diff to decide whether config.yaml is touched.
        # Fail open on fetch errors (same posture as other soft gates) — a
        # transient API failure shouldn't wedge every PR.
        try:
            diff = await ctx.adapter.get_pr_diff(event, event.pr_number)
        except Exception:
            _logger.warning(
                "config_approval_diff_unavailable",
                extra={"pr_number": event.pr_number},
            )
            return MiddlewareDecision.proceed()

        if not _diff_touches_config(diff):
            return MiddlewareDecision.proceed()

        # Approval label present → allow.
        labels = event.raw.get("pull_request", {}).get("labels", [])
        label_names = {lbl.get("name", "") for lbl in labels}
        if _APPROVAL_LABEL in label_names:
            _logger.info(
                "config_approval_granted",
                extra={"pr_number": event.pr_number, "labels": sorted(label_names)},
            )
            return MiddlewareDecision.proceed()

        # Config change without approval — block.
        high_risk = _changed_high_risk_keys(diff)
        _logger.warning(
            "config_approval_blocked",
            extra={
                "pr_number": event.pr_number,
                "labels": sorted(label_names),
                "high_risk_keys": high_risk,
            },
        )
        risk_note = (
            f" High-risk fields changed: {', '.join(high_risk)}."
            if high_risk
            else " (budget, safety, channels, cancel)."
        )
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="config_approval_required",
            comment=(
                f"Config change requires the `{_APPROVAL_LABEL}` label. "
                f"This PR modifies `{_CONFIG_PATH}`.{risk_note} "
                f"Add the `{_APPROVAL_LABEL}` label to proceed."
            ),
            audit_phase=WorkflowPhase.SKIPPED,
        )
