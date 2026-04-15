"""
LangGraph agentic graph.

Nodes:
  agent       — LLM decides what to do next (calls tools or finishes)
  tool_node   — Executes whatever tools the agent called
  router      — Decides: loop back to agent, or exit to done

The agent is in a ReAct loop:
  agent → tool_node → agent → tool_node → ... → agent calls finish_analysis → done

The LLM drives the entire analysis. It:
  - Decides to run scan_repository first
  - Decides to call extract_ast_graph and cluster_communities
  - Decides which files to read_file (based on what looks interesting)
  - Decides what to search_code for
  - Decides when it has enough to record_finding / record_diagram
  - Decides when to call finish_analysis
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from codegrapher.state import AgentState
from codegrapher.tools import make_tools


SYSTEM_PROMPT = """You are an expert software architect performing a deep analysis of a code repository.

## CRITICAL: Minimize LLM round-trips by batching tool calls

You CAN return MULTIPLE tool_calls in a single response. Do this for any independent operations.
This is the most important rule — it directly controls cost and speed.

BAD (4 separate responses):
  response 1: read_file("main.py")
  response 2: read_file("config.py")
  response 3: search_code("api|route")
  response 4: record_finding(...)

GOOD (1 response, 4 parallel tool calls):
  tool_call 1: read_multiple_files(["main.py", "config.py", "models.py"])
  tool_call 2: search_code("api|route")
  tool_call 3: search_code("database|model|schema")
  tool_call 4: record_finding(category="architecture", ...)

## Your 4-step strategy (target: ~8-12 total LLM calls)

### Step 1 — Scan (1 call)
Call scan_repository to understand the repo's shape, file types, and directory structure.

### Step 2 — Build graph (1 call)
Call build_code_graph — this runs the full pipeline in one shot:
  AST extraction → community detection → graph analysis → diagram generation
Read the results carefully: god nodes, communities, and suggested questions guide Step 3.

### Step 3 — Explore (3-5 calls, use parallel batching)
Each response should contain 3-5 tool calls at once:
  - read_multiple_files([...]) to read 3-5 key files at once
  - search_code(...) for API routes, database models, auth patterns, tests
  - record_finding(...) as you discover insights (batch multiple per response)
Focus on: entry points, key modules identified by god nodes, config, models, tests.
Read 5-8 files total across all exploration steps.

### Step 4 — Finish (1 call)
Call finish_analysis with the complete executive summary once you have:
  ✓ Scanned the repo
  ✓ Built the graph
  ✓ Read key files (at least 5)
  ✓ Searched for major patterns
  ✓ Recorded meaningful findings

## Rules
- scan_repository FIRST, always
- build_code_graph SECOND, always (replaces extract+cluster+analyze+diagrams)
- Use read_multiple_files instead of read_file whenever reading >1 file
- Batch independent tool calls in the SAME response to save round-trips
- NEVER write Mermaid manually — build_code_graph generates all diagrams
- Call finish_analysis when you have a complete picture — no minimum call count

## Finding categories
architecture | component | data_flow | api_endpoint | security | tech_stack |
design_pattern | code_quality | improvement | entry_point | dependency |
database_model | testing | deployment | file_detail
"""


def build_graph(
    llm: BaseChatModel,
    repo_root: Path,
    state_accumulator: dict,
    max_iterations: int = 50,
    verbose: bool = False,
) -> StateGraph:
    """
    Build and compile the LangGraph agentic graph.

    Returns a compiled graph ready to .invoke() or .stream().
    """
    tools = make_tools(repo_root, state_accumulator)
    llm_with_tools = llm.bind_tools(tools)
    tool_node = ToolNode(tools)

    # ── Agent node ─────────────────────────────────────────────────────────────
    def agent_node(state: AgentState) -> dict:
        """
        The core agent node. The LLM sees the full message history
        (including all previous tool results) and decides what to do next.
        """
        iteration = state.get("iteration", 0)

        if verbose:
            print(f"   [Agent step {iteration + 1}] thinking...")

        # Build messages: system + full conversation history
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]

        # Call LLM — it may return tool_calls or a plain text response
        response = llm_with_tools.invoke(messages)

        if verbose and hasattr(response, "tool_calls") and response.tool_calls:
            for tc in response.tool_calls:
                print(f"   → {tc['name']}({json.dumps(tc.get('args', {}))[:80]}...)")

        return {
            "messages": [response],
            "iteration": iteration + 1,
        }

    # ── Router ─────────────────────────────────────────────────────────────────
    def router(state: AgentState) -> Literal["tool_node", "done"]:
        """
        Decide whether to execute tool calls or exit.

        Exit conditions:
          - Agent called finish_analysis (state_accumulator["finished"] = True)
          - Agent returned no tool calls (finished reasoning)
          - Max iterations reached (safety limit)
        """
        # Check finish signal from tools
        if state_accumulator.get("finished"):
            if verbose:
                print(f"\n   ✅ Agent finished after {state.get('iteration', 0)} steps")
            return "done"

        # Safety limit
        if state.get("iteration", 0) >= max_iterations:
            if verbose:
                print(f"\n   ⚠️  Max iterations ({max_iterations}) reached")
            return "done"

        # Check last message for tool calls
        last_message = state["messages"][-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tool_node"

        # No tool calls = agent is done
        return "done"

    # ── Build graph ────────────────────────────────────────────────────────────
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
    max_iterations: int = 50,
    verbose: bool = False,
) -> tuple[dict, dict]:
    """
    Run the full agentic analysis loop.

    Returns
    -------
    state_accumulator : dict
        All tool outputs: findings, diagrams, finish_data, graph, etc.
    final_state : dict
        The final LangGraph state (messages, iteration count, etc.)
    """
    state_accumulator: dict = {
        "finished": False,
        "findings": [],
        "diagrams": [],
        "finish_data": {},
    }

    graph = build_graph(
        llm=llm,
        repo_root=repo_root,
        state_accumulator=state_accumulator,
        max_iterations=max_iterations,
        verbose=verbose,
    )

    # Initial state
    initial_state: AgentState = {
        "repo_path": str(repo_root),
        "provider_name": "",
        "messages": [
            HumanMessage(content=(
                f"Please analyze the repository at: {repo_root}\n\n"
                "Step 1: scan_repository\n"
                "Step 2: build_code_graph (runs AST + clustering + analysis + diagrams in one call)\n"
                "Step 3: Explore — use read_multiple_files + search_code + record_finding in parallel batches\n"
                "Step 4: finish_analysis with the complete executive summary\n\n"
                "Batch independent tool calls together to minimize round-trips."
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
