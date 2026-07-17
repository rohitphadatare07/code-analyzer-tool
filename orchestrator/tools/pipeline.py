"""
Deterministic Assessment Pipeline

Runs the 4 per-repo analysis tools (codebase, readiness, business rules,
security/compliance) SEQUENTIALLY, one at a time, then gates report synthesis
behind their REAL status.

Why this exists: the original design put all sequencing ("run these 4 tools,
wait for success, then call the report tool") into English inside the
orchestrator's system prompt, trusted entirely to an LLM's own tool-calling
judgment on every single request. That has no structural guarantee - a model
could skip a step, call synthesis early, or misread a nested status field.
This module makes the fixed, always-the-same-shape part of the workflow (N
analyses -> 1 synthesis, per engagement) real Python control flow instead,
and reserves the LLM for what actually needs judgment: understanding the
request, and writing the synthesis narrative itself (still done by
tools/synthesis.py's own Strands Agent calls). agent.py's invoke() tries this
deterministic path first for anything that looks like a clear assessment
request, falling back to the conversational Strands Agent (agent.py's
ORCHESTRATOR_PROMPT) for anything else - status lookups, out-of-scope
requests, follow-up questions.

Note: an earlier version of this module ran the 4 analyses in parallel via a
ThreadPoolExecutor (they're blocking subprocess/network calls that release the
GIL, so real concurrency was possible). That was reverted back to sequential
execution - one `atx`/scanner subprocess in flight at a time, run in a fixed,
predictable order.
"""

import logging
from typing import Any, Dict, List, Optional

from .assessmenttransform import _extract_params, _run_analysis, ANALYSIS_TDS
from .security_analysis import _run_security_analysis
from .synthesis import _generate_report_from_repo_dirs

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_ANALYSIS_KINDS = ("codebase", "readiness", "business_rules", "security")


def try_extract_assessment_request(query: str) -> Optional[Dict[str, Any]]:
    """
    Classify + extract in one Bedrock call. Returns
    {"repos": [{"name":..., "source":...}], "client_name": str, "context": str}
    if the request clearly names one or more repositories to assess, or None
    for anything else (status/result lookups, out-of-scope execute/upgrade/
    create requests, general questions) - callers should fall back to the
    conversational Agent in that case, never guess.
    """
    try:
        params = _extract_params(query, """Classify and extract. Return ONLY JSON:
{"is_assessment_request": true or false,
 "repos": [{"name": "short repo name/slug", "source": "repo URL or local path"}],
 "client_name": "", "context": "industry/compliance/other free-text engagement context"}

Set is_assessment_request to true ONLY when the request clearly asks to analyze/assess
one or more repositories AND names at least one concrete repo URL or local path. Set to
false for anything else: checking status/results of a previous job, asking to execute/
upgrade/migrate/refactor/create or publish a transformation, opening a PR, general
questions, or a request that doesn't name a concrete repo.""")
        if not params.get("is_assessment_request") or not params.get("repos"):
            return None
        return params
    except Exception as e:
        logger.warning(f"Assessment-request classification failed, falling back to conversational agent: {e}")
        return None


def _run_one(kind: str, source: str, context: str) -> Dict[str, Any]:
    try:
        if kind == "codebase":
            return _run_analysis(ANALYSIS_TDS["codebase"], source, context, "codebase")
        if kind == "readiness":
            return _run_analysis(ANALYSIS_TDS["readiness"], source, context, "readiness")
        if kind == "business_rules":
            return _run_analysis(ANALYSIS_TDS["business_rules"], source, context, "bizrules")
        if kind == "security":
            return _run_security_analysis(source, context)
        raise ValueError(f"Unknown analysis kind: {kind}")
    except Exception as e:
        logger.error(f"{kind} analysis raised for source={source}: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


def run_full_assessment(repos: List[Dict[str, str]], client_name: str, context: str) -> Dict[str, Any]:
    """
    repos: [{"name": str, "source": str}, ...]

    Runs all 4 analyses for every repo SEQUENTIALLY, one at a time, checks each
    one's REAL status, and only calls the synthesis/report step if every
    analysis for every repo succeeded. On any failure, returns a clear
    partial-failure result naming exactly which analysis failed for which
    repo, rather than silently generating a report from incomplete data.
    """
    if not repos:
        return {"status": "error", "error": "No repositories provided."}

    logger.info(
        f"Running full assessment for {len(repos)} repo(s), "
        f"{len(_ANALYSIS_KINDS)} analyses each, sequentially"
    )

    per_repo: Dict[str, Dict[str, Any]] = {}
    for repo in repos:
        per_repo[repo["name"]] = {}
        for kind in _ANALYSIS_KINDS:
            per_repo[repo["name"]][kind] = _run_one(kind, repo["source"], context)

    repo_status: Dict[str, Any] = {}
    all_succeeded = True
    for repo in repos:
        name = repo["name"]
        results = per_repo.get(name, {})
        failures = {
            kind: results.get(kind, {}).get("error", "no result returned")
            for kind in _ANALYSIS_KINDS
            if results.get(kind, {}).get("status") != "success"
        }
        repo_status[name] = {"succeeded": not failures, "failures": failures}
        if failures:
            all_succeeded = False
            logger.warning(f"Repo {name}: analyses failed: {list(failures.keys())}")

    if not all_succeeded:
        return {
            "status": "partial_failure",
            "message": (
                "One or more analyses failed - report was NOT generated from incomplete "
                "data. See repo_status for exactly which analysis failed for which repo."
            ),
            "repo_status": repo_status,
        }

    repo_dirs = [
        {
            "name": repo["name"],
            "codebase_output_dir": per_repo[repo["name"]]["codebase"].get("output_dir", ""),
            "readiness_output_dir": per_repo[repo["name"]]["readiness"].get("output_dir", ""),
            "business_rules_output_dir": per_repo[repo["name"]]["business_rules"].get("output_dir", ""),
            "security_output_dir": per_repo[repo["name"]]["security"].get("output_dir", ""),
        }
        for repo in repos
    ]

    try:
        report_result = _generate_report_from_repo_dirs(repo_dirs, client_name, context)
    except Exception as e:
        logger.error(f"Report synthesis failed after all analyses succeeded: {e}", exc_info=True)
        return {
            "status": "error",
            "error": f"All analyses succeeded but report synthesis failed: {e}",
            "repo_status": repo_status,
        }

    return {
        "status": "success",
        "repo_status": repo_status,
        **report_result,
    }
