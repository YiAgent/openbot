"""Pull aggregate metrics from a LangSmith run, emit markdown — PRD §11.3 / §14 E1.

**E1-T09 SKELETON ONLY.** Blocked on E1-T08 (no real run exists to summarize)
and on E1-T04 (no live LangSmith API client to query).

When unblocked, the CLI shape is:
  python -m evals.scripts.export_run_summary <run_id> > docs/reports/eval-sample-summary.md

Output must include (per task spec):
  - suite 名 / 总 sample / 通过率 / 总 cost / failure_category 分布 / 异常 top-5

See `docs/eval/handoffs/E1-T09.md` for unblock checklist.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print(
        "E1-T09 export_run_summary is blocked: needs E1-T08 (a real run) "
        "and E1-T04 (live LangSmith client). See docs/eval/handoffs/E1-T09.md.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
