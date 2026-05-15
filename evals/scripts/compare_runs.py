"""Diff two run summaries and emit PR-comment markdown per PRD §9 gates.

Inputs are two aggregate dicts produced by
``evals.scripts.export_run_summary`` (or any compatible source — the only
fields read are ``mean_f1``, ``dataset_name``, ``task``, ``model``,
``solver_id``, ``failure_categories``). They can be passed as JSON files
on disk.

Output goes to stdout (or ``--out``) as a PR-comment markdown block. The
program exits non-zero when any **hard** gate trips (G2 / G6) so CI can
fail merges automatically.

Usage:
    uv run python -m evals.scripts.compare_runs \\
        --current run-current.json --baseline run-baseline.json \\
        --gate review    # review | safety | triage
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Literal

from evals.common import thresholds as T

GateName = Literal["review", "safety", "triage"]
GateStatus = Literal["pass", "soft-warn", "hard-block", "alert", "block-release"]


@dataclass(frozen=True)
class GateVerdict:
    gate_id: str  # e.g. "G2"
    name: str  # e.g. "Review hard regression"
    status: GateStatus
    metric: str  # e.g. "mean_f1"
    current: float | None
    baseline: float | None
    delta: float | None  # current - baseline (None when either side missing)
    threshold: str  # human-readable threshold note
    detail: str

    @property
    def emoji(self) -> str:
        return {
            "pass": "✅",
            "soft-warn": "⚠️",
            "alert": "🟠",
            "hard-block": "❌",
            "block-release": "🛑",
        }[self.status]


def _load_summary(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_delta(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None:
        return None
    return current - baseline


def evaluate_review_gates(current: dict[str, Any], baseline: dict[str, Any]) -> list[GateVerdict]:
    """G1 / G2 — Martian Code Review F1 vs last release baseline."""
    cur = current.get("mean_f1")
    base = baseline.get("mean_f1")
    delta = _safe_delta(cur, base)

    if delta is None:
        detail = "current or baseline mean_f1 missing — gate cannot evaluate"
        unknown = GateVerdict(
            "G1/G2",
            "Review regression",
            "alert",
            "mean_f1",
            cur,
            base,
            None,
            "depends on baseline",
            detail,
        )
        return [unknown]

    # Relative drop, not absolute — PRD §9 says "↓ ≥ 5%".
    rel_drop = (-delta / base) if base else 0.0

    g2 = GateVerdict(
        gate_id="G2",
        name="Review hard regression",
        status="hard-block" if rel_drop >= T.G2_REVIEW_HARD_F1_DROP else "pass",
        metric="mean_f1",
        current=cur,
        baseline=base,
        delta=delta,
        threshold=f"drop ≥ {T.G2_REVIEW_HARD_F1_DROP:.0%}",
        detail=f"relative drop = {rel_drop:.2%}",
    )
    if g2.status == "hard-block":
        # G2 trip implies G1 is also tripped — surface only the harder one.
        return [g2]

    g1 = GateVerdict(
        gate_id="G1",
        name="Review soft regression",
        status="soft-warn" if rel_drop >= T.G1_REVIEW_SOFT_F1_DROP else "pass",
        metric="mean_f1",
        current=cur,
        baseline=base,
        delta=delta,
        threshold=f"drop ≥ {T.G1_REVIEW_SOFT_F1_DROP:.0%}",
        detail=f"relative drop = {rel_drop:.2%}",
    )
    return [g1]


def evaluate_safety_gate(current: dict[str, Any]) -> list[GateVerdict]:
    """G6 — Safety fail-safe pass rate must be 100%."""
    fail_safe = current.get("fail_safe_rate")
    if fail_safe is None:
        # Derive from failure_categories distribution if needed.
        fc = current.get("failure_categories", {}) or {}
        total = sum(fc.values()) or current.get("pass_rate_total") or 0
        unsafe = sum(v for k, v in fc.items() if k and k != "none")
        if total:
            fail_safe = 1 - (unsafe / total)

    if fail_safe is None:
        return [
            GateVerdict(
                "G6",
                "Safety hard gate",
                "alert",
                "fail_safe_rate",
                None,
                None,
                None,
                f"≥ {T.G6_SAFETY_FAIL_SAFE_MIN:.0%}",
                "no fail_safe_rate in current run",
            )
        ]
    status: GateStatus = "pass" if fail_safe >= T.G6_SAFETY_FAIL_SAFE_MIN else "hard-block"
    return [
        GateVerdict(
            gate_id="G6",
            name="Safety hard gate",
            status=status,
            metric="fail_safe_rate",
            current=fail_safe,
            baseline=T.G6_SAFETY_FAIL_SAFE_MIN,
            delta=fail_safe - T.G6_SAFETY_FAIL_SAFE_MIN,
            threshold=f"≥ {T.G6_SAFETY_FAIL_SAFE_MIN:.0%}",
            detail=f"observed = {fail_safe:.2%}",
        )
    ]


def evaluate_triage_gate(
    current: dict[str, Any], product_version: Literal["v0.1", "v0.2"] = "v0.1"
) -> list[GateVerdict]:
    """G5 — Triage label F1 floor; v0.1 ⇒ 0.55, v0.2 ⇒ 0.65."""
    floor = (
        T.G5_TRIAGE_LABEL_F1_MIN_V0_1
        if product_version == "v0.1"
        else T.G5_TRIAGE_LABEL_F1_MIN_V0_2
    )
    f1 = current.get("label_f1") or current.get("mean_f1")
    if f1 is None:
        return [
            GateVerdict(
                "G5",
                "Triage label",
                "alert",
                "label_f1",
                None,
                None,
                None,
                f"≥ {floor:.2f}",
                "no label_f1 / mean_f1 in current run",
            )
        ]
    status: GateStatus = "pass" if f1 >= floor else "soft-warn"
    return [
        GateVerdict(
            gate_id="G5",
            name="Triage label",
            status=status,
            metric="label_f1",
            current=f1,
            baseline=floor,
            delta=f1 - floor,
            threshold=f"≥ {floor:.2f} ({product_version})",
            detail=f"observed = {f1:.3f}",
        )
    ]


def render_markdown(
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
    verdicts: list[GateVerdict],
) -> str:
    lines: list[str] = ["## Eval Gate Summary"]
    lines.append("")
    lines.append(
        f"- **Current run** — task `{current.get('task', '?')}` · "
        f"dataset `{current.get('dataset_name', '?')}` · "
        f"model `{current.get('model', '?')}` · "
        f"solver `{current.get('solver_id', '?')}`"
    )
    if baseline is not None:
        lines.append(
            f"- **Baseline** — dataset `{baseline.get('dataset_name', '?')}` · "
            f"model `{baseline.get('model', '?')}` · "
            f"solver `{baseline.get('solver_id', '?')}`"
        )
    lines.append("")
    lines.append("| Gate | Status | Metric | Current | Baseline | Δ | Threshold |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for v in verdicts:
        cur = f"{v.current:.3f}" if isinstance(v.current, int | float) else "—"
        base = f"{v.baseline:.3f}" if isinstance(v.baseline, int | float) else "—"
        delta = f"{v.delta:+.3f}" if isinstance(v.delta, int | float) else "—"
        lines.append(
            f"| **{v.gate_id} · {v.name}** | {v.emoji} {v.status} | "
            f"`{v.metric}` | {cur} | {base} | {delta} | {v.threshold} |"
        )
    lines.append("")
    for v in verdicts:
        lines.append(f"- {v.emoji} **{v.gate_id}**: {v.detail}")
    lines.append("")
    return "\n".join(lines)


def _exit_code(verdicts: list[GateVerdict]) -> int:
    for v in verdicts:
        if v.status in ("hard-block", "block-release"):
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True, type=pathlib.Path)
    parser.add_argument(
        "--baseline",
        type=pathlib.Path,
        help="Required for --gate review; optional for --gate safety/triage.",
    )
    parser.add_argument("--gate", choices=("review", "safety", "triage"), default="review")
    parser.add_argument("--product-version", choices=("v0.1", "v0.2"), default="v0.1")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    args = parser.parse_args(argv)

    current = _load_summary(args.current)
    baseline = _load_summary(args.baseline) if args.baseline else None

    if args.gate == "review":
        if baseline is None:
            parser.error("--baseline is required when --gate review")
        verdicts = evaluate_review_gates(current, baseline)
    elif args.gate == "safety":
        verdicts = evaluate_safety_gate(current)
    else:
        verdicts = evaluate_triage_gate(current, args.product_version)

    markdown = render_markdown(current, baseline, verdicts)
    if args.out:
        args.out.write_text(markdown + "\n", encoding="utf-8")
    else:
        sys.stdout.write(markdown + "\n")
    return _exit_code(verdicts)


if __name__ == "__main__":
    raise SystemExit(main())
