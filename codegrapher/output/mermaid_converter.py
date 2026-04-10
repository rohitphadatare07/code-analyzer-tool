"""
Deterministic Mermaid diagram generation from graphify graph data.

No LLM involved. No hallucination possible.
Every diagram is derived directly from the NetworkX graph that graphify built —
the same graph used for god_nodes, community detection, and surprising connections.

Three diagrams produced:
  1. architecture  — top-level file/module dependencies (graph TD)
  2. flow          — data flow through the system (sequenceDiagram)
  3. components    — community-grouped module structure (graph LR with subgraphs)

Design rules:
  - Max 20 nodes per diagram (above that, pruned by degree)
  - Node IDs sanitized to [A-Za-z0-9_] only (Mermaid requirement)
  - Labels truncated to 25 chars
  - Relation labels on edges where meaningful
  - Community subgraphs colored via Mermaid classDef
"""
from __future__ import annotations

import re
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_id(raw: str) -> str:
    """Convert any string to a valid Mermaid node ID."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or s[0].isdigit():
        s = "n_" + s
    return s[:40]


def _label(raw: str, max_len: int = 25) -> str:
    """Shorten and escape a label for Mermaid."""
    raw = raw.strip().replace('"', "'").replace("\n", " ")
    if len(raw) > max_len:
        raw = raw[: max_len - 1] + "…"
    return raw


def _file_module(source_file: str) -> str:
    """Extract module/directory name from a file path."""
    if not source_file:
        return "unknown"
    p = Path(source_file)
    # Use parent dir if file is inside a package, else stem
    parts = p.parts
    if len(parts) >= 2:
        return parts[0]  # top-level dir
    return p.stem


def _top_n_nodes(G: "nx.Graph", n: int, exclude_isolated: bool = True) -> list[str]:
    """Return top-n nodes by degree, excluding isolates if requested."""
    nodes = [
        (d, node) for node, d in G.degree()
        if not (exclude_isolated and d == 0)
    ]
    nodes.sort(reverse=True)
    return [node for _, node in nodes[:n]]


def _relation_label(relation: str) -> str:
    """Shorten relation names for Mermaid edge labels."""
    mapping = {
        "imports": "imports",
        "imports_from": "from",
        "calls": "calls",
        "contains": "has",
        "implements": "impl",
        "references": "ref",
        "cites": "cites",
        "extends": "extends",
        "uses": "uses",
        "semantically_similar_to": "~similar",
        "rationale_for": "explains",
        "shares_data_with": "shares",
    }
    return mapping.get(relation, relation[:10] if relation else "")


# ── Diagram 1: Architecture (file/module level) ────────────────────────────────

def build_architecture_diagram(
    G: "nx.Graph",
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    god_node_ids: set[str],
    max_nodes: int = 18,
) -> str:
    """
    Top-down architecture diagram.

    Shows the most important modules and how they connect.
    God nodes get a distinct style. Community membership shown via subgraphs.
    Pruned to max_nodes by degree to keep the diagram readable.
    """
    if not HAS_NETWORKX or G is None or G.number_of_nodes() == 0:
        return _fallback_architecture()

    # Pick top nodes by degree
    top_nodes = set(_top_n_nodes(G, max_nodes))
    # Always include god nodes (up to 5)
    for gn in list(god_node_ids)[:5]:
        top_nodes.add(gn)
    top_nodes = list(top_nodes)[:max_nodes]

    # Build node → safe_id map
    nmap: dict[str, str] = {}
    used_ids: dict[str, int] = {}
    for n in top_nodes:
        base = _safe_id(G.nodes[n].get("label", n))
        if base in used_ids:
            used_ids[base] += 1
            nmap[n] = f"{base}_{used_ids[base]}"
        else:
            used_ids[base] = 0
            nmap[n] = base

    top_set = set(top_nodes)

    # Collect relevant edges (both endpoints in top_nodes, skip structural clutter)
    skip_relations = {"contains", "method"}
    edges = []
    seen_pairs: set[frozenset] = set()
    for u, v, data in G.edges(data=True):
        if u not in top_set or v not in top_set:
            continue
        relation = data.get("relation", "")
        if relation in skip_relations:
            continue
        pair = frozenset({u, v})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edges.append((u, v, relation))

    # Group nodes by community
    node_community: dict[str, int] = {}
    for cid, members in communities.items():
        for m in members:
            if m in top_set:
                node_community[m] = cid

    # Nodes with no community go into a default group
    community_groups: dict[int, list[str]] = defaultdict(list)
    ungrouped = []
    for n in top_nodes:
        cid = node_community.get(n)
        if cid is not None:
            community_groups[cid].append(n)
        else:
            ungrouped.append(n)

    lines = ["graph TD"]
    lines.append("")

    # Subgraphs per community (limit to 4 communities for readability)
    rendered_in_subgraph: set[str] = set()
    for cid, members in sorted(community_groups.items(), key=lambda x: -len(x[1]))[:4]:
        clabel = _label(community_labels.get(cid, f"Module {cid}"), 20)
        lines.append(f'  subgraph {_safe_id(clabel)}["{clabel}"]')
        for n in members:
            node_label = _label(G.nodes[n].get("label", n))
            nid = nmap[n]
            shape = f'["{node_label}"]' if n not in god_node_ids else f'(("{node_label}"))'
            lines.append(f"    {nid}{shape}")
            rendered_in_subgraph.add(n)
        lines.append("  end")
        lines.append("")

    # Ungrouped nodes outside subgraphs
    for n in ungrouped:
        node_label = _label(G.nodes[n].get("label", n))
        nid = nmap[n]
        shape = f'["{node_label}"]' if n not in god_node_ids else f'(("{node_label}"))'
        lines.append(f"  {nid}{shape}")

    if ungrouped:
        lines.append("")

    # Edges
    for u, v, relation in edges:
        uid, vid = nmap[u], nmap[v]
        rl = _relation_label(relation)
        if rl and rl not in ("imports", "from"):
            lines.append(f"  {uid} -->|{rl}| {vid}")
        else:
            lines.append(f"  {uid} --> {vid}")

    # Style god nodes
    if god_node_ids:
        lines.append("")
        god_ids_in_diagram = [nmap[n] for n in god_node_ids if n in nmap]
        if god_ids_in_diagram:
            lines.append("  classDef godNode fill:#3b82f6,stroke:#1d4ed8,color:#fff,font-weight:bold")
            lines.append(f"  class {','.join(god_ids_in_diagram)} godNode")

    return "\n".join(lines)


# ── Diagram 2: Data Flow (sequence diagram) ───────────────────────────────────

def build_flow_diagram(
    G: "nx.Graph",
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    god_node_ids: set[str],
    entry_points: list[str] | None = None,
    max_actors: int = 8,
) -> str:
    """
    Sequence diagram showing the main data flow.

    Actors are derived from:
    1. Entry point nodes (if provided)
    2. God nodes (highest-degree real entities)
    3. Remaining top nodes to fill max_actors

    Interactions derived from actual edges in the graph,
    filtered to meaningful call/use/reference relationships.
    """
    if not HAS_NETWORKX or G is None or G.number_of_nodes() == 0:
        return _fallback_flow()

    # Pick actors — start with god nodes as they're the most important
    actor_nodes: list[str] = []

    # Add entry points first
    if entry_points:
        for ep in entry_points[:2]:
            for n in G.nodes():
                label = G.nodes[n].get("label", "")
                source = G.nodes[n].get("source_file", "")
                if ep in label or ep in source:
                    if n not in actor_nodes:
                        actor_nodes.append(n)

    # Add god nodes
    for gn in list(god_node_ids)[:4]:
        if gn not in actor_nodes:
            actor_nodes.append(gn)

    # Fill remaining slots with high-degree nodes
    for _, n in sorted(((d, n) for n, d in G.degree()), reverse=True):
        if len(actor_nodes) >= max_actors:
            break
        if n not in actor_nodes:
            actor_nodes.append(n)

    actor_nodes = actor_nodes[:max_actors]
    actor_set = set(actor_nodes)

    # Build actor labels (short, unique)
    actor_labels: dict[str, str] = {}
    used: set[str] = set()
    for n in actor_nodes:
        raw = G.nodes[n].get("label", n)
        short = _label(raw, 18)
        # Deduplicate
        if short in used:
            short = short[:14] + "_" + n[-3:]
        used.add(short)
        actor_labels[n] = short

    lines = ["sequenceDiagram"]
    lines.append("  autonumber")
    lines.append("")

    # Declare participants
    for n in actor_nodes:
        lbl = actor_labels[n]
        lines.append(f"  participant {_safe_id(lbl)} as {lbl}")

    lines.append("")

    # Derive interactions from edges
    flow_relations = {"calls", "imports", "imports_from", "uses", "references", "implements"}
    interactions = []
    seen: set[tuple] = set()

    for u, v, data in G.edges(data=True):
        if u not in actor_set or v not in actor_set:
            continue
        relation = data.get("relation", "")
        if relation not in flow_relations:
            continue
        key = (u, v)
        if key in seen:
            continue
        seen.add(key)

        src_id = _safe_id(actor_labels[u])
        tgt_id = _safe_id(actor_labels[v])
        conf = data.get("confidence", "EXTRACTED")
        arrow = "->>" if conf == "EXTRACTED" else "-->>"
        rl = _relation_label(relation)
        interactions.append((src_id, tgt_id, arrow, rl))

    # Sort so higher-confidence (extracted) interactions come first
    interactions.sort(key=lambda x: 0 if x[2] == "->>" else 1)

    for src, tgt, arrow, label in interactions[:15]:
        lines.append(f"  {src}{arrow}{tgt}: {label}")

    if not interactions:
        # Fallback: show top edges regardless of relation
        count = 0
        for u, v, data in G.edges(data=True):
            if u not in actor_set or v not in actor_set:
                continue
            src_id = _safe_id(actor_labels[u])
            tgt_id = _safe_id(actor_labels[v])
            relation = data.get("relation", "uses")
            lines.append(f"  {src_id}->>{tgt_id}: {_relation_label(relation)}")
            count += 1
            if count >= 10:
                break

    return "\n".join(lines)


# ── Diagram 3: Component structure (community-grouped) ─────────────────────────

def build_component_diagram(
    G: "nx.Graph",
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    cohesion_scores: dict[int, float],
    god_node_ids: set[str],
    max_communities: int = 5,
    max_nodes_per_community: int = 5,
) -> str:
    """
    Left-right component diagram with community subgraphs.

    Each community becomes a subgraph. Nodes are real code entities.
    Cross-community edges shown as connections between subgraphs.
    Cohesion scores annotated on subgraph labels.
    """
    if not HAS_NETWORKX or G is None or G.number_of_nodes() == 0:
        return _fallback_components()

    # Pick top communities by size
    top_cids = sorted(communities.keys(), key=lambda c: len(communities[c]), reverse=True)
    top_cids = top_cids[:max_communities]

    # Build node set for this diagram
    diagram_nodes: dict[str, str] = {}  # node_id → safe_id
    used_ids: dict[str, int] = {}

    for cid in top_cids:
        # Take top nodes from each community by degree
        members = communities[cid]
        sorted_members = sorted(members, key=lambda n: G.degree(n), reverse=True)
        for n in sorted_members[:max_nodes_per_community]:
            raw = G.nodes[n].get("label", n) if n in G.nodes else n
            base = _safe_id(raw)
            if base in used_ids:
                used_ids[base] += 1
                safe = f"{base}_{used_ids[base]}"
            else:
                used_ids[base] = 0
                safe = base
            diagram_nodes[n] = safe

    diagram_set = set(diagram_nodes.keys())

    lines = ["graph LR"]
    lines.append("")

    # Subgraph per community
    for cid in top_cids:
        members = communities[cid]
        cname = community_labels.get(cid, f"Community {cid}")
        cohesion = cohesion_scores.get(cid, 0.0)
        cohesion_str = f" [{cohesion:.0%}]" if cohesion > 0 else ""
        clabel = _label(f"{cname}{cohesion_str}", 30)
        safe_cid = _safe_id(f"sg_{cid}_{cname}")

        sorted_members = sorted(members, key=lambda n: G.degree(n), reverse=True)
        visible = [n for n in sorted_members if n in diagram_nodes]

        if not visible:
            continue

        lines.append(f"  subgraph {safe_cid}[\"{clabel}\"]")
        for n in visible:
            node_label = _label(G.nodes[n].get("label", n) if n in G.nodes else n)
            nid = diagram_nodes[n]
            if n in god_node_ids:
                lines.append(f'    {nid}(("{node_label}"))')
            else:
                lines.append(f'    {nid}["{node_label}"]')
        lines.append("  end")
        lines.append("")

    # Cross-community edges only
    node_community: dict[str, int] = {}
    for cid in top_cids:
        for n in communities[cid]:
            node_community[n] = cid

    seen_cross: set[frozenset] = set()
    skip_relations = {"contains", "method"}
    for u, v, data in G.edges(data=True):
        if u not in diagram_set or v not in diagram_set:
            continue
        cu = node_community.get(u)
        cv = node_community.get(v)
        if cu is None or cv is None or cu == cv:
            continue
        relation = data.get("relation", "")
        if relation in skip_relations:
            continue
        pair = frozenset({u, v})
        if pair in seen_cross:
            continue
        seen_cross.add(pair)
        uid, vid = diagram_nodes[u], diagram_nodes[v]
        rl = _relation_label(relation)
        if rl:
            lines.append(f"  {uid} -->|{rl}| {vid}")
        else:
            lines.append(f"  {uid} --> {vid}")

    # Style god nodes
    god_in_diagram = [diagram_nodes[n] for n in god_node_ids if n in diagram_nodes]
    if god_in_diagram:
        lines.append("")
        lines.append("  classDef godNode fill:#3b82f6,stroke:#1d4ed8,color:#fff,font-weight:bold")
        lines.append(f"  class {','.join(god_in_diagram)} godNode")

    return "\n".join(lines)


# ── Fallbacks (when no graph data available) ───────────────────────────────────

def _fallback_architecture() -> str:
    return (
        "graph TD\n"
        "  A[\"Run extract_ast_graph first\"] --> B[\"Then cluster_communities\"]\n"
        "  B --> C[\"Diagrams generated from real graph data\"]"
    )


def _fallback_flow() -> str:
    return (
        "sequenceDiagram\n"
        "  participant Agent\n"
        "  participant GraphifyTools\n"
        "  Agent->>GraphifyTools: extract_ast_graph()\n"
        "  GraphifyTools-->>Agent: nodes + edges\n"
        "  Agent->>GraphifyTools: cluster_communities()\n"
        "  GraphifyTools-->>Agent: community map"
    )


def _fallback_components() -> str:
    return (
        "graph LR\n"
        "  subgraph Core[\"Core Pipeline\"]\n"
        "    A[\"AST Extractor\"]\n"
        "    B[\"Graph Builder\"]\n"
        "  end\n"
        "  subgraph Analysis[\"Analysis\"]\n"
        "    C[\"Community Detector\"]\n"
        "    D[\"God Node Finder\"]\n"
        "  end\n"
        "  Core --> Analysis"
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_all_diagrams(state_accumulator: dict) -> list[dict]:
    """
    Generate all three Mermaid diagrams from graphify graph data.

    Reads directly from state_accumulator (written by tools.py):
      - graph       : NetworkX graph from build_from_json()
      - communities : {cid: [node_ids]} from cluster()
      - community_labels : {cid: str}
      - cohesion    : {cid: float}
      - god_nodes   : [{id, label, edges}]

    Returns list of diagram dicts ready for the PDF generator.
    """
    G = state_accumulator.get("graph")
    communities: dict = state_accumulator.get("communities", {})
    community_labels: dict = state_accumulator.get("community_labels", {})
    cohesion: dict = state_accumulator.get("cohesion", {})
    god_nodes_raw: list = state_accumulator.get("god_nodes", [])

    god_node_ids: set[str] = {gn["id"] for gn in god_nodes_raw}

    # Convert community_labels keys to int if needed
    community_labels = {
        int(k) if isinstance(k, str) else k: v
        for k, v in community_labels.items()
    }
    communities = {
        int(k) if isinstance(k, str) else k: v
        for k, v in communities.items()
    }
    cohesion = {
        int(k) if isinstance(k, str) else k: v
        for k, v in cohesion.items()
    }

    diagrams = []

    # 1. Architecture diagram
    arch_mermaid = build_architecture_diagram(
        G=G,
        communities=communities,
        community_labels=community_labels,
        god_node_ids=god_node_ids,
    )
    diagrams.append({
        "diagram_type": "architecture",
        "mermaid_code": arch_mermaid,
        "description": (
            f"Top-level architecture — {G.number_of_nodes() if G else 0} nodes, "
            f"{G.number_of_edges() if G else 0} edges, "
            f"{len(communities)} communities detected. "
            "Blue double-circles = god nodes (highest connectivity)."
        ),
    })

    # 2. Flow diagram
    flow_mermaid = build_flow_diagram(
        G=G,
        communities=communities,
        community_labels=community_labels,
        god_node_ids=god_node_ids,
    )
    diagrams.append({
        "diagram_type": "flow",
        "mermaid_code": flow_mermaid,
        "description": (
            "Data flow sequence diagram derived from actual call/import/use edges "
            "in the graphify knowledge graph. Actors ordered by connectivity."
        ),
    })

    # 3. Component diagram
    comp_mermaid = build_component_diagram(
        G=G,
        communities=communities,
        community_labels=community_labels,
        cohesion_scores=cohesion,
        god_node_ids=god_node_ids,
    )
    diagrams.append({
        "diagram_type": "components",
        "mermaid_code": comp_mermaid,
        "description": (
            "Community-grouped component diagram. Subgraph labels show community name "
            "and cohesion score (% of possible intra-community edges present). "
            "Cross-community edges represent architectural dependencies."
        ),
    })

    return diagrams
