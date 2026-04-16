"""
Synthesiser — separate LLM call that produces finish_data.

This runs AFTER the ReAct exploration loop has ended.
It receives a compact, compressed context built from:
  - state_accumulator  : structured outputs from the 4 core tools
  - exploration_notes  : compressed digest of read_file / search_code
                         results extracted from the agent message history

Why separate from the agent loop
---------------------------------
During exploration the message history grows to 3-5K lines of raw
read_file / search_code results. Calling finish_analysis as a tool
inside that loop means the LLM must process the entire history on
every attempt — and if Bedrock throttles even once, the agent retries
blindly with the same giant payload, causing the loop seen in production.

Here the synthesiser:
  1. Receives only a compact summary (~500-1000 tokens), not the raw history
  2. Has its own retry loop with exponential backoff
  3. Can be retried without re-running the exploration
  4. Uses structured output (with_structured_output / JSON mode) so the
     response is parsed directly into finish_data — no tool call overhead

Flow
----
  run_agent()  ← exploration only (scan/extract/cluster/analyze/read/search)
       ↓ state_accumulator + final_state.messages
  build_synthesis_context()  ← pure Python, no LLM, compresses everything
       ↓ compact dict
  synthesise()  ← single LLM call with retry/backoff
       ↓ finish_data dict
  json_agent.build_analysis_json()
       ↓
  pdf_generator
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, ToolMessage


# ── Synthesis system prompt ───────────────────────────────────────────────────

_SYNTHESIS_SYSTEM = """You are an expert software architect.
You have been given a structural analysis of a code repository.
The analysis includes:
  - File inventory and directory structure
  - AST-extracted classes, functions, and call relationships
  - Community clusters (groups of related code)
  - God nodes (most-connected core abstractions)
  - Surprising cross-community connections
  - Notes from reading key files and searching code patterns

Your job: produce a complete JSON analysis document.
Respond ONLY with a valid JSON object — no markdown fences, no preamble, no explanation.

Required JSON structure:
{
  "summary": "3-5 sentence technical summary of the codebase",
  "purpose": "One sentence — what does this repo DO?",
  "architecture_style": "e.g. MVC, microservices, CLI tool, library, monolith",
  "tech_stack": ["list", "of", "technologies"],
  "key_components": [
    {"name": "...", "description": "...", "files": ["..."], "responsibilities": ["..."]}
  ],
  "data_flow": ["step 1", "step 2", "step 3"],
  "api_endpoints": [
    {"method": "GET", "path": "/api/...", "description": "..."}
  ],
  "database_models": ["ModelName1", "ModelName2"],
  "security_notes": ["observation 1", "observation 2"],
  "improvement_suggestions": ["suggestion 1", "suggestion 2"],
  "file_details": [
    {"file": "relative/path.ts", "summary": "what this file does", "confidence": "high"}
  ],
  "architecture_notes": [
    {"title": "short title", "detail": "full observation"}
  ],
  "dependency_notes": [
    {"title": "package name", "detail": "why it matters or concerns"}
  ],
  "testing_approach": "description of testing strategy",
  "deployment_info": "how the repo is run / deployed / packaged",
  "code_quality_notes": ["observation 1", "observation 2"]
}

Rules:
- Use ONLY the information provided. Do not invent file names or APIs.
- If a field has no data, use an empty list [] or empty string "".
- The JSON must be parseable by Python json.loads().
- Do not wrap in markdown code fences.
"""


# ── Context builder ────────────────────────────────────────────────────────────

def build_synthesis_context(
    state_accumulator: dict,
    messages: list[BaseMessage],
    max_nodes: int = 80,
    max_exploration_chars: int = 8000,
) -> str:
    """
    Build a compact text payload for the synthesiser from:
      - state_accumulator  : structured core tool outputs
      - messages           : the agent message history (for read_file/search_code notes)

    Returns a single string under ~2000 tokens.

    Parameters
    ----------
    max_nodes : int
        Max AST nodes to include in the context (keeps token count bounded).
    max_exploration_chars : int
        Max characters of compressed exploration notes to include.
    """
    parts: list[str] = []

    # ── 1. Repository overview ────────────────────────────────────────────────
    scan = state_accumulator.get("scan_result", {})
    if scan:
        ext_summary = ", ".join(
            f"{ext}:{cnt}"
            for ext, cnt in list(scan.get("by_extension", {}).items())[:10]
        )
        parts.append(
            f"REPOSITORY OVERVIEW\n"
            f"Total files: {scan.get('total_files', 0)}\n"
            f"File types: {ext_summary}\n"
        )
        tree = scan.get("directory_tree", "")
        if tree:
            # Limit tree to 60 lines
            tree_lines = tree.split("\n")[:60]
            parts.append("DIRECTORY TREE\n" + "\n".join(tree_lines))

    # ── 2. AST nodes (capped) ─────────────────────────────────────────────────
    ast_data = state_accumulator.get("graphify_ast", {})
    nodes = ast_data.get("nodes", [])
    edges = ast_data.get("edges", [])
    if nodes:
        # Group by source_file for a compact listing
        by_file: dict[str, list[str]] = {}
        for n in nodes[:max_nodes * 3]:  # sample wider, group tighter
            src = n.get("source_file", "unknown")
            label = n.get("label", n.get("id", ""))
            by_file.setdefault(src, []).append(label)

        node_lines = []
        for src, labels in list(by_file.items())[:max_nodes]:
            node_lines.append(f"  {src}: {', '.join(labels[:12])}")

        parts.append(
            f"AST NODES ({len(nodes)} total, showing {len(node_lines)} files)\n"
            + "\n".join(node_lines)
        )

    # ── 3. Communities ────────────────────────────────────────────────────────
    communities = state_accumulator.get("communities", {})
    labels = state_accumulator.get("community_labels", {})
    cohesion = state_accumulator.get("cohesion", {})
    if communities:
        com_lines = []
        for cid, node_ids in sorted(
            communities.items(), key=lambda x: len(x[1]), reverse=True
        )[:15]:
            lbl = labels.get(cid, f"Community {cid}")
            coh = cohesion.get(cid, 0.0)
            com_lines.append(f"  [{lbl}] size={len(node_ids)} cohesion={coh:.2f}")
        parts.append("COMMUNITIES\n" + "\n".join(com_lines))

    # ── 4. God nodes ──────────────────────────────────────────────────────────
    god_nodes = state_accumulator.get("god_nodes", [])
    if god_nodes:
        gn_lines = [
            f"  {g.get('label', g.get('id', ''))} ({g.get('edges', 0)} connections)"
            for g in god_nodes
        ]
        parts.append("GOD NODES (most-connected abstractions)\n" + "\n".join(gn_lines))

    # ── 5. Surprising connections ─────────────────────────────────────────────
    surprises = state_accumulator.get("surprising_connections", [])
    if surprises:
        sc_lines = [
            f"  {s.get('source','')} → {s.get('target','')} "
            f"[{s.get('confidence','')}]"
            for s in surprises
        ]
        parts.append("SURPRISING CONNECTIONS\n" + "\n".join(sc_lines))

    # ── 6. Suggested questions ────────────────────────────────────────────────
    questions = state_accumulator.get("suggested_questions", [])
    if questions:
        q_lines = [
            f"  - {q.get('question', str(q)) if isinstance(q, dict) else q}"
            for q in questions
        ]
        parts.append("SUGGESTED QUESTIONS FROM GRAPH\n" + "\n".join(q_lines))

    # ── 7. Exploration notes (compressed read_file + search_code results) ─────
    exploration = _compress_exploration(messages, max_exploration_chars)
    if exploration:
        parts.append("EXPLORATION NOTES (from file reads and code searches)\n" + exploration)

    return "\n\n".join(parts)


def _compress_exploration(
    messages: list[BaseMessage],
    max_chars: int,
) -> str:
    """
    Extract and compress ToolMessage results from read_file and search_code calls.

    Each raw result (potentially 300 lines) is trimmed to its first 60 chars
    as a title line, giving the synthesiser the gist without the full body.
    The agent already processed these — the synthesiser just needs the names
    and surface-level content, not every line.
    """
    notes: list[str] = []
    total = 0

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        content = str(msg.content)

        # read_file results start with "=== path (N lines total) ==="
        if content.startswith("==="):
            header = content.split("\n")[0]          # "=== src/main.ts (120 lines) ==="
            # Take the first 10 meaningful lines of the body as a snippet
            body_lines = [
                ln.strip() for ln in content.split("\n")[1:20]
                if ln.strip() and not ln.strip().startswith("//")
                and not ln.strip().startswith("#")
            ][:6]
            snippet = " | ".join(body_lines)[:200]
            note = f"{header}\n  → {snippet}"

        # search_code results start with "Found N matches"
        elif content.startswith("Found ") or content.startswith("No matches"):
            # Keep the header + first 5 match lines
            lines = content.split("\n")
            note = "\n  ".join(lines[:6])

        else:
            continue

        line = note + "\n"
        if total + len(line) > max_chars:
            notes.append("... (further exploration notes truncated)")
            break
        notes.append(line)
        total += len(line)

    return "\n".join(notes)


# ── Synthesiser ────────────────────────────────────────────────────────────────

def synthesise(
    llm: BaseChatModel,
    context: str,
    max_retries: int = 4,
    base_delay: float = 2.0,
    verbose: bool = False,
) -> dict:
    """
    Make a single dedicated LLM call to synthesise the analysis.

    Retries with exponential backoff on throttling or parse errors.
    Returns a finish_data dict ready for json_agent.build_analysis_json().

    Parameters
    ----------
    llm : BaseChatModel
        The same LLM instance used for exploration.
    context : str
        Compact context payload from build_synthesis_context().
    max_retries : int
        Maximum attempts before giving up (default 4).
    base_delay : float
        Initial backoff delay in seconds, doubles each retry (default 2s).
    verbose : bool
        Print attempt/retry info.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    user_message = (
        "Based on the repository analysis below, produce the JSON document.\n\n"
        + context
    )

    messages = [
        SystemMessage(content=_SYNTHESIS_SYSTEM),
        HumanMessage(content=user_message),
    ]

    delay = base_delay
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        if verbose:
            print(f"   [Synthesiser attempt {attempt}/{max_retries}]")

        try:
            response = llm.invoke(messages)
            raw = response.content if hasattr(response, "content") else str(response)

            # Strip any accidental markdown fences
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            finish_data = json.loads(raw)

            if verbose:
                print(f"   ✅ Synthesis complete on attempt {attempt}")

            return finish_data

        except json.JSONDecodeError as e:
            last_error = e
            if verbose:
                print(f"   ⚠️  JSON parse error on attempt {attempt}: {e}")
            # JSON parse errors don't need backoff — retry immediately once
            if attempt == 1:
                continue

        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            is_throttle = any(
                kw in err_str
                for kw in ("throttl", "toomanytokens", "too many", "rate limit",
                           "429", "serviceunavailable", "slow down")
            )
            if verbose:
                kind = "throttle" if is_throttle else "error"
                print(f"   ⚠️  {kind} on attempt {attempt}: {e}")

            if attempt < max_retries:
                wait = delay if is_throttle else base_delay
                if verbose:
                    print(f"   ↻  Retrying in {wait:.1f}s...")
                time.sleep(wait)
                delay *= 2  # exponential backoff

    # All retries exhausted — return a minimal safe dict so the pipeline
    # can still produce a partial PDF rather than crashing entirely
    if verbose:
        print(f"   ❌ Synthesis failed after {max_retries} attempts: {last_error}")

    return _empty_finish_data(str(last_error))


def _empty_finish_data(error_note: str = "") -> dict:
    """Return a safe empty finish_data when synthesis fails."""
    return {
        "summary": f"Synthesis failed — partial report. Error: {error_note}",
        "purpose": "",
        "architecture_style": "",
        "tech_stack": [],
        "key_components": [],
        "data_flow": [],
        "api_endpoints": [],
        "database_models": [],
        "security_notes": [],
        "improvement_suggestions": [],
        "file_details": [],
        "architecture_notes": [],
        "dependency_notes": [],
        "testing_approach": "",
        "deployment_info": "",
        "code_quality_notes": [],
    }
