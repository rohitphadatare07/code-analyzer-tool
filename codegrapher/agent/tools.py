"""
LangGraph tool definitions.

These tools wrap graphify's Python libraries so the LangGraph agent
can call them via tool-use. The agent DECIDES which tools to call —
it is not hard-coded to call them in order.

Tools available to the agent:
  1. scan_repository        - detect files + build dir tree
  2. build_code_graph       - AST extraction + clustering + analysis + diagrams (all-in-one)
  3. read_file              - read a specific file (agent decides which)
  4. read_multiple_files    - read up to 5 files in a single call (use instead of looping read_file)
  5. search_code            - grep across the repo
  6. record_finding         - save an insight
  7. finish_analysis        - signal completion with executive summary

Design principle: minimize LLM round-trips by batching sequential/parallel work.
  - build_code_graph replaces 4 sequential single-step tools (saves ~3 LLM calls)
  - read_multiple_files replaces N individual read_file calls (saves N-1 LLM calls)
  - The agent should return multiple tool_calls per response when operations are independent
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

    # ── 2. build_code_graph ───────────────────────────────────────────────────
    @tool
    def build_code_graph() -> str:
        """
        Run the full graphify pipeline in one shot:
          1. AST extraction (tree-sitter) — extracts classes, functions, imports, calls
          2. Community detection (Leiden/Louvain) — groups related code into clusters
          3. Graph analysis — finds god nodes and surprising cross-community connections
          4. Diagram generation — produces architecture/flow/component Mermaid diagrams

        Call this ONCE after scan_repository. It replaces the four separate steps
        (extract_ast_graph, cluster_communities, analyze_graph, generate_diagrams_from_graph)
        with a single LLM round-trip.

        Returns a combined summary: nodes, edges, communities, god nodes, and diagram status.
        """
        # ── Step 1: AST extraction ────────────────────────────────────────────
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
        except ImportError:
            return json.dumps({"error": "graphify not installed. Run: pip install graphifyy"})

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

            ast_result = gf_extract(code_files)
            state_accumulator["graphify_ast"] = ast_result
            state_accumulator["detection"] = detection

        except Exception as e:
            return json.dumps({"error": f"AST extraction failed: {e}"})

        # ── Step 2: Community detection ───────────────────────────────────────
        try:
            G = build_from_json(ast_result)
            communities = cluster(G)
            cohesion = score_all(G, communities)

            labels: dict[int, str] = {}
            for cid, node_ids in communities.items():
                sources = [
                    G.nodes[n].get("source_file", "") for n in node_ids if n in G.nodes
                ]
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

        except Exception as e:
            return json.dumps({"error": f"Community detection failed: {e}"})

        # ── Step 3: Graph analysis ────────────────────────────────────────────
        try:
            god = gf_god_nodes(G, top_n=10)
            surprises = surprising_connections(G, communities, top_n=5)
            questions = suggest_questions(G, communities, labels, top_n=5)

            state_accumulator["god_nodes"] = god
            state_accumulator["surprising_connections"] = surprises
            state_accumulator["suggested_questions"] = questions

        except Exception as e:
            god, surprises, questions = [], [], []
            state_accumulator["god_nodes"] = []
            state_accumulator["surprising_connections"] = []
            state_accumulator["suggested_questions"] = []

        # ── Step 4: Diagram generation ────────────────────────────────────────
        diagrams_summary = []
        try:
            from codegrapher.output.mermaid_converter import generate_all_diagrams
            diagrams = generate_all_diagrams(state_accumulator)
            state_accumulator["diagrams"] = diagrams
            diagrams_summary = [
                {"type": d["diagram_type"], "lines": d["mermaid_code"].count("\n") + 1}
                for d in diagrams
            ]
        except Exception as e:
            diagrams_summary = [{"error": str(e)}]

        # ── Combined result ───────────────────────────────────────────────────
        top_communities = [
            {
                "id": cid,
                "label": labels.get(cid, f"Community {cid}"),
                "size": len(node_ids),
                "cohesion": cohesion.get(cid, 0.0),
            }
            for cid, node_ids in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:8]
        ]

        return json.dumps({
            "ast": {
                "nodes": len(ast_result.get("nodes", [])),
                "edges": len(ast_result.get("edges", [])),
                "files_processed": len(code_files),
                "sample_nodes": [
                    {"id": n["id"], "label": n.get("label", ""), "file": n.get("source_file", "")}
                    for n in ast_result.get("nodes", [])[:6]
                ],
            },
            "communities": {
                "total_nodes": G.number_of_nodes(),
                "total_edges": G.number_of_edges(),
                "count": len(communities),
                "top": top_communities,
            },
            "god_nodes": god[:5],
            "surprising_connections": surprises[:3],
            "suggested_questions": questions[:3],
            "diagrams_generated": diagrams_summary,
        }, indent=2)

    # ── 3. read_file ──────────────────────────────────────────────────────────
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

    # ── 4b. read_multiple_files ───────────────────────────────────────────────
    @tool
    def read_multiple_files(paths: list, max_lines_each: int = 200) -> str:
        """
        Read up to 5 files in a single tool call.
        Use this instead of calling read_file repeatedly — it saves LLM round-trips.

        Args:
            paths: List of relative file paths (max 5). e.g. ["main.py", "config.py", "models.py"]
            max_lines_each: Max lines per file (default 200, max 300).
        """
        if not paths:
            return "ERROR: No paths provided."
        paths = paths[:5]  # hard cap
        max_lines_each = min(max_lines_each, 300)

        parts = []
        for path in paths:
            try:
                resolved = _safe_resolve(root, path)
            except ValueError as e:
                parts.append(f"=== {path} ===\nERROR: {e}\n")
                continue

            if not resolved.exists():
                parts.append(f"=== {path} ===\nERROR: File not found\n")
                continue
            if not resolved.is_file():
                parts.append(f"=== {path} ===\nERROR: Not a file\n")
                continue

            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
                lines = content.split("\n")
                total = len(lines)
                if total > max_lines_each:
                    content = "\n".join(lines[:max_lines_each])
                    content += f"\n\n... [{total - max_lines_each} more lines]"
                parts.append(f"=== {path} ({total} lines) ===\n{content}\n")
            except Exception as e:
                parts.append(f"=== {path} ===\nERROR: Cannot read: {e}\n")

        return "\n".join(parts)

    # ── 5. search_code ────────────────────────────────────────────────────────
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

    # ── 7. finish_analysis ────────────────────────────────────────────────────
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
        build_code_graph,
        read_file,
        read_multiple_files,
        search_code,
        record_finding,
        finish_analysis,
    ]
