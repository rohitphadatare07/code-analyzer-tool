"""
LangGraph agentic graph — genuine ReAct agent.

What makes this genuinely agentic (vs a pipeline disguised as an agent)
-----------------------------------------------------------------------
1. No numbered steps in the system prompt.
   The agent decides its own exploration path based on what it finds.

2. The LLM contributes real analysis, not just tool dispatch.
   Every finding must have WHAT/WHY/CONSEQUENCE/HOW. The quality gate
   in record_finding enforces this — thin observations are rejected.

3. The agent follows leads, not a checklist.
   After analyze_graph reveals a surprising connection, the agent reads
   the relevant files to understand it — not because a rule says so,
   but because the output of analyze_graph explicitly says to investigate.

4. Budgets replace minimums.
   Old approach: "read at least 8 files" → agent always hits the floor.
   New approach: "you have 6 read_file calls total" → agent prioritises.

5. Retry + context trimming for Bedrock throttling.
   The agent never crashes on ThrottlingException. It waits and retries
   with exponential backoff. Context is trimmed if it grows too large.

Call budget:
  6  pipeline tools (scan, extract, cluster, analyze, assess, diagrams)
  6  read_file calls
  4  search_code calls
  15 record_finding calls
  1  finish_analysis
  ─────────────────
  ~32 total maximum
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from codegrapher.agent.state import AgentState
from codegrapher.agent.tools import make_tools


# ── Call budgets ───────────────────────────────────────────────────────────────
BUDGET_FILES    = 6    # read_file calls
BUDGET_SEARCHES = 4    # search_code calls
BUDGET_FINDINGS = 15   # record_finding calls
MAX_CONTEXT_MESSAGES = 50   # trim conversation beyond this


# ── System prompt ──────────────────────────────────────────────────────────────
# Describes WHAT good analysis looks like — not HOW to sequence steps.
# The agent decides its own path.

SYSTEM_PROMPT = f"""You are a senior software architect analysing a code repository.
You have tools to explore it. You have a LIMITED TOOL BUDGET — use it wisely.

═══ YOUR TOOL BUDGET ═══

Pipeline tools — call each EXACTLY ONCE, in this order:
  scan_repository → extract_ast_graph → cluster_communities →
  analyze_graph → generate_diagrams_from_graph

Exploration tools — LIMITED budget:
  read_file       : {BUDGET_FILES} calls total
  search_code     : {BUDGET_SEARCHES} calls total
  record_finding  : {BUDGET_FINDINGS} calls total
  finish_analysis : 1 call

═══ HOW TO USE YOUR BUDGET WISELY ═══

Run ALL pipeline tools first before any exploration.
They give you the foundation: graph, communities, god nodes, diagrams.

After the pipeline, you know:
  - Which nodes have the most connections (god nodes) → read those files
  - Which cross-community connections are surprising → investigate those
  - Which 12-factor checks failed → find the code causing each failure

Spend your read_file budget on:
  ✓ God nodes (highest-connected files — they are the architectural core)
  ✓ Files in surprising cross-community connections
  ✓ Entry points (boot.js, main.py, index.ts, app.js)
  ✗ NOT files you are just curious about
  ✗ NOT files the graph already explained

Spend your search_code budget on:
  ✓ Finding all API routes: 'app.get|router.post|@GetMapping'
  ✓ Verifying config usage: 'process\\.env|os\\.environ'
  ✓ Finding DB models: 'class.*Model|Schema\\('
  ✗ NOT searching for things scan_repository already told you
  ✗ NOT searching twice for the same concept

═══ FINDING QUALITY — the most important requirement ═══

Every finding MUST have all four elements:
  WHAT:        What exactly did you find? Name the actual file or component.
  WHY:         Why does this matter architecturally?
  CONSEQUENCE: What breaks or cannot be done if this is not fixed?
  HOW:         What specifically should be done? Name the AWS service or pattern.

WEAK (will be REJECTED — just an observation):
  "boot.js has 8 connections and is 605 lines."

STRONG (accepted — architectural insight):
  "boot.js initialises all 7 services sequentially, making startup time
  the SUM of service init times rather than the MAX. This prevents ECS
  from starting services independently for separate scaling. Each service
  failure blocks the entire boot sequence. Fix: use Promise.all for
  independent services (email, themes, url), keeping config and express
  synchronous as pre-requisites. Reduces boot time by ~40-60%."

Record findings IMMEDIATELY after each file read or tool result.
Do not batch findings at the end.

═══ FOLLOWING LEADS ═══

When analyze_graph shows a surprising cross-community connection:
  → Read both files involved to understand WHY they are connected
  → Is it intentional design or accidental coupling?
  → Record a finding explaining the architectural implication

When a file you read imports something unexpected:
  → Use search_code to find all usages of that import across the codebase
  → Understand if this is an isolated issue or a systemic pattern

═══ CONSTRAINTS ═══
  - Never write Mermaid manually — use generate_diagrams_from_graph
  - scan_repository must be the first tool you call
  - finish_analysis must reference specific files you actually read
  - The tools will tell you when a budget is exhausted — stop using that tool
"""


# ── Safe context trimming ──────────────────────────────────────────────────────

def _trim_messages_safe(messages: list, target: int) -> list:
    """
    Trim conversation history to at most `target` messages while preserving
    all tool_use / tool_result pairs.

    Bedrock enforces: every tool_result must have a tool_use with the same
    tool_use_id in the immediately preceding assistant message. Trimming
    naively (e.g. messages[:1] + messages[-N:]) can cut the tool_use while
    keeping the tool_result, causing:
      ValidationException: unexpected tool_use_id found in tool_result blocks

    Strategy:
      1. Always keep the first message (original user request).
      2. Walk backwards from the end, accumulating messages.
      3. When we see a tool_result (ToolMessage), also keep the preceding
         assistant message that contains the matching tool_use — even if it
         would otherwise be trimmed.
      4. Stop once we have `target` messages.

    This guarantees no orphaned tool_result messages.
    """
    if len(messages) <= target:
        return messages

    # Always keep the first message (initial user request)
    first   = messages[:1]
    rest    = messages[1:]
    kept    = []
    budget  = target - 1  # -1 for the first message we always keep

    # Walk backwards through rest so we take the most recent messages
    i = len(rest) - 1
    while i >= 0 and len(kept) < budget:
        msg = rest[i]

        # Detect message type
        msg_type = type(msg).__name__  # HumanMessage, AIMessage, ToolMessage

        if msg_type == "ToolMessage":
            # This is a tool_result. We must also keep the preceding assistant
            # message (tool_use) even if it pushes us over budget slightly.
            kept.insert(0, msg)
            # Look backwards for the preceding assistant message
            if i > 0:
                prev = rest[i - 1]
                prev_type = type(prev).__name__
                if prev_type == "AIMessage":
                    kept.insert(0, prev)
                    i -= 2
                    continue
        else:
            kept.insert(0, msg)

        i -= 1

    return first + kept


# ── Graph construction ─────────────────────────────────────────────────────────

def build_graph(
    llm:               BaseChatModel,
    repo_root:         Path,
    state_accumulator: dict,
    max_iterations:    int  = 35,
    verbose:           bool = False,
    mcp_client                = None,
) -> StateGraph:
    """Build and compile the LangGraph agentic graph."""

    tools          = make_tools(
        repo_root, state_accumulator,
        file_budget=BUDGET_FILES,
        search_budget=BUDGET_SEARCHES,
        finding_budget=BUDGET_FINDINGS,
    )
    llm_with_tools = llm.bind_tools(tools)
    tool_executor  = ToolNode(tools)

    # ── Agent node ─────────────────────────────────────────────────────────────
    def agent_node(state: AgentState) -> dict:
        """
        Core thinking node.
        Includes context trimming and exponential-backoff retry
        for Bedrock ThrottlingException.
        """
        iteration = state.get("iteration", 0)

        if verbose:
            counts = state_accumulator.get("_call_counts", {})
            print(
                f"   [Step {iteration + 1}] thinking... "
                f"(files {counts.get('read_file',0)}/{BUDGET_FILES} "
                f"searches {counts.get('search_code',0)}/{BUDGET_SEARCHES} "
                f"findings {counts.get('record_finding',0)}/{BUDGET_FINDINGS})"
            )

        # Context trimming — must preserve tool_use / tool_result pairs.
        # Bedrock rejects any tool_result whose tool_use was trimmed away.
        # Simple slice trimming (messages[:1] + messages[-N:]) can break
        # pairs and causes ValidationException: unexpected tool_use_id.
        messages = state["messages"]
        if len(messages) > MAX_CONTEXT_MESSAGES:
            messages = _trim_messages_safe(messages, MAX_CONTEXT_MESSAGES)
            if verbose:
                print(f"   [Context trimmed to {len(messages)} messages]")

        full_messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages

        # Retry with exponential backoff for throttling
        max_retries = 5
        base_delay  = 15  # seconds

        for attempt in range(max_retries):
            try:
                response = llm_with_tools.invoke(full_messages)

                if verbose:
                    if hasattr(response, "tool_calls") and response.tool_calls:
                        for tc in response.tool_calls:
                            print(f"   → {tc['name']}({json.dumps(tc.get('args',{}))[:80]})")
                    elif hasattr(response, "content") and response.content:
                        print(f"   [Thinking] {str(response.content)[:120]}")

                return {"messages": [response], "iteration": iteration + 1}

            except Exception as e:
                err = str(e).lower()
                is_throttle = any(k in err for k in (
                    "throttling", "throttle", "too many tokens",
                    "rate limit", "too many requests", "503",
                ))
                # ValidationException with tool_use_id mismatch means the
                # context was trimmed unsafely in a previous retry attempt.
                # Trim again using the safe trimmer and retry.
                is_validation = "validationexception" in err and "tool_use_id" in err

                if (is_throttle or is_validation) and attempt < max_retries - 1:
                    if is_throttle:
                        delay = base_delay * (2 ** attempt)
                        print(f"\n   ⏳ Throttled — waiting {delay}s (attempt {attempt+1}/{max_retries})\n")
                        time.sleep(delay)
                    else:
                        print(f"\n   ⚠️  ValidationException (tool_use_id mismatch) — re-trimming context\n")

                    # Always trim safely on any retry
                    trim_to = max(20, MAX_CONTEXT_MESSAGES - 10 * (attempt + 1))
                    messages = _trim_messages_safe(messages, trim_to)
                    full_messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
                    continue
                raise

    # ── Router ─────────────────────────────────────────────────────────────────
    def router(state: AgentState) -> Literal["tool_node", "done"]:
        """Stop when finished, max iterations reached, or no tool calls."""
        if state_accumulator.get("finished"):
            if verbose:
                counts = state_accumulator.get("_call_counts", {})
                print(
                    f"\n   ✅ Complete — "
                    f"{state.get('iteration',0)} steps, "
                    f"{len(state_accumulator.get('findings',[]))} findings"
                )
                print(f"   Calls: {counts}")
            return "done"

        if state.get("iteration", 0) >= max_iterations:
            if verbose:
                print(f"\n   ⚠️  Max iterations ({max_iterations}) reached")
            return "done"

        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tool_node"

        return "done"

    # ── Assemble ───────────────────────────────────────────────────────────────
    builder = StateGraph(AgentState)
    builder.add_node("agent",     agent_node)
    builder.add_node("tool_node", tool_executor)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", router, {"tool_node": "tool_node", "done": END})
    builder.add_edge("tool_node", "agent")
    return builder.compile()


# ── Public entry point ─────────────────────────────────────────────────────────

def run_agent(
    llm:            BaseChatModel,
    repo_root:      Path,
    max_iterations: int  = 35,
    verbose:        bool = False,
    mcp_client             = None,
) -> tuple[dict, dict]:
    """
    Run the agentic analysis with call budgets.

    Typical call count: 20-28 total
      6  pipeline (scan, extract, cluster, analyze, diagrams + assess if used)
      4-6  file reads (god nodes, entry points, surprising connections)
      2-4  searches (API routes, config patterns)
      8-12 findings (recorded as each insight is discovered)
      1  finish_analysis
    """
    state_accumulator: dict = {
        "finished":              False,
        "findings":              [],
        "diagrams":              [],
        "finish_data":           {},
        "graph":                 None,
        "communities":           {},
        "community_labels":      {},
        "cohesion":              {},
        "god_nodes":             [],
        "surprising_connections":[],
        "scan_result":           {},
        "assessment":            None,
        "graphify_ast":          None,
        "_call_counts": {
            "read_file":      0,
            "search_code":    0,
            "record_finding": 0,
        },
    }

    graph = build_graph(
        llm=llm,
        repo_root=repo_root,
        state_accumulator=state_accumulator,
        max_iterations=max_iterations,
        verbose=verbose,
        mcp_client=mcp_client,
    )

    # Initial message — goal + freedom, no numbered steps
    initial_state: AgentState = {
        "messages": [HumanMessage(content=(
            f"Analyse the repository at: {repo_root}\n\n"
            f"You have a limited tool budget:\n"
            f"  {BUDGET_FILES} read_file calls\n"
            f"  {BUDGET_SEARCHES} search_code calls\n"
            f"  {BUDGET_FINDINGS} record_finding calls\n\n"
            f"Start with scan_repository, run all pipeline tools, then use "
            f"your exploration budget on the most architecturally significant "
            f"files and patterns. Finish with finish_analysis once you have "
            f"genuine understanding — not just when you have hit a call count."
        ))],
        "iteration": 0,
        "finished":  False,
    }

    if verbose:
        print(f"\n🤖 Starting agent: {repo_root.name}")
        print(f"   Budgets: {BUDGET_FILES} files / {BUDGET_SEARCHES} searches / {BUDGET_FINDINGS} findings")
        print(f"   Max steps: {max_iterations}\n")

    final_state = graph.invoke(
        initial_state,
        config={"recursion_limit": max_iterations + 10},
    )
    return state_accumulator, final_state