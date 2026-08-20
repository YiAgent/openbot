"""Shared Style 2 ("Dark Terminal") SVG emitter for the diagrams under ``scripts/``.

``build_architecture_diagram.py`` and ``build_eval_architecture_diagram.py``
draw with the same palette, arrow markers and semantic attributes; only their
node/edge tables differ. Keeping the emitter in one place stops the two
diagrams drifting apart visually, and means both carry the same
``data-graph-role`` / ``data-node-id`` / ``data-edge-id`` contract that the
fireworks-tech-graph geometry and composition validators read.

Not a general-purpose SVG library — it emits exactly the element shapes those
two diagrams need, in the paint order the validators expect (background →
containers → edges → node shapes → node text → edge labels → legend).
"""

from __future__ import annotations

import html

# ── palette ────────────────────────────────────────────────────────────────
BG_A, BG_B = "#0f0f1a", "#1a1a2e"
TXT, SUB, MUTE, LINE = "#e2e8f0", "#94a3b8", "#64748b", "#334155"

# Semantic arrow colours. Each needs a marker pair emitted into <defs>.
FLOW, STATE, LLM, POLICY, EVAL = "#3b82f6", "#10b981", "#a855f7", "#f97316", "#64748b"
MARKER_OF = {FLOW: "m-flow", STATE: "m-state", LLM: "m-llm", POLICY: "m-policy", EVAL: "m-eval"}

# (fill, stroke) per semantic node role.
THEMES = {
    "external": ("#132a47", "#3b82f6"),
    "compute": ("#1c1917", "#ea580c"),
    "queue": ("#052e16", "#059669"),
    "agent": ("#1e1b4b", "#7c3aed"),
    "store": ("#052e16", "#059669"),
    "sandbox": ("#292214", "#eab308"),
    "config": ("#0f172a", "#334155"),
    "eval": ("#0f172a", "#475569"),
    "planned": ("#131a28", "#64748b"),
}


def esc(s: str) -> str:
    return html.escape(s, quote=True)


class Canvas:
    """Accumulates SVG source lines in validator-friendly paint order."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        title: str,
        aria_label: str,
        quality_profile: str = "showcase",
    ) -> None:
        self.width = width
        self.height = height
        self.lines: list[str] = []
        self._open(title, aria_label, quality_profile)

    # ── low-level ──────────────────────────────────────────────────────────
    def add(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"

    # ── document scaffold ──────────────────────────────────────────────────
    def _open(self, title: str, aria_label: str, quality_profile: str) -> None:
        add = self.add
        add(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}"'
            f' width="{self.width}" height="{self.height}"'
        )
        add(f'     role="img" aria-label="{esc(aria_label)}"')
        add(f'     data-quality-profile="{quality_profile}">')
        add(f"  <title>{esc(title)}</title>")
        add("  <style>")
        add(
            "    text { font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', 'JetBrains Mono',"
            " 'Courier New', 'Microsoft YaHei', monospace; letter-spacing: 0.02em; }"
        )
        add("  </style>")
        add("  <defs>")
        add('    <linearGradient id="bg-grad" x1="0%" y1="0%" x2="100%" y2="100%">')
        add(f'      <stop offset="0%" stop-color="{BG_A}"/>')
        add(f'      <stop offset="100%" stop-color="{BG_B}"/>')
        add("    </linearGradient>")
        for colour, mid in MARKER_OF.items():
            add(
                f'    <marker id="{mid}" markerWidth="8" markerHeight="6" refX="7.5" refY="3"'
                ' orient="auto">'
            )
            add(f'      <polygon points="0 0, 8 3, 0 6" fill="{colour}"/>')
            add("    </marker>")
            add(
                f'    <marker id="{mid}-start" markerWidth="8" markerHeight="6" refX="0.5" refY="3"'
                ' orient="auto">'
            )
            add(f'      <polygon points="8 0, 0 3, 8 6" fill="{colour}"/>')
            add("    </marker>")
        add("  </defs>")
        add(
            f'  <rect data-graph-role="background" width="{self.width}" height="{self.height}"'
            ' fill="url(#bg-grad)"/>'
        )

    def title_block(self, heading: str, subtitle: str, tag: str) -> None:
        add = self.add
        add(
            f'  <text x="36" y="42" font-size="18" font-weight="700" fill="{TXT}">{esc(heading)}</text>'
        )
        add(f'  <text x="36" y="62" font-size="11" fill="{SUB}">{esc(subtitle)}</text>')
        add(
            f'  <text x="{self.width - 36}" y="42" font-size="11" fill="{MUTE}"'
            f' text-anchor="end">{esc(tag)}</text>'
        )

    # ── graph elements ─────────────────────────────────────────────────────
    def container(
        self,
        cid: str,
        x: int,
        y: int,
        w: int,
        h: int,
        label: str,
        lx: int,
        ly: int,
        *,
        dashed: bool = True,
    ) -> None:
        """Draw a grouping boundary.

        ``dashed`` defaults to True because a dashed boundary is the plain
        grouping convention. Pass ``dashed=False`` in a diagram that also
        uses dash to mean "not built yet", so a built group is not drawn in
        the unbuilt style.
        """
        dash_attr = ' stroke-dasharray="6,5"' if dashed else ""
        self.add(
            f'  <rect data-graph-role="container" id="{cid}" x="{x}" y="{y}" width="{w}"'
            f' height="{h}" rx="10" fill="none" stroke="{LINE}" stroke-width="1"'
            f"{dash_attr}/>"
        )
        self.add(
            f'  <text x="{lx}" y="{ly}" font-size="10" font-weight="700" fill="{MUTE}"'
            f' letter-spacing="0.18em">{esc(label)}</text>'
        )

    def edge(
        self,
        eid: str,
        source: str,
        target: str,
        d: str,
        colour: str,
        *,
        dash: str | None = None,
        twoway: bool = False,
    ) -> None:
        mid = MARKER_OF[colour]
        attrs = [
            'data-graph-role="edge"',
            f'data-edge-id="{eid}"',
            f'data-source="{source}"',
            f'data-target="{target}"',
            f'd="{d}"',
            'fill="none"',
            f'stroke="{colour}"',
            'stroke-width="1.8"',
            f'marker-end="url(#{mid})"',
        ]
        if twoway:
            attrs.append(f'marker-start="url(#{mid}-start)"')
        if dash:
            attrs.append(f'stroke-dasharray="{dash}"')
        self.add(f"  <path {' '.join(attrs)}/>")

    def node(
        self, nid: str, x: int, y: int, w: int, h: int, theme: str, *, dashed: bool = False
    ) -> None:
        fill, stroke = THEMES[theme]
        dash_attr = ' stroke-dasharray="6,4"' if dashed else ""
        self.add(
            f'  <rect data-graph-role="node" data-node-id="{nid}" x="{x}" y="{y}" width="{w}"'
            f' height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash_attr}/>'
        )

    def badge(self, cx: int, cy: int, text: str, stroke: str) -> None:
        self.add(
            f'  <circle cx="{cx}" cy="{cy}" r="11" fill="{stroke}" fill-opacity="0.18"'
            f' stroke="{stroke}" stroke-width="1.2"/>'
        )
        self.add(
            f'  <text x="{cx}" y="{cy + 4}" font-size="11" font-weight="700" fill="{stroke}"'
            f' text-anchor="middle">{esc(text)}</text>'
        )

    def text(
        self, x: int, y: int, size: int, weight: int, fill: str, txt: str, *, anchor: str = "start"
    ) -> None:
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(
            f'  <text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}"'
            f' fill="{fill}"{anchor_attr}>{esc(txt)}</text>'
        )

    def edge_label(
        self, eid: str, x: int, y: int, size: int, colour: str, txt: str, *, anchor: str = "start"
    ) -> None:
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(
            f'  <text data-graph-role="label" data-owner="{eid}" x="{x}" y="{y}"'
            f' font-size="{size}" fill="{colour}"{anchor_attr}>{esc(txt)}</text>'
        )

    def legend(
        self,
        items: list[tuple[str, str | None, bool, str]],
        *,
        y: int,
        footer: str,
        footer_y: int,
    ) -> None:
        """Emit the swatch row plus a footer note, both excluded from geometry."""
        self.add('  <g data-graph-role="legend">')
        lx = 36.0
        for colour, dash, twoway, text in items:
            mid = MARKER_OF[colour]
            extra = f' stroke-dasharray="{dash}"' if dash else ""
            start = f' marker-start="url(#{mid}-start)"' if twoway else ""
            self.add(
                f'    <path d="M {lx:.0f},{y - 4} L {lx + 22:.0f},{y - 4}" stroke="{colour}"'
                f' stroke-width="1.8" fill="none" marker-end="url(#{mid})"{start}{extra}/>'
            )
            self.add(
                f'    <text x="{lx + 30:.0f}" y="{y}" font-size="10" fill="{SUB}">{esc(text)}</text>'
            )
            lx += 22 + 30 + len(text) * 5.9
        self.add(
            f'    <text x="36" y="{footer_y}" font-size="10" fill="{MUTE}">{esc(footer)}</text>'
        )
        self.add("  </g>")
