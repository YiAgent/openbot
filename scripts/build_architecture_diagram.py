#!/usr/bin/env python3
"""Regenerate ``docs/assets/openbot-architecture.svg`` (embedded in README.md).

The SVG is authored here rather than by hand so the diagram can be kept in
step with the code it describes: edit the ``NODES`` / ``EDGES`` / ``BODY``
tables below, re-run this script, and commit the regenerated SVG.

    python3 scripts/build_architecture_diagram.py

Layout follows the fireworks-tech-graph architecture-diagram rules (Style 2,
"Dark Terminal") under the ``showcase`` composition profile: zero edge
crossings, at most two bends per edge, and >= 40px of whitespace between
nodes. The ``data-graph-role`` / ``data-node-id`` / ``data-edge-id``
attributes are what that skill's geometry and composition validators read --
keep them on any element you add.
"""

from __future__ import annotations

from pathlib import Path

from _diagram_svg import EVAL, FLOW, LLM, MUTE, POLICY, STATE, SUB, THEMES, TXT, Canvas

W, H = 1000, 960

# --------------------------------------------------------------- geometry grid
SPINE_X, SPINE_W = 296, 410  # centre column, centre line x = 501
LCOL_X, LCOL_W = 36, 180  # left column
RCOL_X, RCOL_W = 766, 198  # right column
CX = SPINE_X + SPINE_W // 2  # 501

# id: (x, y, w, h, theme)
NODES = {
    "n1": (SPINE_X, 84, SPINE_W, 54, "external"),
    "n2": (SPINE_X, 178, SPINE_W, 76, "compute"),
    "n3": (SPINE_X, 294, SPINE_W, 84, "compute"),
    "n4": (SPINE_X, 434, SPINE_W, 62, "queue"),
    "n5": (SPINE_X, 552, SPINE_W, 94, "compute"),
    "n6": (SPINE_X, 686, SPINE_W, 94, "agent"),
    "n7": (SPINE_X, 828, SPINE_W, 58, "external"),
    "l1": (LCOL_X, 558, LCOL_W, 82, "config"),
    "l2": (LCOL_X, 692, LCOL_W, 82, "sandbox"),
    "l3": (LCOL_X, 828, LCOL_W, 58, "eval"),
    "r1": (RCOL_X, 300, RCOL_W, 72, "store"),
    "r2": (RCOL_X, 558, RCOL_W, 82, "store"),
    "r3": (RCOL_X, 692, RCOL_W, 82, "agent"),
}

# id, x, y, w, h, label, label x, label y
CONTAINERS = [
    ("cA", 276, 150, 450, 248, "WEB PROCESS", 290, 170),
    ("cB", 276, 520, 450, 280, "WORKER PROCESS", 290, 540),
]

# id, source, target, d, colour, dash, two-way, label, lx, ly, anchor, label size
EDGES = [
    (
        "e1",
        "n1",
        "n2",
        "M 501,138 L 501,178",
        FLOW,
        None,
        False,
        "webhook delivery",
        513,
        162,
        "start",
        11,
    ),
    (
        "e2",
        "n2",
        "n3",
        "M 501,254 L 501,294",
        FLOW,
        None,
        False,
        "UnifiedEvent",
        513,
        278,
        "start",
        11,
    ),
    (
        "e3",
        "n3",
        "n4",
        "M 501,378 L 501,434",
        FLOW,
        None,
        False,
        "TaskSpec v3",
        513,
        418,
        "start",
        11,
    ),
    (
        "e4",
        "n4",
        "n5",
        "M 501,496 L 501,552",
        FLOW,
        None,
        False,
        "XREADGROUP",
        513,
        538,
        "start",
        11,
    ),
    (
        "e5",
        "n5",
        "n6",
        "M 501,646 L 501,686",
        FLOW,
        None,
        False,
        "PreflightContext",
        513,
        670,
        "start",
        11,
    ),
    (
        "e6",
        "n6",
        "n7",
        "M 501,780 L 501,828",
        FLOW,
        None,
        False,
        "review · comment · PR",
        513,
        816,
        "start",
        11,
    ),
    (
        "e7",
        "l1",
        "n5",
        "M 216,599 L 296,599",
        POLICY,
        None,
        False,
        "config",
        246,
        591,
        "middle",
        10,
    ),
    ("e8", "n6", "l2", "M 296,733 L 216,733", POLICY, None, False, "exec", 246, 725, "middle", 10),
    (
        "e9",
        "l3",
        "n6",
        "M 216,857 L 246,857 L 246,770 L 296,770",
        EVAL,
        "5,4",
        False,
        "reuse",
        252,
        816,
        "start",
        10,
    ),
    ("e10", "n3", "r1", "M 706,336 L 766,336", STATE, None, True, "state", 746, 328, "middle", 10),
    ("e11", "n5", "r2", "M 706,599 L 766,599", STATE, None, True, "audit", 746, 591, "middle", 10),
    ("e12", "n6", "r3", "M 706,733 L 766,733", LLM, None, True, "model", 746, 725, "middle", 10),
]

# node id -> (badge, text x or None for centred, [(dy, size, weight, fill, text)])
BODY = {
    "n1": (
        None,
        None,
        [
            (28, 14, 700, TXT, "GitHub"),
            (47, 11, 400, SUB, "issues · pull requests · comments · labels"),
        ],
    ),
    "n2": (
        "1",
        SPINE_X + 40,
        [
            (26, 13, 700, TXT, "POST /webhook/github — FastAPI ingress"),
            (47, 11, 400, SUB, "raw body → HMAC verify → parse → UnifiedEvent"),
            (65, 11, 400, MUTE, "202 Accepted returned before any agent work"),
        ],
    ),
    "n3": (
        "2",
        SPINE_X + 40,
        [
            (25, 13, 700, TXT, "ingest_webhook"),
            (44, 11, 400, SUB, "dedup · router → feature + task_id"),
            (61, 11, 400, SUB, "run state: start / supersede / cancel / ignore"),
            (78, 11, 400, MUTE, "check run created · TaskSpec v3 enqueued"),
        ],
    ),
    "n4": (
        "3",
        SPINE_X + 40,
        [
            (26, 13, 700, TXT, "Redis Stream — task queue"),
            (48, 11, 400, SUB, "consumer group · XAUTOCLAIM reclaim · retry → DLQ"),
        ],
    ),
    "n5": (
        "4",
        SPINE_X + 40,
        [
            (26, 13, 700, TXT, "LLM classifier → preflight chain"),
            (48, 11, 400, SUB, "intent · severity · sandbox need (Redis-cached)"),
            (66, 11, 400, SUB, "sanitize · kill switch · feature toggle · cancel"),
            (84, 11, 400, SUB, "fork-PR · actor role · rate limit · budget · audit"),
        ],
    ),
    "n6": (
        "5",
        SPINE_X + 40,
        [
            (26, 13, 700, TXT, "workflow handler → DeepAgents runtime"),
            (48, 11, 400, SUB, "triage (+ reproduce) · review · fix · chat"),
            (66, 11, 400, SUB, "LangGraph loop · repo tools · budget guard"),
            (84, 11, 400, MUTE, "structured result → domain object"),
        ],
    ),
    "n7": (
        "6",
        SPINE_X + 40,
        [
            (26, 13, 700, TXT, "GitHub write-back"),
            (46, 11, 400, SUB, "PR review · sticky comment · labels · branch + PR"),
        ],
    ),
    "l1": (
        None,
        LCOL_X + 14,
        [
            (24, 12, 700, TXT, ".openbot/config.yaml"),
            (45, 10, 400, SUB, "features · budgets"),
            (60, 10, 400, SUB, "rate limits · actors"),
            (75, 10, 400, MUTE, "reviewed like code"),
        ],
    ),
    "l2": (
        None,
        LCOL_X + 14,
        [
            (24, 12, 700, TXT, "Sandbox"),
            (45, 10, 400, SUB, "daytona · docker · fake"),
            (60, 10, 400, SUB, "clone · exec · diff · push"),
            (75, 10, 400, MUTE, "SandboxPort protocol"),
        ],
    ),
    "l3": (
        None,
        LCOL_X + 14,
        [
            (25, 12, 700, TXT, "evals/ · Inspect AI"),
            (46, 10, 400, SUB, "openbot.evaluation facade"),
        ],
    ),
    "r1": (
        None,
        RCOL_X + 14,
        [
            (27, 12, 700, TXT, "Redis"),
            (48, 10, 400, SUB, "delivery dedup · run locks"),
            (63, 10, 400, SUB, "cancel signals · rate limit"),
        ],
    ),
    "r2": (
        None,
        RCOL_X + 14,
        [
            (24, 12, 700, TXT, "Postgres"),
            (45, 10, 400, SUB, "task_runs · audit_log"),
            (60, 10, 400, SUB, "cost_meter → budget gate"),
            (75, 10, 400, MUTE, "the product ledger"),
        ],
    ),
    "r3": (
        None,
        RCOL_X + 14,
        [
            (24, 12, 700, TXT, "LLM providers"),
            (45, 10, 400, SUB, "LiteLLM model router"),
            (60, 10, 400, SUB, "primary + fallback"),
            (75, 10, 400, MUTE, "bring your own keys"),
        ],
    ),
}

LEGEND = [
    (FLOW, None, False, "runtime request path"),
    (STATE, None, True, "state read / write"),
    (LLM, None, True, "LLM call"),
    (POLICY, None, False, "policy & execution"),
    (EVAL, "4,3", False, "eval reuse (offline)"),
]

canvas = Canvas(
    W,
    H,
    title="OpenBot runtime architecture",
    aria_label=(
        "OpenBot runtime architecture: a GitHub webhook enters the FastAPI web process, is"
        " deduplicated and enqueued as a TaskSpec on a Redis Stream, then a worker process"
        " classifies it, runs the preflight gate chain, and executes a DeepAgents workflow inside"
        " a sandbox before writing results back to GitHub."
    ),
)

canvas.title_block(
    "OpenBot — runtime architecture",
    "self-hosted GitHub App: webhook → preflight gates → queued worker → sandboxed agent"
    " → write-back",
    "v0.1 · pre-alpha",
)

for cid, cx, cy, cw, ch, label, lx, ly in CONTAINERS:
    canvas.container(cid, cx, cy, cw, ch, label, lx, ly)

for eid, src, tgt, d, colour, dash, twoway, *_ in EDGES:
    canvas.edge(eid, src, tgt, d, colour, dash=dash, twoway=twoway)

for nid, (x, y, w, h, theme) in NODES.items():
    canvas.node(nid, x, y, w, h, theme, dashed=theme == "eval")

for nid, (badge, text_x, rows) in BODY.items():
    x, y, w, h, theme = NODES[nid]
    _, stroke = THEMES[theme]
    if badge:
        canvas.badge(x + 20, y + rows[0][0] - 5, badge, stroke)
    for dy, size, weight, fill_c, txt in rows:
        if text_x is None:
            canvas.text(x + w // 2, y + dy, size, weight, fill_c, txt, anchor="middle")
        else:
            canvas.text(text_x, y + dy, size, weight, fill_c, txt)

for eid, _src, _tgt, _d, colour, _dash, _twoway, label, lx, ly, anchor, size in EDGES:
    canvas.edge_label(eid, lx, ly, size, colour, label, anchor=anchor)

canvas.legend(
    LEGEND,
    y=916,
    footer="cross-cutting — Sentry · LangSmith · Langfuse · Prometheus tracing on both"
    " processes; audit_log is the product-facing ledger",
    footer_y=942,
)
canvas.add("</svg>")

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "openbot-architecture.svg"


def main() -> int:
    """Write the diagram to its one tracked location.

    Deliberately takes no output-path argument: the SVG is committed at a
    fixed path that README.md links to, so an override would only ever
    produce an untracked copy the README cannot see.
    """
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(canvas.render(), encoding="utf-8")
    print(f"wrote {OUT} ({len(canvas.lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
