"""Tests for openbot audit CLI — show, export, budget reset."""

from __future__ import annotations

from decimal import Decimal

from openbot.entrypoints.cli.audit import (
    TaskDetail,
    export_csv,
    render_task_detail_markdown,
)


def test_task_detail_to_json():
    detail = TaskDetail(
        task_id="t-1",
        audit_entries=[{"phase": "started", "created_at": "2026-01-01T00:00:00"}],
        cost_entries=[{"feature": "fix", "cost_usd": "1.50"}],
        total_cost_usd=Decimal("1.50"),
    )
    j = detail.to_json()
    assert j["task_id"] == "t-1"
    assert j["total_cost_usd"] == "1.50"
    assert len(j["audit_entries"]) == 1


def test_render_task_detail_markdown_empty():
    detail = TaskDetail(
        task_id="t-empty",
        audit_entries=[],
        cost_entries=[],
        total_cost_usd=Decimal("0"),
    )
    md = render_task_detail_markdown(detail)
    assert "t-empty" in md
    assert "$0.0000" in md


def test_render_task_detail_markdown_with_data():
    detail = TaskDetail(
        task_id="t-42",
        audit_entries=[
            {
                "created_at": "2026-05-27T10:00:00",
                "phase": "started",
                "workflow": "fix",
                "outcome": None,
            },
            {
                "created_at": "2026-05-27T10:05:00",
                "phase": "completed",
                "workflow": "fix",
                "outcome": "pr_opened",
            },
        ],
        cost_entries=[
            {
                "created_at": "2026-05-27T10:01:00",
                "feature": "fix",
                "model": "anthropic/glm-5.1",
                "cost_usd": "0.42",
                "cost_status": "recorded",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
        ],
        total_cost_usd=Decimal("0.42"),
    )
    md = render_task_detail_markdown(detail)
    assert "t-42" in md
    assert "$0.4200" in md
    assert "started" in md
    assert "completed" in md
    assert "pr_opened" in md
    assert "Audit trail" in md
    assert "Cost breakdown" in md


def test_export_csv_empty():
    assert export_csv([]) == ""


def test_export_csv_with_rows():
    rows = [
        {"task_id": "t-1", "feature": "fix", "cost_usd": "1.00"},
        {"task_id": "t-2", "feature": "review", "cost_usd": "0.50"},
    ]
    csv_str = export_csv(rows)
    lines = csv_str.strip().split("\n")
    assert len(lines) == 3  # header + 2 rows
    assert "task_id" in lines[0]
    assert "t-1" in lines[1]


def test_build_parser_show():
    from openbot.entrypoints.cli.audit import _build_parser

    p = _build_parser()
    args = p.parse_args(["show", "delivery-123"])
    assert args.cmd == "show"
    assert args.task_id == "delivery-123"


def test_build_parser_export():
    from openbot.entrypoints.cli.audit import _build_parser

    p = _build_parser()
    args = p.parse_args(["export", "--since", "7", "--format", "json"])
    assert args.cmd == "export"
    assert args.since == 7
    assert args.format == "json"


def test_build_parser_budget_reset_global():
    from openbot.entrypoints.cli.audit import _build_parser

    p = _build_parser()
    args = p.parse_args(["budget", "reset", "--global", "--yes"])
    assert args.cmd == "budget"
    assert args.budget_cmd == "reset"
    assert args.global_reset is True
    assert args.yes is True


def test_build_parser_budget_reset_repo():
    from openbot.entrypoints.cli.audit import _build_parser

    p = _build_parser()
    args = p.parse_args(["budget", "reset", "--repo", "owner/repo", "--yes"])
    assert args.cmd == "budget"
    assert args.repo == "owner/repo"
    assert args.global_reset is False
