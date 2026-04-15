"""
LangGraph agentic graph + single-call analysis.

Two modes:
  run_agent()                — Full ReAct loop (legacy, many LLM calls)
  run_single_call_analysis() — Gather all data deterministically, then ONE LLM call

Single-call flow:
  Phase 1 (no LLM): scan → build graph → read key files → search patterns
  Phase 2 (1 LLM call): pass all gathered context, get complete JSON analysis
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from codegrapher.agent.state import AgentState
from codegrapher.agent.tools import make_tools


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


# ── Single-call analysis ───────────────────────────────────────────────────────

_SINGLE_CALL_SYSTEM_PROMPT = """You are an expert software architect. You will receive a complete data dump about a code repository:
- Directory tree and file statistics
- AST graph summary: nodes, edges, communities, god nodes, surprising connections
- Key file contents (actual source code)
- Pattern search results (API routes, models, tests, etc.)

Analyse everything provided and respond with a single JSON object. No explanation before or after — only valid JSON.

Required JSON schema:
{
  "summary": "3-5 sentence technical summary",
  "purpose": "one sentence — what does this repo do",
  "architecture_style": "e.g. MVC / microservices / CLI tool / library / monolith",
  "tech_stack": ["Python", "FastAPI", "..."],
  "key_components": [
    {"name": "...", "description": "...", "files": ["..."], "responsibilities": ["..."]}
  ],
  "data_flow": ["Step 1: ...", "Step 2: ..."],
  "api_endpoints": [{"method": "GET", "path": "/...", "description": "..."}],
  "database_models": ["ModelName", "..."],
  "security_notes": ["..."],
  "improvement_suggestions": ["..."],
  "testing_approach": "description of test strategy or empty string",
  "deployment_info": "how to run/deploy or empty string",
  "code_quality_notes": ["..."],
  "findings": [
    {
      "category": "architecture|component|data_flow|api_endpoint|security|tech_stack|design_pattern|code_quality|improvement|entry_point|dependency|database_model|testing|deployment|file_detail",
      "title": "short title",
      "detail": "full description",
      "files": ["relative/path.py"],
      "confidence": "high|medium|low"
    }
  ]
}

Use [] for list fields where nothing applies. findings should have 6-12 entries covering the most important observations."""


def _gather_repo_data(repo_root: Path, state_accumulator: dict, verbose: bool) -> str:
    """
    Phase 1: run all deterministic steps and assemble a context string for the LLM.
    Writes into state_accumulator so pipeline.py can read scan_result, graph, etc.
    Returns a single string containing all gathered context.
    """
    from codegrapher.agent.tools import (
        _SKIP_DIRS, _SKIP_EXTS, _CODE_EXTS, _build_tree,
    )

    root = repo_root.resolve()
    sections: list[str] = []

    # ── 1. Scan ───────────────────────────────────────────────────────────────
    if verbose:
        print("   [gather] Scanning repository...")

    import os
    ext_counts: dict[str, int] = {}
    total_files = 0
    total_words = 0
    large_files: list[tuple[int, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if any(fname.endswith(e) for e in _SKIP_EXTS):
                continue
            ext = fp.suffix.lower() or "none"
            size = fp.stat().st_size
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            total_files += 1
            large_files.append((size, str(fp.relative_to(root))))
            if ext in _CODE_EXTS:
                try:
                    total_words += fp.read_text(errors="replace").count(" ")
                except Exception:
                    pass

    large_files.sort(reverse=True)
    tree = _build_tree(root)
    scan_result = {
        "total_files": total_files,
        "total_words": total_words,
        "by_extension": dict(sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:20]),
        "largest_files": [{"size": s, "path": p} for s, p in large_files[:10]],
        "directory_tree": tree,
    }
    state_accumulator["scan_result"] = scan_result

    sections.append(f"## Repository Overview\n"
                    f"Files: {total_files} | Words: {total_words}\n"
                    f"Extensions: {json.dumps(scan_result['by_extension'])}\n\n"
                    f"### Directory Tree\n```\n{tree}\n```")

    # ── 2. Build code graph ───────────────────────────────────────────────────
    if verbose:
        print("   [gather] Building code graph (AST + clustering + analysis)...")

    try:
        from codegrapher.core.detect import detect
        from codegrapher.core.extract import collect_files, extract as gf_extract
        from codegrapher.core.build import build_from_json
        from codegrapher.core.cluster import cluster, score_all
        from codegrapher.core.analyze import (
            god_nodes as gf_god_nodes,
            surprising_connections,
            suggest_questions,
        )

        detection = detect(root)
        code_files_raw = detection.get("files", {}).get("code", [])
        code_files = []
        for f in code_files_raw:
            p = Path(f)
            if p.is_dir():
                code_files.extend(collect_files(p))
            elif p.exists():
                code_files.append(p)

        if code_files:
            ast_result = gf_extract(code_files)
            state_accumulator["graphify_ast"] = ast_result
            state_accumulator["detection"] = detection

            G = build_from_json(ast_result)
            communities = cluster(G)
            cohesion = score_all(G, communities)

            labels: dict[int, str] = {}
            for cid, node_ids in communities.items():
                sources = [G.nodes[n].get("source_file", "") for n in node_ids if n in G.nodes]
                sources = [s for s in sources if s]
                if sources:
                    tops = [Path(s).parts[0] if Path(s).parts else s for s in sources]
                    labels[cid] = max(set(tops), key=tops.count)
                else:
                    labels[cid] = f"Community {cid}"

            state_accumulator["graph"] = G
            state_accumulator["communities"] = communities
            state_accumulator["cohesion"] = cohesion
            state_accumulator["community_labels"] = labels

            god = gf_god_nodes(G, top_n=10)
            surprises = surprising_connections(G, communities, top_n=5)
            questions = suggest_questions(G, communities, labels, top_n=5)
            state_accumulator["god_nodes"] = god
            state_accumulator["surprising_connections"] = surprises
            state_accumulator["suggested_questions"] = questions

            # Diagrams (deterministic)
            try:
                from codegrapher.output.mermaid_converter import generate_all_diagrams
                diagrams = generate_all_diagrams(state_accumulator)
                state_accumulator["diagrams"] = diagrams
            except Exception:
                pass

            top_communities = [
                {"id": cid, "label": labels.get(cid, f"C{cid}"),
                 "size": len(nids), "cohesion": round(cohesion.get(cid, 0.0), 3)}
                for cid, nids in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:8]
            ]

            sections.append(
                f"## Code Graph Analysis\n"
                f"Nodes: {G.number_of_nodes()} | Edges: {G.number_of_edges()} | "
                f"Files processed: {len(code_files)}\n\n"
                f"### Communities\n{json.dumps(top_communities, indent=2)}\n\n"
                f"### God Nodes (highest connectivity)\n{json.dumps(god[:8], indent=2)}\n\n"
                f"### Surprising Cross-Community Connections\n{json.dumps(surprises, indent=2)}\n\n"
                f"### Suggested Investigation Questions\n{json.dumps(questions, indent=2)}"
            )
        else:
            sections.append("## Code Graph Analysis\nNo code files found.")

    except Exception as e:
        sections.append(f"## Code Graph Analysis\nError: {e}")

    # ── 3. Read key files ─────────────────────────────────────────────────────
    if verbose:
        print("   [gather] Reading key files...")

    # Priority: entry points → god node sources → config files
    candidate_names = [
        "__main__.py", "main.py", "app.py", "server.py", "cli.py",
        "index.py", "run.py", "manage.py",
        "config.py", "settings.py", "configuration.py",
        "models.py", "schema.py", "database.py",
        "pipeline.py", "routes.py", "views.py", "api.py",
    ]
    # Add god node source files
    god_sources = []
    for gn in state_accumulator.get("god_nodes", [])[:5]:
        src = gn.get("source_file", "")
        if src:
            try:
                god_sources.append(str(Path(src).relative_to(root)))
            except ValueError:
                god_sources.append(src)

    def _find_file(name: str) -> Path | None:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if name in filenames:
                return Path(dirpath) / name
        return None

    files_to_read: list[Path] = []
    seen: set[str] = set()

    # God node files first
    for rel in god_sources:
        p = (root / rel).resolve()
        if p.exists() and str(p) not in seen:
            files_to_read.append(p)
            seen.add(str(p))

    # Then entry points / config
    for name in candidate_names:
        if len(files_to_read) >= 8:
            break
        p = _find_file(name)
        if p and str(p) not in seen:
            files_to_read.append(p)
            seen.add(str(p))

    file_contents_parts = []
    for fp in files_to_read[:8]:
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            if len(lines) > 250:
                content = "\n".join(lines[:250]) + f"\n... [{len(lines)-250} more lines]"
            rel = str(fp.relative_to(root))
            file_contents_parts.append(f"### {rel}\n```\n{content}\n```")
        except Exception:
            pass

    if file_contents_parts:
        sections.append("## Key File Contents\n\n" + "\n\n".join(file_contents_parts))

    # ── 4. Pattern searches ───────────────────────────────────────────────────
    if verbose:
        print("   [gather] Searching key patterns...")

    import fnmatch as _fnmatch
    import re as _re

    def _search(pattern: str, glob: str = "*", max_results: int = 25) -> list[str]:
        try:
            regex = _re.compile(pattern, _re.IGNORECASE)
        except _re.error:
            regex = _re.compile(_re.escape(pattern), _re.IGNORECASE)
        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if not _fnmatch.fnmatch(fname, glob):
                    continue
                fp = Path(dirpath) / fname
                if any(fname.endswith(e) for e in _SKIP_EXTS):
                    continue
                try:
                    rel = str(fp.relative_to(root))
                    for i, line in enumerate(fp.read_text(errors="replace").split("\n"), 1):
                        if regex.search(line):
                            matches.append(f"{rel}:{i}: {line.strip()[:120]}")
                            if len(matches) >= max_results:
                                return matches
                except Exception:
                    continue
        return matches

    searches = {
        "API routes/endpoints": _search(r"@(app|router|blueprint)\.(get|post|put|delete|patch)|urlpatterns|path\(|route\("),
        "Database models": _search(r"class\s+\w+.*Model|Base\)|DeclarativeBase|mongoose\.model|Schema\("),
        "Authentication": _search(r"auth|jwt|token|password|login|oauth", max_results=15),
        "Tests": _search(r"def test_|it\(|describe\(|@pytest|unittest", glob="test*"),
        "Environment/Config": _search(r"os\.environ|getenv|dotenv|Config\(|settings\.", max_results=15),
    }

    search_parts = []
    for label, results in searches.items():
        if results:
            search_parts.append(f"### {label} ({len(results)} matches)\n" + "\n".join(results))
    if search_parts:
        sections.append("## Pattern Search Results\n\n" + "\n\n".join(search_parts))

    return "\n\n---\n\n".join(sections)


def _parse_llm_json(content: str) -> dict:
    """Extract JSON object from LLM response (handles markdown code blocks)."""
    # Strip markdown code fences
    content = content.strip()
    for fence in ("```json", "```"):
        if content.startswith(fence):
            content = content[len(fence):]
            if content.endswith("```"):
                content = content[:-3]
            break
    content = content.strip()

    # Find the outermost { ... }
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        content = content[start:end + 1]

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Fallback: return minimal valid structure
        return {
            "summary": content[:500],
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
            "findings": [],
        }


def run_single_call_analysis(
    llm: BaseChatModel,
    repo_root: Path,
    verbose: bool = False,
) -> tuple[dict, dict]:
    """
    Gather all repository data deterministically (no LLM), then make exactly
    ONE LLM call with the full context to produce the complete analysis.

    Returns
    -------
    state_accumulator : dict
        Populated with scan_result, graph, communities, god_nodes, diagrams,
        findings, finish_data — same shape as run_agent() output.
    final_state : dict
        Minimal state dict (iteration=1 since only one LLM call was made).
    """
    state_accumulator: dict = {
        "finished": False,
        "findings": [],
        "diagrams": [],
        "finish_data": {},
    }

    # Phase 1: gather everything deterministically
    context = _gather_repo_data(repo_root, state_accumulator, verbose)

    # Phase 2: single LLM call
    if verbose:
        print("   [LLM] Sending full context — single call analysis...")

    response = llm.invoke([
        SystemMessage(content=_SINGLE_CALL_SYSTEM_PROMPT),
        HumanMessage(content=f"Repository path: {repo_root}\n\n{context}"),
    ])

    # Parse structured JSON from response
    parsed = _parse_llm_json(response.content)

    # Populate finish_data (same shape pipeline.py / pdf_generator expect)
    state_accumulator["finish_data"] = {
        "summary": parsed.get("summary", ""),
        "purpose": parsed.get("purpose", ""),
        "architecture_style": parsed.get("architecture_style", ""),
        "tech_stack": parsed.get("tech_stack", []),
        "key_components": parsed.get("key_components", []),
        "data_flow": parsed.get("data_flow", []),
        "api_endpoints": parsed.get("api_endpoints", []),
        "database_models": parsed.get("database_models", []),
        "security_notes": parsed.get("security_notes", []),
        "improvement_suggestions": parsed.get("improvement_suggestions", []),
        "testing_approach": parsed.get("testing_approach", ""),
        "deployment_info": parsed.get("deployment_info", ""),
        "code_quality_notes": parsed.get("code_quality_notes", []),
    }

    # Populate findings list (same shape record_finding produces)
    for f in parsed.get("findings", []):
        state_accumulator["findings"].append({
            "category": f.get("category", "architecture"),
            "title": f.get("title", ""),
            "detail": f.get("detail", ""),
            "files": f.get("files", []),
            "confidence": f.get("confidence", "high"),
        })

    state_accumulator["finished"] = True

    if verbose:
        print(f"   ✅ Single-call analysis complete")
        print(f"      Findings : {len(state_accumulator['findings'])}")
        print(f"      Components: {len(state_accumulator['finish_data'].get('key_components', []))}")

    return state_accumulator, {"iteration": 1}
