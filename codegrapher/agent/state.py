"""
LangGraph state schema.

The State TypedDict is the single shared data structure that flows through
every node in the LangGraph graph. Each node reads from state and returns
a partial dict that LangGraph merges back in.

Design decisions:
- Messages list carries the full LLM conversation history (tool calls + results).
- findings / diagrams / summary are built up incrementally as the agent works.
- graph_data holds the raw graphify output (nodes, edges, communities).
- phase tracks where the agent is so the router can decide the next node.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class GraphifyOutput(TypedDict):
    """Raw output from graphify extraction + clustering."""
    nodes: list[dict]
    edges: list[dict]
    communities: dict          # {community_id: [node_ids]}
    cohesion_scores: dict      # {community_id: float}
    community_labels: dict     # {community_id: str}
    god_nodes: list[dict]
    surprising_connections: list[dict]
    suggested_questions: list[dict]
    total_files: int
    total_words: int
    languages: dict            # {lang: count}
    directory_tree: str


class Finding(TypedDict):
    category: str
    title: str
    detail: str
    files: list[str]
    confidence: str            # high | medium | low


class Diagram(TypedDict):
    diagram_type: str          # architecture | flow | components | er
    mermaid_code: str
    description: str


class AgentState(TypedDict):
    # ── Inputs ─────────────────────────────────────────────────────────────────
    repo_path: str
    provider_name: str

    # ── LLM conversation (LangGraph manages merging via add_messages) ─────────
    messages: Annotated[list, add_messages]

    # ── graphify extraction output ────────────────────────────────────────────
    graphify_output: Optional[GraphifyOutput]

    # ── Agent discoveries (accumulated across nodes) ──────────────────────────
    findings: list[Finding]
    diagrams: list[Diagram]

    # ── Final summary (written by summarize node) ─────────────────────────────
    summary: str
    purpose: str
    architecture_style: str
    tech_stack: list[str]
    key_components: list[dict]
    data_flow: list[str]
    api_endpoints: list[dict]
    database_models: list[str]
    security_notes: list[str]
    improvement_suggestions: list[str]
    testing_approach: str
    deployment_info: str
    code_quality_notes: list[str]

    # ── Control flow ──────────────────────────────────────────────────────────
    phase: str                 # extract → analyze → summarize → diagram → done
    error: Optional[str]
    iteration: int             # safety counter
