"""
Chart & Diagram Rendering

Pure rendering functions: take structured data dicts as produced by the synthesis
prompts (tools/synthesis.py) and render a PNG to disk, returning its path. Every
function here catches its own exceptions and returns None on any failure (missing
Graphviz `dot` binary, malformed/incomplete data, matplotlib error, etc.) - callers
(build_docx.py) treat None as "skip this image", never as a fatal error.

No AWS calls, no Bedrock - pure local rendering, matching build_docx.py's
renderer-only design principle.
"""

import os
import logging

import matplotlib
matplotlib.use("Agg")  # headless - no display backend required
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_BAR_COLOR = "#2E5C8A"
_LINE_COLOR = "#999999"


def render_scorecard_bar_chart(chart_data: dict, output_path: str):
    """
    chart_data: {"type": "scorecard_bar", "title": "...", "scale_max": 5,
                 "dimensions": [{"name": "...", "score": number}, ...]}
    Horizontal bar chart, one bar per dimension. Returns output_path on success,
    None if chart_data is missing/malformed or rendering fails for any reason.
    """
    try:
        if not chart_data:
            return None
        dimensions = chart_data.get("dimensions")
        if not dimensions or not isinstance(dimensions, list):
            return None
        names, scores = [], []
        for d in dimensions:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            names.append(str(d["name"]))
            scores.append(float(d.get("score", 0)))
        if not names:
            return None
        scale_max = float(chart_data.get("scale_max") or 5)

        fig_height = max(2.0, 0.6 * len(names) + 1)
        fig, ax = plt.subplots(figsize=(8, fig_height))
        y_pos = range(len(names))
        ax.barh(y_pos, scores, color=_BAR_COLOR)
        ax.set_yticks(list(y_pos))
        ax.set_yticklabels(names)
        ax.invert_yaxis()
        ax.set_xlim(0, scale_max)
        ax.set_xlabel(f"Score (0-{scale_max:g})")
        ax.set_title(chart_data.get("title") or "Modernization Readiness Scorecard")
        for i, score in enumerate(scores):
            ax.text(min(score + scale_max * 0.02, scale_max * 0.98), i, f"{score:g}",
                    va="center", fontsize=9)
        fig.tight_layout()
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        return output_path
    except Exception as e:
        logger.warning(f"Scorecard chart rendering failed, skipping: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def render_roadmap_timeline_chart(chart_data: dict, output_path: str):
    """
    chart_data: {"type": "roadmap_timeline", "title": "...",
                 "phases": [{"name": "...", "order": int}, ...]}
    Ordinal step chart, left to right - NOT a dated Gantt chart, since no formal
    estimation was performed for the roadmap. Returns output_path or None.
    """
    try:
        if not chart_data:
            return None
        phases = chart_data.get("phases")
        if not phases or not isinstance(phases, list):
            return None
        ordered = sorted(
            (p for p in phases if isinstance(p, dict) and p.get("name")),
            key=lambda p: p.get("order", 0),
        )
        names = [str(p["name"]) for p in ordered]
        n = len(names)
        if n == 0:
            return None

        fig, ax = plt.subplots(figsize=(max(6, 2.2 * n), 2.5))
        xs = list(range(n))
        ax.plot(xs, [0] * n, color=_LINE_COLOR, zorder=1, linewidth=2)
        ax.scatter(xs, [0] * n, s=400, color=_BAR_COLOR, zorder=2)
        for i, name in enumerate(names):
            ax.annotate(
                f"Phase {i + 1}\n{name}", (i, 0),
                xytext=(0, 30 if i % 2 == 0 else -45), textcoords="offset points",
                ha="center", fontsize=9, arrowprops=dict(arrowstyle="-", color=_LINE_COLOR),
            )
        ax.set_xlim(-0.5, n - 0.5)
        ax.set_ylim(-1, 1)
        ax.axis("off")
        ax.set_title(chart_data.get("title") or "Migration Roadmap", pad=20)
        fig.tight_layout()
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        return output_path
    except Exception as e:
        logger.warning(f"Roadmap timeline chart rendering failed, skipping: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def render_architecture_diagram(diagram_data: dict, output_path: str):
    """
    diagram_data: {"title": "...", "nodes": [{"id": "...", "label": "..."}],
                   "edges": [[from_id, to_id], ...]}
    Boxes-and-arrows diagram via Graphviz. Used for both the current-architecture
    diagram (section 1) and recommended to-be architecture diagram (section 5) -
    same renderer, different node/edge data.

    Requires the `graphviz` Python package (requirements.txt) AND the Graphviz
    system binary (`dot`) on PATH - a host prerequisite documented in README.md,
    analogous to atx/git/uvx. Returns None (never raises) on a missing binary,
    malformed/empty data, or any other failure.
    """
    try:
        import graphviz  # imported lazily: a missing pip package should only
                          # break diagram rendering, not this whole module
    except ImportError:
        logger.warning("graphviz Python package not installed, skipping diagram")
        return None

    try:
        if not diagram_data:
            return None
        nodes = diagram_data.get("nodes") or []
        edges = diagram_data.get("edges") or []
        if not nodes:
            return None

        node_ids = {str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")}
        if not node_ids:
            return None

        g = graphviz.Digraph(format="png")
        g.attr(rankdir="LR", fontsize="11")
        g.attr("node", shape="box", style="rounded,filled", fillcolor="#EAF1FB", fontname="Helvetica")
        for n in nodes:
            if not isinstance(n, dict) or not n.get("id"):
                continue
            nid = str(n["id"])
            g.node(nid, str(n.get("label", nid)))
        for edge in edges:
            if not (isinstance(edge, (list, tuple)) and len(edge) == 2):
                continue
            src, dst = str(edge[0]), str(edge[1])
            if src in node_ids and dst in node_ids:
                g.edge(src, dst)

        base, _ = os.path.splitext(output_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        rendered_path = g.render(filename=base, cleanup=True)
        return rendered_path
    except Exception as e:
        # Most commonly graphviz's ExecutableNotFound (the `dot` binary isn't
        # installed) but also catches malformed data.
        logger.warning(f"Architecture diagram rendering failed (is Graphviz's `dot` "
                        f"binary installed? see README.md prerequisites), skipping: {e}")
        return None
