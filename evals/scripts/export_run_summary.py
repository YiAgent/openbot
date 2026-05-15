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
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from evals.common.artifacts import export_artifact
from evals.common.langsmith import log_run_metadata, log_sample
from evals.common.metadata import collect_run_metadata


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


def _sample_usage(sample: dict[str, Any]) -> tuple[int | None, int | None, float | None]:
    usage = sample.get("model_usage", {}) or {}
    if isinstance(usage, dict) and usage:
        tokens_in = 0
        tokens_out = 0
        cost = 0.0
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            tokens_in += value.get("input_tokens", value.get("prompt_tokens", 0)) or 0
            tokens_out += value.get("output_tokens", value.get("completion_tokens", 0)) or 0
            cost += value.get("total_cost", 0.0) or 0.0
        return tokens_in, tokens_out, cost

    provider_usage = (sample.get("metadata") or {}).get("provider_usage", {})
    if isinstance(provider_usage, dict) and provider_usage:
        return (
            provider_usage.get("input_tokens"),
            provider_usage.get("output_tokens"),
            provider_usage.get("total_cost"),
        )
    return None, None, None


def _scorer_payload(sample: dict[str, Any], scorer_name: str) -> dict[str, Any]:
    score = sample.get("scores", {}).get(scorer_name, {}) or {}
    metadata = score.get("metadata", {}) if isinstance(score, dict) else {}
    return {
        "f1": score.get("value") if isinstance(score, dict) else None,
        "precision": metadata.get("precision"),
        "recall": metadata.get("recall"),
        "candidate_count": metadata.get("candidate_count"),
        "golden_count": metadata.get("golden_count"),
        "explanation": score.get("explanation", "") if isinstance(score, dict) else "",
    }


def sync_eval_payload_to_langsmith(
    data: dict[str, Any],
    *,
    client: Any,
    project_name: str,
    dataset_sha256: str,
    mode: str,
) -> str:
    """Create one LangSmith run from an Inspect `.eval` payload and attach samples."""
    eval_meta = data.get("eval", {})
    results = data.get("results", {})
    scorer_block = (results.get("scores") or [{}])[0]
    scorer_name = scorer_block.get("name", "?")
    task_metadata = eval_meta.get("metadata", {}) or {}
    dataset_version = task_metadata.get("dataset_version") or eval_meta.get("dataset", {}).get(
        "name"
    )
    solver_id = task_metadata.get("solver_id", "deepagents_baseline")
    solver_family = task_metadata.get("solver_family", "baseline")

    run_metadata = collect_run_metadata(
        eval_meta.get("task", "review_martian"),
        mode,
        dataset_version=dataset_version,
        dataset_sha256=dataset_sha256,
        solver_id=solver_id,
        solver_family=solver_family,
    )
    git_sha_short = run_metadata["git_sha"][:6]
    model_alias = "baseline"
    run_name = f"review-{dataset_version}-{git_sha_short}-{model_alias}-{mode}"
    run_id = str(uuid4())
    now = datetime.now(UTC)
    client.create_run(
        id=run_id,
        name=run_name,
        run_type="chain",
        project_name=project_name,
        inputs={"dataset_name": dataset_version},
        outputs={"sample_count": len(data.get("samples", []) or [])},
        start_time=now,
        end_time=now,
    )
    log_run_metadata(client, run_id, run_metadata)

    for sample in data.get("samples", []) or []:
        sample_id = sample.get("id", "?")
        input_ref = export_artifact(
            sample_id,
            "diff",
            sample.get("input", ""),
            client=client,
            project_name=project_name,
        )
        output_ref = export_artifact(
            sample_id,
            "raw_output",
            (sample.get("output") or {}).get("completion", ""),
            client=client,
            project_name=project_name,
        )
        tokens_in, tokens_out, cost_usd = _sample_usage(sample)
        scorer_payload = _scorer_payload(sample, scorer_name)
        sample_record = {
            "sample_id": sample_id,
            "input_artifact_refs": [input_ref],
            "output_artifact_refs": [output_ref],
            "score_payload": scorer_payload,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "latency_ms": int((sample.get("total_time") or 0) * 1000),
            "step_count": (sample.get("metadata") or {}).get("step_count", 0),
            "tool_call_count": (sample.get("metadata") or {}).get("tool_call_count", 0),
            "retry_count": sample.get("error_retries", 0),
            "sandbox_restart_count": (sample.get("metadata") or {}).get("sandbox_restart_count", 0),
            "failure_category": (
                (sample.get("scores", {}).get(scorer_name, {}) or {}).get("metadata") or {}
            ).get("failure_category", "none"),
        }
        log_sample(client, run_id, sample_record)
    return run_id


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
    parser.add_argument(
        "--push-langsmith",
        action="store_true",
        help="After loading a local .eval log, create a LangSmith run and sample feedback records.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="LangSmith project name for --push-langsmith.",
    )
    parser.add_argument(
        "--dataset-sha256",
        default=None,
        help="Dataset SHA256 required by --push-langsmith run metadata.",
    )
    parser.add_argument(
        "--mode",
        choices=["smoke", "regression", "weekly", "release"],
        default="smoke",
        help="Run mode recorded by --push-langsmith (default: smoke).",
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
        if args.push_langsmith:
            if not args.project or not args.dataset_sha256:
                print(
                    "FATAL: --push-langsmith requires both --project and --dataset-sha256",
                    file=sys.stderr,
                )
                return 1
            from langsmith import Client

            run_id = sync_eval_payload_to_langsmith(
                data,
                client=Client(),
                project_name=args.project,
                dataset_sha256=args.dataset_sha256,
                mode=args.mode,
            )
            print(f"synced LangSmith run {run_id}", file=sys.stderr)
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
