"""
LangGraph exploration graph — pure exploration, no synthesis.

Nodes:
  agent       — LLM decides which exploration tool to call next
  tool_node   — Executes the tool call
  router      — Loop back or exit when exploration is done

The agent explores freely with 6 tools:
  scan_repository, extract_ast_graph, cluster_communities,
  analyze_graph, read_file, search_code

It does NOT call finish_analysis — that is handled by synthesiser.synthesise()
after this loop exits. This means:
  - Each LLM call during exploration is small (no giant synthesis payload)
  - Throttling during exploration doesn't cause a stuck synthesis loop
  - The synthesiser retries independently with exponential backoff

Exit conditions (router):
  - Agent returns no tool calls (done exploring)
  - max_iterations safety limit reached
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from codegrapher.agent.state import AgentState
from codegrapher.agent.tools import make_tools


EXPLORATION_PROMPT = """You are an expert software architect and cloud infrastructure engineer
exploring a code repository for a full AS-IS Analysis.

You have 6 exploration tools. Use them to build a thorough understanding of BOTH
the application code AND any infrastructure / deployment configuration.
A separate synthesis step will convert your exploration into the final report.

## Strategy

1. scan_repository        — understand file types, sizes, structure.
                            CHECK the returned has_terraform / has_kubernetes /
                            has_docker / has_cicd / has_helm flags — they tell you
                            what infra is present so you know what to read next.

2. extract_ast_graph      — extract classes, functions, call graph from code files.

3. cluster_communities    — group related code into semantic clusters.

4. analyze_graph          — find god nodes and surprising connections.

5. read_file (×8-15)     — read key files. Prioritise by what scan_repository found:
   APPLICATION files:
     entry points, main modules, package.json / requirements.txt / go.mod,
     config files, README, test files
   INFRASTRUCTURE files (read ALL that exist):
     Terraform:       main.tf, variables.tf, outputs.tf, backend.tf,
                      providers.tf, modules/*/main.tf
     Kubernetes:      deployment.yaml, service.yaml, ingress.yaml,
                      configmap.yaml, hpa.yaml, namespace.yaml
     Docker:          Dockerfile, docker-compose.yml, .dockerignore
     Helm:            Chart.yaml, values.yaml, values-prod.yaml
     CI/CD:           .github/workflows/*.yml, Jenkinsfile,
                      .gitlab-ci.yml, azure-pipelines.yml
     Cloud configs:   serverless.yml, cdk.json, pulumi.yaml,
                      cloudformation/*.yaml, *.template.json
     Other:           Makefile, Procfile, nginx.conf, .env.example

6. search_code (×3-8)    — search for patterns in both code and infra:
   - "resource \\"aws_\|google_\|azurerm_" in *.tf  (cloud resources)
   - "image:" in *.yaml  (container images used)
   - "replicas:" in *.yaml  (scaling config)
   - "env:" or "ENV " in Dockerfile / *.yaml  (env vars)
   - "@app.route\|router\.\|express\|fastapi"  (API routes)
   - "database\|db\|postgres\|mysql\|mongo\|redis"  (data stores)

## Rules
- Always start with scan_repository and read its has_* flags
- Always run extract_ast_graph → cluster_communities → analyze_graph in order
- If has_terraform=true, read at minimum: main.tf, variables.tf, outputs.tf
- If has_kubernetes=true, read at minimum: deployment.yaml, service.yaml, ingress.yaml
- If has_docker=true, read Dockerfile and docker-compose.yml
- If has_cicd=true, read at least one CI/CD pipeline file
- Stop when you have seen both the application code AND all infra config
- Do NOT try to produce a structured report — just explore
- There is no finish_analysis tool — simply stop calling tools when done
"""


def build_graph(
    llm: BaseChatModel,
    repo_root: Path,
    state_accumulator: dict,
    max_iterations: int = 40,
    verbose: bool = False,
) -> StateGraph:
    """Build and compile the exploration LangGraph."""
    tools = make_tools(repo_root, state_accumulator)
    llm_with_tools = llm.bind_tools(tools)
    tool_node = ToolNode(tools)

    def agent_node(state: AgentState) -> dict:
        iteration = state.get("iteration", 0)
        if verbose:
            print(f"   [Agent step {iteration + 1}] thinking...")

        messages = [SystemMessage(content=EXPLORATION_PROMPT)] + state["messages"]
        response = llm_with_tools.invoke(messages)

        if verbose and hasattr(response, "tool_calls") and response.tool_calls:
            for tc in response.tool_calls:
                print(f"   → {tc['name']}({json.dumps(tc.get('args', {}))[:80]}...)")

        return {
            "messages": [response],
            "iteration": iteration + 1,
        }

    def router(state: AgentState) -> Literal["tool_node", "done"]:
        # Safety limit
        if state.get("iteration", 0) >= max_iterations:
            if verbose:
                print(f"\n   ⚠️  Max iterations ({max_iterations}) reached")
            return "done"

        # Agent returned no tool calls — exploration complete
        last_message = state["messages"][-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tool_node"

        if verbose:
            print(f"\n   ✅ Exploration done after {state.get('iteration', 0)} steps")
        return "done"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tool_node", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        router,
        {"tool_node": "tool_node", "done": END},
    )
    builder.add_edge("tool_node", "agent")
    return builder.compile()


def run_agent(
    llm: BaseChatModel,
    repo_root: Path,
    max_iterations: int = 40,
    verbose: bool = False,
) -> tuple[dict, dict]:
    """
    Run the exploration loop.

    Returns
    -------
    state_accumulator : dict
        Structured tool outputs: scan_result, graphify_ast, graph,
        communities, cohesion, community_labels, god_nodes,
        surprising_connections, suggested_questions.
    final_state : dict
        LangGraph final state — includes messages (for synthesiser context).
    """
    state_accumulator: dict = {}

    graph = build_graph(
        llm=llm,
        repo_root=repo_root,
        state_accumulator=state_accumulator,
        max_iterations=max_iterations,
        verbose=verbose,
    )

    initial_state: AgentState = {
        "repo_path": str(repo_root),
        "provider_name": "",
        "messages": [
            HumanMessage(content=(
                f"Please explore the repository at: {repo_root}\n\n"
                "Run: scan_repository → extract_ast_graph → cluster_communities → "
                "analyze_graph. Then read key files (entry points, main modules, "
                "config, tests) and search for important patterns. "
                "Be thorough — read at least 8-12 files. "
                "Stop when you have a complete picture of the codebase."
            ))
        ],
        "graphify_output": None,
        "findings": [],
        "diagrams": [],
        "summary": "",
        "purpose": "",
        "architecture_style": "",
        "tech_stack": [],
        "key_components": [],
        "data_flow": [],
        "api_endpoints": [],
        "database_models": [],
        "security_notes": [],
        "improvement_suggestions": [],
        "testing_approach": "",
        "deployment_info": "",
        "code_quality_notes": [],
        "phase": "start",
        "error": None,
        "iteration": 0,
    }

    final_state = graph.invoke(initial_state)
    return state_accumulator, final_state
