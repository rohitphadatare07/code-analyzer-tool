"""
Synthesis Agent (agents-as-tools)

Cross-references the per-repo analysis outputs, derives the sections no TD
produces (migration roadmap, risks & mitigations, cost/performance benefit -
the latter two as directional qualitative estimates only), and assembles the
Executive Summary + 10-section due-diligence DOCX via build_docx.py.

Drafted as TWO separate agent calls, not one, so groundedness is structurally
checkable (Tier 2) rather than just prompted-for:

1. Codebase-grounded call - sections 1 (architecture), 2 (business logic),
   4 (modernization readiness). Given ZERO tools (not just told not to use
   them - it structurally cannot call anything). Every claim must carry a
   [[finding:<Source Label>]] tag naming which TD it came from.
2. Strategy call - sections 6 (AWS services), 7 (roadmap), 8 (cost benefit),
   9 (performance benefit), 10 (risks), + the executive summary. Given the
   AWS Documentation MCP server's tools (search_documentation,
   read_documentation, recommend). Claims must carry [[finding:...]] or
   [[doc:<url actually retrieved this run>]].

Citation tags are verified mechanically (grounding.py): a finding citation
must name a real TD; a doc citation must match a URL that actually appears
in that call's tool-call trace - not just one the model claims exists. Tags
are stripped before rendering; cited AWS doc URLs surface as a "Sources"
appendix in the DOCX instead.

Requires `uvx` (from the `uv` package, in requirements.txt) on PATH to launch
`awslabs.aws-documentation-mcp-server`, plus outbound internet access to
docs.aws.amazon.com. If the MCP server fails to start, the strategy call
falls back to an ungrounded pass rather than failing the whole report.
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
from .grounding import extract_tool_trace, verify_citations, strip_citations, strip_citations_collect_sources

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_FINDINGS_CHARS = 40000  # per-repo cap fed into the synthesis prompt

FINDING_LABELS = {
    "Comprehensive Codebase Analysis",
    "Modernization Readiness Analysis",
    "Business Rules Extraction",
}

# Official AWS Documentation MCP server (awslabs). Launched on-demand via uvx,
# one process per generate_assessment_report call.
_aws_docs_mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])
))

_CITATION_RULES = """Every factual sentence must carry exactly one inline citation tag,
placed right after the sentence:
- [[finding:<Source Label>]] for a claim taken from the analysis findings, where
  <Source Label> is EXACTLY one of: "Comprehensive Codebase Analysis",
  "Modernization Readiness Analysis", "Business Rules Extraction".
- [[doc:<url>]] for a claim taken from an AWS Documentation tool result, where <url>
  is the EXACT url you retrieved via search_documentation/read_documentation this
  turn - never a url you did not actually look up.
Do not write uncited factual sentences. Framing/transition sentences don't need tags."""

CODEBASE_SECTIONS_PROMPT = f"""You are drafting the codebase-facing sections of a technical
due-diligence report for a prospective AWS migration client, based on the raw analysis
findings the user provides. You have NO tools - base everything strictly on the findings
given to you. Never mention AWS services, migration targets, or external best practices
here; that belongs in a later section you are not drafting.

{_CITATION_RULES}
(You have no tools, so every citation here must be a [[finding:...]] tag - never [[doc:...]].)

Produce ONLY valid JSON as your final answer (no markdown fences, no commentary) with this
shape:

{{
  "sections": {{
    "1": "Current Architecture of the Codebase - narrative prose covering architecture, tech stack/versions, code quality, and tech debt.",
    "2": "Business Logic & Domain Understanding - narrative prose covering key business rules, data model, and workflows extracted.",
    "4": "Modernization Readiness - narrative prose on cloud-native maturity and specific anti-patterns blocking cloud adoption."
  }}
}}

If a section's findings are too thin to write meaningfully, say so explicitly rather than
fabricating detail."""

STRATEGY_SECTIONS_PROMPT = f"""You are drafting the AWS-strategy sections of a technical
due-diligence report for a prospective AWS migration client. You are given the raw analysis
findings, the codebase-facing sections already drafted by a colleague, and engagement
context. You have AWS Documentation tools (search_documentation, read_documentation,
recommend) - use them whenever grounding a recommendation in official AWS guidance
(Well-Architected Framework pillars, specific service capabilities/limits, migration
patterns) would strengthen it. Look up a service before recommending it.

{_CITATION_RULES}

Produce ONLY valid JSON as your final answer (no markdown fences, no commentary) with this
shape:

{{
  "client_name": "string, from request context or empty string",
  "sections": {{
    "6": "Recommended AWS Service Usage - narrative prose citing the SPECIFIC AWS services recommended per component/pattern found in the findings.",
    "7": "Migration Roadmap - a phased plan (e.g. quick wins vs longer-term work) and sequencing/dependencies between components, informed by AWS migration best practices. Directional only - no fixed dates or durations, since no formal estimation was performed.",
    "8": "Cost Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a formal cost model or priced TCO analysis.' Do not state specific dollar figures.",
    "9": "Performance & Reliability Benefit - directional/qualitative ONLY. Open with exactly: 'Note: this is a directional estimate based on codebase analysis findings, not a benchmark or load-tested projection.' Do not state specific throughput/latency numbers.",
    "10": "Risks & Mitigations - specific migration risks visible in the findings paired with a mitigation for each, informed by AWS guidance where relevant."
  }},
  "executive_summary": "3-5 sentences for a CTO/VP audience: current-state pain points, recommended direction, headline benefits. Do NOT substantively summarize sections 3 (Security & Compliance) or 5 (Recommended To-Be Architecture) - if mentioned at all, note only that they are recommended as a follow-up phase, since no data exists for them in this engagement. No citation tag needed on the executive summary itself."
}}

Sections 3 (Security & Compliance Findings) and 5 (Recommended To-Be Architecture) are handled
separately as "not covered" - do NOT include them. Base every claim strictly on the findings
or documentation you actually looked up; do not invent detail not present in either."""


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


def _make_bedrock_model() -> BedrockModel:
    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    return BedrockModel(model_id=model_id, region_name=region, temperature=0.3, max_tokens=8192)


def _synthesize_sections(findings_by_repo: Dict[str, Dict[str, str]], context: str) -> Dict[str, Any]:
    findings_text = ""
    for repo, findings in findings_by_repo.items():
        findings_text += f"\n\n=== Repository: {repo} ===\n"
        for label, text in findings.items():
            findings_text += f"\n[{label}]\n{text or '(no output captured)'}\n"

    # --- Call 1: codebase-grounded sections, structurally NO tools ---
    codebase_agent = Agent(model=_make_bedrock_model(), system_prompt=CODEBASE_SECTIONS_PROMPT, tools=[])
    codebase_result = codebase_agent(f"Findings:{findings_text}")
    codebase_text = _extract_agent_text(codebase_result)
    codebase_json = _parse_json_response(codebase_text)
    codebase_trace = extract_tool_trace(codebase_agent)  # expected to always be empty

    # --- Call 2: AWS-strategy sections, WITH MCP tools ---
    strategy_prompt = (
        f"Engagement context: {context}\n\nFindings:{findings_text}\n\n"
        f"Already-drafted codebase-facing sections (for context, do not re-cite these as "
        f"[[doc:...]]):{json.dumps(codebase_json)}"
    )
    try:
        with _aws_docs_mcp_client:
            mcp_tools = _aws_docs_mcp_client.list_tools_sync()
            strategy_agent = Agent(model=_make_bedrock_model(), system_prompt=STRATEGY_SECTIONS_PROMPT, tools=mcp_tools)
            strategy_result = strategy_agent(strategy_prompt)
            strategy_trace = extract_tool_trace(strategy_agent)
    except Exception as e:
        logger.warning(f"AWS Documentation MCP server unavailable, falling back to ungrounded synthesis: {e}")
        strategy_agent = Agent(model=_make_bedrock_model(), system_prompt=STRATEGY_SECTIONS_PROMPT, tools=[])
        strategy_result = strategy_agent(strategy_prompt)
        strategy_trace = []

    strategy_text = _extract_agent_text(strategy_result)
    strategy_json = _parse_json_response(strategy_text)

    # --- Groundedness verification (Tier 1 mechanics, Tier 2 audit trail) ---
    codebase_ground = verify_citations(json.dumps(codebase_json), FINDING_LABELS, codebase_trace)
    strategy_ground = verify_citations(json.dumps(strategy_json), FINDING_LABELS, strategy_trace)

    # --- Strip citation tags for display; collect cited AWS doc URLs ---
    merged_sections = {**codebase_json.get('sections', {}), **strategy_json.get('sections', {})}
    clean_sections = {}
    all_doc_urls = set()
    for key, text in merged_sections.items():
        clean_text, doc_urls = strip_citations_collect_sources(str(text))
        clean_sections[key] = clean_text
        all_doc_urls.update(doc_urls)

    return {
        "client_name": strategy_json.get("client_name", ""),
        "sections": clean_sections,
        "executive_summary": strip_citations(strategy_json.get("executive_summary", "")),
        "sources": sorted(all_doc_urls),
        "groundedness": {
            "codebase_sections_citations": codebase_ground["total"],
            "codebase_sections_unverified": codebase_ground["unverified"],
            "strategy_sections_citations": strategy_ground["total"],
            "strategy_sections_unverified": strategy_ground["unverified"],
            "aws_doc_tool_calls_made": len(strategy_trace),
        },
    }


@tool
def generate_assessment_report(query: str) -> Dict[str, Any]:
    """
    Cross-references completed analysis results for one or more repositories and
    produces the due-diligence DOCX (Executive Summary + 10 sections). Call this AFTER
    codebase_analysis_agent, modernization_readiness_agent, and business_rules_agent have
    all returned success. Returns a groundedness summary alongside the report path - if it
    lists any unverified citations, flag the report for human review before sending it to
    the client.

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

        groundedness = sections_result.pop('groundedness')
        if groundedness['codebase_sections_unverified'] or groundedness['strategy_sections_unverified']:
            logger.warning(f"Unverified citations in generated report: {groundedness}")

        job_name = f"report-{'-'.join(findings_by_repo.keys())[:40]}-{int(datetime.utcnow().timestamp())}"
        output_path = os.path.join(WORKSPACE_ROOT, "reports", f"{job_name}.docx")
        build_docx(sections_result, output_path)

        return {"status": "success", "result": json.dumps({
            "report_path": output_path,
            "client_name": sections_result.get('client_name', ''),
            "repos": sections_result['repos'],
            "groundedness": groundedness,
        })}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Failed to parse synthesis output: {e}"}
    except Exception as e:
        logger.error(f"generate_assessment_report failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
