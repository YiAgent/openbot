"""Inspect AI .eval log → markdown summary — PRD §11.3 / §14 E1.

Reads either an Inspect AI `.eval` log file or a LangSmith-shaped payload and
writes a markdown report containing:
  - suite name / dataset / sample count / pass rate
  - per-sample F1 + scorer explanation
  - aggregate mean F1 / stderr
  - failure_category distribution (PRD §12.4)
  - top-5 cost samples

Use:
    uv run python -m evals.scripts.export_run_summary <path.eval> [--out report.md]
    uv run python -m evals.scripts.export_run_summary --from-langsmith-json payload.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections import Counter
from typing import Any


def _load_eval_log(path: pathlib.Path) -> dict[str, Any]:
    """Use `inspect log dump` to convert the binary .eval into JSON."""
    result = subprocess.run(
        ["uv", "run", "inspect", "log", "dump", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _aggregate(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the metrics we want into a flat dict."""
    eval_meta = data.get("eval", {})
    results = data.get("results", {})
    samples = data.get("samples", []) or []

    scorer_block = (results.get("scores") or [{}])[0]
    scorer_name = scorer_block.get("name", "?")
    metrics_field = scorer_block.get("metrics", [])
    if isinstance(metrics_field, dict):
        metric_lookup = {m["name"]: m["value"] for m in metrics_field.values()}
    else:
        metric_lookup = {m["name"]: m["value"] for m in metrics_field}

    per_sample: list[dict[str, Any]] = []
    failure_categories: Counter[str] = Counter()
    cost_per_sample: list[tuple[str, float | None]] = []
    for s in samples:
        sid = s.get("id", "?")
        rev = s.get("scores", {}).get(scorer_name, {})
        meta = rev.get("metadata", {}) if isinstance(rev, dict) else {}
        per_sample.append(
            {
                "id": sid,
                "f1": rev.get("value") if isinstance(rev, dict) else None,
                "precision": meta.get("precision"),
                "recall": meta.get("recall"),
                "candidate_count": meta.get("candidate_count"),
                "golden_count": meta.get("golden_count"),
                "explanation": rev.get("explanation", "") if isinstance(rev, dict) else "",
            }
        )
        failure_categories[meta.get("failure_category", "none")] += 1
        usage = s.get("model_usage", {}) or {}
        cost: float | None = None
        if isinstance(usage, dict) and usage:
            cost = 0.0
            for u in usage.values():
                if isinstance(u, dict):
                    cost += u.get("total_cost", 0.0) or 0.0
        else:
            provider_usage = (s.get("metadata") or {}).get("provider_usage", {})
            if isinstance(provider_usage, dict):
                provider_cost = provider_usage.get("total_cost")
                if isinstance(provider_cost, int | float):
                    cost = float(provider_cost)
        cost_per_sample.append((sid, cost))

    cost_per_sample.sort(key=lambda kv: kv[1] if kv[1] is not None else -1, reverse=True)

    return {
        "task": eval_meta.get("task", "?"),
        "task_display_name": eval_meta.get("task_display_name", "?"),
        "dataset_name": eval_meta.get("dataset", {}).get("name", "?"),
        "dataset_samples": eval_meta.get("dataset", {}).get("samples", len(samples)),
        "sample_ids": eval_meta.get("dataset", {}).get("sample_ids", []),
        "model": eval_meta.get("model", "?"),
        "solver_id": eval_meta.get("metadata", {}).get("solver_id", "?"),
        "solver_family": eval_meta.get("metadata", {}).get("solver_family", "?"),
        "scorer": scorer_name,
        "mean_f1": metric_lookup.get("mean"),
        "stderr": metric_lookup.get("stderr"),
        "per_sample": per_sample,
        "pass_rate": sum(1 for p in per_sample if (p["f1"] or 0) >= 0.5) / max(len(per_sample), 1),
        "failure_categories": dict(failure_categories),
        "top_cost_samples": cost_per_sample[:5],
    }


def _aggregate_langsmith_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate a LangSmith-shaped payload into the same report schema."""
    run = payload.get("run", {})
    metadata = (run.get("extra") or {}).get("metadata", {})
    samples = payload.get("samples", []) or []

    per_sample: list[dict[str, Any]] = []
    failure_categories: Counter[str] = Counter()
    cost_per_sample: list[tuple[str, float | None]] = []
    for sample in samples:
        score = sample.get("score_payload", {}) or {}
        sid = sample.get("sample_id", "?")
        per_sample.append(
            {
                "id": sid,
                "f1": score.get("f1"),
                "precision": score.get("precision"),
                "recall": score.get("recall"),
                "candidate_count": score.get("candidate_count"),
                "golden_count": score.get("golden_count"),
                "explanation": score.get("explanation", ""),
            }
        )
        failure_categories[sample.get("failure_category", "none")] += 1
        cost_per_sample.append((sid, sample.get("cost_usd")))

    cost_per_sample.sort(key=lambda kv: kv[1] if kv[1] is not None else -1, reverse=True)
    f1_values = [p["f1"] for p in per_sample if isinstance(p["f1"], int | float)]
    mean_f1 = sum(f1_values) / len(f1_values) if f1_values else None

    return {
        "task": metadata.get("suite_name", "?"),
        "task_display_name": metadata.get("suite_name", "?"),
        "dataset_name": metadata.get(
            "dataset_version", run.get("inputs", {}).get("dataset_name", "?")
        ),
        "dataset_samples": len(samples),
        "sample_ids": [p["id"] for p in per_sample],
        "model": metadata.get("model_id", "?"),
        "solver_id": metadata.get("solver_id", "?"),
        "solver_family": metadata.get("solver_family", "?"),
        "scorer": "langsmith_sample_feedback",
        "mean_f1": mean_f1,
        "stderr": None,
        "per_sample": per_sample,
        "pass_rate": sum(1 for p in per_sample if (p["f1"] or 0) >= 0.5) / max(len(per_sample), 1),
        "failure_categories": dict(failure_categories),
        "top_cost_samples": cost_per_sample[:5],
    }


def _render_markdown(agg: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Eval Run Summary · {agg['task_display_name']}")
    lines.append("")
    lines.append(f"- **Dataset**: `{agg['dataset_name']}` — {agg['dataset_samples']} samples")
    lines.append(f"- **Model**: `{agg['model']}`")
    lines.append(f"- **Solver**: `{agg['solver_id']}` (`{agg['solver_family']}`)")
    lines.append(f"- **Scorer**: `{agg['scorer']}`")
    mean_f1 = agg["mean_f1"]
    se = agg["stderr"]
    mean_str = f"{mean_f1:.3f}" if isinstance(mean_f1, int | float) else "?"
    se_str = f"{se:.3f}" if isinstance(se, int | float) else "?"
    lines.append(f"- **Mean F1**: **{mean_str}** ± {se_str} (stderr)")
    passed = sum(1 for p in agg["per_sample"] if (p["f1"] or 0) >= 0.5)
    total = len(agg["per_sample"])
    lines.append(f"- **Pass rate (F1 ≥ 0.5)**: {agg['pass_rate']:.0%} ({passed} / {total})")
    lines.append("")
    lines.append("## Per-sample")
    lines.append("")
    lines.append("| Sample | F1 | Precision | Recall | Cand. | Golden | Notes |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for p in agg["per_sample"]:
        f1 = f"{p['f1']:.3f}" if p["f1"] is not None else "?"
        prec = f"{p['precision']:.3f}" if p["precision"] is not None else "?"
        rec = f"{p['recall']:.3f}" if p["recall"] is not None else "?"
        lines.append(
            f"| `{p['id']}` | {f1} | {prec} | {rec} | {p['candidate_count']} | {p['golden_count']} | {p['explanation']} |"
        )
    lines.append("")
    lines.append("## failure_category distribution (PRD §12.4)")
    lines.append("")
    for cat, n in sorted(agg["failure_categories"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- `{cat}`: {n}")
    lines.append("")
    if agg["top_cost_samples"]:
        lines.append("## Top-5 cost samples")
        lines.append("")
        lines.append("| Sample | Cost (USD) |")
        lines.append("|---|---:|")
        for sid, cost in agg["top_cost_samples"]:
            cost_text = f"{cost:.4f}" if isinstance(cost, int | float) else "unavailable"
            lines.append(f"| `{sid}` | {cost_text} |")
        lines.append("")
    lines.append(
        "_Generated by `evals/scripts/export_run_summary.py` from the inspect-ai .eval log._"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_run_summary")
    parser.add_argument("eval_log", nargs="?", type=pathlib.Path, help="Path to the .eval log")
    parser.add_argument(
        "--from-langsmith-json",
        type=pathlib.Path,
        default=None,
        help="Path to a LangSmith-shaped JSON payload containing `run` + `samples`.",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=None,
        help="Output markdown path (default: stdout)",
    )
    args = parser.parse_args(argv)

    if args.from_langsmith_json is not None:
        if not args.from_langsmith_json.exists():
            print(f"FATAL: {args.from_langsmith_json} not found", file=sys.stderr)
            return 1
        payload = json.loads(args.from_langsmith_json.read_text(encoding="utf-8"))
        agg = _aggregate_langsmith_payload(payload)
    else:
        if args.eval_log is None or not args.eval_log.exists():
            print(f"FATAL: {args.eval_log} not found", file=sys.stderr)
            return 1
        data = _load_eval_log(args.eval_log)
        agg = _aggregate(data)
    md = _render_markdown(agg)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
