"""Per-repository analysis cache.

The MCP tools are designed so each can be called independently — clustering
will lazily run extraction if it hasn't been done yet. This module holds
the in-memory cache that makes that work efficient.

Cache key is the resolved absolute path to a repository. Each entry holds
the staged outputs of the analysis pipeline so we don't redo expensive work
across tool calls within the same MCP session.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import networkx as nx


@dataclass
class RepoAnalysis:
    """All staged outputs for a single repository.

    Each field is populated lazily by the corresponding pipeline step.
    None means "not yet computed".
    """
    repo_path: Path

    # Stage 1: file detection
    detection: Optional[dict] = None         # output of core.detect.detect()
    scan_summary: Optional[dict] = None      # file counts + tree

    # Stage 2: AST extraction
    extraction: Optional[dict] = None        # {"nodes": [...], "edges": [...]}

    # Stage 3: graph build
    graph: Optional[nx.Graph] = None

    # Stage 4: community detection
    communities: Optional[dict[int, list[str]]] = None
    community_labels: Optional[dict[int, str]] = None
    cohesion: Optional[dict[int, float]] = None

    # Stage 5: deep analysis
    god_nodes: Optional[list[dict]] = None
    surprising_connections: Optional[list[dict]] = None
    suggested_questions: Optional[list[dict]] = None

    # Stage 6: diagrams
    diagrams: Optional[list[dict]] = None

    # Free-form scratchpad for callers that want to attach metadata
    extra: dict[str, Any] = field(default_factory=dict)


class AnalysisCache:
    """Thread-safe cache of RepoAnalysis objects keyed by absolute repo path."""

    def __init__(self) -> None:
        self._cache: dict[str, RepoAnalysis] = {}
        self._lock = threading.Lock()

    def get(self, repo_path: Path) -> RepoAnalysis:
        key = str(repo_path.resolve())
        with self._lock:
            if key not in self._cache:
                self._cache[key] = RepoAnalysis(repo_path=Path(key))
            return self._cache[key]

    def reset(self, repo_path: Path) -> None:
        """Drop cached analysis for a repo. Useful after the repo changes on disk."""
        key = str(repo_path.resolve())
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# Module-level singleton — one cache per MCP server process
CACHE = AnalysisCache()
