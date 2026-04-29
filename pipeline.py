"""Lazy analysis pipeline.

Each ensure_* function runs the corresponding stage if it hasn't been run yet,
populating the cached RepoAnalysis. Tools call these so the consumer never
has to remember the ordering: calling cluster_communities will quietly run
extraction and graph build first if needed.
"""
from __future__ import annotations

from pathlib import Path

from .repo_cache import CACHE, RepoAnalysis
from .core.detect import detect as _detect
from .core.extract import collect_files, extract as _extract
from .core.build import build_from_json
from .core.cluster import cluster as _cluster, score_all as _score_all
from .core.analyze import (
    god_nodes as _god_nodes,
    surprising_connections as _surprising_connections,
    suggest_questions as _suggest_questions,
)


def get_analysis(repo_path: str | Path) -> RepoAnalysis:
    """Resolve repo_path and return its cached analysis container."""
    p = Path(repo_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Repository path does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {p}")
    return CACHE.get(p)


def ensure_detection(analysis: RepoAnalysis) -> dict:
    """Run file detection if not yet done."""
    if analysis.detection is None:
        analysis.detection = _detect(analysis.repo_path)
    return analysis.detection


def ensure_extraction(analysis: RepoAnalysis) -> dict:
    """Run tree-sitter AST extraction if not yet done.

    Depends on: detection.
    """
    if analysis.extraction is not None:
        return analysis.extraction

    detection = ensure_detection(analysis)
    code_files_raw = detection.get("files", {}).get("code", [])

    code_files: list[Path] = []
    for f in code_files_raw:
        p = Path(f)
        if p.is_dir():
            code_files.extend(collect_files(p))
        elif p.exists():
            code_files.append(p)

    if not code_files:
        analysis.extraction = {"nodes": [], "edges": []}
    else:
        analysis.extraction = _extract(code_files)

    return analysis.extraction


def ensure_graph(analysis: RepoAnalysis):
    """Build NetworkX graph if not yet done.

    Depends on: extraction.
    """
    if analysis.graph is not None:
        return analysis.graph

    extraction = ensure_extraction(analysis)
    analysis.graph = build_from_json(extraction)
    return analysis.graph


def ensure_clusters(analysis: RepoAnalysis) -> dict:
    """Run community detection if not yet done.

    Depends on: graph.
    Returns dict with communities, community_labels, cohesion.
    """
    if analysis.communities is not None:
        return {
            "communities": analysis.communities,
            "community_labels": analysis.community_labels,
            "cohesion": analysis.cohesion,
        }

    G = ensure_graph(analysis)
    communities = _cluster(G)
    cohesion = _score_all(G, communities)

    # Label each community by most common top-level path component,
    # computed RELATIVE to the repo root so absolute paths in source_file
    # don't all collapse to "/".
    repo_root = analysis.repo_path
    labels: dict[int, str] = {}
    for cid, node_ids in communities.items():
        sources = [
            G.nodes[n].get("source_file", "") for n in node_ids if n in G.nodes
        ]
        sources = [s for s in sources if s]
        tops: list[str] = []
        for s in sources:
            try:
                rel = Path(s).resolve().relative_to(repo_root)
                if rel.parts:
                    tops.append(rel.parts[0])
            except (ValueError, OSError):
                # Path isn't under repo root — fall back to bare basename
                p = Path(s)
                if p.parts:
                    tops.append(p.parts[-1])
        if tops:
            labels[cid] = max(set(tops), key=tops.count)
        else:
            labels[cid] = f"Community {cid}"

    analysis.communities = communities
    analysis.community_labels = labels
    analysis.cohesion = cohesion

    return {
        "communities": communities,
        "community_labels": labels,
        "cohesion": cohesion,
    }


def ensure_god_nodes(analysis: RepoAnalysis, top_n: int = 10) -> list[dict]:
    """Find god nodes (most-connected). Depends on: graph."""
    G = ensure_graph(analysis)
    # Recompute if requested top_n is larger than what's cached
    if analysis.god_nodes is None or len(analysis.god_nodes) < top_n:
        analysis.god_nodes = _god_nodes(G, top_n=top_n)
    return analysis.god_nodes[:top_n]


def ensure_surprising_connections(
    analysis: RepoAnalysis, top_n: int = 5
) -> list[dict]:
    """Find surprising cross-community edges. Depends on: clusters."""
    ensure_clusters(analysis)
    if analysis.surprising_connections is None or \
       len(analysis.surprising_connections) < top_n:
        analysis.surprising_connections = _surprising_connections(
            analysis.graph, analysis.communities, top_n=top_n
        )
    return analysis.surprising_connections[:top_n]


def ensure_suggested_questions(
    analysis: RepoAnalysis, top_n: int = 5
) -> list[dict]:
    """Generate investigation prompts. Depends on: clusters."""
    ensure_clusters(analysis)
    if analysis.suggested_questions is None or \
       len(analysis.suggested_questions) < top_n:
        analysis.suggested_questions = _suggest_questions(
            analysis.graph,
            analysis.communities,
            analysis.community_labels,
            top_n=top_n,
        )
    return analysis.suggested_questions[:top_n]
