"""
JSON Structure Agent.

Converts the raw state_accumulator (written by LangGraph tools) into a single
clean, typed JSON document that fully describes the analysis.

The JSON is then passed to pdf_generator.generate_pdf_from_json() to produce
the final PDF — no Mermaid, no diagram rendering.

Schema
------
{
  "meta": {
    "repo_name": str,
    "provider_name": str,
    "analysis_date": str,       # ISO-8601
    "elapsed_seconds": float,
    "agent_steps": int,
    "total_files": int,
    "total_lines": int,
  },
  "summary": {
    "purpose": str,
    "architecture_style": str,
    "overview": str,            # 3-5 sentence narrative
    "testing_approach": str,
    "deployment_info": str,
  },
  "tech_stack": [str, ...],
  "key_components": [
    {"name": str, "description": str, "files": [...], "responsibilities": [...]},
    ...
  ],
  "data_flow": [str, ...],
  "api_endpoints": [
    {"method": str, "path": str, "description": str},
    ...
  ],
  "database_models": [str, ...],
  "security_notes": [str, ...],
  "code_quality_notes": [str, ...],
  "improvement_suggestions": [str, ...],
  "file_details": [
    {"file": str, "summary": str, "confidence": str},
    ...
  ],
  "architecture_notes": [
    {"title": str, "detail": str},
    ...
  ],
  "dependency_notes": [
    {"title": str, "detail": str},
    ...
  ],
  "graph_analysis": {
    "total_nodes": int,
    "total_edges": int,
    "communities_count": int,
    "communities": [
      {"id": int, "label": str, "size": int, "cohesion": float},
      ...
    ],
    "god_nodes": [
      {"label": str, "id": str, "edges": int},
      ...
    ],
    "surprising_connections": [
      {"source": str, "target": str, "why": str, "confidence": str},
      ...
    ],
    "suggested_questions": [str, ...]
  },
  "repository": {
    "directory_tree": str,
    "file_counts": {str: int},
    "largest_files": [{"size": int, "path": str}, ...]
  }
}
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any


def build_analysis_json(
    state_accumulator: dict,
    repo_name: str,
    provider_name: str,
    total_files: int = 0,
    total_lines: int = 0,
    agent_steps: int = 0,
    elapsed_seconds: float = 0.0,
    directory_tree: str = "",
) -> dict:
    """
    Transform state_accumulator into a clean, JSON-serialisable analysis doc.

    Parameters
    ----------
    state_accumulator : dict
        The mutable dict all LangGraph tools write into. Expected keys:
          finish_data  : dict from finish_analysis (all analysis fields)
          god_nodes    : list from analyze_graph
          surprising_connections : list from analyze_graph
          suggested_questions    : list from analyze_graph
          communities  : dict {cid: [node_ids]} from cluster_communities
          community_labels : dict {cid: str}
          cohesion     : dict {cid: float}
          graph        : NetworkX graph object (optional)
          scan_result  : dict from scan_repository
    """
    fd: dict = state_accumulator.get("finish_data", {})
    god_nodes: list = state_accumulator.get("god_nodes", [])
    surprising: list = state_accumulator.get("surprising_connections", [])
    suggested_qs: list = state_accumulator.get("suggested_questions", [])
    communities: dict = state_accumulator.get("communities", {})
    community_labels: dict = state_accumulator.get("community_labels", {})
    cohesion: dict = state_accumulator.get("cohesion", {})
    scan: dict = state_accumulator.get("scan_result", {})
    G = state_accumulator.get("graph")

    # ── Graph metrics ──────────────────────────────────────────────────────────
    total_nodes = G.number_of_nodes() if G is not None else 0
    total_edges = G.number_of_edges() if G is not None else 0

    communities_list = [
        {
            "id": cid,
            "label": community_labels.get(cid, f"Community {cid}"),
            "size": len(node_ids),
            "cohesion": round(float(cohesion.get(cid, 0.0)), 4),
        }
        for cid, node_ids in sorted(
            communities.items(), key=lambda x: len(x[1]), reverse=True
        )
    ]

    # Normalise surprising connections
    normalised_surprising = [
        {
            "source": sc.get("source", ""),
            "target": sc.get("target", ""),
            "why": sc.get("why", sc.get("note", "")),
            "confidence": sc.get("confidence", ""),
        }
        for sc in surprising
    ]

    # Normalise suggested questions (may be dicts or strings)
    normalised_questions = [
        q.get("question", str(q)) if isinstance(q, dict) else str(q)
        for q in suggested_qs
    ]

    # ── Assemble document ──────────────────────────────────────────────────────
    doc: dict[str, Any] = {
        "meta": {
            "repo_name": repo_name,
            "provider_name": provider_name,
            "analysis_date": date.today().isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "agent_steps": agent_steps,
            "total_files": total_files,
            "total_lines": total_lines,
        },
        "summary": {
            "purpose": fd.get("purpose", ""),
            "architecture_style": fd.get("architecture_style", ""),
            "overview": fd.get("summary", ""),
            "testing_approach": fd.get("testing_approach", ""),
            "deployment_info": fd.get("deployment_info", ""),
        },
        "tech_stack": _ensure_list(fd.get("tech_stack")),
        "key_components": _normalise_components(fd.get("key_components", [])),
        "data_flow": _ensure_list(fd.get("data_flow")),
        "api_endpoints": _normalise_endpoints(fd.get("api_endpoints", [])),
        "database_models": _ensure_list(fd.get("database_models")),
        "security_notes": _ensure_list(fd.get("security_notes")),
        "code_quality_notes": _ensure_list(fd.get("code_quality_notes")),
        "improvement_suggestions": _ensure_list(fd.get("improvement_suggestions")),
        # New fields from the expanded finish_analysis
        "file_details": _normalise_file_details(fd.get("file_details", [])),
        "architecture_notes": _normalise_note_pairs(fd.get("architecture_notes", [])),
        "dependency_notes": _normalise_note_pairs(fd.get("dependency_notes", [])),
        "graph_analysis": {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "communities_count": len(communities),
            "communities": communities_list,
            "god_nodes": [
                {
                    "label": g.get("label", g.get("id", "")),
                    "id": g.get("id", ""),
                    "edges": int(g.get("edges", 0)),
                }
                for g in god_nodes
            ],
            "surprising_connections": normalised_surprising,
            "suggested_questions": normalised_questions,
        },
        "repository": {
            "directory_tree": directory_tree or scan.get("directory_tree", ""),
            "file_counts": scan.get("by_extension", {}),
            "largest_files": scan.get("largest_files", []),
        },
    }

    return doc


def build_analysis_json_string(doc: dict, indent: int = 2) -> str:
    """Serialise the analysis document to a formatted JSON string."""
    return json.dumps(doc, indent=indent, ensure_ascii=False, default=str)


# ── Private helpers ────────────────────────────────────────────────────────────

def _ensure_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalise_components(raw: list) -> list[dict]:
    out = []
    for c in raw:
        if isinstance(c, dict):
            out.append({
                "name": c.get("name", ""),
                "description": c.get("description", ""),
                "files": _ensure_list(c.get("files")),
                "responsibilities": _ensure_list(c.get("responsibilities")),
            })
        elif isinstance(c, str):
            out.append({"name": c, "description": "", "files": [], "responsibilities": []})
    return out


def _normalise_endpoints(raw: list) -> list[dict]:
    return [
        {
            "method": ep.get("method", "").upper(),
            "path": ep.get("path", ""),
            "description": ep.get("description", ""),
        }
        for ep in raw if isinstance(ep, dict)
    ]


def _normalise_file_details(raw: list) -> list[dict]:
    """Normalise file_details from finish_analysis."""
    out = []
    for f in raw:
        if isinstance(f, dict):
            out.append({
                "file": f.get("file", ""),
                "summary": f.get("summary", ""),
                "confidence": f.get("confidence", "high"),
            })
        elif isinstance(f, str):
            out.append({"file": f, "summary": "", "confidence": "high"})
    return out


def _normalise_note_pairs(raw: list) -> list[dict]:
    """Normalise architecture_notes / dependency_notes — list of {title, detail}."""
    out = []
    for n in raw:
        if isinstance(n, dict):
            out.append({
                "title": n.get("title", ""),
                "detail": n.get("detail", ""),
            })
        elif isinstance(n, str):
            out.append({"title": n, "detail": ""})
    return out
