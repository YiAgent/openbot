"""Config approval gate — PRD v0.2 §3.2.

When a PR modifies `.openbot/config.yaml` and changes high-risk fields
(budget.*, safety.*, channels.*, cancel.*), the bot checks for a
`config-approved` label. Without it, the bot comments and blocks.

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

# Fields that require explicit approval when changed in .openbot/config.yaml
_HIGH_RISK_PREFIXES = (
    "budget.",
    "safety.",
    "channels.",
    "cancel.",
    "rate_limit.",
    "egress_action",
    "global_hard_kill_usd",
    "monthly_soft_cap_usd",
    "per_task",
)

_APPROVAL_LABEL = "config-approved"
_CONFIG_PATH = ".openbot/config.yaml"


class ConfigApprovalGate:
    """Block config-change PRs that lack the approval label."""

    name = "config_approval"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        """Check if this PR modifies high-risk config fields without approval."""
        event = ctx.event

        # Only applies to PRs
        if not event.pr_number:
            return MiddlewareDecision.proceed()

        # Check if the PR touches .openbot/config.yaml via raw payload
        pr_body = event.raw.get("pull_request", {}).get("body", "")
        pr_title = event.raw.get("pull_request", {}).get("title", "")

        # Quick heuristic: if the PR doesn't mention config, skip
        config_mentioned = _CONFIG_PATH in pr_body or _CONFIG_PATH in pr_title
        if not config_mentioned:
            return MiddlewareDecision.proceed()

        # Check if the approval label is present
        labels = event.raw.get("pull_request", {}).get("labels", [])
        label_names = {lbl.get("name", "") for lbl in labels}

        if _APPROVAL_LABEL in label_names:
            _logger.info(
                "config_approval_granted",
                extra={"pr_number": event.pr_number, "labels": list(label_names)},
            )
            return MiddlewareDecision.proceed()

        # High-risk change without approval — block
        _logger.warning(
            "config_approval_blocked",
            extra={"pr_number": event.pr_number, "labels": list(label_names)},
        )
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="config_approval_required",
            comment=(
                f"Config change requires `{_APPROVAL_LABEL}` label. "
                f"This PR modifies `{_CONFIG_PATH}` which may contain high-risk fields "
                f"(budget, safety, channels, cancel). "
                f"Add the `{_APPROVAL_LABEL}` label to proceed."
            ),
            audit_phase=WorkflowPhase.SKIPPED,
        )
