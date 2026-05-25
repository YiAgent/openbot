"""CLI router for eval data lifecycle operations.

Usage:
    python -m evals.data <verb> <suite>

Verbs:
    collect  <suite>              Pull upstream, emit CollectedExample JSON to stdout.
    publish  <suite>              Read JSON on stdin, upsert into LangSmith dataset.
    refresh  <suite>              collect + publish in one step (idempotent).
    writeback <fix|swt>
        --report PATH             Path to Docker harness JSON report.
        --experiment NAME         LangSmith experiment project name.
        --dry-run                 Plan only, no create_feedback calls.
    inspect  <suite>              Print dataset status. Read-only.
"""

from __future__ import annotations

import argparse
import json
import sys

from evals.data._base import EvalDataset


def _resolve(name: str) -> EvalDataset:
    from evals.data import DATASETS

    try:
        return DATASETS[name]
    except KeyError:
        raise SystemExit(f"unknown suite {name!r}; choices: {sorted(DATASETS)}") from None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.data")
    sub = p.add_subparsers(dest="cmd", required=True)

    for verb in ("collect", "refresh"):
        s = sub.add_parser(verb)
        s.add_argument("suite")

    pub = sub.add_parser("publish")
    pub.add_argument("suite")

    wb = sub.add_parser("writeback")
    wb.add_argument("suite")
    wb.add_argument("--report", required=True)
    wb.add_argument("--experiment", required=True)
    wb.add_argument("--dry-run", action="store_true")

    insp = sub.add_parser("inspect")
    insp.add_argument("suite")

    args = p.parse_args(argv)
    suite = _resolve(args.suite)

    if args.cmd == "collect":
        json.dump(suite.collect(), sys.stdout, ensure_ascii=False)
        print()
    elif args.cmd == "publish":
        examples = json.loads(sys.stdin.read())
        r = suite.publish(examples)
        print(f"published {r.example_count} examples sha256={r.sha256}")
    elif args.cmd == "refresh":
        r = suite.publish(suite.collect())
        print(f"refreshed {suite.suite}: {r.example_count} examples sha256={r.sha256}")
    elif args.cmd == "writeback":
        s = suite.writeback_grades(
            report_path=args.report,
            experiment_name=args.experiment,
            dry_run=args.dry_run,
        )
        print(s)
    elif args.cmd == "inspect":
        print(f"suite={suite.suite} dataset_version={suite.dataset_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
