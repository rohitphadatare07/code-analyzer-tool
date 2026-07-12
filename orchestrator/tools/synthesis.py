"""
Synthesis Agent (agents-as-tools)

Cross-references the per-repo analysis outputs, derives the sections no TD
produces (migration roadmap, risks & mitigations, cost/performance benefit -
the latter two as directional qualitative estimates only), and assembles the
Executive Summary + 10-section due-diligence DOCX via build_docx.py.

Sections 6-10 (recommended AWS services, migration roadmap, cost benefit,
performance benefit, risks & mitigations) are drafted by a real Strands Agent
with the AWS Documentation MCP server's tools attached (search_documentation,
read_documentation, recommend), so those recommendations are grounded in
official AWS guidance rather than the model's training knowledge alone.
Sections 1/2/4 describe the client's own codebase and deliberately do NOT use
these tools - they should only ever cite the analysis findings.

Requires `uvx` (from the `uv` package, in requirements.txt) on PATH to launch
`awslabs.aws-documentation-mcp-server`, plus outbound internet access to
docs.aws.amazon.com. If the MCP server fails to start, synthesis falls back
to an ungrounded LLM pass rather than failing the whole report.
"""

import os
import json
import glob
import logging
from typing import Any, Dict
from datetime import datetime

from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp import StdioServerParameters, stdio_client

from .assessmenttransform import _extract_params, WORKSPACE_ROOT
from .build_docx import build_docx

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_FINDINGS_CHARS = 40000  # per-repo cap fed into the synthesis prompt

# Official AWS Documentation MCP server (awslabs). Launched on-demand via uvx,
# one process per generate_assessment_report call (see MCPClient's context-
# manager usage in _synthesize_sections).
_aws_docs_mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])
))

SYNTHESIS_SCHEMA_PROMPT = """You are compiling a technical due-diligence report for a
prospective AWS migration client, based on the raw analysis findings the user provides.

You have access to AWS Documentation tools (search_documentation, read_documentation,
recommend). Use them WHILE DRAFTING sections 6, 7, 8, 9, and 10 below, whenever grounding a
recommendation in official AWS guidance (Well-Architected Framework pillars, specific
service capabilities/limits, migration patterns) would strengthen it - e.g. look up a
service before recommending it, or check best-practice migration guidance for a pattern you
see in the findings. Do NOT use these tools for sections 1, 2, or 4 - those describe the
client's own codebase and must be based solely on the findings provided, never on external
lookups.

Produce ONLY valid JSON as your final answer (no markdown fences, no commentary before or
after it) with this shape:

{
  "client_name": "string, from request context or empty string",
  "sections": {
    "1": "Current Architecture of the Codebase - narrative prose covering architecture, tech stack/versions, code quality, and tech debt. Cite specific findings only.",
    "2": "Business Logic & Domain Understanding - narrative prose covering key business rules, data model, and workflows extracted. Cite specific findings only.",
    "4": "Modernization Readiness - narrative prose on cloud-native maturity and specific anti-patterns blocking cloud adoption. Cite specific findings only.",
    "6": "Recommended AWS Service Usage - narrative prose citing the SPECIFIC AWS services recommended per component/pattern found in the findings, grounded in official AWS documentation where you looked it up.",
    "7": "Migration Roadmap - a phased plan (e.g. quick wins vs longer-term work) and sequencing/dependencies between components, informed by AWS migration best practices. Directional only - no fixed dates or durations, since no formal estimation was performed.",
    "8": "Cost Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a formal cost model or priced TCO analysis.' Do not state specific dollar figures.",
    "9": "Performance & Reliability Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a benchmark or load-tested projection.' Do not state specific throughput/latency numbers.",
    "10": "Risks & Mitigations - specific migration risks visible in the findings (e.g. tightly-coupled legacy dependencies, missing tests, stateful assumptions) paired with a mitigation for each, informed by AWS guidance where relevant."
  },
  "executive_summary": "3-5 sentences for a CTO/VP audience: current-state pain points, recommended direction, headline benefits. Do NOT substantively summarize sections 3 (Security & Compliance) or 5 (Recommended To-Be Architecture) - if mentioned at all, note only that they are recommended as a follow-up phase, since no data exists for them in this engagement."
}

Sections 3 (Security & Compliance Findings) and 5 (Recommended To-Be Architecture) are handled
separately as "not covered" - do NOT include them in the "sections" object. Base every claim
strictly on the findings provided (plus documentation you looked up for sections 6-10); do not
invent detail not present in either. If a section's findings are too thin to write
meaningfully, say so explicitly rather than fabricating detail."""


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


def _extract_agent_text(result: Any) -> str:
    """Pull the final assistant text out of a Strands AgentResult, defensively."""
    if hasattr(result, 'message'):
        msg = result.message
        if isinstance(msg, dict) and isinstance(msg.get('content'), list):
            return "".join(
                block.get('text', '') for block in msg['content'] if isinstance(block, dict)
            ).strip()
        return str(msg).strip()
    if hasattr(result, 'content'):
        return str(result.content).strip()
    return str(result).strip()


def _parse_json_response(raw_text: str) -> Dict[str, Any]:
    raw_text = raw_text.strip()
    if '```' in raw_text:
        raw_text = raw_text.split('```')[1]
        if raw_text.startswith('json'):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    return json.loads(raw_text)


def _run_synthesis_agent(prompt: str, tools: list) -> str:
    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    bedrock_model = BedrockModel(model_id=model_id, region_name=region, temperature=0.3, max_tokens=8192)
    agent = Agent(model=bedrock_model, system_prompt=SYNTHESIS_SCHEMA_PROMPT, tools=tools)
    result = agent(prompt)
    return _extract_agent_text(result)


def _synthesize_sections(findings_by_repo: Dict[str, Dict[str, str]], context: str) -> Dict[str, Any]:
    findings_text = ""
    for repo, findings in findings_by_repo.items():
        findings_text += f"\n\n=== Repository: {repo} ===\n"
        for label, text in findings.items():
            findings_text += f"\n[{label}]\n{text or '(no output captured)'}\n"

    prompt = f"Engagement context: {context}\n\nFindings:{findings_text}"

    try:
        with _aws_docs_mcp_client:
            mcp_tools = _aws_docs_mcp_client.list_tools_sync()
            raw_text = _run_synthesis_agent(prompt, mcp_tools)
    except Exception as e:
        logger.warning(f"AWS Documentation MCP server unavailable, falling back to ungrounded synthesis: {e}")
        raw_text = _run_synthesis_agent(prompt, [])

    return _parse_json_response(raw_text)


@tool
def generate_assessment_report(query: str) -> Dict[str, Any]:
    """
    Cross-references completed analysis results for one or more repositories and
    produces the due-diligence DOCX (Executive Summary + 10 sections). Call this AFTER
    codebase_analysis_agent, modernization_readiness_agent, and business_rules_agent have
    all returned success.

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
