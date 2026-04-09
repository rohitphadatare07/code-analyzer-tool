"""PDF report generator - creates a comprehensive repository analysis PDF."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, Image, KeepTogether,
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.colors import HexColor

from repoanalyzer_v2.results import AnalysisResult
from repoanalyzer_v2.scanner import RepoScan
from repoanalyzer_v2.diagrams import mermaid_to_png_bytes, generate_fallback_diagram


# ── Color palette ─────────────────────────────────────────────────────────────
PRIMARY = HexColor("#1a1a2e")
ACCENT = HexColor("#16213e")
HIGHLIGHT = HexColor("#0f3460")
LIGHT_BLUE = HexColor("#e8f4f8")
LIGHT_GRAY = HexColor("#f5f5f5")
MID_GRAY = HexColor("#888888")
DARK_GRAY = HexColor("#333333")
SUCCESS = HexColor("#28a745")
WARNING = HexColor("#ffc107")
CODE_BG = HexColor("#1e1e2e")
CODE_FG = HexColor("#cdd6f4")


class ColoredRect(Flowable):
    """A colored rectangle with text - used for section headers."""
    def __init__(self, text: str, width: float, bg_color=PRIMARY, text_color=colors.white, height: float = 30):
        super().__init__()
        self.text = text
        self._width = width
        self.bg_color = bg_color
        self.text_color = text_color
        self._height = height

    def draw(self):
        self.canv.setFillColor(self.bg_color)
        self.canv.roundRect(0, 0, self._width, self._height, 4, fill=1, stroke=0)
        self.canv.setFillColor(self.text_color)
        self.canv.setFont("Helvetica-Bold", 12)
        self.canv.drawString(12, 9, self.text)

    def wrap(self, *args):
        return self._width, self._height


def _make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles = {}

    styles["title"] = ParagraphStyle(
        "title", parent=base["Normal"],
        fontSize=28, fontName="Helvetica-Bold",
        textColor=colors.white, alignment=TA_CENTER,
        spaceAfter=6,
    )
    styles["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["Normal"],
        fontSize=14, fontName="Helvetica",
        textColor=HexColor("#aaccee"), alignment=TA_CENTER,
        spaceAfter=4,
    )
    styles["h1"] = ParagraphStyle(
        "h1", parent=base["Normal"],
        fontSize=18, fontName="Helvetica-Bold",
        textColor=PRIMARY, spaceBefore=16, spaceAfter=8,
    )
    styles["h2"] = ParagraphStyle(
        "h2", parent=base["Normal"],
        fontSize=13, fontName="Helvetica-Bold",
        textColor=HIGHLIGHT, spaceBefore=12, spaceAfter=6,
    )
    styles["body"] = ParagraphStyle(
        "body", parent=base["Normal"],
        fontSize=10, fontName="Helvetica",
        textColor=DARK_GRAY, leading=15, spaceAfter=6,
        alignment=TA_JUSTIFY,
    )
    styles["bullet"] = ParagraphStyle(
        "bullet", parent=base["Normal"],
        fontSize=10, fontName="Helvetica",
        textColor=DARK_GRAY, leading=14, spaceAfter=3,
        leftIndent=16, bulletIndent=4,
    )
    styles["code"] = ParagraphStyle(
        "code", parent=base["Normal"],
        fontSize=8, fontName="Courier",
        textColor=HexColor("#cccccc"),
        backColor=CODE_BG,
        leading=12, spaceAfter=2,
        leftIndent=8, rightIndent=8,
    )
    styles["tag"] = ParagraphStyle(
        "tag", parent=base["Normal"],
        fontSize=9, fontName="Helvetica-Bold",
        textColor=colors.white, backColor=HIGHLIGHT,
        leading=12, spaceAfter=3, alignment=TA_CENTER,
    )
    styles["caption"] = ParagraphStyle(
        "caption", parent=base["Normal"],
        fontSize=9, fontName="Helvetica-Oblique",
        textColor=MID_GRAY, alignment=TA_CENTER, spaceAfter=8,
    )
    styles["small"] = ParagraphStyle(
        "small", parent=base["Normal"],
        fontSize=8, fontName="Helvetica",
        textColor=MID_GRAY, spaceAfter=2,
    )
    return styles


def _bullet(text: str, style) -> Paragraph:
    return Paragraph(f"• {text}", style)


def _badge_table(items: list[str], page_width: float) -> Table | None:
    if not items:
        return None
    # Arrange in rows of 4
    row_size = 4
    rows = []
    row = []
    for item in items:
        row.append(Paragraph(f" {item} ", ParagraphStyle(
            "badge", fontSize=9, fontName="Helvetica-Bold",
            textColor=colors.white, backColor=HIGHLIGHT,
            leading=12, alignment=TA_CENTER,
        )))
        if len(row) == row_size:
            rows.append(row)
            row = []
    if row:
        # Pad with empty cells
        while len(row) < row_size:
            row.append(Paragraph("", ParagraphStyle("empty", fontSize=9)))
        rows.append(row)

    col_w = page_width / row_size
    t = Table(rows, colWidths=[col_w] * row_size, rowHeights=20)
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [4]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_GRAY]),
    ]))
    return t


def _diagram_image(mermaid_code: str, title: str, page_width: float) -> list:
    """Return flowable list for a diagram (image or fallback SVG text)."""
    elements = []
    if not mermaid_code:
        return elements

    png_bytes = mermaid_to_png_bytes(mermaid_code)
    if png_bytes:
        img = Image(io.BytesIO(png_bytes), width=page_width, height=page_width * 0.55)
        elements.append(img)
    else:
        # Fallback: show the mermaid code in a styled code block
        elements.append(Paragraph(f"[{title} - diagram code]", ParagraphStyle(
            "fb", fontSize=9, fontName="Helvetica-Oblique", textColor=MID_GRAY, spaceAfter=4,
        )))
        for line in mermaid_code.split("\n")[:25]:
            if line.strip():
                elements.append(Paragraph(
                    line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
                    ParagraphStyle("fc", fontSize=8, fontName="Courier",
                                   textColor=HexColor("#333"), leading=11, leftIndent=10),
                ))
    return elements


def _cover_page(story: list, repo_name: str, scan: RepoScan, result: AnalysisResult,
                provider_name: str, styles: dict, page_width: float, page_height: float) -> None:
    """Add a styled cover page."""
    from reportlab.platypus.flowables import Flowable

    class CoverBackground(Flowable):
        def __init__(self, w, h):
            super().__init__()
            self._w = w
            self._h = h

        def draw(self):
            c = self.canv
            # Dark gradient background
            c.setFillColor(PRIMARY)
            c.rect(0, 0, self._w, self._h, fill=1, stroke=0)
            # Accent stripe
            c.setFillColor(HIGHLIGHT)
            c.rect(0, self._h * 0.38, self._w, self._h * 0.28, fill=1, stroke=0)
            # Bottom band
            c.setFillColor(ACCENT)
            c.rect(0, 0, self._w, self._h * 0.12, fill=1, stroke=0)

        def wrap(self, *args):
            return self._w, self._h

    # We'll use a simpler approach with colored table
    today = date.today().strftime("%B %d, %Y")

    lang_str = ", ".join(
        f"{lang} ({n})" for lang, n in
        sorted(scan.languages.items(), key=lambda x: x[1], reverse=True)[:4]
    )

    # Cover header box
    cover_data = [[
        Paragraph(f"<b>{repo_name}</b>", ParagraphStyle(
            "ct", fontSize=32, fontName="Helvetica-Bold",
            textColor=colors.white, alignment=TA_CENTER,
        )),
    ], [
        Paragraph("Repository Analysis Report", ParagraphStyle(
            "cs", fontSize=16, fontName="Helvetica",
            textColor=HexColor("#aaccee"), alignment=TA_CENTER,
        )),
    ]]

    cover_table = Table(cover_data, colWidths=[page_width])
    cover_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARY),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 24),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 24),
        ("LEFTPADDING", (0, 0), (-1, -1), 20),
        ("RIGHTPADDING", (0, 0), (-1, -1), 20),
    ]))
    story.append(cover_table)
    story.append(Spacer(1, 20))

    # Purpose
    if result.purpose:
        story.append(Paragraph(result.purpose, ParagraphStyle(
            "purpose", fontSize=13, fontName="Helvetica-Oblique",
            textColor=HIGHLIGHT, alignment=TA_CENTER, spaceAfter=20,
        )))

    story.append(Spacer(1, 10))

    # Stats table
    stats = [
        ["📁 Files", str(len(scan.files)), "📝 Lines", f"{scan.total_lines:,}"],
        ["🏗 Architecture", result.architecture_style or "N/A", "🔧 Provider", provider_name],
        ["📅 Date", today, "🌐 Languages", lang_str[:30]],
    ]
    stat_table = Table(stats, colWidths=[page_width * 0.15, page_width * 0.35, page_width * 0.15, page_width * 0.35])
    stat_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), HIGHLIGHT),
        ("TEXTCOLOR", (2, 0), (2, -1), HIGHLIGHT),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(stat_table)
    story.append(PageBreak())


def _toc_page(story: list, styles: dict) -> None:
    """Add table of contents."""
    story.append(Paragraph("Table of Contents", styles["h1"]))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY))
    story.append(Spacer(1, 12))

    sections = [
        ("1.", "Executive Summary"),
        ("2.", "Technology Stack"),
        ("3.", "Architecture Overview"),
        ("4.", "System Architecture Diagram"),
        ("5.", "Data Flow Diagram"),
        ("6.", "Component Diagram"),
        ("7.", "Key Components"),
        ("8.", "API Endpoints"),
        ("9.", "Code Quality & Security"),
        ("10.", "File-by-File Analysis"),
        ("11.", "Improvement Recommendations"),
        ("12.", "Repository File Tree"),
    ]

    toc_data = [[
        Paragraph(f"<b>{num}</b>", ParagraphStyle("toc_num", fontSize=11, fontName="Helvetica-Bold", textColor=HIGHLIGHT)),
        Paragraph(section, ParagraphStyle("toc_item", fontSize=11, fontName="Helvetica", textColor=DARK_GRAY)),
    ] for num, section in sections]

    toc_table = Table(toc_data, colWidths=[40, None])
    toc_table.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, HexColor("#dddddd")),
    ]))
    story.append(toc_table)
    story.append(PageBreak())


def generate_pdf(
    output_path: Path,
    scan: RepoScan,
    result: AnalysisResult,
    provider_name: str,
) -> None:
    """Generate the full PDF report."""
    repo_name = scan.root.name
    page_width, page_height = A4
    margin = 1.8 * cm
    content_width = page_width - 2 * margin

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=f"{repo_name} - Repository Analysis",
        author="RepoAnalyzer",
        subject="Code Repository Analysis Report",
    )

    styles = _make_styles()
    story = []

    # ── Cover ─────────────────────────────────────────────────────────────────
    _cover_page(story, repo_name, scan, result, provider_name, styles, content_width, page_height)

    # ── TOC ───────────────────────────────────────────────────────────────────
    _toc_page(story, styles)

    # ── 1. Executive Summary ──────────────────────────────────────────────────
    story.append(ColoredRect("1. Executive Summary", content_width))
    story.append(Spacer(1, 10))
    if result.summary:
        story.append(Paragraph(result.summary, styles["body"]))
    story.append(Spacer(1, 8))

    if result.design_patterns:
        story.append(Paragraph("Design Patterns", styles["h2"]))
        for p in result.design_patterns:
            story.append(_bullet(p, styles["bullet"]))

    if result.testing_approach:
        story.append(Paragraph("Testing Strategy", styles["h2"]))
        story.append(Paragraph(result.testing_approach, styles["body"]))

    if result.deployment_info:
        story.append(Paragraph("Deployment", styles["h2"]))
        story.append(Paragraph(result.deployment_info, styles["body"]))

    if result.entry_points:
        story.append(Paragraph("Entry Points", styles["h2"]))
        for ep in result.entry_points:
            story.append(_bullet(ep, styles["bullet"]))

    story.append(PageBreak())

    # ── 2. Technology Stack ───────────────────────────────────────────────────
    story.append(ColoredRect("2. Technology Stack", content_width))
    story.append(Spacer(1, 12))

    if result.tech_stack:
        story.append(Paragraph("Technologies & Frameworks", styles["h2"]))
        tech_badge = _badge_table(result.tech_stack, content_width)
        if tech_badge:
            story.append(tech_badge)
        story.append(Spacer(1, 10))

    if result.dependencies:
        story.append(Paragraph("External Dependencies", styles["h2"]))
        for dep in result.dependencies:
            story.append(_bullet(dep, styles["bullet"]))

    # Language breakdown table
    story.append(Spacer(1, 10))
    story.append(Paragraph("Language Distribution", styles["h2"]))
    if scan.languages:
        lang_sorted = sorted(scan.languages.items(), key=lambda x: x[1], reverse=True)
        lang_data = [["Language", "Files", "Share"]]
        total = sum(n for _, n in lang_sorted)
        for lang, n in lang_sorted:
            pct = f"{n/total*100:.1f}%"
            lang_data.append([lang, str(n), pct])
        lang_table = Table(lang_data, colWidths=[content_width * 0.5, content_width * 0.25, content_width * 0.25])
        lang_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
            ("PADDING", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ]))
        story.append(lang_table)

    story.append(PageBreak())

    # ── 3. Architecture Overview ──────────────────────────────────────────────
    story.append(ColoredRect("3. Architecture Overview", content_width))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Architecture Style: <b>{result.architecture_style}</b>", styles["body"]))
    story.append(Spacer(1, 8))

    if result.data_flow:
        story.append(Paragraph("Data Flow", styles["h2"]))
        for i, step in enumerate(result.data_flow, 1):
            story.append(Paragraph(f"<b>{i}.</b> {step}", styles["bullet"]))

    if result.database_models:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Database Models / Entities", styles["h2"]))
        for model in result.database_models:
            story.append(_bullet(model, styles["bullet"]))

    story.append(PageBreak())

    # ── 4. Architecture Diagram ───────────────────────────────────────────────
    story.append(ColoredRect("4. System Architecture Diagram", content_width))
    story.append(Spacer(1, 10))
    story.append(Paragraph("High-level system components and their relationships.", styles["body"]))
    story.append(Spacer(1, 10))
    arch_elems = _diagram_image(result.architecture_diagram_mermaid, "Architecture", content_width)
    story.extend(arch_elems)
    if result.architecture_diagram_mermaid and not arch_elems:
        story.append(Paragraph("Diagram could not be rendered.", styles["small"]))
    story.append(PageBreak())

    # ── 5. Flow Diagram ───────────────────────────────────────────────────────
    story.append(ColoredRect("5. Data Flow Diagram", content_width))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Main request / data flow through the system.", styles["body"]))
    story.append(Spacer(1, 10))
    flow_elems = _diagram_image(result.flow_diagram_mermaid, "Data Flow", content_width)
    story.extend(flow_elems)
    story.append(PageBreak())

    # ── 6. Component Diagram ──────────────────────────────────────────────────
    story.append(ColoredRect("6. Component Diagram", content_width))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Module dependencies and internal structure.", styles["body"]))
    story.append(Spacer(1, 10))
    comp_elems = _diagram_image(result.component_diagram_mermaid, "Components", content_width)
    story.extend(comp_elems)
    story.append(PageBreak())

    # ── 7. Key Components ─────────────────────────────────────────────────────
    story.append(ColoredRect("7. Key Components", content_width))
    story.append(Spacer(1, 10))

    for comp in result.key_components:
        name = comp.get("name", "Unknown")
        desc = comp.get("description", "")
        files = comp.get("files", [])
        responsibilities = comp.get("responsibilities", [])
        depends_on = comp.get("depends_on", [])

        story.append(KeepTogether([
            Paragraph(f"<b>{name}</b>", ParagraphStyle(
                "comp_name", fontSize=12, fontName="Helvetica-Bold",
                textColor=HIGHLIGHT, spaceBefore=10, spaceAfter=4,
            )),
            Paragraph(desc, styles["body"]) if desc else Spacer(1, 1),
        ]))

        if responsibilities:
            for r in responsibilities:
                story.append(_bullet(r, styles["bullet"]))

        if files:
            story.append(Paragraph(
                "Files: " + ", ".join(f"<font name='Courier' size='9'>{f}</font>" for f in files),
                styles["small"],
            ))
        if depends_on:
            story.append(Paragraph(
                "Depends on: " + ", ".join(f"<b>{d}</b>" for d in depends_on),
                styles["small"],
            ))
        story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#dddddd")))

    story.append(PageBreak())

    # ── 8. API Endpoints ──────────────────────────────────────────────────────
    if result.api_endpoints:
        story.append(ColoredRect("8. API Endpoints", content_width))
        story.append(Spacer(1, 10))

        ep_data = [["Method", "Path", "Description"]]
        method_colors = {
            "GET": HexColor("#28a745"), "POST": HexColor("#007bff"),
            "PUT": HexColor("#ffc107"), "DELETE": HexColor("#dc3545"),
            "PATCH": HexColor("#6f42c1"),
        }
        for ep in result.api_endpoints:
            method = ep.get("method", "").upper()
            path = ep.get("path", "")
            desc = ep.get("description", "")
            ep_data.append([
                Paragraph(f"<b>{method}</b>", ParagraphStyle(
                    "method", fontSize=9, fontName="Helvetica-Bold",
                    textColor=method_colors.get(method, DARK_GRAY), alignment=TA_CENTER,
                )),
                Paragraph(f"<font name='Courier' size='9'>{path}</font>", styles["small"]),
                Paragraph(desc, styles["small"]),
            ])

        ep_table = Table(ep_data, colWidths=[content_width * 0.12, content_width * 0.38, content_width * 0.5])
        ep_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
            ("PADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(ep_table)
        story.append(PageBreak())

    # ── 9. Code Quality & Security ────────────────────────────────────────────
    story.append(ColoredRect("9. Code Quality & Security", content_width))
    story.append(Spacer(1, 10))

    if result.code_quality_notes:
        story.append(Paragraph("Code Quality Observations", styles["h2"]))
        for note in result.code_quality_notes:
            story.append(_bullet(note, styles["bullet"]))

    if result.security_notes:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Security Considerations", styles["h2"]))
        for note in result.security_notes:
            story.append(_bullet(note, styles["bullet"]))

    story.append(PageBreak())

    # ── 10. File-by-File Analysis ─────────────────────────────────────────────
    story.append(ColoredRect("10. File-by-File Analysis", content_width))
    story.append(Spacer(1, 10))

    complexity_colors = {"low": SUCCESS, "medium": WARNING, "high": colors.red}

    for fd in result.file_details[:40]:
        file_path = fd.get("file", "")
        purpose = fd.get("purpose", "")
        key_classes = fd.get("key_classes", [])
        key_functions = fd.get("key_functions", [])
        complexity = fd.get("complexity", "").lower()
        notes = fd.get("notes", "")

        comp_color = complexity_colors.get(complexity, MID_GRAY)

        row = [[
            Paragraph(
                f"<font name='Courier' size='9'>{file_path}</font>",
                ParagraphStyle("fp", fontSize=9, fontName="Courier", textColor=HIGHLIGHT),
            ),
            Paragraph(
                f"<b>{complexity.upper()}</b>" if complexity else "",
                ParagraphStyle("cx", fontSize=8, fontName="Helvetica-Bold",
                               textColor=comp_color, alignment=TA_CENTER),
            ),
        ]]
        header_t = Table(row, colWidths=[content_width * 0.85, content_width * 0.15])
        header_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
            ("PADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))

        detail_items = [header_t]
        if purpose:
            detail_items.append(Paragraph(purpose, styles["body"]))
        if key_classes:
            detail_items.append(Paragraph(
                "Classes: " + ", ".join(f"<b>{c}</b>" for c in key_classes[:5]),
                styles["small"],
            ))
        if key_functions:
            detail_items.append(Paragraph(
                "Functions: " + ", ".join(f"<font name='Courier' size='8'>{f}</font>" for f in key_functions[:6]),
                styles["small"],
            ))
        if notes:
            detail_items.append(Paragraph(f"<i>{notes}</i>", styles["small"]))
        detail_items.append(Spacer(1, 6))

        story.append(KeepTogether(detail_items))

    story.append(PageBreak())

    # ── 11. Improvement Recommendations ──────────────────────────────────────
    story.append(ColoredRect("11. Improvement Recommendations", content_width))
    story.append(Spacer(1, 10))

    if result.improvement_suggestions:
        for i, suggestion in enumerate(result.improvement_suggestions, 1):
            story.append(KeepTogether([
                Paragraph(
                    f"<b>{i}.</b> {suggestion}",
                    ParagraphStyle("sugg", fontSize=10, fontName="Helvetica",
                                   textColor=DARK_GRAY, leading=15, spaceAfter=8,
                                   leftIndent=10),
                ),
            ]))
    else:
        story.append(Paragraph("No specific improvements identified.", styles["body"]))

    story.append(PageBreak())

    # ── 12. Repository File Tree ──────────────────────────────────────────────
    story.append(ColoredRect("12. Repository File Tree", content_width))
    story.append(Spacer(1, 10))

    tree_lines = scan.directory_tree.split("\n")
    for line in tree_lines[:80]:
        story.append(Paragraph(
            line.replace(" ", "&nbsp;").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            ParagraphStyle("tree", fontSize=8, fontName="Courier",
                           textColor=HexColor("#333"), leading=11),
        ))

    if len(tree_lines) > 80:
        story.append(Paragraph(f"... [{len(tree_lines) - 80} more lines]", styles["small"]))

    # ── Agent Trace (agentic metadata) ─────────────────────────────────────────
    story.append(ColoredRect("Appendix: Agentic Analysis Trace", content_width, bg_color=ACCENT))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"This report was generated by an AI agent that autonomously explored the repository, "
        f"decided which files to read, and recorded findings — rather than following a fixed script.",
        styles["body"],
    ))
    story.append(Spacer(1, 8))

    meta_data = [
        ["Provider", result.provider_name or "unknown"],
        ["Total Agent Steps", str(result.total_steps)],
        ["Total Tool Calls", str(result.total_tool_calls)],
        ["Analysis Time", f"{result.elapsed_seconds:.1f}s"],
        ["Files Scanned", str(len(scan.files))],
        ["Findings Recorded", str(len(scan.files))],
    ]
    meta_table = Table(meta_data, colWidths=[content_width * 0.35, content_width * 0.65])
    meta_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [LIGHT_BLUE, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dddddd")),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (0, -1), HIGHLIGHT),
    ]))
    story.append(meta_table)

    
        # ── Build ─────────────────────────────────────────────────────────────────
    def on_page(canvas, doc):
        canvas.saveState()
        # Footer
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID_GRAY)
        canvas.drawString(margin, 0.8 * cm, f"RepoAnalyzer | {repo_name}")
        canvas.drawRightString(page_width - margin, 0.8 * cm, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
