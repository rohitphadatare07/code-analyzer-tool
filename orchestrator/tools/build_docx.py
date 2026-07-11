"""
DOCX Report Assembly

Renders the 8-section technical due-diligence report from a report_content
dict into a .docx file. Sections with no v1 data source (infrastructure due
diligence, vulnerabilities/license, consolidated to-be architecture) are
rendered as an explicit "not covered" placeholder - never fabricated.
"""

import os
from docx import Document

SECTION_TITLES = [
    "Infrastructure Due Diligence",
    "As-Is Code Quality & Recommendations",
    "Application Code Due Diligence",
    "Vulnerabilities & License Compliance",
    "Consolidated To-Be Architecture",
    "Recommended AWS Service Usage",
    "Cost Benefit",
    "Performance Benefit",
]

# 1-indexed section numbers with no v1 data source.
NOT_COVERED_SECTIONS = {1, 4, 5}

NOT_COVERED_NOTE = (
    "Not covered in this engagement. This section requires tooling not yet wired into "
    "the v1 assessment platform (AWS Migration Evaluator / discovery for infrastructure "
    "due diligence, Sonar/BlackDuck for vulnerabilities and license compliance, and a "
    "cross-repo portfolio analysis for consolidated to-be architecture)."
)


def build_docx(report_content: dict, output_path: str) -> str:
    """
    report_content: {
        "client_name": str,
        "repos": [str, ...],
        "sections": {"2": "...", "3": "...", "6": "...", "7": "...", "8": "..."}
    }
    Sections 1/4/5 are always rendered via NOT_COVERED_NOTE regardless of input.
    Returns output_path.
    """
    doc = Document()

    doc.add_heading('Technical Due Diligence Report', level=0)
    if report_content.get('client_name'):
        doc.add_paragraph(report_content['client_name'])
    if report_content.get('repos'):
        doc.add_paragraph('Repositories assessed: ' + ', '.join(report_content['repos']))

    sections = report_content.get('sections', {})

    for i, heading in enumerate(SECTION_TITLES, start=1):
        doc.add_heading(f"{i}. {heading}", level=1)
        if i in NOT_COVERED_SECTIONS:
            p = doc.add_paragraph()
            run = p.add_run(NOT_COVERED_NOTE)
            run.italic = True
            continue
        content = sections.get(str(i)) or sections.get(i)
        if not content:
            doc.add_paragraph("No content available for this section.")
            continue
        for para in str(content).split('\n\n'):
            if para.strip():
                doc.add_paragraph(para.strip())

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    doc.save(output_path)
    return output_path
