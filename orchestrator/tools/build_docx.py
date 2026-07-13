"""
DOCX Report Assembly

Renders the technical due-diligence report from a report_content dict into a
.docx file: an Executive Summary followed by 10 numbered sections. Sections
with no v1 data source (security & compliance findings, consolidated to-be
architecture) are rendered as an explicit "not covered" placeholder - never
fabricated.
"""

import os
from docx import Document

SECTION_TITLES = [
    "Current Architecture of the Codebase",
    "Business Logic & Domain Understanding",
    "Security & Compliance Findings",
    "Modernization Readiness",
    "Recommended To-Be Architecture",
    "Recommended AWS Service Usage",
    "Migration Roadmap",
    "Cost Benefit",
    "Performance & Reliability Benefit",
    "Risks & Mitigations",
]

# 1-indexed section numbers with no v1 data source.
NOT_COVERED_SECTIONS = {3, 5}

NOT_COVERED_NOTES = {
    3: (
        "Not covered in this engagement. Security vulnerability and license-compliance "
        "scanning (Sonar / BlackDuck integration) is not yet wired into the v1 assessment "
        "platform."
    ),
    5: (
        "Not covered in this engagement. A consolidated to-be architecture requires "
        "cross-repository portfolio analysis, which is not yet wired into the v1 "
        "assessment platform."
    ),
}


def build_docx(report_content: dict, output_path: str) -> str:
    """
    report_content: {
        "client_name": str,
        "repos": [str, ...],
        "executive_summary": "...",
        "sections": {"1": "...", "2": "...", "4": "...", "6": "...", "7": "...",
                     "8": "...", "9": "...", "10": "..."},
        "sources": ["https://docs.aws.amazon.com/...", ...]  # optional
    }
    Sections 3/5 are always rendered via NOT_COVERED_NOTES regardless of input.
    "sources", if present, is rendered as a final appendix - these are AWS
    documentation URLs the synthesis agent actually retrieved and cited while
    drafting sections 6-10 (see grounding.py), not a general reading list.
    Returns output_path.
    """
    doc = Document()

    doc.add_heading('Technical Due Diligence Report', level=0)
    if report_content.get('client_name'):
        doc.add_paragraph(report_content['client_name'])
    if report_content.get('repos'):
        doc.add_paragraph('Repositories assessed: ' + ', '.join(report_content['repos']))

    if report_content.get('executive_summary'):
        doc.add_heading('Executive Summary', level=1)
        for para in str(report_content['executive_summary']).split('\n\n'):
            if para.strip():
                doc.add_paragraph(para.strip())

    sections = report_content.get('sections', {})

    for i, heading in enumerate(SECTION_TITLES, start=1):
        doc.add_heading(f"{i}. {heading}", level=1)
        if i in NOT_COVERED_SECTIONS:
            p = doc.add_paragraph()
            run = p.add_run(NOT_COVERED_NOTES[i])
            run.italic = True
            continue
        content = sections.get(str(i)) or sections.get(i)
        if not content:
            doc.add_paragraph("No content available for this section.")
            continue
        for para in str(content).split('\n\n'):
            if para.strip():
                doc.add_paragraph(para.strip())

    if report_content.get('sources'):
        doc.add_heading('Sources', level=1)
        doc.add_paragraph(
            'AWS documentation referenced while preparing the recommendations in this report:'
        )
        for url in report_content['sources']:
            doc.add_paragraph(url, style='List Bullet')

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    doc.save(output_path)
    return output_path
