#!/usr/bin/env python3
"""
ATX Assessment Orchestrator Agent

A Strands agent that orchestrates READ-ONLY technical due-diligence analysis
using the ATX CLI. Coordinates 3 assessment sub-agents (agents-as-tools),
one per whitelisted analysis transformation definition, plus result-inspection
tools. This orchestrator never modifies, executes, or pushes changes to
client source code.
"""

import os
import json
import logging
from datetime import datetime

# Monkey-patch Strands streaming to fix type concatenation bug
# (upstream issue: streaming.py line 216 does str += int when tool input has integer values)
try:
    from strands.event_loop import streaming as _streaming
    _original_handle = _streaming.handle_content_block_delta
    def _patched_handle(content_block_delta, state):
        if "toolUse" in content_block_delta.get("delta", {}):
            delta = content_block_delta["delta"]["toolUse"]
            if "input" in delta and isinstance(delta["input"], (int, float)):
                delta["input"] = str(delta["input"])
        return _original_handle(content_block_delta, state)
    _streaming.handle_content_block_delta = _patched_handle
except Exception:
    pass  # If patch fails, continue without it

from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Lazy imports for tools (loaded on first request, not at startup)
codebase_analysis_agent = None
modernization_readiness_agent = None
business_rules_agent = None
list_output_files = None
read_output_file = None
generate_assessment_report = None

def _load_tools():
    global codebase_analysis_agent, modernization_readiness_agent, business_rules_agent
    global list_output_files, read_output_file, generate_assessment_report
    if codebase_analysis_agent is None:
        from tools.assessmenttransform import (
            codebase_analysis_agent as _codebase,
            modernization_readiness_agent as _readiness,
            business_rules_agent as _bizrules,
            list_output_files as _list,
            read_output_file as _read,
        )
        from tools.synthesis import generate_assessment_report as _report
        codebase_analysis_agent = _codebase
        modernization_readiness_agent = _readiness
        business_rules_agent = _bizrules
        list_output_files = _list
        read_output_file = _read
        generate_assessment_report = _report
from tools.memory_hooks import ShortTermMemoryHook

# Initialize the App
app = BedrockAgentCoreApp()

# Lazy-initialize memory (don't block startup)
memory_client = None
memory_id = None

def _init_memory():
    global memory_client, memory_id
    if memory_client is None:
        try:
            from tools.memory_client import get_memory_client, initialize_memory as _init_mem
            memory_client = get_memory_client()
            memory_id = _init_mem()
        except Exception as e:
            logger.warning(f"Memory init failed (continuing without memory): {e}")
            memory_id = None

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ORCHESTRATOR_PROMPT = """You are the ATX Assessment Orchestrator. You run READ-ONLY technical due-diligence
analysis on prospective-client repositories. You NEVER modify, execute, build, test, or push
changes to client source code, and you NEVER open pull requests. You only read code and produce
analysis findings.

# Available Tools

Per-repository analysis (each call BLOCKS until that analysis finishes and returns its result
directly — there is no separate job-status step to poll):
1. **codebase_analysis_agent**: Runs AWS/comprehensive-codebase-analysis — architecture, tech
   debt, EOL dependencies, code metrics, diagrams.
2. **modernization_readiness_agent**: Runs AWS/modernization-readiness-analysis — cloud-native
   maturity and AWS service recommendations per pattern.
3. **business_rules_agent**: Runs AWS/business-rules-extraction — numbered business rules, data
   model, workflows.

Report synthesis (run ONLY after all 3 analyses above have returned success for a repository):
4. **generate_assessment_report**: Cross-references the 3 analyses' output_dirs and produces the
   8-section technical due-diligence DOCX (sections with no v1 data source are marked "not
   covered"; cost/performance benefit sections are directional estimates, clearly labeled as such).

Result inspection (use freely, any time, with the output_dir from a prior result):
5. **list_output_files**: List files produced by a completed analysis.
6. **read_output_file**: Read a specific output file's contents.

# These are the ONLY transformation definitions you may invoke

- AWS/comprehensive-codebase-analysis
- AWS/modernization-readiness-analysis
- AWS/business-rules-extraction

Do NOT invoke, mention as available, or attempt to run any other transformation (no version
upgrades, SDK migrations, framework migrations, Graviton/ARM migrations, custom transformation
creation/publishing, or anything that would change client source code). If a user asks you to
execute, upgrade, migrate, fix, refactor, or create/publish a transformation, or to open a PR or
push changes, politely decline and explain that this platform performs read-only assessment and
due-diligence reporting only — it does not modify or execute client code. Do not silently attempt
a workaround.

# Orchestration Protocol

1. For each repository in the engagement, call codebase_analysis_agent,
   modernization_readiness_agent, and business_rules_agent.
2. Once all 3 have returned success for a repository, call generate_assessment_report, passing
   along each tool's output_dir and any client/industry/compliance context from the original
   request, to produce the due-diligence DOCX.
3. Use list_output_files / read_output_file to answer follow-up questions about any result's
   output_dir.

# Response Format
Always provide clear status, details, and next steps. Do NOT ask follow-up questions like "Would
you like me to..." or "Is there anything else..." - this is a one-shot API, not a chatbot. Just
report what was done and the results."""


def create_orchestrator(session_id: str = None, actor_id: str = None) -> Agent:
    """Create the ATX Transform orchestrator agent."""
    _init_memory()
    _load_tools()

    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    bedrock_model = BedrockModel(
        model_id=model_id,
        region_name=region,
        temperature=0.5,
        max_tokens=4096
    )

    hooks = []
    if memory_id:
        from tools.memory_hooks import ShortTermMemoryHook
        hooks.append(ShortTermMemoryHook(memory_client, memory_id))

    orchestrator = Agent(
        model=bedrock_model,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[
            codebase_analysis_agent, modernization_readiness_agent, business_rules_agent,
            generate_assessment_report, list_output_files, read_output_file,
        ],
        hooks=hooks,
        state={"actor_id": actor_id, "session_id": session_id}
    )

    return orchestrator


@app.entrypoint
def invoke(payload):
    """Bedrock AgentCore entrypoint."""
    try:
        user_message = payload.get("prompt", payload.get("message", ""))

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        session_id = f"atx-transform-{timestamp}"

        orchestrator = create_orchestrator(
            session_id=session_id,
            actor_id="atx_user"
        )

        logger.info("Starting ATX Transform orchestration")
        response = orchestrator(user_message)
        logger.info("Orchestration completed")

        if hasattr(response, 'message'):
            response_content = response.message
        elif hasattr(response, 'content'):
            response_content = response.content
        else:
            response_content = str(response)

        return {"result": response_content}

    except Exception as e:
        logger.error(f"Orchestration failed: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }


if __name__ == "__main__":
    print("Starting ATX Transform Orchestrator...")
    print("Server will be available at http://localhost:8080")
    app.run()
