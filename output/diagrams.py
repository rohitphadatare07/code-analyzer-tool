"""
Mermaid diagram renderer — fully offline capable.

Rendering chain (tried in order, first success wins):
  1. Playwright + local mermaid.js  — headless Chromium, zero network
  2. mmdc CLI                       — mermaid-cli if Chrome is configured
  3. mermaid.ink API                — only if internet is available
  4. SVG fallback                   — pure Python, always works

The SVG fallback parses the Mermaid syntax and draws a clean diagram
using only Python's standard library. No network, no Node.js, no browser.
It handles: graph TD, graph LR, sequenceDiagram.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ── Find local mermaid.js ──────────────────────────────────────────────────────

def _find_local_mermaid_js() -> Optional[str]:
    # Explicit known paths first
    known = [
        "/home/claude/.npm-global/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.min.js",
        str(Path.home() / ".npm-global/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.min.js"),
        "/usr/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.min.js",
        "/usr/local/lib/node_modules/@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.min.js",
    ]
    for p in known:
        if Path(p).is_file():
            return p
    # Search npm global dirs
    try:
        r = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=5)
        npm_root = r.stdout.strip()
        if npm_root:
            candidate = Path(npm_root) / "@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.min.js"
            if candidate.is_file():
                return str(candidate)
    except Exception:
        pass
    # find command fallback
    try:
        r = subprocess.run(
            ["find", str(Path.home()), "-name", "mermaid.min.js", "-path", "*/mermaid/dist/*"],
            capture_output=True, text=True, timeout=8,
        )
        for line in r.stdout.strip().splitlines():
            if line and Path(line).is_file():
                return line
    except Exception:
        pass
    return None


_MERMAID_JS_CACHE: Optional[str] = None


def _get_mermaid_js() -> Optional[str]:
    global _MERMAID_JS_CACHE
    if _MERMAID_JS_CACHE:
        return _MERMAID_JS_CACHE
    path = _find_local_mermaid_js()
    if path:
        try:
            _MERMAID_JS_CACHE = Path(path).read_text(encoding="utf-8")
            return _MERMAID_JS_CACHE
        except Exception:
            pass
    return None


# ── Renderer 1: Playwright + local mermaid.js ──────────────────────────────────

def _render_playwright(mermaid_code: str) -> Optional[bytes]:
    mermaid_src = _get_mermaid_js()
    if not mermaid_src:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    escaped = (mermaid_code
               .replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{margin:0;padding:24px;background:white;font-family:sans-serif;}}</style>
<script>{mermaid_src}</script></head>
<body>
<div class="mermaid" id="d">{escaped}</div>
<script>mermaid.initialize({{startOnLoad:true,theme:'default',
  flowchart:{{htmlLabels:true,curve:'basis'}},
  sequence:{{diagramMarginX:20,diagramMarginY:10}}}});</script>
</body></html>"""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.set_viewport_size({"width": 1000, "height": 900})
            page.set_content(html, wait_until="networkidle")
            page.wait_for_timeout(1200)
            svg_el = page.query_selector("#d svg")
            if not svg_el:
                browser.close()
                return None
            box = svg_el.bounding_box()
            if not box or box["width"] < 10 or box["height"] < 10:
                browser.close()
                return None
            pad = 20
            clip = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                    "width": box["width"] + pad * 2, "height": box["height"] + pad * 2}
            png = page.screenshot(clip=clip)
            browser.close()
            return png if len(png) > 500 else None
    except Exception:
        return None


# ── Renderer 2: mmdc CLI ───────────────────────────────────────────────────────

def _render_mmdc(mermaid_code: str) -> Optional[bytes]:
    mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd")
    if not mmdc:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "d.mmd"
            out = Path(tmp) / "d.png"
            inp.write_text(mermaid_code, encoding="utf-8")
            r = subprocess.run(
                [mmdc, "-i", str(inp), "-o", str(out), "-b", "white", "--width", "1000"],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and out.exists():
                return out.read_bytes()
    except Exception:
        pass
    return None


# ── Renderer 3: mermaid.ink ────────────────────────────────────────────────────

def _render_mermaid_ink(mermaid_code: str) -> Optional[bytes]:
    try:
        encoded = base64.urlsafe_b64encode(mermaid_code.encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            f"https://mermaid.ink/img/{encoded}",
            headers={"User-Agent": "repograph/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        if data[:4] in (b"\x89PNG", b"GIF8") or data[:2] == b"\xff\xd8":
            return data
    except Exception:
        pass
    return None


# ── Renderer 4: Pure-Python SVG fallback ──────────────────────────────────────

def _esc(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")


def _parse_graph(code: str):
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    skip = {"graph","td","lr","tb","rl","subgraph","end","classdef","class","style","autonumber"}
    node_re = re.compile(r'(\w+)\s*(?:\(\("([^"]+)"\)\)|\["([^"]+)"\]|\("([^"]+)"\)|"([^"]+)")?')
    edge_re = re.compile(r'(\w+)\s*(?:-->|==>|--[^>]*-->)\|?([^|]*?)\|?\s*(\w+)')
    simple_re = re.compile(r'(\w+)\s*--?>+\s*(\w+)')
    for line in code.split("\n"):
        line = line.strip()
        if not line or line.startswith("%%") or line.lower().split()[0] in skip:
            continue
        em = edge_re.search(line)
        if em:
            s, lbl, t = em.group(1), em.group(2).strip(), em.group(3)
            edges.append((s, t, lbl))
            for nm in node_re.finditer(line):
                nid = nm.group(1)
                if nid.lower() in skip: continue
                lbl_raw = nm.group(2) or nm.group(3) or nm.group(4) or nm.group(5)
                if nid not in nodes: nodes[nid] = lbl_raw or nid
            continue
        sm = simple_re.search(line)
        if sm:
            s, t = sm.group(1), sm.group(2)
            edges.append((s, t, ""))
            for nid in (s, t):
                if nid not in nodes: nodes[nid] = nid
            continue
        for nm in node_re.finditer(line):
            nid = nm.group(1)
            if nid.lower() in skip: continue
            lbl_raw = nm.group(2) or nm.group(3) or nm.group(4) or nm.group(5)
            if nid not in nodes: nodes[nid] = lbl_raw or nid
    return nodes, edges


def _svg_graph(code: str, direction: str = "TD") -> str:
    nodes, edges = _parse_graph(code)
    if not nodes:
        return _svg_empty("No nodes parsed")
    node_list = list(nodes.keys())
    n = len(node_list)
    BW, BH, PX, PY = 155, 42, 55, 65
    COLS = max(1, min(4, n)) if direction == "LR" else max(1, min(3, (n + 2) // 3))
    pos: dict[str, tuple[float, float]] = {}
    for i, nid in enumerate(node_list):
        col, row = (i % COLS, i // COLS) if direction == "TD" else (i // max(1,(n+COLS-1)//COLS), i % max(1,(n+COLS-1)//COLS))
        pos[nid] = (40 + col * (BW + PX), 40 + row * (BH + PY))
    W = int(max(x + BW for x, _ in pos.values()) + 50)
    H = int(max(y + BH for _, y in pos.values()) + 50)
    FILLS = ["#eff6ff","#f0fdf4","#fff7ed","#fdf4ff","#fff1f2","#f0fdfa","#fefce8"]
    STRKS = ["#3b82f6","#22c55e","#f97316","#a855f7","#f43f5e","#14b8a6","#ca8a04"]
    TXTS  = ["#1d4ed8","#15803d","#c2410c","#7e22ce","#be123c","#0f766e","#92400e"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">',
        '<path d="M1 1L9 5L1 9" fill="none" stroke="#94a3b8" stroke-width="1.5" stroke-linecap="round"/></marker></defs>',
        f'<rect width="{W}" height="{H}" fill="white"/>',
    ]
    for s, t, lbl in edges:
        if s not in pos or t not in pos: continue
        sx, sy = pos[s]; tx, ty = pos[t]
        if direction == "TD":
            x1,y1,x2,y2 = sx+BW/2, sy+BH, tx+BW/2, ty
        else:
            x1,y1,x2,y2 = sx+BW, sy+BH/2, tx, ty+BH/2
        mx, my = (x1+x2)/2, (y1+y2)/2
        parts.append(f'<path d="M{x1:.0f} {y1:.0f} Q{mx:.0f} {my:.0f} {x2:.0f} {y2:.0f}" fill="none" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
        if lbl:
            parts.append(f'<text x="{mx:.0f}" y="{my-4:.0f}" font-family="sans-serif" font-size="10" fill="#64748b" text-anchor="middle">{_esc(lbl[:18])}</text>')
    for i, nid in enumerate(node_list):
        x, y = pos[nid]; ci = i % len(FILLS)
        lbl = _esc(nodes[nid][:22])
        parts += [
            f'<rect x="{x}" y="{y}" width="{BW}" height="{BH}" rx="8" fill="{FILLS[ci]}" stroke="{STRKS[ci]}" stroke-width="1.5"/>',
            f'<text x="{x+BW/2:.0f}" y="{y+BH/2+5:.0f}" font-family="sans-serif" font-size="12" font-weight="500" fill="{TXTS[ci]}" text-anchor="middle">{lbl}</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def _svg_sequence(code: str) -> str:
    parts_list: list[tuple[str,str]] = []
    messages: list[tuple[str,str,str,str]] = []
    seen: set[str] = set()
    pp = re.compile(r'participant\s+(\w+)(?:\s+as\s+(.+))?', re.IGNORECASE)
    mp = re.compile(r'(\w+)\s*(-->>|->|--?>|->>)\s*(\w+)\s*:\s*(.+)')
    for line in code.split("\n"):
        line = line.strip()
        m = pp.match(line)
        if m:
            pid, alias = m.group(1), (m.group(2) or m.group(1)).strip()
            if pid not in seen: parts_list.append((pid, alias)); seen.add(pid)
            continue
        m = mp.match(line)
        if m:
            s, arr, t, lbl = m.group(1), m.group(2), m.group(3), m.group(4).strip()
            for p in (s, t):
                if p not in seen: parts_list.append((p, p)); seen.add(p)
            messages.append((s, t, arr, lbl))
    if not parts_list: return _svg_empty("No sequence participants")
    PW, PH, MH, PL, PT = 130, 38, 44, 30, 20
    W = max(400, PL*2 + len(parts_list)*(PW+20))
    H = max(200, PT + PH + len(messages)*MH + 60)
    px: dict[str,float] = {pid: PL + i*(PW+20) + PW/2 for i,(pid,_) in enumerate(parts_list)}
    FILLS = ["#dbeafe","#dcfce7","#fef3c7","#f3e8ff","#fce7f3","#ccfbf1"]
    BORDS = ["#3b82f6","#22c55e","#f59e0b","#a855f7","#ec4899","#14b8a6"]
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">',
        '<path d="M1 1L9 5L1 9" fill="none" stroke="#555" stroke-width="1.5" stroke-linecap="round"/></marker></defs>',
        f'<rect width="{W}" height="{H}" fill="white"/>',
    ]
    ll_end = PT + PH + len(messages)*MH + 20
    for pid,_ in parts_list:
        cx = px[pid]
        svg.append(f'<line x1="{cx:.0f}" y1="{PT+PH}" x2="{cx:.0f}" y2="{ll_end}" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="6 4"/>')
    for i,(pid,alias) in enumerate(parts_list):
        cx = px[pid]; x = cx - PW/2; ci = i%len(FILLS)
        svg += [
            f'<rect x="{x:.0f}" y="{PT}" width="{PW}" height="{PH}" rx="6" fill="{FILLS[ci]}" stroke="{BORDS[ci]}" stroke-width="1.5"/>',
            f'<text x="{cx:.0f}" y="{PT+PH/2+5:.0f}" font-family="sans-serif" font-size="11" font-weight="600" fill="{BORDS[ci]}" text-anchor="middle">{_esc(alias[:18])}</text>',
        ]
    for idx,(s,t,arr,lbl) in enumerate(messages):
        y = PT + PH + (idx+1)*MH - MH/2
        sx = px.get(s, PL); tx = px.get(t, PL)
        dash = 'stroke-dasharray="6 3"' if "--" in arr else ""
        if s == t:
            svg.append(f'<path d="M{sx:.0f} {y:.0f} q28 -14 28 0 q0 14 -28 14" fill="none" stroke="#64748b" stroke-width="1.5" marker-end="url(#ar)"/>')
        else:
            svg.append(f'<line x1="{sx:.0f}" y1="{y:.0f}" x2="{tx:.0f}" y2="{y:.0f}" stroke="#64748b" stroke-width="1.5" {dash} marker-end="url(#ar)"/>')
        mx = (sx+tx)/2
        svg += [
            f'<rect x="{mx-48:.0f}" y="{y-15:.0f}" width="96" height="16" rx="3" fill="white" opacity="0.9"/>',
            f'<text x="{mx:.0f}" y="{y-2:.0f}" font-family="sans-serif" font-size="10" fill="#374151" text-anchor="middle">{_esc(lbl[:22])}</text>',
        ]
    svg.append("</svg>")
    return "\n".join(svg)


def _svg_empty(msg: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="80">'
            f'<rect width="400" height="80" rx="8" fill="#f1f5f9" stroke="#cbd5e1"/>'
            f'<text x="200" y="45" font-family="sans-serif" font-size="13" fill="#64748b" text-anchor="middle">{_esc(msg)}</text></svg>')


def _svg_fallback(code: str, diagram_type: str = "") -> bytes:
    code = code.strip()
    first = code.split("\n")[0].strip().lower()
    if first.startswith("sequencediagram"):
        return _svg_sequence(code).encode("utf-8")
    elif first.startswith("graph lr"):
        return _svg_graph(code, "LR").encode("utf-8")
    else:
        return _svg_graph(code, "TD").encode("utf-8")


# ── Public API ─────────────────────────────────────────────────────────────────

def is_svg(data: bytes) -> bool:
    return b"<svg" in data[:200]


def mermaid_to_png_bytes(mermaid_code: str, diagram_type: str = "") -> bytes:
    """
    Render Mermaid → bytes (PNG or SVG). Always returns bytes, never raises.

    Chain:
      1. Playwright + local mermaid.js  (offline, best quality)
      2. mmdc CLI                       (offline if Chrome configured)
      3. mermaid.ink API                (online fallback)
      4. SVG fallback                   (pure Python, always works)
    """
    code = mermaid_code.strip().replace("\\n", "\n") if mermaid_code else ""
    if not code:
        return _svg_empty("Empty diagram").encode("utf-8")

    result = _render_playwright(code)
    if result:
        return result

    result = _render_mmdc(code)
    if result:
        return result

    result = _render_mermaid_ink(code)
    if result:
        return result

    return _svg_fallback(code, diagram_type)
