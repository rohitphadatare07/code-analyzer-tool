#!/usr/bin/env python3
"""
ascii_diagram_pipeline.py
=========================

Deterministic replacement for the LLM-driven parts of the
`ascii-to-mermaid-diagrams` Kiro skill.

Principle (handoff §6.2): *the LLM is a per-diagram subroutine inside a
deterministic loop, not the batch orchestrator.* This script owns extraction,
qualification, classification, grid-parsing, rendering, placement resolution
and DOCX embedding. Anything it cannot confidently parse is written to
``_needs_llm.json`` for a one-block-at-a-time LLM fallback; everything else is
fully mechanical and reproducible.

Scope: this skill targets every section EXCEPT 7.2. The Section 7.2 target
architecture is rendered by the awslabs.aws-diagram-mcp-server MCP path and is
out of scope here (handoff §6.3 — keep 7.2).

Subcommands
-----------
  scan      <md_root> --out <dir>
            Walk every .md under ATXDocumentation/, extract candidate art
            blocks, qualify/classify/grid-parse them, emit one <id>.mmd per
            confidently parsed block, list the rest in _needs_llm.json, and
            write _manifest.partial.json.

  finalize  --out <dir> [--placement placement.json]
            Render each <id>.mmd to PNG via mmdc (width chosen by node count,
            playwright/cairosvg fallbacks, content-hash render cache), resolve
            each block's target_section from placement.json, write
            ascii-mermaid-manifest.json, and run self_check.

  embed     --docx <path> --out <dir>
            For each manifest row, add_picture under its target_section
            heading, caption + explanation placeholder, then assert
            embedded_count == manifest_count.

The .mmd / manifest formats are intentionally simple JSON so the LLM fallback
can drop converted blocks straight back in (id + mermaid text) and re-run
finalize.

Bug fixes baked in (all found during the original testing pass, §8):
  (1) a letter such as the 'v' in "Server" is no longer stripped as an
      arrowhead — arrowhead glyphs are only recognised in connector cells,
      never inside a box label (label whitelist + connector-context check).
  (2) a horizontal connector run is no longer misread as a box — boxes must
      have a 4-sided closed border and an alphanumeric label.
  (3) a box drawn with ``| label |`` plus a ``---`` border no longer trips the
      markdown-table disqualifier — a block is only rejected as a table when it
      has a *real* separator row (pipes + dashes + optional colons only).
  (4) transitive skip-edges are suppressed via an intervening-box check, so a
      column A | v | B | v | C yields A->B->C, not A->C.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Character classes
# --------------------------------------------------------------------------- #
# Horizontal border / connector glyphs.
H_BORDER = set("-_=─━═")
# Vertical border / connector glyphs.
V_BORDER = set("|│┃║")
# Corner / junction glyphs (ASCII '+' plus box-drawing corners & tees).
CORNERS = set("+┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬")
# Arrowhead glyphs. Note: 'v'/'V'/'^' are *contextual* (see is_arrowhead_cell).
ARROW_GLYPHS = set(">→▶▸<←◀◂^↑▲↓▼")
CONTEXTUAL_ARROWS = set("vV^")
# Any glyph that may appear in a connector cell (never inside a label).
CONNECTOR = H_BORDER | V_BORDER | CORNERS | ARROW_GLYPHS | {"/", "\\", "."}

# Label whitelist: characters allowed to survive into a node label.
LABEL_ALLOWED = re.compile(r"[A-Za-z0-9 ._/&()+-]")

MERMAID_KEYWORDS: list[tuple[str, list[str]]] = [
    ("sequenceDiagram", ["sequence", "->>", "participant", "actor", "lifeline"]),
    ("stateDiagram-v2", ["state", "[*]", "transition"]),
    ("erDiagram", ["entity", "||--", "}o--", "relationship", "cardinality"]),
    ("flowchart TD", ["flow", "process", "decision", "start", "end", "->"]),
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    id: str
    source_md: str
    line_range: tuple[int, int]
    raw: str
    fenced: bool
    heading: str = ""          # nearest Markdown heading above the block
    heading_path: str = ""     # full "H1 > H2 > H3" trail (for disambiguation)


@dataclass
class Node:
    key: str          # n0, n1, ...
    label: str
    r0: int
    c0: int
    r1: int
    c1: int


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ManifestRow:
    id: str
    source: str = "ascii_converted"
    title: str = ""
    target_section: str | None = None
    placement_signal: str = "tiebreak"
    placement_reason: str = ""
    render_method: str = "mmdc"
    png_path: str | None = None
    mmd_path: str | None = None
    node_count: int = 0
    edge_count: int = 0
    confidence: float = 0.0
    mermaid_status: str = "pending"     # pending|needs_conversion|ok|dropped
    render_status: str = "pending"      # pending|ok|failed
    original_md_path: str = ""
    original_line_range: tuple[int, int] = (0, 0)
    raw_sha256: str = ""                 # provenance: sha of the exact source block


# --------------------------------------------------------------------------- #
# 1. Extraction
# --------------------------------------------------------------------------- #
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
HEADING_MD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def _heading_trail(stack: list[tuple[int, str]]) -> str:
    return " > ".join(text for _level, text in stack)


def extract_blocks(md_root: Path) -> list[Block]:
    """Pull fenced and indented candidate art blocks out of every .md file.

    Each block records the nearest Markdown heading above it (``heading``) and
    the full heading trail (``heading_path``). The heading — not any label drawn
    inside the art — is the source of the figure title (so a diagram under
    ``## Component Diagram`` is titled "Component Diagram", never "SERVER").
    """
    blocks: list[Block] = []
    counter = 0
    for md in sorted(md_root.rglob("*.md")):
        lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = str(md.relative_to(md_root))
        heading_stack: list[tuple[int, str]] = []  # (level, text), shallow->deep
        i = 0
        n = len(lines)
        while i < n:
            # track markdown headings (outside fences) to title diagrams by section
            hm = HEADING_MD_RE.match(lines[i])
            if hm:
                level = len(hm.group(1))
                text = hm.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, text))
                i += 1
                continue

            nearest = heading_stack[-1][1] if heading_stack else ""
            trail = _heading_trail(heading_stack)

            fence = FENCE_RE.match(lines[i])
            if fence:
                info = fence.group(3).strip().lower()
                close = fence.group(2)[0]
                j = i + 1
                buf: list[str] = []
                while j < n and not lines[j].strip().startswith(close * 3):
                    buf.append(lines[j])
                    j += 1
                # skip fences that are clearly mermaid or a known language;
                # bare fences and "text"/"" are candidate art.
                if info in ("", "text", "txt", "plain", "ascii", "diagram"):
                    counter += 1
                    blocks.append(Block(
                        id=f"ascii-{counter:03d}",
                        source_md=rel,
                        line_range=(i + 1, j + 1),
                        raw="\n".join(buf),
                        fenced=True,
                        heading=nearest,
                        heading_path=trail,
                    ))
                i = j + 1
                continue
            # indented candidate: >=3 consecutive lines indented >=4 spaces
            if lines[i].startswith("    ") and lines[i].strip():
                j = i
                buf = []
                while j < n and (lines[j].startswith("    ") or not lines[j].strip()):
                    buf.append(lines[j][4:] if lines[j].startswith("    ") else "")
                    j += 1
                while buf and not buf[-1].strip():
                    buf.pop()
                if len(buf) >= 3:
                    counter += 1
                    blocks.append(Block(
                        id=f"ascii-{counter:03d}",
                        source_md=rel,
                        line_range=(i + 1, j),
                        raw="\n".join(buf),
                        fenced=False,
                        heading=nearest,
                        heading_path=trail,
                    ))
                i = j
                continue
            i += 1
    return blocks


# --------------------------------------------------------------------------- #
# 2. Qualification — reject obvious non-diagrams
# --------------------------------------------------------------------------- #
SEPARATOR_ROW_RE = re.compile(r"^\s*\|?[\s|:-]*-{3,}[\s|:-]*\|?\s*$")
CODE_HINT_RE = re.compile(
    r"(;\s*$|^\s*(def|class|function|import|from|public|private|return|if|for|while)\b"
    r"|=>|::|\{\s*$|\}\s*$)")
LOG_RE = re.compile(r"\b(INFO|WARN|ERROR|DEBUG|TRACE)\b|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
TREE_RE = re.compile(r"^[\s│]*[├└]──\s")


def qualify(block: Block) -> tuple[bool, str]:
    """Return (is_candidate_diagram, reason_if_rejected)."""
    lines = block.raw.splitlines()
    nonblank = [ln for ln in lines if ln.strip()]
    if len(nonblank) < 3:
        return False, "too_short"

    # markdown table: requires a *real* separator row (fix #3).
    if any(SEPARATOR_ROW_RE.match(ln) and ln.count("|") >= 2 for ln in nonblank):
        return False, "markdown_table"

    # tree listing (├── └──)
    if sum(1 for ln in nonblank if TREE_RE.match(ln)) >= max(2, len(nonblank) // 2):
        return False, "tree_listing"

    # log output
    if sum(1 for ln in nonblank if LOG_RE.search(ln)) >= max(2, len(nonblank) // 2):
        return False, "log_output"

    # source code
    if sum(1 for ln in nonblank if CODE_HINT_RE.search(ln)) >= max(2, len(nonblank) // 2):
        return False, "source_code"

    # must contain some box-drawing / connector signal to be art at all
    glyphs = set(block.raw)
    if not (glyphs & (CONNECTOR | CONTEXTUAL_ARROWS)):
        return False, "no_connector_glyphs"
    return True, ""


# --------------------------------------------------------------------------- #
# 3. Classification
# --------------------------------------------------------------------------- #
def classify_type(raw: str) -> str:
    """Suggest a Mermaid diagram type. Used ONLY as a hint for the LLM-fallback
    residue — the deterministic parsed path is always a flowchart.

    Alphabetic keywords are matched on word boundaries so that, e.g., "actor"
    does not match inside "extractors.js" and "state" does not match inside
    "statement".
    """
    low = raw.lower()
    for mtype, kws in MERMAID_KEYWORDS:
        for kw in kws:
            if kw.isalpha():
                if re.search(r"\b" + re.escape(kw) + r"\b", low):
                    return mtype
            elif kw in low:  # symbol keywords like "->>", "[*]", "||--"
                return mtype
    return "flowchart TD"


# --------------------------------------------------------------------------- #
# 4. Grid parse: boxes + edges
# --------------------------------------------------------------------------- #
def _build_grid(raw: str) -> list[str]:
    lines = raw.splitlines()
    width = max((len(ln) for ln in lines), default=0)
    return [ln.ljust(width) for ln in lines]


def sanitize_label(text: str) -> str:
    """Keep only whitelisted label characters; collapse whitespace (fix #1)."""
    kept = "".join(ch for ch in text if LABEL_ALLOWED.match(ch))
    return re.sub(r"\s+", " ", kept).strip()


def _is_h_border(s: str) -> bool:
    s = s.strip(" " + "".join(CORNERS))
    return len(s) >= 2 and all(c in H_BORDER for c in s)


def _detect_boxes(grid: list[str]) -> list[Node]:
    """Detect 4-sided closed-border boxes (fix #2)."""
    rows = len(grid)
    boxes: list[Node] = []
    used: set[tuple[int, int]] = set()
    for r0 in range(rows):
        line = grid[r0]
        for c0 in range(len(line)):
            ch = line[c0]
            if ch not in CORNERS and ch not in H_BORDER:
                continue
            # find a horizontal top border starting at c0
            c1 = c0
            while c1 + 1 < len(line) and (line[c1 + 1] in H_BORDER or line[c1 + 1] in CORNERS):
                c1 += 1
            if c1 - c0 < 2:
                continue
            if not _is_h_border(line[c0:c1 + 1]):
                continue
            # find a matching bottom border below, with vertical sides between
            for r1 in range(r0 + 1, rows):
                bl = grid[r1]
                if c1 >= len(bl):
                    break
                if _is_h_border(bl[c0:c1 + 1]) and bl[c0] in (CORNERS | H_BORDER) \
                        and bl[c1] in (CORNERS | H_BORDER):
                    # check both vertical sides are present on interior rows
                    ok = True
                    for rr in range(r0 + 1, r1):
                        rowln = grid[rr]
                        left = rowln[c0] if c0 < len(rowln) else " "
                        right = rowln[c1] if c1 < len(rowln) else " "
                        if left not in V_BORDER and left not in CORNERS:
                            ok = False
                            break
                        if right not in V_BORDER and right not in CORNERS:
                            ok = False
                            break
                    if not ok or r1 - r0 < 2:
                        continue
                    if any((rr, c0) in used for rr in range(r0, r1 + 1)):
                        continue
                    label = _box_label(grid, r0, c0, r1, c1)
                    if not label or not re.search(r"[A-Za-z0-9]", label):
                        continue  # reject non-alphanumeric labels (fix #2)
                    boxes.append(Node(key="", label=label, r0=r0, c0=c0, r1=r1, c1=c1))
                    for rr in range(r0, r1 + 1):
                        for cc in range(c0, c1 + 1):
                            used.add((rr, cc))
                    break
    # assign stable keys top-to-bottom, left-to-right
    boxes.sort(key=lambda b: (b.r0, b.c0))
    for i, b in enumerate(boxes):
        b.key = f"n{i}"
    return boxes


def _box_label(grid: list[str], r0: int, c0: int, r1: int, c1: int) -> str:
    parts: list[str] = []
    for rr in range(r0 + 1, r1):
        seg = grid[rr][c0 + 1:c1] if c1 <= len(grid[rr]) else grid[rr][c0 + 1:]
        parts.append(sanitize_label(seg))
    return sanitize_label(" ".join(p for p in parts if p))


def _contains(node: Node, r: int, c: int) -> bool:
    return node.r0 <= r <= node.r1 and node.c0 <= c <= node.c1


def is_arrowhead_cell(grid: list[str], r: int, c: int, nodes: list[Node]) -> str | None:
    """Return a direction if (r,c) is a real arrowhead, else None (fix #1).

    Contextual glyphs (v/V/^) only count as arrowheads when they sit in a
    connector cell (adjacent vertical bar above/below), never inside a label.
    """
    if any(_contains(n, r, c) for n in nodes):
        return None
    ch = grid[r][c]
    if ch in ARROW_GLYPHS:
        if ch in "<←◀◂":
            return "left"
        if ch in ">→▶▸":
            return "right"
        if ch in "^↑▲":
            return "up"
        return "down"
    if ch in CONTEXTUAL_ARROWS:
        above = grid[r - 1][c] if r > 0 and c < len(grid[r - 1]) else " "
        below = grid[r + 1][c] if r + 1 < len(grid) and c < len(grid[r + 1]) else " "
        if ch in "vV" and (above in V_BORDER or above in CORNERS):
            return "down"
        if ch == "^" and (below in V_BORDER or below in CORNERS):
            return "up"
    return None


def _node_at(nodes: list[Node], r: int, c: int) -> Node | None:
    for n in nodes:
        if _contains(n, r, c):
            return n
    return None


def _trace_edges(grid: list[str], nodes: list[Node]) -> list[tuple[str, str]]:
    """Band-scan for vertical and horizontal connectors between boxes.

    A run may contain spaces, but must contain at least one real connector or
    arrowhead glyph to count as an edge. Scanning stops at the *first* box
    reached, which suppresses transitive skip-edges (fix #4).
    """
    edges: set[tuple[str, str]] = set()

    def is_connector(r: int, c: int) -> bool:
        if r < 0 or r >= len(grid) or c < 0 or c >= len(grid[r]):
            return False
        ch = grid[r][c]
        return (ch in CONNECTOR or is_arrowhead_cell(grid, r, c, nodes) is not None)

    def scan_down(col: int, start_row: int) -> Node | None:
        seen_connector = False
        for rr in range(start_row, len(grid)):
            n = _node_at(nodes, rr, col)
            if n is not None:
                return n if seen_connector else None
            ch = grid[rr][col] if col < len(grid[rr]) else " "
            if ch == " ":
                continue
            if is_connector(rr, col):
                seen_connector = True
                continue
            return None  # hit a non-connector, non-space, non-box glyph
        return None

    def scan_right(row: int, start_col: int) -> Node | None:
        seen_connector = False
        line = grid[row]
        for cc in range(start_col, len(line)):
            n = _node_at(nodes, row, cc)
            if n is not None:
                return n if seen_connector else None
            ch = line[cc]
            if ch == " ":
                continue
            if is_connector(row, cc):
                seen_connector = True
                continue
            return None
        return None

    for src in nodes:
        # vertical connectors leaving the bottom edge
        for c in range(src.c0, src.c1 + 1):
            tgt = scan_down(c, src.r1 + 1)
            if tgt and tgt.key != src.key:
                edges.add((src.key, tgt.key))
        # horizontal connectors leaving the right edge
        for rr in range(src.r0, src.r1 + 1):
            tgt = scan_right(rr, src.c1 + 1)
            if tgt and tgt.key != src.key:
                edges.add((src.key, tgt.key))
    return sorted(edges)


def parse_ascii_to_graph(raw: str) -> tuple[Graph | None, float]:
    """Conservative parse: need >=2 boxed nodes + >=1 edge, else defer to LLM."""
    grid = _build_grid(raw)
    nodes = _detect_boxes(grid)
    if len(nodes) < 2:
        return None, 0.0
    edges = _trace_edges(grid, nodes)
    if not edges:
        return None, 0.2
    g = Graph(nodes=nodes, edges=edges)
    # confidence: fraction of nodes that participate in an edge.
    touched = {k for e in edges for k in e}
    conf = round(len(touched) / len(nodes), 2)
    return g, conf


# --------------------------------------------------------------------------- #
# 5. Mermaid emission
# --------------------------------------------------------------------------- #
def graph_to_mermaid(g: Graph, mtype: str = "flowchart TD") -> str:
    lines = [mtype]
    for n in g.nodes:
        lbl = n.label.replace('"', "'")
        lines.append(f'    {n.key}["{lbl}"]')
    for a, b in g.edges:
        lines.append(f"    {a} --> {b}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 6. Rendering (mmdc + fallbacks + cache)
# --------------------------------------------------------------------------- #
def _width_for(node_count: int) -> int:
    if node_count > 40:
        return 3200
    if node_count > 20:
        return 2400
    return 1600


MERMAID_CONFIG = {
    "theme": "base",
    "themeVariables": {
        "fontFamily": "Arial, Helvetica, sans-serif",
        "fontSize": "16px",
        "primaryColor": "#eef3fb",
        "primaryBorderColor": "#3b6fb5",
        "primaryTextColor": "#1b2a41",
        "lineColor": "#5b6b7f",
        "tertiaryColor": "#f7f9fc",
        "clusterBkg": "#f4f7fb",
        "clusterBorder": "#c3d0e0",
    },
    "flowchart": {"curve": "basis", "htmlLabels": True,
                  "nodeSpacing": 55, "rankSpacing": 65, "padding": 12},
}
# Subtle rounded corners + readable cluster (subgraph) titles, applied to all
# diagrams so the report looks cohesive for the client.
MERMAID_THEME_CSS = (
    ".node rect,.node polygon{rx:6px;ry:6px}"
    ".cluster rect{rx:8px;ry:8px}"
    ".cluster .cluster-label,.cluster text{font-weight:600;fill:#1b2a41}"
    ".edgeLabel{background:#ffffff}"
)


def _write_render_assets(out: Path) -> tuple[Path, Path, Path]:
    cfg = out / "mermaid-config.json"
    css = out / "mermaid-theme.css"
    pup = out / "puppeteer.json"
    cfg.write_text(json.dumps(MERMAID_CONFIG, indent=2), encoding="utf-8")
    css.write_text(MERMAID_THEME_CSS, encoding="utf-8")
    # containers/sandboxes typically need --no-sandbox for headless Chromium
    pup.write_text(json.dumps({"args": ["--no-sandbox", "--disable-setuid-sandbox"]}),
                   encoding="utf-8")
    return cfg, css, pup


def _ensure_mmdc() -> str | None:
    exe = shutil.which("mmdc")
    if exe:
        return exe
    # try a local npm install (best effort; offline envs will skip)
    try:
        subprocess.run(["npm", "install", "-g", "@mermaid-js/mermaid-cli"],
                       check=True, capture_output=True, timeout=180)
    except Exception:
        return None
    return shutil.which("mmdc")


def render_png(mmd_path: Path, png_path: Path, node_count: int, cache_dir: Path,
               config_path: Path | None = None, css_path: Path | None = None,
               puppeteer_path: Path | None = None) -> bool:
    """Render .mmd -> .png with the shared theme, hi-dpi scale, cache + fallback.

    White background and 2x scale produce a crisp, client-ready figure; the
    shared config/CSS keep every diagram visually consistent. A puppeteer config
    (--no-sandbox) is passed for containerized hosts; PUPPETEER_EXECUTABLE_PATH is
    honored if set.
    """
    content = mmd_path.read_bytes()
    width = _width_for(node_count)
    key_src = content + str(width).encode()
    if config_path and config_path.exists():
        key_src += config_path.read_bytes()
    if css_path and css_path.exists():
        key_src += css_path.read_bytes()
    key = hashlib.sha256(key_src).hexdigest()
    cached = cache_dir / f"{key}.png"
    if cached.exists():
        shutil.copyfile(cached, png_path)
        return True

    mmdc = _ensure_mmdc()
    if mmdc:
        cmd = [mmdc, "-i", str(mmd_path), "-o", str(png_path),
               "-b", "white", "-w", str(width), "-s", "2"]
        if config_path and config_path.exists():
            cmd += ["-c", str(config_path)]
        if css_path and css_path.exists():
            cmd += ["-C", str(css_path)]
        if puppeteer_path and puppeteer_path.exists():
            cmd += ["-p", str(puppeteer_path)]
        env = dict(os.environ)
        for _attempt in range(3):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=120, env=env)
                if png_path.exists() and png_path.stat().st_size > 0:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(png_path, cached)
                    return True
            except Exception:
                continue
    if _render_with_playwright(mmd_path, png_path, width):
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png_path, cached)
        return True
    return False


def _render_with_playwright(mmd_path: Path, png_path: Path, width: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return False
    mmd = mmd_path.read_text(encoding="utf-8")
    html = (
        "<html><body><div class='mermaid'>" + mmd + "</div>"
        "<script src='https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js'></script>"
        "<script>mermaid.initialize({startOnLoad:true});</script></body></html>"
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.set_content(html)
            page.wait_for_selector("svg", timeout=10000)
            page.locator(".mermaid").screenshot(path=str(png_path))
            browser.close()
        return png_path.exists() and png_path.stat().st_size > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 7. Placement resolution
# --------------------------------------------------------------------------- #
def resolve_target_section(row: ManifestRow, placement: dict[str, Any]) -> None:
    """Resolve target_section from a placement.json hint map.

    placement.json shape (any subset):
      {"by_id": {"ascii-001": "3.1"},
       "by_heading": {"data flow": "4.4"},
       "default": "Appendix F"}
    Section 7.2 is never a legal target here (handoff §6.3).
    """
    by_id = placement.get("by_id", {})
    if row.id in by_id:
        row.target_section, row.placement_signal, row.placement_reason = (
            _guard72(by_id[row.id]), "hint", "explicit by_id hint")
        return
    low = row.title.lower()
    for needle, sec in placement.get("by_heading", {}).items():
        if needle.lower() in low:
            row.target_section, row.placement_signal, row.placement_reason = (
                _guard72(sec), "heading", f"title matched {needle!r}")
            return
    row.target_section = _guard72(placement.get("default", "Appendix F"))
    row.placement_signal = "appendix" if row.target_section == "Appendix F" else "tiebreak"
    row.placement_reason = "no signal matched; fell through to default"


def _guard72(sec: str) -> str:
    return "Appendix F" if str(sec).strip() == "7.2" else str(sec).strip()


# --------------------------------------------------------------------------- #
# 8. Self check
# --------------------------------------------------------------------------- #
def self_check(rows: list[ManifestRow]) -> list[str]:
    problems: list[str] = []
    for r in rows:
        if r.mermaid_status == "ok" and r.render_status != "ok":
            problems.append(f"{r.id}: mermaid ok but render {r.render_status}")
        if r.render_status == "ok":
            if not r.png_path or not Path(r.png_path).exists():
                problems.append(f"{r.id}: render ok but png missing")
            elif Path(r.png_path).stat().st_size < 5 * 1024:
                problems.append(f"{r.id}: png < 5KB")
            if not r.raw_sha256:
                problems.append(f"{r.id}: embedded without source provenance (raw_sha256)")
        if r.target_section == "7.2":
            problems.append(f"{r.id}: 7.2 is reserved for the MCP path")
    return problems


# --------------------------------------------------------------------------- #
# Subcommand: scan
# --------------------------------------------------------------------------- #
def cmd_scan(args: argparse.Namespace) -> int:
    """Enumerate the source ASCII diagrams and LOCK the set.

    Every qualified block becomes one manifest row with a provenance hash of its
    exact source bytes. The LLM converts each block to Mermaid for visual quality
    (it reads the whole diagram — all nodes, subgraphs, edges — which the old grid
    parser could not). The locked manifest is the allowlist: finalize refuses any
    rendered diagram whose id is not in it, so no fabricated/extra diagram can
    reach the DOCX. N source blocks -> at most N figures, never more.

    With --auto-parse, a deterministic grid-parsed draft is written for the LLM to
    refine (off by default; the from-scratch LLM conversion looks better).
    """
    md_root = Path(args.md_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "mmd").mkdir(exist_ok=True)
    (out / "blocks").mkdir(exist_ok=True)

    blocks = extract_blocks(md_root)
    rows: list[ManifestRow] = []
    queue: list[dict[str, Any]] = []

    for b in blocks:
        ok, reason = qualify(b)
        if not ok:
            continue
        raw_sha = hashlib.sha256(b.raw.encode("utf-8")).hexdigest()
        (out / "blocks" / f"{b.id}.txt").write_text(b.raw, encoding="utf-8")
        row = ManifestRow(
            id=b.id,
            title=_title_from(b),
            original_md_path=b.source_md,
            original_line_range=b.line_range,
            raw_sha256=raw_sha,
            mermaid_status="needs_conversion",
        )
        if args.auto_parse:
            g, conf = parse_ascii_to_graph(b.raw)
            if g is not None:
                (out / "mmd" / f"{b.id}.mmd").write_text(
                    graph_to_mermaid(g, "flowchart TD"), encoding="utf-8")
                row.confidence = conf  # a draft the LLM should review/improve
        queue.append({
            "id": b.id,
            "title": row.title,
            "heading_path": b.heading_path,
            "original_md_path": b.source_md,
            "line_range": list(b.line_range),
            "suggested_type": classify_type(b.raw),
            "raw_sha256": raw_sha,
            "raw": b.raw,
        })
        rows.append(row)

    (out / "_convert_queue.json").write_text(json.dumps(queue, indent=2), encoding="utf-8")
    (out / "_scan_meta.json").write_text(
        json.dumps({"md_root": str(md_root), "locked_ids": [r.id for r in rows]}, indent=2),
        encoding="utf-8")
    _write_manifest(out / "_manifest.partial.json", rows)
    print(json.dumps({
        "blocks_found": len(blocks),
        "qualified_locked": len(rows),
        "to_convert": str(out / "_convert_queue.json"),
        "note": "LLM: convert every queue entry to mmd/<id>.mmd, faithfully + "
                "attractively, one at a time. Do NOT add diagrams not in the queue.",
        "manifest": str(out / "_manifest.partial.json"),
    }, indent=2))
    return 0


_GENERIC_HEADINGS = {
    "diagram", "diagrams", "structural diagrams", "structural", "architecture",
    "overview", "components", "component", "figure",
}


def _title_from(b: Block) -> str:
    """Title a diagram by its surrounding Markdown heading — never by a label
    drawn inside the art.

    Order of preference:
      1. nearest Markdown heading (e.g. "## Component Diagram" -> "Component Diagram")
      2. if that heading is too generic ("Diagrams"), the deepest non-generic
         step of the heading trail, else the humanized filename stem
      3. as a last resort only, the first text label inside the art
    """
    nearest = sanitize_label(b.heading or "")
    if nearest and nearest.lower() not in _GENERIC_HEADINGS:
        return nearest[:80]

    # heading missing or generic: try the deepest meaningful trail step
    if b.heading_path:
        for step in reversed(b.heading_path.split(" > ")):
            s = sanitize_label(step)
            if s and s.lower() not in _GENERIC_HEADINGS:
                return s[:80]

    stem = Path(b.source_md).stem.replace("-", " ").replace("_", " ").strip()
    if stem and stem.lower() not in _GENERIC_HEADINGS:
        return stem.title()[:80]

    # keep a generic heading rather than a misleading box label, if we have one
    if nearest:
        return nearest[:80]

    # last resort: first text label inside the art
    for ln in b.raw.splitlines():
        s = sanitize_label(ln)
        if len(s) >= 4 and re.search(r"[A-Za-z]", s):
            return s[:80]
    return (stem.title() or "Diagram")[:80]


# --------------------------------------------------------------------------- #
# Subcommand: finalize
# --------------------------------------------------------------------------- #
def cmd_finalize(args: argparse.Namespace) -> int:
    out = Path(args.out)
    rows = _read_manifest(out / "_manifest.partial.json")
    locked_ids = {r.id for r in rows}

    # LOCK: any mmd/<id>.mmd whose id is not in the locked manifest is a rogue,
    # fabricated diagram with no source block — refuse it outright.
    rogue = sorted(p.stem for p in (out / "mmd").glob("*.mmd")
                   if p.stem not in locked_ids)
    if rogue:
        print(json.dumps({
            "error": "FABRICATED_DIAGRAMS",
            "detail": "these .mmd files have no source block in the manifest "
                      "and will not be rendered or embedded",
            "rogue_ids": rogue,
        }, indent=2))
        return 2

    # absorb each LLM-written conversion into its locked row
    by_id = {r.id: r for r in rows}
    for mmd in (out / "mmd").glob("*.mmd"):
        row = by_id.get(mmd.stem)
        if row is None:
            continue
        text = mmd.read_text(encoding="utf-8")
        row.mmd_path = str(mmd)
        row.mermaid_status = "ok"
        row.node_count = text.count("[") + text.count("((") + text.count("{")
        row.edge_count = text.count("-->") + text.count("---") + text.count("-.->")

    placement = {}
    if args.placement and Path(args.placement).exists():
        placement = json.loads(Path(args.placement).read_text(encoding="utf-8"))

    png_dir = out / "png"; png_dir.mkdir(exist_ok=True)
    cache_dir = out / ".render-cache"
    cfg, css, pup = _write_render_assets(out)

    unconverted: list[str] = []
    for r in rows:
        if r.mermaid_status != "ok" or not r.mmd_path:
            r.mermaid_status = "dropped" if r.mermaid_status != "ok" else r.mermaid_status
            unconverted.append(r.id)
            continue
        png_path = png_dir / f"{r.id}.png"
        ok = render_png(Path(r.mmd_path), png_path, r.node_count, cache_dir, cfg, css, pup)
        r.render_status = "ok" if ok else "failed"
        r.png_path = str(png_path) if ok else None
        resolve_target_section(r, placement)

    problems = self_check(rows)
    _write_manifest(out / "ascii-mermaid-manifest.json", rows)
    print(json.dumps({
        "locked_blocks": len(rows),
        "rendered": sum(1 for r in rows if r.render_status == "ok"),
        "failed": sum(1 for r in rows if r.render_status == "failed"),
        "unconverted": unconverted,  # queue entries the LLM has not yet converted
        "self_check_problems": problems,
        "manifest": str(out / "ascii-mermaid-manifest.json"),
    }, indent=2))
    return 0 if not problems and not unconverted else 1


# --------------------------------------------------------------------------- #
# Subcommand: embed
# --------------------------------------------------------------------------- #
def cmd_embed(args: argparse.Namespace) -> int:
    from docx import Document
    from docx.shared import Inches
    from docx.oxml.ns import qn

    out = Path(args.out)
    rows = [r for r in _read_manifest(out / "ascii-mermaid-manifest.json")
            if r.render_status == "ok" and r.png_path]
    doc = Document(args.docx)

    # map section number -> the paragraph element of its heading
    heading_para: dict[str, Any] = {}
    for p in doc.paragraphs:
        if (p.style.name or "").lower().startswith("heading"):
            m = re.match(r"^\s*(appendix\s+[a-f]|\d+(?:\.\d+){0,2})\b",
                         p.text, re.IGNORECASE)
            if m:
                tok = m.group(1)
                tok = ("Appendix " + tok.split()[-1].upper()
                       if tok.lower().startswith("appendix") else tok)
                heading_para.setdefault(tok, p)

    embedded = 0
    for r in rows:
        sec = r.target_section or "Appendix F"
        anchor = heading_para.get(sec) or heading_para.get("Appendix F")
        target = anchor if anchor is not None else None
        # build image + caption + explanation paragraphs
        img_par = _insert_paragraph_after(target, doc)
        run = img_par.add_run()
        run.add_picture(r.png_path, width=_embed_width(r))
        cap_par = _insert_paragraph_after(img_par, doc)
        figno = _figure_number(sec, embedded)
        cap_par.add_run(f"Figure {figno} — {r.title}").italic = True
        exp_par = _insert_paragraph_after(cap_par, doc)
        exp_par.add_run(r.placement_reason or
                        f"Converted from ASCII art in {r.original_md_path}.")
        embedded += 1

    doc.save(args.docx)
    manifest_count = len(rows)
    assert embedded == manifest_count, (
        f"embedded {embedded} != manifest {manifest_count}")
    print(json.dumps({"embedded": embedded, "manifest_count": manifest_count,
                      "docx": args.docx}, indent=2))
    return 0


def _embed_width(r: ManifestRow):
    from docx.shared import Inches
    dense = r.node_count > 25 or r.edge_count > 30
    return Inches(9.5) if dense else Inches(6.5)


def _figure_number(sec: str, n: int) -> str:
    base = sec if sec[0].isdigit() else "F"
    return f"{base}.{n + 1}"


def _insert_paragraph_after(anchor, doc):
    """Insert a new empty paragraph immediately after `anchor` (or at end)."""
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    new_p = doc.add_paragraph()  # appended at end first
    if anchor is not None:
        anchor._p.addnext(new_p._p)
    return new_p


# --------------------------------------------------------------------------- #
# Manifest IO
# --------------------------------------------------------------------------- #
def _write_manifest(path: Path, rows: list[ManifestRow]) -> None:
    data = []
    for r in rows:
        d = asdict(r)
        d["line_range"] = list(r.original_line_range)
        data.append(d)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_manifest(path: Path) -> list[ManifestRow]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows: list[ManifestRow] = []
    for d in raw:
        d = dict(d)
        d.pop("line_range", None)
        if isinstance(d.get("original_line_range"), list):
            d["original_line_range"] = tuple(d["original_line_range"])
        rows.append(ManifestRow(**{k: v for k, v in d.items()
                                   if k in ManifestRow.__dataclass_fields__}))
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="enumerate + lock source ASCII art; emit LLM convert queue")
    s.add_argument("md_root", help="root of ATXDocumentation/ (.md files)")
    s.add_argument("--out", required=True, help="pipeline working dir")
    s.add_argument("--auto-parse", action="store_true",
                   help="also write a deterministic grid-parsed draft .mmd for the "
                        "LLM to refine (default off; from-scratch LLM output looks better)")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("finalize", help="render .mmd -> png, resolve placement")
    f.add_argument("--out", required=True)
    f.add_argument("--placement", help="placement.json (id/heading -> section)")
    f.set_defaults(func=cmd_finalize)

    e = sub.add_parser("embed", help="embed rendered PNGs into the DOCX")
    e.add_argument("--docx", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(func=cmd_embed)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())