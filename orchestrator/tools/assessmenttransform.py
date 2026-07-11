"""
Assessment Sub-Agents (agents-as-tools)

Runs READ-ONLY analysis transformation definitions synchronously, in-process,
via the `atx` CLI against a local clone of the target repository. NEVER
applies changes, NEVER commits, NEVER pushes. Only the 3 whitelisted analysis
TDs below may be run.

v1: no AWS Batch. Assumes `atx` CLI + git are installed and the analysis TDs
are registered/available on whatever host the orchestrator itself runs on.
Each tool call blocks until that analysis finishes and returns its output
directly - there is no separate job-status/polling step.
"""

import os
import time
import json
import shutil
import logging
import subprocess
import boto3
from typing import Any, Dict
from datetime import datetime

from strands import Agent, tool

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_bedrock_region = os.getenv("AWS_REGION", "us-east-1")
_model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# ONLY these 3 TDs may ever be run by this file.
ANALYSIS_TDS = {
    "codebase": "AWS/comprehensive-codebase-analysis",
    "readiness": "AWS/modernization-readiness-analysis",
    "business_rules": "AWS/business-rules-extraction",
}

WORKSPACE_ROOT = os.getenv("ASSESSMENT_WORKSPACE", "/tmp/atx-assessments")
ANALYSIS_TIMEOUT_SECONDS = int(os.getenv("ANALYSIS_TIMEOUT_SECONDS", "1800"))


def _extract_repo_name(source: str) -> str:
    if not source:
        return 'unknown'
    try:
        from urllib.parse import urlparse
        parsed = urlparse(source)
        if parsed.hostname and (parsed.hostname == 'github.com' or parsed.hostname.endswith('.github.com')):
            return parsed.path.rstrip('/').rstrip('.git').split('/')[-1] or 'unknown'
    except Exception:
        pass
    if os.path.isdir(source):
        return os.path.basename(source.rstrip('/')) or 'unknown'
    return 'unknown'


def _extract_params(query: str, schema_prompt: str) -> Dict[str, Any]:
    """Shared NL-extraction helper (direct Bedrock call, avoids Strands streaming bug)."""
    bedrock_rt = boto3.client('bedrock-runtime', region_name=_bedrock_region)
    response = bedrock_rt.invoke_model(
        modelId=_model_id,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048, "temperature": 0.1,
            "messages": [{"role": "user", "content": f"{schema_prompt}\n\nRequest: {query}"}]
        })
    )
    raw_text = json.loads(response['body'].read())['content'][0]['text'].strip()
    if '```' in raw_text:
        raw_text = raw_text.split('```')[1]
        if raw_text.startswith('json'):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    return json.loads(raw_text)


def _clone_repo(source: str, dest: str) -> None:
    """Fetch a READ-ONLY checkout of source into dest. Git URL or local path."""
    if os.path.isdir(source):
        shutil.copytree(source, dest)
        return
    subprocess.run(
        ["git", "clone", "--depth", "1", source, dest],
        check=True, capture_output=True, text=True, timeout=300,
    )


def _run_atx_exec(td_name: str, repo_path: str, additional_context: str) -> Dict[str, Any]:
    """
    Run `atx custom def exec` synchronously against a local repo checkout.
    READ-ONLY: -x -t only, argv list (not shell=True) so no quoting/injection
    concerns around additionalPlanContext.
    """
    cmd = ["atx", "custom", "def", "exec", "-n", td_name, "-p", repo_path]
    if additional_context:
        cmd += ["--configuration", f"additionalPlanContext={additional_context}"]
    cmd += ["-x", "-t"]
    try:
        proc = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True,
            timeout=ANALYSIS_TIMEOUT_SECONDS,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-20000:],
            "stderr": proc.stderr[-5000:],
            "command": " ".join(cmd),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "returncode": -1,
            "stdout": (e.stdout or "")[-20000:] if isinstance(e.stdout, str) else "",
            "stderr": f"Timed out after {ANALYSIS_TIMEOUT_SECONDS}s",
            "command": " ".join(cmd),
        }
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "'atx' CLI not found on PATH", "command": " ".join(cmd)}


def _run_analysis(td_name: str, source: str, additional_context: str, job_prefix: str) -> Dict[str, Any]:
    repo_name = _extract_repo_name(source)
    job_name = f"{job_prefix}-{repo_name}-{int(time.time())}"
    workdir = os.path.join(WORKSPACE_ROOT, job_name)
    repo_path = os.path.join(workdir, "repo")
    os.makedirs(workdir, exist_ok=True)

    try:
        _clone_repo(source, repo_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch source: {e}"}

    exec_result = _run_atx_exec(td_name, repo_path, additional_context)
    success = exec_result["returncode"] == 0

    return {
        "status": "success" if success else "error",
        "transformation": td_name,
        "job_name": job_name,
        "source": source,
        "output_dir": repo_path,
        "returncode": exec_result["returncode"],
        "stdout": exec_result["stdout"],
        "stderr": exec_result["stderr"],
        "completed_at": datetime.utcnow().isoformat() + 'Z',
    }


@tool
def list_output_files(output_dir: str) -> Dict[str, Any]:
    """List files produced by a completed assessment. Args: output_dir (from a prior analysis result)."""
    try:
        files = []
        for root, _, filenames in os.walk(output_dir):
            for fn in filenames:
                full = os.path.join(root, fn)
                files.append({"path": full, "size": os.path.getsize(full)})
        return {"status": "success", "output_dir": output_dir, "file_count": len(files), "files": files[:200]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool
def read_output_file(file_path: str) -> Dict[str, Any]:
    """Read a specific output file's contents (from list_output_files). Truncated to 50KB."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        truncated = len(content) > 50000
        if truncated:
            content = content[:50000]
        return {"status": "success", "path": file_path, "content": content, "truncated": truncated}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# The 3 assessment subagents (one @tool per whitelisted analysis TD).
# Each blocks until the analysis finishes and returns the result directly -
# there is no async job to poll.
# ---------------------------------------------------------------------------

@tool
def codebase_analysis_agent(query: str) -> Dict[str, Any]:
    """
    Runs AWS/comprehensive-codebase-analysis (architecture, tech debt, EOL
    deps, code metrics, diagrams) on ONE repository. READ-ONLY. Blocks until
    finished.

    Args:
        query: e.g. "Analyze https://github.com/user/repo for tech debt and EOL dependencies"
    """
    logger.info("CODEBASE ANALYSIS AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"source": "repo URL or local path", "context": "free-text framing for the analysis, e.g. pre-migration due diligence focus areas"}""")
        source = params.get('source', '')
        if not source:
            return {"status": "error", "error": "Could not extract a repository source from the request."}
        result = _run_analysis(ANALYSIS_TDS['codebase'], source, params.get('context', ''), 'codebase')
        return {"status": "success", "result": json.dumps(result)}
    except Exception as e:
        logger.error(f"codebase_analysis_agent failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


@tool
def modernization_readiness_agent(query: str) -> Dict[str, Any]:
    """
    Runs AWS/modernization-readiness-analysis (cloud-native maturity, AWS
    service recs per pattern) on ONE repository. READ-ONLY. Blocks until
    finished.

    Args:
        query: e.g. "Assess modernization readiness for https://github.com/user/repo,
            archetype web-app, prefer eks/aurora-postgresql/bedrock, avoid oracle"
    """
    logger.info("MODERNIZATION READINESS AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"source": "repo URL or local path", "service_archetype": "", "agent_scope": "read-only",
 "priority": "", "context": "", "prefer": "comma-separated services", "avoid": "comma-separated services"}
Default agent_scope to "read-only" if not specified (this is due diligence, not an agentic target).""")
        source = params.get('source', '')
        if not source:
            return {"status": "error", "error": "Could not extract a repository source from the request."}
        ctx_parts = [params.get('context', '')]
        if params.get('service_archetype'):
            ctx_parts.append(f"service_archetype: {params['service_archetype']}")
        ctx_parts.append(f"agent_scope: {params.get('agent_scope', 'read-only')}")
        if params.get('priority'):
            ctx_parts.append(f"priority: {params['priority']}")
        if params.get('prefer'):
            ctx_parts.append(f"prefer: {params['prefer']}")
        if params.get('avoid'):
            ctx_parts.append(f"avoid: {params['avoid']}")
        flattened_context = " | ".join(p for p in ctx_parts if p)
        result = _run_analysis(ANALYSIS_TDS['readiness'], source, flattened_context, 'readiness')
        return {"status": "success", "result": json.dumps(result)}
    except Exception as e:
        logger.error(f"modernization_readiness_agent failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


@tool
def business_rules_agent(query: str) -> Dict[str, Any]:
    """
    Runs AWS/business-rules-extraction (numbered business rules, data model,
    workflows) on ONE repository. READ-ONLY. Blocks until finished.

    Args:
        query: e.g. "Extract business rules from https://github.com/user/repo"
    """
    logger.info("BUSINESS RULES AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"source": "repo URL or local path", "context": "free-text framing, focus domains for the extraction"}""")
        source = params.get('source', '')
        if not source:
            return {"status": "error", "error": "Could not extract a repository source from the request."}
        result = _run_analysis(ANALYSIS_TDS['business_rules'], source, params.get('context', ''), 'bizrules')
        return {"status": "success", "result": json.dumps(result)}
    except Exception as e:
        logger.error(f"business_rules_agent failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
