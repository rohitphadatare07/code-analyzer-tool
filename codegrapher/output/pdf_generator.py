"""
PDF Report Generator.

Takes the structured JSON document produced by json_agent.build_analysis_json()
and renders a full multi-section PDF using ReportLab.

Input
-----
Call generate_pdf_from_json(output_path, doc) where `doc` is the dict
returned by json_agent.build_analysis_json().

Alternatively, call the legacy generate_pdf() shim which builds the JSON
document from a state_accumulator dict before delegating here.

Sections
--------
  1.  Cover page
  2.  Table of contents
  3.  Executive summary
  4.  Technology stack
  5.  Architecture overview
  6.  Key components
  7.  API endpoints
  8.  Code quality & security
  9.  Graph analysis (god nodes + surprising connections)
  10. File-by-file findings  (from finish_analysis.file_details)
  11. Architecture notes     (from finish_analysis.architecture_notes)
  12. Improvement recommendations
  13. Repository file tree
  Apx. Agentic analysis trace

No Mermaid diagrams. No record_finding incremental bookkeeping.
All content comes from the single finish_analysis call.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.colors import HexColor
from reportlab.platypus.flowables import Flowable


# ── Palette ───────────────────────────────────────────────────────────────────
PRIMARY    = HexColor("#0f172a")
ACCENT     = HexColor("#1e293b")
HIGHLIGHT  = HexColor("#3b82f6")
LIGHT_BG   = HexColor("#f1f5f9")
LIGHT_BLUE = HexColor("#eff6ff")
MID_GRAY   = HexColor("#64748b")
DARK_TEXT  = HexColor("#1e293b")
SUCCESS    = HexColor("#22c55e")
WARNING    = HexColor("#f59e0b")
DANGER     = HexColor("#ef4444")
CODE_BG    = HexColor("#1e293b")


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


def _bullet(text: str, s: dict) -> Paragraph:
    return Paragraph(f"• {text}", s["bullet"])


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_pdf_from_json(output_path: Path, doc: dict) -> None:
    """
    Generate the PDF from a structured analysis JSON document.

    Parameters
    ----------
    output_path : Path
        Destination .pdf file path.
    doc : dict
        The analysis document produced by json_agent.build_analysis_json().
    """
    meta      = doc.get("meta", {})
    summary   = doc.get("summary", {})
    graph     = doc.get("graph_analysis", {})
    repo_info = doc.get("repository", {})

    # New structured fields from finish_analysis
    file_details      = doc.get("file_details", [])
    architecture_notes = doc.get("architecture_notes", [])
    dependency_notes  = doc.get("dependency_notes", [])

    repo_name     = meta.get("repo_name", "Repository")
    provider_name = meta.get("provider_name", "")
    total_files   = meta.get("total_files", 0)
    agent_steps   = meta.get("agent_steps", 0)
    elapsed_secs  = meta.get("elapsed_seconds", 0.0)
    analysis_date = meta.get("analysis_date", "")

    page_w, page_h = A4
    margin = 1.8 * cm
    cw = page_w - 2 * margin

    pdf_doc = SimpleDocTemplate(
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

    # ── COVER ──────────────────────────────────────────────────────────────────
    cover_data = [
        [Paragraph(repo_name, s["cover_title"])],
        [Paragraph("Repository Analysis Report", s["cover_sub"])],
        [Paragraph(summary.get("purpose", ""), ParagraphStyle("cp", fontSize=12,
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
        ["📁 Files", str(total_files), "🏗 Architecture", summary.get("architecture_style", "N/A")],
        ["📊 Communities", str(graph.get("communities_count", 0)), "📅 Date", analysis_date],
        ["🤖 Provider", provider_name, "⏱ Time", f"{elapsed_secs:.1f}s"],
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
        ("1.",    "Executive Summary"),
        ("2.",    "Technology Stack"),
        ("3.",    "Architecture Overview"),
        ("4.",    "Key Components"),
        ("5.",    "API Endpoints"),
        ("6.",    "Code Quality & Security"),
        ("7.",    "Graph Analysis (God Nodes)"),
        ("8.",    "File-by-File Findings"),
        ("9.",    "Improvement Recommendations"),
        ("10.",   "Repository File Tree"),
        ("Apx.", "Agentic Analysis Trace"),
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
    if summary.get("overview"):
        story.append(Paragraph(summary["overview"], s["body"]))
    story.append(Spacer(1, 8))

    if summary.get("testing_approach"):
        story.append(Paragraph("Testing", s["h2"]))
        story.append(Paragraph(summary["testing_approach"], s["body"]))

    if summary.get("deployment_info"):
        story.append(Paragraph("Deployment", s["h2"]))
        story.append(Paragraph(summary["deployment_info"], s["body"]))
    story.append(PageBreak())

    # ── 2. TECH STACK ──────────────────────────────────────────────────────────
    add_section("2.", "Technology Stack")
    tech = doc.get("tech_stack", [])
    if tech:
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
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BG]),
        ]))
        story.append(bt)
        story.append(Spacer(1, 12))

    dep_notes = doc.get("dependency_notes", [])
    if dep_notes:
        story.append(Paragraph("Key Dependencies", s["h2"]))
        for n in dep_notes:
            story.append(_bullet(f"{n['title']}: {n['detail']}" if n.get("detail") else n["title"], s))
    story.append(PageBreak())

    # ── 3. ARCHITECTURE OVERVIEW ───────────────────────────────────────────────
    add_section("3.", "Architecture Overview")
    story.append(Paragraph(
        f"<b>Style:</b> {summary.get('architecture_style', 'N/A')}", s["body"]))
    story.append(Spacer(1, 8))

    data_flow = doc.get("data_flow", [])
    if data_flow:
        story.append(Paragraph("Data Flow", s["h2"]))
        for i, step in enumerate(data_flow, 1):
            story.append(Paragraph(f"<b>{i}.</b> {step}", s["bullet"]))

    db_models = doc.get("database_models", [])
    if db_models:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Data Models / Entities", s["h2"]))
        for m in db_models:
            story.append(_bullet(m, s))
    story.append(PageBreak())

    # ── 4. KEY COMPONENTS ──────────────────────────────────────────────────────
    add_section("4.", "Key Components")
    components = doc.get("key_components", [])

    for comp in components:
        name = comp.get("name", "?")
        desc = comp.get("description", "")
        files = comp.get("files", [])
        responsibilities = comp.get("responsibilities", [])

        story.append(KeepTogether([
            Paragraph(f"<b>{name}</b>", ParagraphStyle("cn", fontSize=12,
                fontName="Helvetica-Bold", textColor=HIGHLIGHT,
                spaceBefore=10, spaceAfter=4)),
            Paragraph(desc, s["body"]) if desc else Spacer(1, 1),
        ]))
        for r in (responsibilities or []):
            story.append(_bullet(r, s))
        if files:
            story.append(Paragraph(
                "Files: " + ", ".join(
                    f"<font name='Courier' size='8'>{f}</font>" for f in files[:5]
                ),
                s["small"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#e2e8f0")))

    story.append(PageBreak())

    # ── 5. API ENDPOINTS ───────────────────────────────────────────────────────
    add_section("5.", "API Endpoints")
    endpoints = doc.get("api_endpoints", [])

    if endpoints:
        ep_data = [["Method", "Path", "Description"]]
        m_colors = {
            "GET": HexColor("#22c55e"), "POST": HexColor("#3b82f6"),
            "PUT": HexColor("#f59e0b"), "DELETE": HexColor("#ef4444"),
            "PATCH": HexColor("#a855f7"),
        }
        for ep in endpoints:
            m = ep.get("method", "").upper()
            ep_data.append([
                Paragraph(f"<b>{m}</b>", ParagraphStyle("m", fontSize=9,
                    fontName="Helvetica-Bold",
                    textColor=m_colors.get(m, DARK_TEXT), alignment=TA_CENTER)),
                Paragraph(f"<font name='Courier' size='8'>{ep.get('path','')}</font>",
                    s["small"]),
                Paragraph(ep.get("description", ""), s["small"]),
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

    # ── 6. CODE QUALITY & SECURITY ─────────────────────────────────────────────
    add_section("6.", "Code Quality & Security")
    security = doc.get("security_notes", [])
    quality  = doc.get("code_quality_notes", [])

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

    # ── 7. GRAPH ANALYSIS ──────────────────────────────────────────────────────
    add_section("7.", "Graph Analysis — God Nodes & Surprising Connections", bg=ACCENT)
    story.append(Paragraph(
        "Discovered by graphify's Leiden community detection and graph centrality analysis.",
        s["body"]))
    story.append(Paragraph(
        f"Total nodes: <b>{graph.get('total_nodes', 0)}</b>  |  "
        f"Total edges: <b>{graph.get('total_edges', 0)}</b>  |  "
        f"Communities: <b>{graph.get('communities_count', 0)}</b>",
        s["small"]))
    story.append(Spacer(1, 8))

    god_nodes = graph.get("god_nodes", [])
    if god_nodes:
        story.append(Paragraph("God Nodes — Core Abstractions", s["h2"]))
        story.append(Paragraph(
            "The most-connected real entities in the codebase — "
            "the concepts everything else depends on.",
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

    surprising = graph.get("surprising_connections", [])
    if surprising:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Surprising Connections", s["h2"]))
        story.append(Paragraph(
            "Cross-community edges that weren't obvious from file structure alone.",
            s["small"]))
        story.append(Spacer(1, 6))
        for sc in surprising:
            why = sc.get("why", "")
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

    communities = graph.get("communities", [])
    if communities:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Community Summary", s["h2"]))
        com_data = [["Community", "Label", "Size", "Cohesion"]]
        for c in communities[:20]:
            com_data.append([
                str(c.get("id", "")),
                c.get("label", ""),
                str(c.get("size", 0)),
                f"{c.get('cohesion', 0.0):.3f}",
            ])
        com_t = Table(com_data, colWidths=[cw * 0.12, cw * 0.55, cw * 0.15, cw * 0.18])
        com_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 6),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ]))
        story.append(com_t)
    story.append(PageBreak())

    # ── 8. FILE-BY-FILE FINDINGS ───────────────────────────────────────────────
    add_section("8.", "File-by-File Findings")

    if file_details:
        for ff in file_details[:30]:
            story.append(KeepTogether([
                Table([[
                    Paragraph(
                        f"<font name='Courier' size='9'>{ff.get('file', '')}</font>",
                        ParagraphStyle("fp", fontSize=9, fontName="Courier",
                                       textColor=HIGHLIGHT)),
                    Paragraph(ff.get("confidence", "high").upper(),
                        ParagraphStyle("fc", fontSize=8, fontName="Helvetica-Bold",
                            textColor=SUCCESS if ff.get("confidence") == "high" else WARNING,
                            alignment=TA_CENTER)),
                ]], colWidths=[cw * 0.85, cw * 0.15]),
                Paragraph(ff.get("summary", ""), s["body"]),
                Spacer(1, 4),
            ]))
    else:
        story.append(Paragraph("No file-level detail recorded.", s["small"]))

    arch_notes = doc.get("architecture_notes", [])
    if arch_notes:
        story.append(Paragraph("Architecture Notes", s["h2"]))
        for an in arch_notes:
            story.append(KeepTogether([
                Paragraph(f"<b>{an.get('title', '')}</b>",
                    ParagraphStyle("afn", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=DARK_TEXT, spaceBefore=6, spaceAfter=2)),
                Paragraph(an.get("detail", ""), s["body"]),
            ]))
    story.append(PageBreak())

    # ── 9. IMPROVEMENTS ────────────────────────────────────────────────────────
    add_section("9.", "Improvement Recommendations")
    improvements = doc.get("improvement_suggestions", [])
    for i, suggestion in enumerate(improvements, 1):
        story.append(Paragraph(
            f"<b>{i}.</b> {suggestion}",
            ParagraphStyle("imp", fontSize=10, leading=15, textColor=DARK_TEXT,
                           leftIndent=10, spaceAfter=8)))
    if not improvements:
        story.append(Paragraph("No specific improvements identified.", s["small"]))
    story.append(PageBreak())

    # ── 10. FILE TREE ──────────────────────────────────────────────────────────
    add_section("10.", "Repository File Tree")
    tree = repo_info.get("directory_tree", "")
    for line in (tree or "").split("\n")[:80]:
        story.append(Paragraph(
            line.replace(" ", "&nbsp;").replace("&", "&amp;").replace("<", "&lt;"),
            ParagraphStyle("tree", fontSize=8, fontName="Courier",
                           textColor=DARK_TEXT, leading=11)))

    file_counts = repo_info.get("file_counts", {})
    if file_counts:
        story.append(Spacer(1, 12))
        story.append(Paragraph("File Types", s["h2"]))
        fc_data = [["Extension", "Count"]]
        for ext, cnt in sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:15]:
            fc_data.append([ext or "(none)", str(cnt)])
        fc_t = Table(fc_data, colWidths=[cw * 0.4, cw * 0.6])
        fc_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(fc_t)
    story.append(PageBreak())

    # ── APPENDIX: AGENTIC TRACE ────────────────────────────────────────────────
    add_section("Apx.", "Agentic Analysis Trace", bg=ACCENT)
    story.append(Paragraph(
        "This report was generated by a LangGraph agent that autonomously explored "
        "the repository — deciding which tools to call, which files to read, and when "
        "the analysis was complete. The agent used graphify's AST extraction and "
        "Leiden community detection as its core analytical tools.",
        s["body"]))
    story.append(Spacer(1, 10))

    trace_data = [
        ["Metric", "Value"],
        ["LLM Provider",         provider_name],
        ["Agent Steps",          str(agent_steps)],
        ["Analysis Time",        f"{elapsed_secs:.1f}s"],
        ["Files Scanned",        str(total_files)],
        ["Files Detailed",       str(len(file_details))],
        ["Architecture Notes",   str(len(architecture_notes))],
        ["Dependency Notes",     str(len(dependency_notes))],
        ["Communities Detected", str(graph.get("communities_count", 0))],
        ["God Nodes Found",      str(len(god_nodes))],
        ["Total Graph Nodes",    str(graph.get("total_nodes", 0))],
        ["Total Graph Edges",    str(graph.get("total_edges", 0))],
    ]
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
    def on_page(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GRAY)
        canvas.drawString(margin, 0.8 * cm, f"CodeGrapher | {repo_name}")
        canvas.drawRightString(page_w - margin, 0.8 * cm, f"Page {doc_obj.page}")
        canvas.restoreState()

    pdf_doc.build(story, onFirstPage=on_page, onLaterPages=on_page)


# ── Legacy shim ───────────────────────────────────────────────────────────────

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
    """
    Legacy entry point kept for backward compatibility with pipeline.py.

    Converts state_accumulator → JSON doc via json_agent,
    then delegates to generate_pdf_from_json().
    """
    from codegrapher.agent.json_agent import build_analysis_json

    doc = build_analysis_json(
        state_accumulator=state_accumulator,
        repo_name=repo_name,
        provider_name=provider_name,
        total_files=total_files,
        total_lines=total_lines,
        agent_steps=agent_steps,
        elapsed_seconds=elapsed_seconds,
        directory_tree=directory_tree,
    )
    generate_pdf_from_json(output_path=output_path, doc=doc)
