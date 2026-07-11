"""
Synthesis Agent (agents-as-tools)

Cross-references the per-repo analysis outputs, derives the sections no TD
produces (cost/performance benefit, as directional qualitative estimates
only), and assembles the 8-section due-diligence DOCX via build_docx.py.
"""

import os
import json
import glob
import logging
import boto3
from typing import Any, Dict
from datetime import datetime

from strands import tool

from .assessmenttransform import _extract_params, WORKSPACE_ROOT
from .build_docx import build_docx

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_FINDINGS_CHARS = 40000  # per-repo cap fed into the synthesis prompt

SYNTHESIS_SCHEMA_PROMPT = """You are compiling a technical due-diligence report for a
prospective AWS migration client, based on the raw analysis findings below. Produce ONLY
valid JSON (no markdown fences) with this shape:

{
  "client_name": "string, from request context or empty string",
  "sections": {
    "2": "As-Is Code Quality & Recommendations - narrative prose, cite specific findings",
    "3": "Application Code Due Diligence - narrative prose, cite specific findings",
    "6": "Recommended AWS Service Usage - narrative prose, cite specific service recommendations found",
    "7": "Cost Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a formal cost model or priced TCO analysis.' Do not state specific dollar figures.",
    "8": "Performance Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a benchmark or load-tested projection.' Do not state specific throughput/latency numbers."
  }
}

Do NOT include sections 1, 4, or 5 - those are handled separately as "not covered". Base
every claim strictly on the findings provided; do not invent detail not present in them. If
a section's findings are too thin to write meaningfully, say so explicitly."""


def _read_findings(output_dir: str) -> str:
    """Concatenate an analysis TD's output files into one capped text blob."""
    if not output_dir or not os.path.isdir(output_dir):
        return ""
    chunks = []
    for pattern in ("**/*.md", "**/*.json"):
        for path in glob.glob(os.path.join(output_dir, pattern), recursive=True):
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        rel = os.path.relpath(path, output_dir)
                        chunks.append(f"--- {rel} ---\n{f.read()}")
                except Exception:
                    continue
    return "\n\n".join(chunks)[:MAX_FINDINGS_CHARS]


def _synthesize_sections(findings_by_repo: Dict[str, Dict[str, str]], context: str) -> Dict[str, Any]:
    bedrock_rt = boto3.client('bedrock-runtime', region_name=os.getenv("AWS_REGION", "us-east-1"))
    findings_text = ""
    for repo, findings in findings_by_repo.items():
        findings_text += f"\n\n=== Repository: {repo} ===\n"
        for label, text in findings.items():
            findings_text += f"\n[{label}]\n{text or '(no output captured)'}\n"

    prompt = f"{SYNTHESIS_SCHEMA_PROMPT}\n\nEngagement context: {context}\n\nFindings:{findings_text}"
    response = bedrock_rt.invoke_model(
        modelId=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8192, "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}]
        })
    )
    raw_text = json.loads(response['body'].read())['content'][0]['text'].strip()
    if '```' in raw_text:
        raw_text = raw_text.split('```')[1]
        if raw_text.startswith('json'):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    return json.loads(raw_text)


@tool
def generate_assessment_report(query: str) -> Dict[str, Any]:
    """
    Cross-references completed analysis results for one or more repositories and
    produces the 8-section due-diligence DOCX. Call this AFTER codebase_analysis_agent,
    modernization_readiness_agent, and business_rules_agent have all returned success.

    Args:
        query: Natural language naming each repo and the 3 output_dir paths its analyses
            returned, plus any client context, e.g. "Generate the report for repo
            'acme-api': codebase output at /tmp/atx-assessments/codebase-acme-api-.../repo,
            readiness output at .../readiness-acme-api-.../repo, business rules output at
            .../bizrules-acme-api-.../repo. Client: Acme Corp, industry: healthcare."
    """
    logger.info("SYNTHESIS/REPORT AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"repos": [{"name": "repo-name", "codebase_output_dir": "", "readiness_output_dir": "", "business_rules_output_dir": ""}],
 "client_name": "", "context": "industry/compliance/other free-text engagement context"}""")
        repos = params.get('repos', [])
        if not repos:
            return {"status": "error", "error": "Could not extract repo/output_dir info from the request."}

        findings_by_repo = {}
        for r in repos:
            name = r.get('name', 'repo')
            findings_by_repo[name] = {
                "Comprehensive Codebase Analysis": _read_findings(r.get('codebase_output_dir', '')),
                "Modernization Readiness Analysis": _read_findings(r.get('readiness_output_dir', '')),
                "Business Rules Extraction": _read_findings(r.get('business_rules_output_dir', '')),
            }

        sections_result = _synthesize_sections(findings_by_repo, params.get('context', ''))
        sections_result['repos'] = list(findings_by_repo.keys())
        if params.get('client_name'):
            sections_result['client_name'] = params['client_name']

        job_name = f"report-{'-'.join(findings_by_repo.keys())[:40]}-{int(datetime.utcnow().timestamp())}"
        output_path = os.path.join(WORKSPACE_ROOT, "reports", f"{job_name}.docx")
        build_docx(sections_result, output_path)

        return {"status": "success", "result": json.dumps({
            "report_path": output_path,
            "client_name": sections_result.get('client_name', ''),
            "repos": sections_result['repos'],
        })}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Failed to parse synthesis output: {e}"}
    except Exception as e:
        logger.error(f"generate_assessment_report failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
