"""
PDF Report Generator.

Takes the agent's accumulated findings, diagrams, and graphify graph analysis
and renders a full multi-section PDF using ReportLab.

Sections:
  1.  Cover page
  2.  Table of contents
  3.  Executive summary
  4.  Technology stack
  5.  Architecture overview
  6.  Architecture diagram (Mermaid → PNG)
  7.  Data flow diagram
  8.  Component diagram
  9.  Key components
  10. API endpoints
  11. Code quality & security
  12. Graph analysis (god nodes + surprising connections)
  13. File-by-file findings
  14. Improvement recommendations
  15. Repository file tree
  16. Appendix: agentic trace
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.colors import HexColor
from reportlab.platypus.flowables import Flowable

from codegrapher_mcp.output.diagrams import mermaid_to_png_bytes


# ── Palette ───────────────────────────────────────────────────────────────────
PRIMARY   = HexColor("#0f172a")
ACCENT    = HexColor("#1e293b")
HIGHLIGHT = HexColor("#3b82f6")
LIGHT_BG  = HexColor("#f1f5f9")
LIGHT_BLUE = HexColor("#eff6ff")
MID_GRAY  = HexColor("#64748b")
DARK_TEXT = HexColor("#1e293b")
SUCCESS   = HexColor("#22c55e")
WARNING   = HexColor("#f59e0b")
DANGER    = HexColor("#ef4444")
CODE_BG   = HexColor("#1e293b")


# ── Colored header block ───────────────────────────────────────────────────────
class SectionHeader(Flowable):
    def __init__(self, text: str, width: float, number: str = "",
                 bg=PRIMARY, fg=colors.white, height: float = 34):
        super().__init__()
        self.text = text
        self._w = width
        self.number = number
        self.bg = bg
        self.fg = fg
        self._h = height

    def draw(self):
        c = self.canv
        c.setFillColor(self.bg)
        c.roundRect(0, 0, self._w, self._h, 5, fill=1, stroke=0)
        c.setFillColor(self.fg)
        if self.number:
            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(HexColor("#94a3b8"))
            c.drawString(12, 11, self.number)
            c.setFillColor(self.fg)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(40, 11, self.text)
        else:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(12, 11, self.text)

    def wrap(self, *args):
        return self._w, self._h


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1", parent=base["Normal"],
            fontSize=18, fontName="Helvetica-Bold", textColor=PRIMARY,
            spaceBefore=14, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=base["Normal"],
            fontSize=13, fontName="Helvetica-Bold", textColor=HIGHLIGHT,
            spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("body", parent=base["Normal"],
            fontSize=10, leading=15, textColor=DARK_TEXT,
            spaceAfter=6, alignment=TA_JUSTIFY),
        "bullet": ParagraphStyle("bullet", parent=base["Normal"],
            fontSize=10, leading=14, textColor=DARK_TEXT,
            leftIndent=16, spaceAfter=3),
        "code": ParagraphStyle("code", parent=base["Normal"],
            fontSize=8, fontName="Courier", textColor=HexColor("#e2e8f0"),
            backColor=CODE_BG, leading=12, leftIndent=8, rightIndent=8),
        "small": ParagraphStyle("small", parent=base["Normal"],
            fontSize=8, textColor=MID_GRAY, spaceAfter=2),
        "caption": ParagraphStyle("caption", parent=base["Normal"],
            fontSize=9, fontName="Helvetica-Oblique", textColor=MID_GRAY,
            alignment=TA_CENTER, spaceAfter=8),
        "cover_title": ParagraphStyle("cover_title", parent=base["Normal"],
            fontSize=34, fontName="Helvetica-Bold", textColor=colors.white,
            alignment=TA_CENTER, spaceAfter=8),
        "cover_sub": ParagraphStyle("cover_sub", parent=base["Normal"],
            fontSize=15, fontName="Helvetica", textColor=HexColor("#93c5fd"),
            alignment=TA_CENTER, spaceAfter=6),
    }


def _bullet(text: str, s) -> Paragraph:
    return Paragraph(f"• {text}", s["bullet"])


def _diagram_flowables(mermaid_code: str, diagram_type: str, page_width: float, styles: dict) -> list:
    """
    Return flowable(s) for a diagram.

    Rendering chain (handled inside mermaid_to_png_bytes):
      1. Playwright + local mermaid.js  (offline, PNG)
      2. mmdc CLI                       (offline, PNG)
      3. mermaid.ink API                (online, PNG)
      4. SVG fallback                   (offline, pure Python SVG — always works)

    Never shows raw Mermaid code in the PDF — always renders a visual.
    """
    if not mermaid_code:
        return []

    from codegrapher_mcp.output.diagrams import is_svg
    data = mermaid_to_png_bytes(mermaid_code, diagram_type)

    if is_svg(data):
        # SVG fallback — embed as SVG drawing via reportlab's SVG support
        try:
            from reportlab.graphics import renderPDF
            from svglib.svg2rlg import svg2rlg
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
                f.write(data)
                tmp_path = f.name
            try:
                drawing = svg2rlg(tmp_path)
                if drawing:
                    # Scale to page width
                    scale = page_width / drawing.width if drawing.width else 1
                    drawing.width *= scale
                    drawing.height *= scale
                    drawing.transform = (scale, 0, 0, scale, 0, 0)
                    return [drawing]
            finally:
                os.unlink(tmp_path)
        except ImportError:
            pass

        # svglib not available — render SVG as inline image via cairosvg
        try:
            import cairosvg
            png_data = cairosvg.svg2png(bytestring=data, output_width=int(page_width))
            return [Image(io.BytesIO(png_data), width=page_width,
                         height=page_width * 0.55)]
        except ImportError:
            pass

        # Last resort: decode SVG and draw it as a ReportLab drawing manually
        # using only standard library — parse width/height and scale
        return _svg_to_reportlab_image(data, page_width, styles)

    # PNG / JPEG — straightforward
    try:
        from PIL import Image as PILImage
        img_buf = io.BytesIO(data)
        pil = PILImage.open(img_buf)
        orig_w, orig_h = pil.size
        img_buf.seek(0)
        # Scale to fit page width, cap height at 60% of A4 content height
        max_h = page_width * 0.60
        scale_w = page_width / orig_w
        h = orig_h * scale_w
        if h > max_h:
            scale_w = max_h / orig_h
            w = orig_w * scale_w
            h = max_h
        else:
            w = page_width
        return [Image(img_buf, width=w, height=h)]
    except ImportError:
        pass

    # Pillow not available — use fixed safe aspect ratio
    img_buf = io.BytesIO(data)
    return [Image(img_buf, width=page_width, height=page_width * 0.50)]


def _svg_to_reportlab_image(svg_bytes: bytes, page_width: float, styles: dict) -> list:
    """
    Convert SVG bytes to a ReportLab Image using an in-memory PNG render
    via Playwright if available, otherwise draw a styled placeholder box
    showing the diagram title extracted from the SVG.
    """
    # Try rendering the SVG via Playwright (it's already a valid HTML-embeddable SVG)
    try:
        from playwright.sync_api import sync_playwright
        svg_str = svg_bytes.decode("utf-8", errors="replace")
        html = (f"<!DOCTYPE html><html><head>"
                f"<style>body{{margin:0;padding:0;background:white;}}</style></head>"
                f"<body>{svg_str}</body></html>")
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            page.set_viewport_size({"width": int(page_width), "height": 900})
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(300)
            svg_el = page.query_selector("svg")
            if svg_el:
                box = svg_el.bounding_box()
                if box and box["width"] > 10:
                    clip = {"x": 0, "y": 0,
                            "width": box["width"] + 10, "height": box["height"] + 10}
                    png = page.screenshot(clip=clip)
                    browser.close()
                    scale = page_width / (box["width"] + 10)
                    h = (box["height"] + 10) * scale
                    return [Image(io.BytesIO(png), width=page_width, height=h)]
            browser.close()
    except Exception:
        pass

    # Absolute fallback: styled grey placeholder
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib.colors import HexColor
    box_data = [[Paragraph(
        "📊 Diagram rendered (SVG — open HTML report for interactive view)",
        ParagraphStyle("dph", fontSize=9, fontName="Helvetica-Oblique",
                       textColor=HexColor("#64748b"), alignment=1))]]
    t = Table(box_data, colWidths=[page_width], rowHeights=[50])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 1, HexColor("#cbd5e1")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return [t]


def generate_pdf(
    output_path: Path,
    repo_name: str,
    provider_name: str,
    state_accumulator: dict,
    directory_tree: str = "",
    total_files: int = 0,
    total_lines: int = 0,
    agent_steps: int = 0,
    agent_tool_calls: int = 0,
    elapsed_seconds: float = 0.0,
) -> None:
    """Generate the full PDF report from agent findings."""
    fd = state_accumulator.get("finish_data", {})
    findings: list[dict] = state_accumulator.get("findings", [])
    diagrams: list[dict] = state_accumulator.get("diagrams", [])
    god_nodes: list[dict] = state_accumulator.get("god_nodes", [])
    surprising: list[dict] = state_accumulator.get("surprising_connections", [])
    communities: dict = state_accumulator.get("communities", {})

    page_w, page_h = A4
    margin = 1.8 * cm
    cw = page_w - 2 * margin      # content width

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
        title=f"{repo_name} — CodeGrapher Analysis",
        author="CodeGrapher",
    )
    s = _styles()
    story = []

    def add_section(num: str, title: str, bg=PRIMARY):
        story.append(Spacer(1, 6))
        story.append(SectionHeader(title, cw, number=num, bg=bg))
        story.append(Spacer(1, 10))

    # ── COVER ─────────────────────────────────────────────────────────────────
    cover_data = [
        [Paragraph(repo_name, s["cover_title"])],
        [Paragraph("Repository Analysis Report", s["cover_sub"])],
        [Paragraph(fd.get("purpose", ""), ParagraphStyle("cp", fontSize=12,
            fontName="Helvetica-Oblique", textColor=HexColor("#bfdbfe"),
            alignment=TA_CENTER, spaceAfter=20))],
    ]
    cover_t = Table(cover_data, colWidths=[cw])
    cover_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 24),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 24),
    ]))
    story.append(cover_t)
    story.append(Spacer(1, 20))

    stats = [
        ["📁 Files", str(total_files), "📝 Lines", f"{total_lines:,}"],
        ["🏗 Architecture", fd.get("architecture_style", "N/A"), "🤖 Provider", provider_name],
        ["📊 Communities", str(len(communities)), "📅 Date", date.today().strftime("%B %d, %Y")],
    ]
    st = Table(stats, colWidths=[cw * 0.16, cw * 0.34, cw * 0.16, cw * 0.34])
    st.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), HIGHLIGHT),
        ("TEXTCOLOR", (2, 0), (2, -1), HIGHLIGHT),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(st)
    story.append(PageBreak())

    # ── TOC ────────────────────────────────────────────────────────────────────
    story.append(Paragraph("Table of Contents", s["h1"]))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY))
    story.append(Spacer(1, 10))
    toc_items = [
        ("1.", "Executive Summary"), ("2.", "Technology Stack"),
        ("3.", "Architecture Overview"), ("4.", "Architecture Diagram"),
        ("5.", "Data Flow Diagram"), ("6.", "Component Diagram"),
        ("7.", "Key Components"), ("8.", "API Endpoints"),
        ("9.", "Code Quality & Security"), ("10.", "Graph Analysis (God Nodes)"),
        ("11.", "File-by-File Findings"), ("12.", "Improvement Recommendations"),
        ("13.", "Repository File Tree"), ("Apx.", "Agentic Analysis Trace"),
    ]
    toc_data = [[
        Paragraph(f"<b>{n}</b>", ParagraphStyle("tn", fontSize=11,
            fontName="Helvetica-Bold", textColor=HIGHLIGHT)),
        Paragraph(t, ParagraphStyle("ti", fontSize=11, textColor=DARK_TEXT)),
    ] for n, t in toc_items]
    toc_t = Table(toc_data, colWidths=[40, cw - 40])
    toc_t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, HexColor("#e2e8f0")),
    ]))
    story.append(toc_t)
    story.append(PageBreak())

    # ── 1. EXECUTIVE SUMMARY ───────────────────────────────────────────────────
    add_section("1.", "Executive Summary")
    if fd.get("summary"):
        story.append(Paragraph(fd["summary"], s["body"]))
    story.append(Spacer(1, 8))

    if fd.get("testing_approach"):
        story.append(Paragraph("Testing", s["h2"]))
        story.append(Paragraph(fd["testing_approach"], s["body"]))

    if fd.get("deployment_info"):
        story.append(Paragraph("Deployment", s["h2"]))
        story.append(Paragraph(fd["deployment_info"], s["body"]))
    story.append(PageBreak())

    # ── 2. TECH STACK ─────────────────────────────────────────────────────────
    add_section("2.", "Technology Stack")
    tech = fd.get("tech_stack", [])
    if tech:
        # Badge grid
        row_size = 4
        rows = []
        row = []
        badge_style = ParagraphStyle("badge", fontSize=9, fontName="Helvetica-Bold",
            textColor=colors.white, backColor=HIGHLIGHT, alignment=TA_CENTER, leading=12)
        for item in tech:
            row.append(Paragraph(f" {item} ", badge_style))
            if len(row) == row_size:
                rows.append(row); row = []
        if row:
            while len(row) < row_size:
                row.append(Paragraph("", s["small"]))
            rows.append(row)
        col_w = cw / row_size
        bt = Table(rows, colWidths=[col_w] * row_size, rowHeights=22)
        bt.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BG]),
        ]))
        story.append(bt)
        story.append(Spacer(1, 12))

    if fd.get("dependency"):
        story.append(Paragraph("Key Dependencies", s["h2"]))

    # Find dependency findings
    for f in findings:
        if f["category"] == "dependency":
            story.append(_bullet(f"{f['title']}: {f['detail']}", s))
    story.append(PageBreak())

    # ── 3. ARCHITECTURE OVERVIEW ──────────────────────────────────────────────
    add_section("3.", "Architecture Overview")
    story.append(Paragraph(
        f"<b>Style:</b> {fd.get('architecture_style', 'N/A')}", s["body"]))
    story.append(Spacer(1, 8))

    if fd.get("data_flow"):
        story.append(Paragraph("Data Flow", s["h2"]))
        for i, step in enumerate(fd["data_flow"], 1):
            story.append(Paragraph(f"<b>{i}.</b> {step}", s["bullet"]))

    if fd.get("database_models"):
        story.append(Spacer(1, 8))
        story.append(Paragraph("Data Models / Entities", s["h2"]))
        for m in fd["database_models"]:
            story.append(_bullet(m, s))
    story.append(PageBreak())

    # ── 4-6. DIAGRAMS ─────────────────────────────────────────────────────────
    diagram_map: dict[str, dict] = {}
    for d in diagrams:
        diagram_map[d["diagram_type"]] = d

    for num, dtype, title in [
        ("4.", "architecture", "Architecture Diagram"),
        ("5.", "flow", "Data Flow Diagram"),
        ("6.", "components", "Component Diagram"),
    ]:
        add_section(num, title)
        d = diagram_map.get(dtype)
        if d:
            story.append(Paragraph(d.get("description", ""), s["body"]))
            story.append(Spacer(1, 8))
            story.extend(_diagram_flowables(d["mermaid_code"], dtype, cw, s))
        else:
            story.append(Paragraph("No diagram generated for this section.", s["small"]))
        story.append(PageBreak())

    # ── 7. KEY COMPONENTS ─────────────────────────────────────────────────────
    add_section("7.", "Key Components")
    components = fd.get("key_components", [])
    # Also pull from findings
    comp_findings = [f for f in findings if f["category"] == "component"]

    for comp in components:
        name = comp.get("name", "?")
        desc = comp.get("description", "")
        files = comp.get("files", [])
        responsibilities = comp.get("responsibilities", [])

        story.append(KeepTogether([
            Paragraph(f"<b>{name}</b>", ParagraphStyle("cn", fontSize=12,
                fontName="Helvetica-Bold", textColor=HIGHLIGHT, spaceBefore=10, spaceAfter=4)),
            Paragraph(desc, s["body"]) if desc else Spacer(1, 1),
        ]))
        for r in (responsibilities or []):
            story.append(_bullet(r, s))
        if files:
            story.append(Paragraph(
                "Files: " + ", ".join(f"<font name='Courier' size='8'>{f}</font>" for f in files[:5]),
                s["small"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e2e8f0")))

    # Supplement with component findings not already in key_components
    for cf in comp_findings:
        story.append(KeepTogether([
            Paragraph(f"<b>{cf['title']}</b>", ParagraphStyle("cfn", fontSize=11,
                fontName="Helvetica-Bold", textColor=HIGHLIGHT, spaceBefore=8, spaceAfter=3)),
            Paragraph(cf["detail"], s["body"]),
        ]))
        if cf.get("files"):
            story.append(Paragraph(
                "Files: " + ", ".join(cf["files"][:5]), s["small"]))
    story.append(PageBreak())

    # ── 8. API ENDPOINTS ──────────────────────────────────────────────────────
    add_section("8.", "API Endpoints")
    endpoints = fd.get("api_endpoints", [])
    api_findings = [f for f in findings if f["category"] == "api_endpoint"]

    if endpoints or api_findings:
        ep_data = [["Method", "Path", "Description"]]
        m_colors = {"GET": HexColor("#22c55e"), "POST": HexColor("#3b82f6"),
                    "PUT": HexColor("#f59e0b"), "DELETE": HexColor("#ef4444"),
                    "PATCH": HexColor("#a855f7")}
        for ep in endpoints:
            m = ep.get("method", "").upper()
            ep_data.append([
                Paragraph(f"<b>{m}</b>", ParagraphStyle("m", fontSize=9,
                    fontName="Helvetica-Bold", textColor=m_colors.get(m, DARK_TEXT),
                    alignment=TA_CENTER)),
                Paragraph(f"<font name='Courier' size='8'>{ep.get('path','')}</font>", s["small"]),
                Paragraph(ep.get("description", ""), s["small"]),
            ])
        for af in api_findings:
            parts = af["title"].split(" ", 1)
            m = parts[0].upper() if len(parts) == 2 and parts[0].upper() in m_colors else ""
            path = parts[1] if len(parts) == 2 else af["title"]
            ep_data.append([
                Paragraph(f"<b>{m}</b>", ParagraphStyle("m2", fontSize=9,
                    fontName="Helvetica-Bold", textColor=m_colors.get(m, DARK_TEXT),
                    alignment=TA_CENTER)),
                Paragraph(f"<font name='Courier' size='8'>{path}</font>", s["small"]),
                Paragraph(af["detail"], s["small"]),
            ])
        et = Table(ep_data, colWidths=[cw * 0.12, cw * 0.38, cw * 0.50])
        et.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(et)
    else:
        story.append(Paragraph("No API endpoints detected.", s["small"]))
    story.append(PageBreak())

    # ── 9. CODE QUALITY & SECURITY ────────────────────────────────────────────
    add_section("9.", "Code Quality & Security")
    security = fd.get("security_notes", []) + [
        f["detail"] for f in findings if f["category"] == "security"
    ]
    quality = fd.get("code_quality_notes", []) + [
        f["detail"] for f in findings if f["category"] == "code_quality"
    ]

    if quality:
        story.append(Paragraph("Code Quality", s["h2"]))
        for note in quality:
            story.append(_bullet(note, s))

    if security:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Security", s["h2"]))
        for note in security:
            story.append(_bullet(note, s))
    story.append(PageBreak())

    # ── 10. GRAPH ANALYSIS ────────────────────────────────────────────────────
    add_section("10.", "Graph Analysis — God Nodes & Surprising Connections", bg=ACCENT)
    story.append(Paragraph(
        "Discovered by graphify's Leiden community detection and graph centrality analysis.",
        s["body"]))

    if god_nodes:
        story.append(Paragraph("God Nodes — Core Abstractions", s["h2"]))
        story.append(Paragraph(
            "The most-connected real entities in the codebase — the concepts everything else depends on.",
            s["small"]))
        story.append(Spacer(1, 6))
        gn_data = [["Rank", "Entity", "Connections"]]
        for i, gn in enumerate(god_nodes, 1):
            gn_data.append([str(i), gn.get("label", gn.get("id", "")), str(gn.get("edges", 0))])
        gnt = Table(gn_data, colWidths=[cw * 0.1, cw * 0.7, cw * 0.2])
        gnt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HIGHLIGHT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BLUE]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ]))
        story.append(gnt)

    if surprising:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Surprising Connections", s["h2"]))
        story.append(Paragraph(
            "Cross-community edges that weren't obvious from file structure alone.",
            s["small"]))
        story.append(Spacer(1, 6))
        for sc in surprising:
            why = sc.get("why", sc.get("note", ""))
            conf = sc.get("confidence", "")
            story.append(KeepTogether([
                Paragraph(
                    f"<b>{sc.get('source', '')} → {sc.get('target', '')}</b>"
                    f" <font color='#64748b'>[{conf}]</font>",
                    ParagraphStyle("sc", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=DARK_TEXT, spaceAfter=2)),
                Paragraph(why, s["small"]),
                HRFlowable(width="100%", thickness=0.5, color=HexColor("#e2e8f0")),
            ]))
    story.append(PageBreak())

    # ── 11. FILE-BY-FILE FINDINGS ─────────────────────────────────────────────
    add_section("11.", "File-by-File Findings")
    file_findings = [f for f in findings if f["category"] == "file_detail"]
    arch_findings = [f for f in findings if f["category"] == "architecture"]

    for ff in file_findings[:30]:
        story.append(KeepTogether([
            Table([[
                Paragraph(f"<font name='Courier' size='9'>{ff['title']}</font>",
                    ParagraphStyle("fp", fontSize=9, fontName="Courier", textColor=HIGHLIGHT)),
                Paragraph(ff.get("confidence", "").upper(),
                    ParagraphStyle("fc", fontSize=8, fontName="Helvetica-Bold",
                                   textColor=SUCCESS if ff.get("confidence") == "high" else WARNING,
                                   alignment=TA_CENTER)),
            ]], colWidths=[cw * 0.85, cw * 0.15]),
            Paragraph(ff["detail"], s["body"]),
            Spacer(1, 4),
        ]))

    if arch_findings:
        story.append(Paragraph("Architecture Findings", s["h2"]))
        for af in arch_findings:
            story.append(KeepTogether([
                Paragraph(f"<b>{af['title']}</b>",
                    ParagraphStyle("afn", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=DARK_TEXT, spaceBefore=6, spaceAfter=2)),
                Paragraph(af["detail"], s["body"]),
            ]))
    story.append(PageBreak())

    # ── 12. IMPROVEMENTS ──────────────────────────────────────────────────────
    add_section("12.", "Improvement Recommendations")
    improvements = fd.get("improvement_suggestions", []) + [
        f["detail"] for f in findings if f["category"] == "improvement"
    ]
    for i, suggestion in enumerate(improvements, 1):
        story.append(Paragraph(
            f"<b>{i}.</b> {suggestion}",
            ParagraphStyle("imp", fontSize=10, leading=15, textColor=DARK_TEXT,
                           leftIndent=10, spaceAfter=8)))
    if not improvements:
        story.append(Paragraph("No specific improvements identified.", s["small"]))
    story.append(PageBreak())

    # ── 13. FILE TREE ─────────────────────────────────────────────────────────
    add_section("13.", "Repository File Tree")
    tree = directory_tree or state_accumulator.get("scan_result", {}).get("directory_tree", "")
    for line in (tree or "").split("\n")[:80]:
        story.append(Paragraph(
            line.replace(" ", "&nbsp;").replace("&", "&amp;").replace("<", "&lt;"),
            ParagraphStyle("tree", fontSize=8, fontName="Courier",
                           textColor=DARK_TEXT, leading=11)))

    # ── APPENDIX: AGENTIC TRACE ───────────────────────────────────────────────
    story.append(PageBreak())
    add_section("Apx.", "Analysis Trace", bg=ACCENT)
    story.append(Paragraph(
        "This report was generated deterministically from static analysis "
        "of the repository. Tree-sitter produced the AST; NetworkX "
        "assembled the graph; Leiden (or Louvain fallback) detected "
        "communities; centrality and cross-community analysis produced "
        "the god-node and surprising-connection findings. Narrative "
        "sections were supplied by the caller.",
        s["body"]))
    story.append(Spacer(1, 10))

    trace_data = [
        ["Metric", "Value"],
        ["Generator", provider_name or "codegrapher-mcp"],
        ["Files Scanned", str(total_files)],
        ["Total Lines", f"{total_lines:,}"],
        ["Findings Recorded", str(len(findings))],
        ["Diagrams Generated", str(len(diagrams))],
        ["Communities Detected", str(len(communities))],
        ["God Nodes Found", str(len(god_nodes))],
    ]
    if elapsed_seconds:
        trace_data.append(["Analysis Time", f"{elapsed_seconds:.1f}s"])
    trace_t = Table(trace_data, colWidths=[cw * 0.45, cw * 0.55])
    trace_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 1), (0, -1), HIGHLIGHT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(trace_t)

    # ── Footer ─────────────────────────────────────────────────────────────────
    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GRAY)
        canvas.drawString(margin, 0.8 * cm, f"CodeGrapher | {repo_name}")
        canvas.drawRightString(page_w - margin, 0.8 * cm, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
