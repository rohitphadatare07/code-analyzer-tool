"""
LangGraph tool definitions.

These tools wrap graphify's Python libraries so the LangGraph agent
can call them via tool-use. The agent DECIDES which tools to call —
it is not hard-coded to call them in order.

Tools available to the agent:
  1. scan_repository        - detect files + build dir tree
  2. extract_ast_graph      - run graphify AST extraction (tree-sitter)
  3. cluster_communities    - run Leiden/Louvain community detection
  4. analyze_graph          - find god nodes, surprising connections
  5. read_file              - read a specific file (agent decides which)
  6. search_code            - grep across the repo
  7. record_finding         - save an insight
  8. record_diagram         - save a Mermaid diagram
  9. finish_analysis        - signal completion with executive summary
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool


# ── Internal helpers ──────────────────────────────────────────────────────────

_SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "venv", ".venv", "env", "dist", "build",
    ".next", ".nuxt", "coverage", ".tox", ".idea", ".vscode",
}

_SKIP_EXTS = {
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".zip", ".tar", ".gz", ".whl", ".egg",
}

_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".cpp", ".c", ".h", ".cs", ".kt", ".swift", ".sh",
}


def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    p = (repo_root / rel_path).resolve()
    if not str(p).startswith(str(repo_root)):
        raise ValueError(f"Path escapes repo root: {rel_path}")
    return p


def _build_tree(root: Path, max_depth: int = 4) -> str:
    lines = [f"{root.name}/"]

    def _walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        entries = [e for e in entries if e.name not in _SKIP_DIRS and not e.name.startswith(".")]
        for i, entry in enumerate(entries[:25]):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and depth < max_depth:
                _walk(entry, prefix + ("    " if is_last else "│   "), depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


# ── Tool factory ──────────────────────────────────────────────────────────────
# We return bound tools so each tool closure captures the repo_root.

def make_tools(repo_root: Path, state_accumulator: dict) -> list:
    """
    Build the tool list bound to a specific repo_root.

    state_accumulator is a mutable dict the tools write into:
      - findings: list of Finding dicts
      - diagrams: list of Diagram dicts
      - finish_data: dict with summary/purpose/architecture_style
      - finished: bool
      - graphify_output: GraphifyOutput dict (written by extract/cluster tools)
    """
    root = repo_root.resolve()

    # ── 1. scan_repository ────────────────────────────────────────────────────
    @tool
    def scan_repository(include_tree: bool = True) -> str:
        """
        Scan the repository: count files by type, measure corpus size, build
        directory tree. Always call this FIRST before reading any files.
        Returns JSON with file counts, word counts, and optionally the dir tree.
        """
        ext_counts: dict[str, int] = {}
        total_files = 0
        total_words = 0
        large_files = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
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
        result = {
            "total_files": total_files,
            "total_words": total_words,
            "by_extension": dict(sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:20]),
            "largest_files": [{"size": s, "path": p} for s, p in large_files[:10]],
        }
        if include_tree:
            result["directory_tree"] = _build_tree(root)

        state_accumulator["scan_result"] = result
        return json.dumps(result, indent=2)

    # ── 2. extract_ast_graph ──────────────────────────────────────────────────
    @tool
    def extract_ast_graph() -> str:
        """
        Run graphify's AST extraction using tree-sitter on all code files.
        Extracts classes, functions, imports, and call relationships.
        Returns a summary of extracted nodes and edges.
        Call this after scan_repository to build the structural graph.
        """
        try:
            from codegrapher.core.detect import detect
            from codegrapher.core.extract import collect_files, extract as gf_extract
            from codegrapher.core.build import build_from_json
        except ImportError:
            return json.dumps({
                "error": "graphify not installed. Run: pip install graphifyy",
                "nodes": 0, "edges": 0,
            })

        try:
            detection = detect(root)
            code_files_raw = detection.get("files", {}).get("code", [])
            code_files = []
            for f in code_files_raw:
                p = Path(f)
                if p.is_dir():
                    code_files.extend(collect_files(p))
                elif p.exists():
                    code_files.append(p)

            if not code_files:
                state_accumulator["graphify_ast"] = {"nodes": [], "edges": []}
                return json.dumps({"message": "No code files found", "nodes": 0, "edges": 0})

            result = gf_extract(code_files)
            state_accumulator["graphify_ast"] = result
            state_accumulator["detection"] = detection

            # Quick summary for the agent
            return json.dumps({
                "nodes_extracted": len(result.get("nodes", [])),
                "edges_extracted": len(result.get("edges", [])),
                "files_processed": len(code_files),
                "sample_nodes": [
                    {"id": n["id"], "label": n.get("label", ""), "file": n.get("source_file", "")}
                    for n in result.get("nodes", [])[:8]
                ],
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e), "nodes": 0, "edges": 0})

    # ── 3. cluster_communities ────────────────────────────────────────────────
    @tool
    def cluster_communities() -> str:
        """
        Run Leiden/Louvain community detection on the extracted graph.
        Groups related code entities into semantic clusters.
        Call this AFTER extract_ast_graph. Returns community labels and sizes.
        """
        try:
            from codegrapher.core.cluster import cluster, score_all, build_graph
            from codegrapher.core.build import build_from_json
        except ImportError:
            return json.dumps({"error": "graphify not installed"})

        ast_data = state_accumulator.get("graphify_ast")
        if not ast_data:
            return json.dumps({"error": "No AST data — call extract_ast_graph first"})

        try:
            G = build_from_json(ast_data)
            communities = cluster(G)
            cohesion = score_all(G, communities)

            # Label each community by most common file/module prefix
            labels: dict[int, str] = {}
            for cid, node_ids in communities.items():
                sources = [
                    G.nodes[n].get("source_file", "") for n in node_ids
                    if n in G.nodes
                ]
                sources = [s for s in sources if s]
                if sources:
                    # Pick most common top-level path component
                    tops = [Path(s).parts[0] if Path(s).parts else s for s in sources]
                    labels[cid] = max(set(tops), key=tops.count)
                else:
                    labels[cid] = f"Community {cid}"

            state_accumulator["graph"] = G
            state_accumulator["communities"] = communities
            state_accumulator["cohesion"] = cohesion
            state_accumulator["community_labels"] = labels

            return json.dumps({
                "total_nodes": G.number_of_nodes(),
                "total_edges": G.number_of_edges(),
                "communities_detected": len(communities),
                "communities": [
                    {
                        "id": cid,
                        "label": labels.get(cid, f"Community {cid}"),
                        "size": len(node_ids),
                        "cohesion": cohesion.get(cid, 0.0),
                    }
                    for cid, node_ids in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:10]
                ],
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── 4. analyze_graph ─────────────────────────────────────────────────────
    @tool
    def analyze_graph() -> str:
        """
        Run graphify's deep analysis: find god nodes (most-connected core abstractions),
        surprising cross-community connections, and suggested investigation questions.
        Call this AFTER cluster_communities.
        """
        try:
            from codegrapher.core.analyze import (
                god_nodes as gf_god_nodes,
                surprising_connections,
                suggest_questions,
            )
        except ImportError:
            return json.dumps({"error": "graphify not installed"})

        G = state_accumulator.get("graph")
        communities = state_accumulator.get("communities")
        labels = state_accumulator.get("community_labels", {})

        if G is None or communities is None:
            return json.dumps({"error": "Call cluster_communities first"})

        try:
            god = gf_god_nodes(G, top_n=10)
            surprises = surprising_connections(G, communities, top_n=5)
            questions = suggest_questions(G, communities, labels, top_n=5)

            state_accumulator["god_nodes"] = god
            state_accumulator["surprising_connections"] = surprises
            state_accumulator["suggested_questions"] = questions

            return json.dumps({
                "god_nodes": god,
                "surprising_connections": surprises,
                "suggested_questions": questions,
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── 5. read_file ──────────────────────────────────────────────────────────
    @tool
    def read_file(path: str, max_lines: int = 300) -> str:
        """
        Read a specific file in the repository.
        Use this to deeply understand key files the agent identifies as important.
        The agent decides which files to read based on its exploration.

        Args:
            path: Relative file path within the repository.
            max_lines: Maximum lines to return (default 300, max 500).
        """
        max_lines = min(max_lines, 500)
        try:
            resolved = _safe_resolve(root, path)
        except ValueError as e:
            return f"ERROR: {e}"

        if not resolved.exists():
            return f"ERROR: File not found: {path}"
        if not resolved.is_file():
            return f"ERROR: Not a file: {path}"

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"ERROR: Cannot read {path}: {e}"

        lines = content.split("\n")
        total = len(lines)
        if total > max_lines:
            content = "\n".join(lines[:max_lines])
            content += f"\n\n... [{total - max_lines} more lines — increase max_lines to see more]"

        return f"=== {path} ({total} lines total) ===\n{content}"

    # ── 6. search_code ────────────────────────────────────────────────────────
    @tool
    def search_code(
        pattern: str,
        file_glob: str = "*",
        max_results: int = 30,
    ) -> str:
        """
        Search for a pattern across all code files in the repository.
        Use this to trace how classes/functions/routes are used across files,
        find all API endpoints, find database models, etc.

        Args:
            pattern: Text or regex pattern to search for.
            file_glob: Glob pattern to filter files (e.g. '*.py', '*.ts').
            max_results: Maximum number of matching lines to return.
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                if not fnmatch.fnmatch(fname, file_glob):
                    continue
                fp = Path(dirpath) / fname
                if any(fname.endswith(e) for e in _SKIP_EXTS):
                    continue
                try:
                    rel = str(fp.relative_to(root))
                    for i, line in enumerate(fp.read_text(errors="replace").split("\n"), 1):
                        if regex.search(line):
                            matches.append(f"{rel}:{i}:  {line.strip()[:120]}")
                            if len(matches) >= max_results:
                                break
                except Exception:
                    continue
                if len(matches) >= max_results:
                    break

        if not matches:
            return f"No matches found for pattern: {pattern}"
        return f"Found {len(matches)} matches for '{pattern}':\n\n" + "\n".join(matches)

    # ── 7. record_finding ─────────────────────────────────────────────────────
    @tool
    def record_finding(
        category: str,
        title: str,
        detail: str,
        files: Optional[list] = None,
        confidence: str = "high",
    ) -> str:
        """
        Record an important finding about the codebase.
        Call this whenever you discover something significant.
        These findings are compiled into the final PDF report.

        Args:
            category: One of: architecture | component | data_flow | api_endpoint |
                      security | tech_stack | design_pattern | code_quality |
                      improvement | entry_point | dependency | database_model |
                      testing | deployment | file_detail
            title: Short title for this finding (used as heading in report).
            detail: Full description of what you found.
            files: List of relevant file paths.
            confidence: high | medium | low
        """
        finding = {
            "category": category,
            "title": title,
            "detail": detail,
            "files": files or [],
            "confidence": confidence,
        }
        if "findings" not in state_accumulator:
            state_accumulator["findings"] = []
        state_accumulator["findings"].append(finding)
        return f"✓ Finding recorded [{category}]: {title}"

    # ── 8. generate_diagrams_from_graph ───────────────────────────────────────
    @tool
    def generate_diagrams_from_graph() -> str:
        """
        Generate all three Mermaid diagrams DETERMINISTICALLY from the real
        graphify graph data — no LLM guessing, no hallucination.

        Produces:
          - architecture : top-down module dependency graph (god nodes highlighted)
          - flow         : sequence diagram from actual call/import edges
          - components   : community-grouped LR diagram with cohesion scores

        IMPORTANT: Call this AFTER cluster_communities and analyze_graph,
        since it reads the NetworkX graph, communities, and god_nodes that
        those tools computed and stored. Do NOT call record_diagram manually —
        this tool replaces it with accurate, data-driven diagrams.
        """
        G = state_accumulator.get("graph")
        if G is None:
            return "ERROR: No graph found. Call extract_ast_graph and cluster_communities first."

        try:
            from codegrapher.output.mermaid_converter import generate_all_diagrams
            diagrams = generate_all_diagrams(state_accumulator)
            state_accumulator["diagrams"] = diagrams

            summary = []
            for d in diagrams:
                lines = d["mermaid_code"].count("\n") + 1
                summary.append(f"  [{d['diagram_type']}] {lines} lines — {d['description'][:80]}")

            return (
                f"✓ Generated {len(diagrams)} deterministic diagrams from graph data:\n"
                + "\n".join(summary)
            )
        except Exception as e:
            return f"ERROR generating diagrams: {e}"

    # ── 9. finish_analysis ────────────────────────────────────────────────────
    @tool
    def finish_analysis(
        summary: str,
        purpose: str,
        architecture_style: str,
        tech_stack: list,
        key_components: list,
        data_flow: list,
        api_endpoints: list,
        database_models: list,
        security_notes: list,
        improvement_suggestions: list,
        testing_approach: str = "",
        deployment_info: str = "",
        code_quality_notes: list = None,
    ) -> str:
        """
        Signal that analysis is complete and provide the executive summary.
        Call this ONLY when you have: explored the structure, run extraction +
        clustering + analysis, read key files, recorded findings, and generated
        at least one diagram.

        Args:
            summary: 3-5 sentence technical summary of the codebase.
            purpose: One sentence — what does this repo DO?
            architecture_style: e.g. MVC, microservices, CLI tool, library, monolith.
            tech_stack: List of technologies/frameworks/languages used.
            key_components: List of dicts with name/description/files/responsibilities.
            data_flow: List of steps describing main data flow.
            api_endpoints: List of dicts with method/path/description.
            database_models: List of data model names.
            security_notes: List of security observations.
            improvement_suggestions: List of concrete improvement ideas.
            testing_approach: Description of testing strategy.
            deployment_info: How this is deployed/run.
            code_quality_notes: List of code quality observations.
        """
        state_accumulator["finished"] = True
        state_accumulator["finish_data"] = {
            "summary": summary,
            "purpose": purpose,
            "architecture_style": architecture_style,
            "tech_stack": tech_stack,
            "key_components": key_components,
            "data_flow": data_flow,
            "api_endpoints": api_endpoints,
            "database_models": database_models,
            "security_notes": security_notes,
            "improvement_suggestions": improvement_suggestions,
            "testing_approach": testing_approach,
            "deployment_info": deployment_info,
            "code_quality_notes": code_quality_notes or [],
        }
        return "✓ Analysis complete. Generating PDF report..."

    return [
        scan_repository,
        extract_ast_graph,
        cluster_communities,
        analyze_graph,
        read_file,
        search_code,
        record_finding,
        generate_diagrams_from_graph,
        finish_analysis,
    ]
