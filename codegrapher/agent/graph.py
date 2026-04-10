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

You have tools that let you explore the repository. YOU are the decision-maker — you decide:
  - Which tools to call, and in what order
  - Which files to read (based on what you find as you explore)
  - When to search for specific patterns
  - When you have enough context to record a finding
  - When the analysis is complete and you can call finish_analysis

## Your strategy

1. START with scan_repository to understand the repo's shape and scale
2. Run extract_ast_graph + cluster_communities to build the structural graph
3. Run analyze_graph to find god nodes and surprising connections
4. Call generate_diagrams_from_graph — this creates ALL diagrams deterministically
   from the real graph data (no guessing, no hallucination, no manual Mermaid writing)
5. Read key files — entry points, main modules, config files
6. Search for patterns — API routes, database models, auth, tests
7. Record findings as you discover them (don't batch everything at the end)
8. Call finish_analysis with the complete executive summary

## Rules
- Call scan_repository FIRST, always
- Call extract_ast_graph before cluster_communities
- Call cluster_communities before analyze_graph
- Call generate_diagrams_from_graph AFTER analyze_graph (needs graph + communities + god_nodes)
- NEVER write Mermaid manually — always use generate_diagrams_from_graph instead
- Read at least 5-8 files before calling finish_analysis
- Record a finding every time you discover something significant
- Call finish_analysis only when you have a complete picture (15+ tool calls minimum)

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
                "Start with scan_repository, then extract_ast_graph, then cluster_communities, "
                "then analyze_graph. After that, read key files, record your findings, "
                "generate diagrams, and finish with finish_analysis. Be thorough."
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
