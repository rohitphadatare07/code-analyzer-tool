"""
PDF Report Generator — AS-IS Analysis format.

Renders the structured JSON document (from json_agent.build_analysis_json)
into a professional AS-IS Analysis PDF using ReportLab.

Sections
--------
  Cover
  TOC
  1.  Executive Summary
  2.  Analysis Objective, Scope & Preparation
  3.  Methodology & Data Collection
  4.  AS-IS State Documentation
  5.  Data & API Details
  6.  System Architecture
  7.  Security & Compliance
  8.  Code Quality & Technical Debt
  9.  Graph Analysis (God Nodes & Communities)
  10. File Details
  11. Repository File Tree
  Apx. Analysis Trace
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


class SectionHeader(Flowable):
    def __init__(self, text, width, number="", bg=PRIMARY, fg=colors.white, height=34):
        super().__init__()
        self.text = text; self._w = width; self.number = number
        self.bg = bg; self.fg = fg; self._h = height

    def draw(self):
        c = self.canv
        c.setFillColor(self.bg)
        c.roundRect(0, 0, self._w, self._h, 5, fill=1, stroke=0)
        if self.number:
            c.setFont("Helvetica-Bold", 10); c.setFillColor(HexColor("#94a3b8"))
            c.drawString(12, 11, self.number)
            c.setFillColor(self.fg); c.setFont("Helvetica-Bold", 12)
            c.drawString(40, 11, self.text)
        else:
            c.setFillColor(self.fg); c.setFont("Helvetica-Bold", 12)
            c.drawString(12, 11, self.text)

    def wrap(self, *a): return self._w, self._h


def _styles():
    b = getSampleStyleSheet()
    return {
        "h1":    ParagraphStyle("h1",    parent=b["Normal"], fontSize=18,
                    fontName="Helvetica-Bold", textColor=PRIMARY, spaceBefore=14, spaceAfter=8),
        "h2":    ParagraphStyle("h2",    parent=b["Normal"], fontSize=13,
                    fontName="Helvetica-Bold", textColor=HIGHLIGHT, spaceBefore=10, spaceAfter=6),
        "h3":    ParagraphStyle("h3",    parent=b["Normal"], fontSize=11,
                    fontName="Helvetica-Bold", textColor=DARK_TEXT, spaceBefore=8, spaceAfter=4),
        "body":  ParagraphStyle("body",  parent=b["Normal"], fontSize=10, leading=15,
                    textColor=DARK_TEXT, spaceAfter=6, alignment=TA_JUSTIFY),
        "bullet":ParagraphStyle("bullet",parent=b["Normal"], fontSize=10, leading=14,
                    textColor=DARK_TEXT, leftIndent=16, spaceAfter=3),
        "small": ParagraphStyle("small", parent=b["Normal"], fontSize=8,
                    textColor=MID_GRAY, spaceAfter=2),
        "code":  ParagraphStyle("code",  parent=b["Normal"], fontSize=8,
                    fontName="Courier", textColor=HexColor("#e2e8f0"),
                    backColor=HexColor("#1e293b"), leading=12, leftIndent=8, rightIndent=8),
        "cover_title": ParagraphStyle("ct", parent=b["Normal"], fontSize=32,
                    fontName="Helvetica-Bold", textColor=colors.white,
                    alignment=TA_CENTER, spaceAfter=8),
        "cover_sub":   ParagraphStyle("cs", parent=b["Normal"], fontSize=14,
                    fontName="Helvetica", textColor=HexColor("#93c5fd"),
                    alignment=TA_CENTER, spaceAfter=6),
    }


def _bullet(text, s): return Paragraph(f"• {text}", s["bullet"])
def _h2(text, s):     return Paragraph(text, s["h2"])
def _h3(text, s):     return Paragraph(text, s["h3"])
def _body(text, s):   return Paragraph(text or "—", s["body"])
def _hr(cw):          return HRFlowable(width="100%", thickness=0.5, color=HexColor("#e2e8f0"))

def _kv_table(rows, cw, s):
    """Simple two-column key-value table."""
    data = [[
        Paragraph(f"<b>{k}</b>", ParagraphStyle("kk", fontSize=9,
            fontName="Helvetica-Bold", textColor=HIGHLIGHT)),
        Paragraph(str(v) if v else "—", s["small"]),
    ] for k, v in rows]
    t = Table(data, colWidths=[cw * 0.30, cw * 0.70])
    t.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_pdf_from_json(output_path: Path, doc: dict) -> None:
    meta     = doc.get("meta", {})
    exec_s   = doc.get("executive_summary", {})
    obj      = doc.get("analysis_objective", {})
    meth     = doc.get("methodology", {})
    as_is    = doc.get("as_is_state", {})
    data_api = doc.get("data_and_api", {})
    arch     = doc.get("system_architecture", {})
    sec      = doc.get("security_and_compliance", {})
    quality  = doc.get("code_quality", {})
    graph    = doc.get("graph_analysis", {})
    repo_info= doc.get("repository", {})
    file_det = doc.get("file_details", [])
    dep_notes= doc.get("dependency_notes", [])

    repo_name     = meta.get("repo_name", "Repository")
    provider_name = meta.get("provider_name", "")
    total_files   = meta.get("total_files", 0)
    agent_steps   = meta.get("agent_steps", 0)
    elapsed       = meta.get("elapsed_seconds", 0.0)
    analysis_date = meta.get("analysis_date", "")

    page_w, page_h = A4
    margin = 1.8 * cm
    cw = page_w - 2 * margin

    pdf = SimpleDocTemplate(str(output_path), pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
        title=f"{repo_name} — AS-IS Analysis", author="CodeGrapher")
    s = _styles()
    story = []

    def sec_hdr(num, title, bg=PRIMARY):
        story.append(Spacer(1, 6))
        story.append(SectionHeader(title, cw, number=num, bg=bg))
        story.append(Spacer(1, 10))

    # ── COVER ─────────────────────────────────────────────────────────────────
    cover = Table([
        [Paragraph(repo_name, s["cover_title"])],
        [Paragraph("AS-IS Analysis Report", s["cover_sub"])],
        [Paragraph(exec_s.get("purpose", ""), ParagraphStyle("cp", fontSize=12,
            fontName="Helvetica-Oblique", textColor=HexColor("#bfdbfe"),
            alignment=TA_CENTER))],
    ], colWidths=[cw])
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), PRIMARY),
        ("ALIGN", (0,0),(-1,-1), "CENTER"),
        ("TOPPADDING", (0,0),(-1,-1), 28),
        ("BOTTOMPADDING", (0,0),(-1,-1), 28),
    ]))
    story.append(cover)
    story.append(Spacer(1, 20))

    stats = [
        ["📁 Files", str(total_files), "🏗 Architecture",
         obj.get("architecture_style", "N/A")],
        ["📊 Communities", str(graph.get("communities_count", 0)),
         "📅 Date", analysis_date],
        ["🤖 Provider", provider_name, "⏱ Time", f"{elapsed:.1f}s"],
    ]
    st = Table(stats, colWidths=[cw*0.16, cw*0.34, cw*0.16, cw*0.34])
    st.setStyle(TableStyle([
        ("FONTNAME",   (0,0),(-1,-1), "Helvetica"),
        ("FONTSIZE",   (0,0),(-1,-1), 10),
        ("FONTNAME",   (0,0),(0,-1),  "Helvetica-Bold"),
        ("FONTNAME",   (2,0),(2,-1),  "Helvetica-Bold"),
        ("TEXTCOLOR",  (0,0),(0,-1),  HIGHLIGHT),
        ("TEXTCOLOR",  (2,0),(2,-1),  HIGHLIGHT),
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [LIGHT_BLUE, colors.white]),
        ("GRID",       (0,0),(-1,-1), 0.5, HexColor("#e2e8f0")),
        ("PADDING",    (0,0),(-1,-1), 8),
    ]))
    story.append(st)
    story.append(PageBreak())

    # ── TOC ───────────────────────────────────────────────────────────────────
    story.append(Paragraph("Table of Contents", s["h1"]))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY))
    story.append(Spacer(1, 10))
    toc = [
        ("1.",    "Executive Summary"),
        ("2.",    "Analysis Objective, Scope & Preparation"),
        ("3.",    "Methodology & Data Collection"),
        ("4.",    "AS-IS State Documentation"),
        ("5.",    "Data & API Details"),
        ("6.",    "System Architecture"),
        ("7.",    "Security & Compliance"),
        ("8.",    "Code Quality & Technical Debt"),
        ("9.",    "Graph Analysis"),
        ("10.",   "File Details"),
        ("11.",   "Repository File Tree"),
        ("Apx.", "Analysis Trace"),
    ]
    toc_data = [[
        Paragraph(f"<b>{n}</b>", ParagraphStyle("tn", fontSize=11,
            fontName="Helvetica-Bold", textColor=HIGHLIGHT)),
        Paragraph(t, ParagraphStyle("ti", fontSize=11, textColor=DARK_TEXT)),
    ] for n, t in toc]
    toc_t = Table(toc_data, colWidths=[40, cw-40])
    toc_t.setStyle(TableStyle([
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [LIGHT_BLUE, colors.white]),
        ("PADDING",       (0,0),(-1,-1), 8),
        ("LINEBELOW",     (0,0),(-1,-1), 0.3, HexColor("#e2e8f0")),
    ]))
    story.append(toc_t)
    story.append(PageBreak())

    # ── 1. EXECUTIVE SUMMARY ──────────────────────────────────────────────────
    sec_hdr("1.", "Executive Summary")
    if exec_s.get("scope"):
        story.append(_h3("Scope", s))
        story.append(_body(exec_s["scope"], s))
    if exec_s.get("methodology"):
        story.append(_h3("Methodology", s))
        story.append(_body(exec_s["methodology"], s))
    if exec_s.get("critical_findings"):
        story.append(_h3("Critical Findings", s))
        for f in exec_s["critical_findings"]:
            story.append(_bullet(f, s))
    if exec_s.get("strengths"):
        story.append(_h3("Strengths", s))
        for f in exec_s["strengths"]:
            story.append(_bullet(f, s))
    if exec_s.get("bottlenecks"):
        story.append(_h3("Bottlenecks", s))
        for f in exec_s["bottlenecks"]:
            story.append(_bullet(f, s))
    story.append(PageBreak())

    # ── 2. ANALYSIS OBJECTIVE ─────────────────────────────────────────────────
    sec_hdr("2.", "Analysis Objective, Scope & Preparation")
    if obj.get("defined_goals"):
        story.append(_h3("Defined Goals", s))
        for g in obj["defined_goals"]:
            story.append(_bullet(g, s))
    if obj.get("scope_boundaries"):
        story.append(_h3("Scope & Boundaries", s))
        story.append(_body(obj["scope_boundaries"], s))
    if obj.get("architecture_style"):
        story.append(_h3("Architecture Style", s))
        story.append(_body(obj["architecture_style"], s))
    story.append(PageBreak())

    # ── 3. METHODOLOGY ────────────────────────────────────────────────────────
    sec_hdr("3.", "Methodology & Data Collection")
    if meth.get("data_collection_methods"):
        story.append(_h3("Data Collection Methods", s))
        for m in meth["data_collection_methods"]:
            story.append(_bullet(m, s))
    if meth.get("tools_used"):
        story.append(_h3("Technologies & Tools Used", s))
        row_size = 4
        rows, row = [], []
        badge = ParagraphStyle("b", fontSize=9, fontName="Helvetica-Bold",
            textColor=colors.white, backColor=HIGHLIGHT, alignment=TA_CENTER, leading=12)
        for item in meth["tools_used"]:
            row.append(Paragraph(f" {item} ", badge))
            if len(row) == row_size:
                rows.append(row); row = []
        if row:
            while len(row) < row_size:
                row.append(Paragraph("", s["small"]))
            rows.append(row)
        bt = Table(rows, colWidths=[cw/row_size]*row_size, rowHeights=22)
        bt.setStyle(TableStyle([
            ("ALIGN",(0,0),(-1,-1),"CENTER"),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ("PADDING",(0,0),(-1,-1),4),
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[LIGHT_BG]),
        ]))
        story.append(bt)
    story.append(PageBreak())

    # ── 4. AS-IS STATE ────────────────────────────────────────────────────────
    sec_hdr("4.", "AS-IS State Documentation")
    if as_is.get("process_description"):
        story.append(_h3("Process Description", s))
        story.append(_body(as_is["process_description"], s))
    if as_is.get("technologies"):
        story.append(_h3("Resources & Technologies", s))
        for t in as_is["technologies"]:
            story.append(_bullet(t, s))
    if as_is.get("infrastructure"):
        story.append(_h3("Infrastructure Configuration", s))
        infra = as_is["infrastructure"]
        if infra:
            inf_data = [["Component", "Type", "Configuration"]]
            for i in infra:
                if isinstance(i, dict):
                    inf_data.append([
                        i.get("component", ""),
                        i.get("type", ""),
                        i.get("config", ""),
                    ])
            if len(inf_data) > 1:
                it = Table(inf_data, colWidths=[cw*0.25, cw*0.20, cw*0.55])
                it.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,0), PRIMARY),
                    ("TEXTCOLOR",(0,0),(-1,0), colors.white),
                    ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                    ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
                    ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
                    ("PADDING",(0,0),(-1,-1),7),
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                ]))
                story.append(it)
    story.append(PageBreak())

    # ── 5. DATA & API ─────────────────────────────────────────────────────────
    sec_hdr("5.", "Data & API Details")
    db = data_api.get("database_config", {})
    if db:
        story.append(_h3("Database Configuration", s))
        db_rows = [(k.replace("_", " ").title(), v)
                   for k, v in db.items() if v]
        if db_rows:
            story.append(_kv_table(db_rows, cw, s))
        story.append(Spacer(1, 8))

    tables = data_api.get("tables", [])
    if tables:
        story.append(_h3("Database Tables / Collections", s))
        t_data = [["Table", "Purpose", "Key Columns"]]
        for t in tables:
            if isinstance(t, dict):
                cols = ", ".join(t.get("key_columns", []))
                t_data.append([t.get("name",""), t.get("purpose",""), cols])
        if len(t_data) > 1:
            tt = Table(t_data, colWidths=[cw*0.22, cw*0.43, cw*0.35])
            tt.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0), ACCENT),
                ("TEXTCOLOR",(0,0),(-1,0), colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
                ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
                ("PADDING",(0,0),(-1,-1),7),
                ("VALIGN",(0,0),(-1,-1),"TOP"),
            ]))
            story.append(tt)
        story.append(Spacer(1, 8))

    endpoints = data_api.get("api_endpoints", [])
    if endpoints:
        story.append(_h3("API Endpoints", s))
        m_colors = {"GET": SUCCESS, "POST": HIGHLIGHT,
                    "PUT": WARNING, "DELETE": DANGER, "PATCH": HexColor("#a855f7")}
        ep_data = [["Method", "Path", "Purpose", "Auth"]]
        for ep in endpoints:
            if isinstance(ep, dict):
                m = ep.get("method","").upper()
                auth = "✓" if ep.get("auth_required") else ""
                ep_data.append([
                    Paragraph(f"<b>{m}</b>", ParagraphStyle("mm", fontSize=9,
                        fontName="Helvetica-Bold",
                        textColor=m_colors.get(m, DARK_TEXT), alignment=TA_CENTER)),
                    Paragraph(f"<font name='Courier' size='8'>{ep.get('path','')}</font>",
                        s["small"]),
                    Paragraph(ep.get("purpose", ep.get("description", "")), s["small"]),
                    Paragraph(auth, s["small"]),
                ])
        if len(ep_data) > 1:
            et = Table(ep_data, colWidths=[cw*0.10, cw*0.32, cw*0.48, cw*0.10])
            et.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0), PRIMARY),
                ("TEXTCOLOR",(0,0),(-1,0), colors.white),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
                ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
                ("PADDING",(0,0),(-1,-1),7),
                ("ALIGN",(0,0),(0,-1),"CENTER"),
                ("ALIGN",(3,0),(3,-1),"CENTER"),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ]))
            story.append(et)
    story.append(PageBreak())

    # ── 6. SYSTEM ARCHITECTURE ────────────────────────────────────────────────
    sec_hdr("6.", "System Architecture")
    if arch.get("overview"):
        story.append(_h3("Architecture Overview", s))
        story.append(_body(arch["overview"], s))
    if arch.get("components"):
        story.append(_h3("Component Breakdown", s))
        for comp in arch["components"]:
            if not isinstance(comp, dict):
                continue
            story.append(KeepTogether([
                Paragraph(f"<b>{comp.get('name','')}</b>",
                    ParagraphStyle("cn", fontSize=12, fontName="Helvetica-Bold",
                        textColor=HIGHLIGHT, spaceBefore=8, spaceAfter=3)),
                _body(comp.get("role", comp.get("description", "")), s),
            ]))
            if comp.get("dependencies"):
                story.append(_bullet(
                    "Dependencies: " + ", ".join(comp["dependencies"]), s))
            if comp.get("files"):
                story.append(Paragraph(
                    "Files: " + ", ".join(
                        f"<font name='Courier' size='8'>{f}</font>"
                        for f in comp["files"][:4]),
                    s["small"]))
            story.append(_hr(cw))
    if arch.get("data_flow"):
        story.append(_h3("Data Flow", s))
        for i, step in enumerate(arch["data_flow"], 1):
            story.append(Paragraph(f"<b>{i}.</b> {step}", s["bullet"]))
    story.append(PageBreak())

    # ── 7. SECURITY & COMPLIANCE ──────────────────────────────────────────────
    sec_hdr("7.", "Security & Compliance")
    if sec.get("auth_mechanism"):
        story.append(_h3("Authentication & Authorisation", s))
        story.append(_body(sec["auth_mechanism"], s))
    if sec.get("data_protection"):
        story.append(_h3("Data Protection", s))
        for note in sec["data_protection"]:
            story.append(_bullet(note, s))
    if sec.get("compliance_gaps"):
        story.append(_h3("Compliance Gaps & Risks", s))
        for gap in sec["compliance_gaps"]:
            story.append(_bullet(gap, s))
    story.append(PageBreak())

    # ── 8. CODE QUALITY ───────────────────────────────────────────────────────
    sec_hdr("8.", "Code Quality & Technical Debt")
    if quality.get("strengths"):
        story.append(_h3("Strengths", s))
        for item in quality["strengths"]:
            story.append(_bullet(item, s))
    if quality.get("weaknesses"):
        story.append(_h3("Weaknesses", s))
        for item in quality["weaknesses"]:
            story.append(_bullet(item, s))
    if quality.get("technical_debt"):
        story.append(_h3("Technical Debt", s))
        for item in quality["technical_debt"]:
            story.append(_bullet(item, s))
    if quality.get("test_coverage"):
        story.append(_h3("Testing Approach", s))
        story.append(_body(quality["test_coverage"], s))
    if quality.get("improvement_suggestions"):
        story.append(_h3("Improvement Recommendations", s))
        for i, imp in enumerate(quality["improvement_suggestions"], 1):
            if isinstance(imp, dict):
                priority = imp.get("priority", "medium").upper()
                pcolor = {"HIGH": "#ef4444", "MEDIUM": "#f59e0b",
                          "LOW": "#22c55e"}.get(priority, "#64748b")
                story.append(Paragraph(
                    f"<b>{i}. [{priority}]</b> {imp.get('suggestion','')}",
                    ParagraphStyle("imp", fontSize=10, leading=15,
                        textColor=DARK_TEXT, leftIndent=10, spaceAfter=4)))
                if imp.get("rationale"):
                    story.append(Paragraph(
                        f"<i>Rationale: {imp['rationale']}</i>",
                        ParagraphStyle("rat", fontSize=9, textColor=MID_GRAY,
                            leftIndent=20, spaceAfter=6)))
            else:
                story.append(Paragraph(f"<b>{i}.</b> {imp}",
                    ParagraphStyle("imp2", fontSize=10, leading=15,
                        textColor=DARK_TEXT, leftIndent=10, spaceAfter=6)))
    story.append(PageBreak())

    # ── 9. GRAPH ANALYSIS ─────────────────────────────────────────────────────
    sec_hdr("9.", "Graph Analysis — God Nodes & Communities", bg=ACCENT)
    story.append(Paragraph(
        f"Total nodes: <b>{graph.get('total_nodes',0)}</b>  |  "
        f"Total edges: <b>{graph.get('total_edges',0)}</b>  |  "
        f"Communities: <b>{graph.get('communities_count',0)}</b>", s["small"]))
    story.append(Spacer(1, 8))

    god_nodes = graph.get("god_nodes", [])
    if god_nodes:
        story.append(_h3("God Nodes — Core Abstractions", s))
        gn_data = [["Rank", "Entity", "Connections"]]
        for i, gn in enumerate(god_nodes, 1):
            gn_data.append([str(i), gn.get("label", gn.get("id","")),
                             str(gn.get("edges",0))])
        gnt = Table(gn_data, colWidths=[cw*0.10, cw*0.70, cw*0.20])
        gnt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), HIGHLIGHT),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BLUE]),
            ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
            ("PADDING",(0,0),(-1,-1),7),
            ("ALIGN",(2,0),(2,-1),"CENTER"),
        ]))
        story.append(gnt)

    communities = graph.get("communities", [])
    if communities:
        story.append(Spacer(1, 12))
        story.append(_h3("Community Summary", s))
        com_data = [["Community", "Label", "Size", "Cohesion"]]
        for c in communities[:15]:
            com_data.append([str(c.get("id","")), c.get("label",""),
                              str(c.get("size",0)), f"{c.get('cohesion',0):.3f}"])
        com_t = Table(com_data, colWidths=[cw*0.12, cw*0.55, cw*0.15, cw*0.18])
        com_t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), ACCENT),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
            ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
            ("PADDING",(0,0),(-1,-1),6),
            ("ALIGN",(2,0),(-1,-1),"CENTER"),
        ]))
        story.append(com_t)

    surprising = graph.get("surprising_connections", [])
    if surprising:
        story.append(Spacer(1, 12))
        story.append(_h3("Surprising Connections", s))
        for sc in surprising:
            story.append(KeepTogether([
                Paragraph(
                    f"<b>{sc.get('source','')} → {sc.get('target','')}</b>"
                    f" <font color='#64748b'>[{sc.get('confidence','')}]</font>",
                    ParagraphStyle("sc", fontSize=10, fontName="Helvetica-Bold",
                        textColor=DARK_TEXT, spaceAfter=2)),
                Paragraph(sc.get("why",""), s["small"]),
                _hr(cw),
            ]))
    story.append(PageBreak())

    # ── 10. FILE DETAILS ──────────────────────────────────────────────────────
    sec_hdr("10.", "File Details")
    if file_det:
        for ff in file_det[:20]:
            if not isinstance(ff, dict):
                continue
            conf = ff.get("confidence", "high")
            story.append(KeepTogether([
                Table([[
                    Paragraph(
                        f"<font name='Courier' size='9'>{ff.get('file','')}</font>",
                        ParagraphStyle("fp", fontSize=9, fontName="Courier",
                            textColor=HIGHLIGHT)),
                    Paragraph(conf.upper(),
                        ParagraphStyle("fc", fontSize=8, fontName="Helvetica-Bold",
                            textColor=SUCCESS if conf=="high" else WARNING,
                            alignment=TA_CENTER)),
                ]], colWidths=[cw*0.85, cw*0.15]),
                _body(ff.get("summary",""), s),
                Spacer(1, 4),
            ]))
    else:
        story.append(Paragraph("No file-level detail recorded.", s["small"]))

    if dep_notes:
        story.append(_h3("Dependency Notes", s))
        for dn in dep_notes:
            if isinstance(dn, dict):
                story.append(_bullet(
                    f"{dn.get('title','')}: {dn.get('detail','')}", s))
    story.append(PageBreak())

    # ── 11. FILE TREE ─────────────────────────────────────────────────────────
    sec_hdr("11.", "Repository File Tree")
    tree = repo_info.get("directory_tree", "")
    for line in (tree or "").split("\n")[:80]:
        story.append(Paragraph(
            line.replace(" ", "&nbsp;").replace("&","&amp;").replace("<","&lt;"),
            ParagraphStyle("tr", fontSize=8, fontName="Courier",
                textColor=DARK_TEXT, leading=11)))
    fc = repo_info.get("file_counts", {})
    if fc:
        story.append(Spacer(1, 12))
        story.append(_h3("File Types", s))
        fc_data = [["Extension", "Count"]]
        for ext, cnt in sorted(fc.items(), key=lambda x: x[1], reverse=True)[:12]:
            fc_data.append([ext or "(none)", str(cnt)])
        fc_t = Table(fc_data, colWidths=[cw*0.4, cw*0.6])
        fc_t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), PRIMARY),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
            ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
            ("PADDING",(0,0),(-1,-1),6),
        ]))
        story.append(fc_t)
    story.append(PageBreak())

    # ── APPENDIX ──────────────────────────────────────────────────────────────
    sec_hdr("Apx.", "Analysis Trace", bg=ACCENT)
    story.append(_body(
        "This AS-IS Analysis was generated by a LangGraph exploration agent that "
        "autonomously scanned the repository structure, extracted AST graphs, "
        "detected community clusters, and read key files. A dedicated synthesis "
        "call then produced this structured report.", s))
    story.append(Spacer(1, 10))
    trace = [
        ["Metric", "Value"],
        ["LLM Provider",         provider_name],
        ["Agent Steps",          str(agent_steps)],
        ["Analysis Time",        f"{elapsed:.1f}s"],
        ["Files Scanned",        str(total_files)],
        ["Communities Detected", str(graph.get("communities_count", 0))],
        ["God Nodes Found",      str(len(god_nodes))],
        ["Total Graph Nodes",    str(graph.get("total_nodes", 0))],
        ["Total Graph Edges",    str(graph.get("total_edges", 0))],
    ]
    tr_t = Table(trace, colWidths=[cw*0.45, cw*0.55])
    tr_t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), PRIMARY),
        ("TEXTCOLOR",(0,0),(-1,0), colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTNAME",(0,1),(0,-1),"Helvetica-Bold"),
        ("TEXTCOLOR",(0,1),(0,-1), HIGHLIGHT),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT_BG]),
        ("GRID",(0,0),(-1,-1),0.5, HexColor("#e2e8f0")),
        ("PADDING",(0,0),(-1,-1),9),
    ]))
    story.append(tr_t)

    def on_page(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GRAY)
        canvas.drawString(margin, 0.8*cm, f"CodeGrapher AS-IS | {repo_name}")
        canvas.drawRightString(page_w - margin, 0.8*cm, f"Page {doc_obj.page}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)


# ── Legacy shim ───────────────────────────────────────────────────────────────

def generate_pdf(output_path, repo_name, provider_name, state_accumulator,
                 directory_tree="", total_files=0, total_lines=0,
                 agent_steps=0, agent_tool_calls=0, elapsed_seconds=0.0):
    from codegrapher.agent.json_agent import build_analysis_json
    doc = build_analysis_json(
        state_accumulator=state_accumulator, repo_name=repo_name,
        provider_name=provider_name, total_files=total_files,
        total_lines=total_lines, agent_steps=agent_steps,
        elapsed_seconds=elapsed_seconds, directory_tree=directory_tree)
    generate_pdf_from_json(output_path=Path(output_path), doc=doc)
