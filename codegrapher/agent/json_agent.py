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

    normalised_surprising = [
        {
            "source": sc.get("source", ""),
            "target": sc.get("target", ""),
            "why": sc.get("why", sc.get("note", "")),
            "confidence": sc.get("confidence", ""),
        }
        for sc in surprising
    ]

    normalised_questions = [
        q.get("question", str(q)) if isinstance(q, dict) else str(q)
        for q in suggested_qs
    ]

    # ── Pull from AS-IS nested structure (new synthesiser format) ─────────────
    exec_sum   = fd.get("executive_summary", {})
    obj        = fd.get("analysis_objective", {})
    meth       = fd.get("methodology", {})
    as_is      = fd.get("as_is_state", {})
    data_api   = fd.get("data_and_api", {})
    arch       = fd.get("system_architecture", {})
    sec        = fd.get("security_and_compliance", {})
    quality    = fd.get("code_quality", {})

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
        # ── AS-IS sections ──────────────────────────────────────────────────
        "executive_summary": {
            "purpose":            exec_sum.get("purpose", fd.get("purpose", "")),
            "scope":              exec_sum.get("scope", ""),
            "methodology":        exec_sum.get("methodology", ""),
            "critical_findings":  _ensure_list(exec_sum.get("critical_findings")),
            "strengths":          _ensure_list(exec_sum.get("strengths")),
            "bottlenecks":        _ensure_list(exec_sum.get("bottlenecks")),
        },
        "analysis_objective": {
            "defined_goals":      _ensure_list(obj.get("defined_goals")),
            "scope_boundaries":   obj.get("scope_boundaries", ""),
            "architecture_style": obj.get("architecture_style",
                                          fd.get("architecture_style", "")),
        },
        "methodology": {
            "data_collection_methods": _ensure_list(meth.get("data_collection_methods")),
            "tools_used":              _ensure_list(meth.get("tools_used",
                                                   fd.get("tech_stack", []))),
        },
        "as_is_state": {
            "process_description": as_is.get("process_description",
                                             fd.get("summary", "")),
            "technologies":        _ensure_list(as_is.get("technologies",
                                               fd.get("tech_stack", []))),
            "infrastructure":      _ensure_list(as_is.get("infrastructure")),
        },
        "data_and_api": {
            "database_config":     data_api.get("database_config", {}),
            "tables":              _ensure_list(data_api.get("tables")),
            "api_endpoints":       _normalise_endpoints(
                                       data_api.get("api_endpoints",
                                       fd.get("api_endpoints", []))),
        },
        "system_architecture": {
            "overview":    arch.get("overview", ""),
            "components":  _normalise_components(
                               arch.get("components",
                               fd.get("key_components", []))),
            "data_flow":   _ensure_list(arch.get("data_flow",
                                        fd.get("data_flow", []))),
        },
        "security_and_compliance": {
            "auth_mechanism":   sec.get("auth_mechanism", ""),
            "data_protection":  _ensure_list(sec.get("data_protection",
                                             fd.get("security_notes", []))),
            "compliance_gaps":  _ensure_list(sec.get("compliance_gaps")),
        },
        "code_quality": {
            "strengths":              _ensure_list(quality.get("strengths")),
            "weaknesses":             _ensure_list(quality.get("weaknesses")),
            "technical_debt":         _ensure_list(quality.get("technical_debt")),
            "test_coverage":          quality.get("test_coverage",
                                                  fd.get("testing_approach", "")),
            "improvement_suggestions": _normalise_improvements(
                                           quality.get("improvement_suggestions",
                                           fd.get("improvement_suggestions", []))),
        },
        "file_details":     _normalise_file_details(fd.get("file_details", [])),
        "dependency_notes": _normalise_note_pairs(fd.get("dependency_notes", [])),
        # ── Graph analysis (always from core tools, not LLM) ────────────────
        "graph_analysis": {
            "total_nodes":      total_nodes,
            "total_edges":      total_edges,
            "communities_count": len(communities),
            "communities":      communities_list,
            "god_nodes": [
                {
                    "label": g.get("label", g.get("id", "")),
                    "id":    g.get("id", ""),
                    "edges": int(g.get("edges", 0)),
                }
                for g in god_nodes
            ],
            "surprising_connections": normalised_surprising,
            "suggested_questions":    normalised_questions,
        },
        "repository": {
            "directory_tree": directory_tree or scan.get("directory_tree", ""),
            "file_counts":    scan.get("by_extension", {}),
            "largest_files":  scan.get("largest_files", []),
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


def _normalise_improvements(raw: list) -> list[dict]:
    """Normalise improvement_suggestions — handles both dicts and plain strings."""
    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append({
                "priority":   item.get("priority", "medium"),
                "suggestion": item.get("suggestion", ""),
                "rationale":  item.get("rationale", ""),
            })
        elif isinstance(item, str):
            out.append({"priority": "medium", "suggestion": item, "rationale": ""})
    return out


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
