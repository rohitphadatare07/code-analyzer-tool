"""
Groundedness Verification (Tier 1 + Tier 2)

Tier 1 - citation forcing: synthesis prompts require every factual sentence
to carry an inline tag, [[finding:<Source Label>]] for a claim sourced from
an analysis TD's output, or [[doc:<url>]] for a claim sourced from an AWS
Documentation MCP tool call. verify_citations() checks each tag mechanically:
a finding citation must name a real source label; a doc citation must match
a URL that actually appeared in that agent run's tool-call trace.

Tier 2 - tool-call audit trail: extract_tool_trace() walks a Strands Agent's
message history after a run and reconstructs every tool call (name, input,
output), independent of what the model claims it did.

Citation tags are stripped from the final prose before it goes in the DOCX -
they're an internal verification signal, not something a client should see.
Cited AWS doc URLs are instead surfaced as a "Sources" appendix.
"""

import json
import re
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CITATION_RE = re.compile(r'\[\[(finding|doc):([^\]]+)\]\]')


def extract_tool_trace(agent: Any) -> List[Dict[str, Any]]:
    """
    Reconstruct every tool call an Agent made during its last run, from its
    message history. Defensive throughout - if the Strands message shape
    doesn't match what we expect, degrade to an empty trace rather than
    breaking report generation over a diagnostic feature.
    """
    trace: List[Dict[str, Any]] = []
    try:
        messages = getattr(agent, "messages", None) or []
        pending: Dict[str, Dict[str, Any]] = {}
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if "toolUse" in block and isinstance(block["toolUse"], dict):
                    tu = block["toolUse"]
                    pending[tu.get("toolUseId")] = {"name": tu.get("name"), "input": tu.get("input")}
                elif "toolResult" in block and isinstance(block["toolResult"], dict):
                    tr = block["toolResult"]
                    call = pending.get(tr.get("toolUseId"), {})
                    output_text = ""
                    for c in (tr.get("content") or []):
                        if isinstance(c, dict) and isinstance(c.get("text"), str):
                            output_text += c["text"]
                    trace.append({
                        "name": call.get("name", "unknown"),
                        "input": call.get("input", {}),
                        "output": output_text[:2000],
                    })
    except Exception as e:
        logger.warning(f"Failed to extract tool trace (non-fatal): {e}")
        return []
    return trace


def extract_citations(text: str) -> List[Tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for m in CITATION_RE.finditer(text or "")]


def verify_citations(
    text: str, valid_finding_labels: set, tool_trace: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Mechanically check each citation tag in text:
    - [[finding:X]] is verified iff X is one of the known analysis TD labels.
    - [[doc:url]] is verified iff url literally appears somewhere in the
      tool-call trace (an argument passed to a tool, or text a tool returned)
      for THIS agent run - i.e. the model didn't invent a URL it never
      actually retrieved.
    """
    citations = extract_citations(text)
    trace_blob = json.dumps(tool_trace, default=str)
    unverified = []
    for kind, value in citations:
        if kind == "finding":
            if value not in valid_finding_labels:
                unverified.append(f"finding:{value}")
        elif kind == "doc":
            if value not in trace_blob:
                unverified.append(f"doc:{value}")
    return {
        "total": len(citations),
        "verified": len(citations) - len(unverified),
        "unverified": unverified,
    }


def strip_citations(text: str) -> str:
    cleaned = CITATION_RE.sub('', text or '')
    return re.sub(r'[ \t]{2,}', ' ', cleaned).strip()


def strip_citations_collect_sources(text: str) -> Tuple[str, List[str]]:
    """Strip citation tags for display, returning (clean_text, doc_urls_cited)."""
    doc_urls = [value for kind, value in extract_citations(text) if kind == "doc"]
    return strip_citations(text), doc_urls
