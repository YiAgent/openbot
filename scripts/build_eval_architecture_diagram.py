#!/usr/bin/env python3
"""Regenerate ``docs/assets/openbot-eval-architecture.svg`` (embedded in README.md).

Shows the two eval lanes described by ``docs/prd/openbot-eval-prd.md`` and
``docs/prd/openbot-eval-suites.md``:

  * OFFLINE (v0.1, built) — benchmark datasets driven through the *production*
    agent path via ``openbot.evaluation``, scored, then exported to LangSmith.
  * ONLINE (v0.3, planned) — signal harvested from real production runs.

plus a coverage strip for the four product workflows in
``openbot/domain/workflows.py`` (triage / review / fix / chat).

Solid borders are built today; dashed borders are specified in the eval PRD
but not implemented. Edit the tables below and re-run:

    python3 scripts/build_eval_architecture_diagram.py

Layout follows the fireworks-tech-graph architecture-diagram rules (Style 2,
"Dark Terminal") under the ``showcase`` composition profile. The shared
emitter lives in ``scripts/_diagram_svg.py``.
"""

from __future__ import annotations

from pathlib import Path

from _diagram_svg import FLOW, LLM, MUTE, STATE, SUB, THEMES, TXT, Canvas

W, H = 1060, 1012

# ── grid ───────────────────────────────────────────────────────────────────
LANE_W = 432
A_X, B_X = 58, 570  # lane node left edges
A_CX, B_CX = A_X + LANE_W // 2, B_X + LANE_W // 2
ROWS = [(132, 66), (238, 74), (352, 74), (466, 80), (586, 74)]
LS_Y, LS_H = 722, 66
CARD_Y, CARD_H, CARD_W = 858, 70, 207
CARD_X = [56, 303, 550, 797]

# id: (x, y, w, h, theme, dashed)
NODES: dict[str, tuple[int, int, int, int, str, bool]] = {}
for _i, (_y, _h) in enumerate(ROWS, start=1):
    NODES[f"a{_i}"] = (A_X, _y, LANE_W, _h, "", False)
    NODES[f"b{_i}"] = (B_X, _y, LANE_W, _h, "", False)

_A_THEMES = ["store", "external", "external", "agent", "compute"]
_B_THEMES = [
    ("external", False),
    ("store", False),
    ("planned", True),
    ("agent", True),
    ("sandbox", True),
]
for _i, _theme in enumerate(_A_THEMES, start=1):
    _x, _y, _w, _h, _, _ = NODES[f"a{_i}"]
    NODES[f"a{_i}"] = (_x, _y, _w, _h, _theme, False)
for _i, (_theme, _dashed) in enumerate(_B_THEMES, start=1):
    _x, _y, _w, _h, _, _ = NODES[f"b{_i}"]
    NODES[f"b{_i}"] = (_x, _y, _w, _h, _theme, _dashed)

NODES["ls"] = (36, LS_Y, 988, LS_H, "store", False)
for _i, _cx in enumerate(CARD_X, start=1):
    NODES[f"c{_i}"] = (_cx, CARD_Y, CARD_W, CARD_H, "eval" if _i == 1 else "external", _i == 1)

# id, x, y, w, h, label, label x, label y, dashed (dashed == not built yet)
CONTAINERS = [
    ("cA", 36, 96, 476, 586, "OFFLINE · v0.1 · BUILT", 50, 116, False),
    ("cB", 548, 96, 476, 586, "ONLINE · v0.3 · PLANNED", 562, 116, True),
    ("cC", 36, 822, 988, 126, "COVERAGE — THE FOUR PRODUCT WORKFLOWS", 50, 842, False),
]

# id, source, target, d, colour, dash, label, label x, label y
EDGES = [
    ("ea1", "a1", "a2", f"M {A_CX},198 L {A_CX},238", FLOW, None, "sample", 286, 221),
    ("ea2", "a2", "a3", f"M {A_CX},312 L {A_CX},352", FLOW, None, "solve()", 286, 335),
    ("ea3", "a3", "a4", f"M {A_CX},426 L {A_CX},466", FLOW, None, "run_*_sample", 286, 449),
    ("ea4", "a4", "a5", f"M {A_CX},546 L {A_CX},586", FLOW, None, "domain result", 286, 569),
    ("ea5", "a5", "ls", f"M {A_CX},660 L {A_CX},{LS_Y}", STATE, None, "experiment row", 286, 697),
    ("eb1", "b1", "b2", f"M {B_CX},198 L {B_CX},238", LLM, "5,4", "traced", 798, 221),
    ("eb2", "b2", "b3", f"M {B_CX},312 L {B_CX},352", LLM, "5,4", "run id", 798, 335),
    ("eb3", "b3", "b4", f"M {B_CX},426 L {B_CX},466", LLM, "5,4", "sampled", 798, 449),
    ("eb4", "b4", "b5", f"M {B_CX},546 L {B_CX},586", LLM, "5,4", "scored", 798, 569),
    ("eb5", "b5", "ls", f"M {B_CX},660 L {B_CX},{LS_Y}", STATE, None, "trend rows", 798, 697),
]

# node id -> (badge, text x or None for centred, [(dy, size, weight, fill, text)])
BODY = {
    "a1": (
        "1",
        A_X + 40,
        [
            (22, 12, 700, TXT, "benchmark datasets"),
            (40, 10, 400, SUB, "LangSmith mirrors published by make -C evals data-*"),
            (56, 10, 400, SUB, "Martian CRB · SWE-bench V · SWE-QA-Pro · SWT-Bench"),
        ],
    ),
    "a2": (
        "2",
        A_X + 40,
        [
            (22, 12, 700, TXT, "Inspect AI task — one per benchmark"),
            (40, 10, 400, SUB, "evals/tasks/: review_martian · fix_swe_bench ·"),
            (55, 10, 400, SUB, "chat_swe_qa · test_swt_bench"),
            (70, 10, 400, MUTE, "dataset + solver + scorers wired per task"),
        ],
    ),
    "a3": (
        "3",
        A_X + 40,
        [
            (22, 12, 700, TXT, "thin solver → openbot.evaluation"),
            (40, 10, 400, SUB, "evals/solvers/*.py call runner.run_*_sample and adapt"),
            (55, 10, 400, SUB, "the domain result into scorer / prediction shape"),
            (70, 10, 400, MUTE, "forbidden here: eval-only agents, sandboxes, tools"),
        ],
    ),
    "a4": (
        "4",
        A_X + 40,
        [
            (22, 12, 700, TXT, "production agent + real sandbox"),
            (40, 10, 400, SUB, "the facade opens the prod sandbox, clones at base_sha,"),
            (55, 10, 400, SUB, "then calls DeepAgents review / fix / chat / repro —"),
            (70, 10, 400, SUB, "the same code the worker runs in production"),
        ],
    ),
    "a5": (
        "5",
        A_X + 40,
        [
            (22, 12, 700, TXT, "scorers + prediction export"),
            (40, 10, 400, SUB, "review: LLM judge + overlap · chat: SWE-QA-Pro judge"),
            (55, 10, 400, SUB, "fix / test: JSONL → official harness (Modal / docker)"),
            (70, 10, 400, MUTE, "→ grade writeback as pass@1 feedback"),
        ],
    ),
    "b1": (
        "1",
        B_X + 40,
        [
            (22, 12, 700, TXT, "production workflow runs"),
            (40, 10, 400, SUB, "triage · review · fix · chat on real repositories"),
            (56, 10, 400, MUTE, "live traffic, no benchmark dataset"),
        ],
    ),
    "b2": (
        "2",
        B_X + 40,
        [
            (22, 12, 700, TXT, "auto-instrumented traces"),
            (40, 10, 400, SUB, "LANGSMITH_TRACING on the web and worker processes;"),
            (55, 10, 400, SUB, "every agent run already lands in LangSmith today"),
            (70, 10, 400, MUTE, "this half of the online lane exists now"),
        ],
    ),
    "b3": (
        "3",
        B_X + 40,
        [
            (22, 12, 700, TXT, "outcome signals from GitHub"),
            (40, 10, 400, SUB, "was the review comment resolved or acted on; did the"),
            (55, 10, 400, SUB, "chat answer need a follow-up; was a triage label fixed"),
            (70, 10, 400, MUTE, "collector not built yet"),
        ],
    ),
    "b4": (
        "4",
        B_X + 40,
        [
            (22, 12, 700, TXT, "online eval + annotation queue"),
            (40, 10, 400, SUB, "sampled LLM judge over live runs, plus human labels"),
            (55, 10, 400, SUB, "from the LangSmith annotation queue"),
            (70, 10, 400, MUTE, "suites: triage_online · review_online · chat_online"),
        ],
    ),
    "b5": (
        "5",
        B_X + 40,
        [
            (22, 12, 700, TXT, "30-day trend metrics"),
            (40, 10, 400, SUB, "action_rate · resolved_rate · correct_30d ·"),
            (55, 10, 400, SUB, "macro_f1_30d · followup_rate · pass@1_90d"),
            (70, 10, 400, MUTE, "trend observation only — never blocks a merge"),
        ],
    ),
    "ls": (
        None,
        None,
        [
            (24, 13, 700, TXT, "LangSmith — one substrate for both lanes"),
            (
                43,
                11,
                400,
                SUB,
                "datasets · experiment rows · per-sample feedback · traces · annotation queue",
            ),
            (
                59,
                10,
                400,
                MUTE,
                "every row carries dataset_version · solver_family=openbot_agent · capability ·"
                " openbot_git_sha · mode",
            ),
        ],
    ),
    "c1": (
        None,
        CARD_X[0] + 14,
        [
            (20, 12, 700, TXT, "triage"),
            (36, 10, 400, SUB, "off · triage_gitbugs (v0.2)"),
            (51, 10, 400, SUB, "on · macro_f1_30d (v0.3)"),
            (66, 10, 400, MUTE, "no solver in evals/ yet"),
        ],
    ),
    "c2": (
        None,
        CARD_X[1] + 14,
        [
            (20, 12, 700, TXT, "review"),
            (36, 10, 400, SUB, "off · review_martian — mean F1"),
            (51, 10, 400, SUB, "on · action / resolved rate"),
            (66, 10, 400, MUTE, "10% F1 drop blocks merge"),
        ],
    ),
    "c3": (
        None,
        CARD_X[2] + 14,
        [
            (20, 12, 700, TXT, "fix"),
            (36, 10, 400, SUB, "off · fix_swe_bench — pass@1"),
            (51, 10, 400, SUB, "on · fix_swe_bench_live 90d"),
            (66, 10, 400, MUTE, "rolling benchmark, not prod"),
        ],
    ),
    "c4": (
        None,
        CARD_X[3] + 14,
        [
            (20, 12, 700, TXT, "chat"),
            (36, 10, 400, SUB, "off · chat_swe_qa — 5-dim"),
            (51, 10, 400, SUB, "on · correct_30d · followup"),
            (66, 10, 400, MUTE, "judge must exist; warn only"),
        ],
    ),
}

LEGEND = [
    (FLOW, None, False, "offline path — built"),
    (LLM, "5,4", False, "online path — planned"),
    (STATE, None, False, "writes to LangSmith"),
]

canvas = Canvas(
    W,
    H,
    title="OpenBot eval architecture",
    aria_label=(
        "OpenBot eval architecture: an offline lane runs benchmark datasets through Inspect AI"
        " tasks, thin solvers and the openbot.evaluation facade into the production agent and"
        " sandbox, then scores and exports predictions; a planned online lane harvests traces and"
        " GitHub outcome signals from production runs into online eval and 30-day trend metrics."
        " Both lanes write to LangSmith. A coverage strip lists the four product workflows:"
        " triage, review, fix and chat."
    ),
)

canvas.title_block(
    "OpenBot — eval architecture",
    "two lanes over one LangSmith substrate: offline benchmarks driven through the production"
    " agent, online signal from production runs",
    "v0.1 built · v0.3 planned",
)

for cid, cx, cy, cw, ch, label, lx, ly, dashed in CONTAINERS:
    canvas.container(cid, cx, cy, cw, ch, label, lx, ly, dashed=dashed)

for eid, src, tgt, d, colour, dash, *_ in EDGES:
    canvas.edge(eid, src, tgt, d, colour, dash=dash)

for nid, (x, y, w, h, theme, dashed) in NODES.items():
    canvas.node(nid, x, y, w, h, theme, dashed=dashed)

for nid, (badge, text_x, rows) in BODY.items():
    x, y, w, h, theme, _ = NODES[nid]
    _, stroke = THEMES[theme]
    if badge:
        canvas.badge(x + 20, y + rows[0][0] - 5, badge, stroke)
    for dy, size, weight, fill_c, txt in rows:
        if text_x is None:
            canvas.text(x + w // 2, y + dy, size, weight, fill_c, txt, anchor="middle")
        else:
            canvas.text(text_x, y + dy, size, weight, fill_c, txt)

for eid, _src, _tgt, _d, colour, _dash, label, lx, ly in EDGES:
    canvas.edge_label(eid, lx, ly, 10, colour, label)

canvas.legend(
    LEGEND,
    y=974,
    footer="solid = built today · dashed = specified in the eval PRD but not built ·"
    " test_swt_bench is a fifth surface, not a workflow",
    footer_y=998,
)
canvas.add("</svg>")

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "openbot-eval-architecture.svg"


def main() -> int:
    """Write the diagram to its one tracked location (see the sibling script)."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(canvas.render(), encoding="utf-8")
    print(f"wrote {OUT} ({len(canvas.lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
