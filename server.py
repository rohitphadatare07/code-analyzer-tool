"""CodeGrapher MCP server.

Exposes pure static-analysis tools over the Model Context Protocol.
No LLM calls. No agent loop. Each tool runs deterministically against
the local repository and returns structured results.

Run as: python -m codegrapher_mcp.server
Or via:  uvx codegrapher-mcp
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import pipeline
from .repo_cache import CACHE
from .core.detect import classify_file, FileType
from .output.mermaid_converter import generate_all_diagrams


mcp = FastMCP(
    "codegrapher",
    instructions=(
        "Static codebase analysis tools. Each tool takes a repo_path "
        "(absolute or ~-expandable). Tools are independent and lazy: calling "
        "a downstream tool (e.g. cluster_communities) will automatically run "
        "upstream stages (detection, extraction, graph build) if needed. "
        "Results are cached per repo_path within the session."
    ),
)


# ── Constants for filesystem walking ──────────────────────────────────────────

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


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_repo(repo_path: str) -> Path:
    """Resolve and validate a repo path argument."""
    p = Path(repo_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Repository path does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    return p


def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """Resolve rel_path under repo_root, refusing path traversal."""
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
        entries = [
            e for e in entries
            if e.name not in _SKIP_DIRS and not e.name.startswith(".")
        ]
        for i, entry in enumerate(entries[:25]):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(
                f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}"
            )
            if entry.is_dir() and depth < max_depth:
                _walk(entry, prefix + ("    " if is_last else "│   "), depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


# ── 1. scan_repository ────────────────────────────────────────────────────────

@mcp.tool()
def scan_repository(repo_path: str, include_tree: bool = True) -> dict:
    """Inventory a repository: file counts by extension, total size, directory tree.

    This is a fast, no-parsing first-look at the repo. It does NOT run AST
    extraction — use extract_ast_graph for that.

    Args:
        repo_path: Absolute path (or ~-expandable) to the repository root.
        include_tree: Whether to include a rendered directory tree in the result.

    Returns:
        Dict with total_files, total_words, by_extension, largest_files,
        and optionally directory_tree.
    """
    root = _resolve_repo(repo_path)

    ext_counts: dict[str, int] = {}
    total_files = 0
    total_words = 0
    large_files: list[tuple[int, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if any(fname.endswith(e) for e in _SKIP_EXTS):
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            ext = fp.suffix.lower() or "none"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            total_files += 1
            large_files.append((size, str(fp.relative_to(root))))
            if ext in _CODE_EXTS:
                try:
                    total_words += fp.read_text(errors="replace").count(" ")
                except Exception:
                    pass

    large_files.sort(reverse=True)
    result: dict[str, Any] = {
        "repo_path": str(root),
        "total_files": total_files,
        "total_words": total_words,
        "by_extension": dict(
            sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        ),
        "largest_files": [{"size": s, "path": p} for s, p in large_files[:10]],
    }
    if include_tree:
        result["directory_tree"] = _build_tree(root)

    # Stash on the analysis cache so other tools can reuse the scan summary
    analysis = CACHE.get(root)
    analysis.scan_summary = result

    return result


# ── 2. detect_files ───────────────────────────────────────────────────────────

@mcp.tool()
def detect_files(repo_path: str) -> dict:
    """Classify every file in the repo using CodeGrapher's detection rules.

    Files are bucketed into categories: code, docs, config, data, build,
    notebooks, etc. Honors .graphifyignore if present. Use this when you
    want a structured view of what's in the repo before extraction.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        Dict with keys 'files' (category -> list of paths) and 'stats'
        (counts per category).
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    detection = pipeline.ensure_detection(analysis)

    # Render to a JSON-serializable form. The detect() output is already
    # mostly serializable but may contain Path objects.
    files = detection.get("files", {})
    stats = {cat: len(paths) for cat, paths in files.items()}

    return {
        "repo_path": str(root),
        "stats": stats,
        "files": {
            cat: [str(p) for p in paths] for cat, paths in files.items()
        },
    }


# ── 3. extract_ast_graph ──────────────────────────────────────────────────────

@mcp.tool()
def extract_ast_graph(
    repo_path: str,
    sample_nodes: int = 8,
    return_full: bool = False,
) -> dict:
    """Run tree-sitter AST extraction across all code files.

    Extracts classes, functions, imports, and call relationships into
    a graph of nodes and edges. Supports 15+ languages (Python, JS/TS,
    Go, Rust, Java, C/C++, C#, Ruby, Kotlin, Scala, PHP, Lua, Swift,
    Julia, Zig, PowerShell, Objective-C, Elixir).

    Args:
        repo_path: Absolute path to the repository root.
        sample_nodes: Number of sample nodes to include in the response
            for inspection. Ignored if return_full=True.
        return_full: If True, return ALL nodes and edges. Can be very large.
            Defaults to False — most callers want a summary plus samples.

    Returns:
        Dict with nodes_extracted, edges_extracted, files_processed,
        and either sample_nodes or full nodes/edges arrays.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    extraction = pipeline.ensure_extraction(analysis)

    nodes = extraction.get("nodes", [])
    edges = extraction.get("edges", [])
    files_processed = len({
        n.get("source_file") for n in nodes if n.get("source_file")
    })

    result: dict[str, Any] = {
        "repo_path": str(root),
        "nodes_extracted": len(nodes),
        "edges_extracted": len(edges),
        "files_processed": files_processed,
    }

    if return_full:
        result["nodes"] = nodes
        result["edges"] = edges
    else:
        result["sample_nodes"] = [
            {
                "id": n["id"],
                "label": n.get("label", ""),
                "kind": n.get("kind", ""),
                "file": n.get("source_file", ""),
            }
            for n in nodes[:sample_nodes]
        ]

    return result


# ── 4. cluster_communities ────────────────────────────────────────────────────

@mcp.tool()
def cluster_communities(repo_path: str, top_n: int = 10) -> dict:
    """Run Leiden (or Louvain fallback) community detection on the code graph.

    Groups related code entities into semantic clusters and computes
    cohesion scores. Each community is labelled by its most common
    top-level path component. Will lazily run extraction + graph build
    if not already done.

    Args:
        repo_path: Absolute path to the repository root.
        top_n: Number of largest communities to include in the response.

    Returns:
        Dict with total_nodes, total_edges, communities_detected, and
        a list of the top_n largest communities with id, label, size,
        and cohesion score.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    pipeline.ensure_clusters(analysis)

    G = analysis.graph
    communities = analysis.communities or {}
    labels = analysis.community_labels or {}
    cohesion = analysis.cohesion or {}

    sorted_communities = sorted(
        communities.items(), key=lambda x: len(x[1]), reverse=True
    )

    return {
        "repo_path": str(root),
        "total_nodes": G.number_of_nodes() if G is not None else 0,
        "total_edges": G.number_of_edges() if G is not None else 0,
        "communities_detected": len(communities),
        "communities": [
            {
                "id": cid,
                "label": labels.get(cid, f"Community {cid}"),
                "size": len(node_ids),
                "cohesion": cohesion.get(cid, 0.0),
            }
            for cid, node_ids in sorted_communities[:top_n]
        ],
    }


# ── 5. find_god_nodes ─────────────────────────────────────────────────────────

@mcp.tool()
def find_god_nodes(repo_path: str, top_n: int = 10) -> dict:
    """Find the most-connected nodes in the code graph.

    God nodes are core abstractions or hubs — modifying them ripples
    everywhere, so they're prime targets for refactoring scrutiny and
    architectural review.

    Args:
        repo_path: Absolute path to the repository root.
        top_n: How many top nodes to return.

    Returns:
        Dict with a 'god_nodes' list. Each entry has id, label, kind,
        edges (degree), and source_file.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    god = pipeline.ensure_god_nodes(analysis, top_n=top_n)
    return {"repo_path": str(root), "god_nodes": god}


# ── 6. find_surprising_connections ────────────────────────────────────────────

@mcp.tool()
def find_surprising_connections(repo_path: str, top_n: int = 5) -> dict:
    """Find unexpected cross-community edges.

    These are edges that bridge otherwise-distinct clusters. They often
    reveal hidden coupling, leaky abstractions, or architectural
    boundary violations — exactly what you want to surface during
    modernization planning.

    Args:
        repo_path: Absolute path to the repository root.
        top_n: How many surprising connections to return.

    Returns:
        Dict with a 'surprising_connections' list.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    surprises = pipeline.ensure_surprising_connections(analysis, top_n=top_n)
    return {"repo_path": str(root), "surprising_connections": surprises}


# ── 7. suggest_questions ──────────────────────────────────────────────────────

@mcp.tool()
def suggest_investigation_questions(repo_path: str, top_n: int = 5) -> dict:
    """Generate graph-derived investigation prompts.

    These are deterministic questions computed from graph structure —
    'what does this hub do?', 'why does X depend on Y?'. They're not
    LLM-generated; they're surfaced from centrality and community-edge
    patterns that warrant human inspection.

    Args:
        repo_path: Absolute path to the repository root.
        top_n: How many questions to return.

    Returns:
        Dict with a 'questions' list.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    questions = pipeline.ensure_suggested_questions(analysis, top_n=top_n)
    return {"repo_path": str(root), "questions": questions}


# ── 8. generate_mermaid_diagrams ──────────────────────────────────────────────

@mcp.tool()
def generate_mermaid_diagrams(repo_path: str) -> dict:
    """Generate three deterministic Mermaid diagrams from the code graph.

    Produces:
      - architecture: top-down module dependency graph (god nodes highlighted)
      - flow:         sequence diagram from actual call/import edges
      - components:   community-grouped LR diagram with cohesion scores

    No LLM is involved — diagrams are derived directly from the NetworkX
    graph, communities, and god_nodes. Will lazily run all upstream
    stages (extraction, clustering, god-node analysis) if needed.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        Dict with a 'diagrams' list. Each entry has diagram_type,
        mermaid_code, and description.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)

    # Ensure everything mermaid_converter needs is populated
    pipeline.ensure_clusters(analysis)
    pipeline.ensure_god_nodes(analysis, top_n=10)

    state = {
        "graph": analysis.graph,
        "communities": analysis.communities or {},
        "community_labels": analysis.community_labels or {},
        "cohesion": analysis.cohesion or {},
        "god_nodes": analysis.god_nodes or [],
    }
    diagrams = generate_all_diagrams(state)
    analysis.diagrams = diagrams

    return {"repo_path": str(root), "diagrams": diagrams}


# ── 9. read_file ──────────────────────────────────────────────────────────────

@mcp.tool()
def read_file(repo_path: str, path: str, max_lines: int = 300) -> str:
    """Read a single file inside the repository.

    Path-traversal-safe — refuses paths that escape the repo root.
    Useful for the consuming agent to drill into specific files
    flagged by other tools (god nodes, keystone files, etc.).

    Args:
        repo_path: Absolute path to the repository root.
        path: Path to the file, relative to the repo root.
        max_lines: Cap on lines returned (default 300, max 500).

    Returns:
        Header line followed by file contents. Truncation note appended
        if the file exceeded max_lines.
    """
    root = _resolve_repo(repo_path)
    max_lines = min(max(max_lines, 1), 500)

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
        content += (
            f"\n\n... [{total - max_lines} more lines — "
            f"increase max_lines to see more]"
        )

    return f"=== {path} ({total} lines total) ===\n{content}"


# ── 10. search_code ───────────────────────────────────────────────────────────

@mcp.tool()
def search_code(
    repo_path: str,
    pattern: str,
    file_glob: str = "*",
    max_results: int = 30,
) -> dict:
    """Regex-search the repository for a pattern.

    Use this to trace how classes/functions/routes are used, find all
    API endpoints, locate database models, etc.

    Args:
        repo_path: Absolute path to the repository root.
        pattern: Text or regex pattern. Falls back to literal match
            if the pattern is not valid regex.
        file_glob: Glob filter for filenames (e.g. '*.py', '*.ts').
        max_results: Cap on matching lines returned.

    Returns:
        Dict with pattern, total_matches, and a 'matches' list.
        Each match: {file, line, text}.
    """
    root = _resolve_repo(repo_path)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    matches: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if not fnmatch.fnmatch(fname, file_glob):
                continue
            fp = Path(dirpath) / fname
            if any(fname.endswith(e) for e in _SKIP_EXTS):
                continue
            try:
                rel = str(fp.relative_to(root))
                for i, line in enumerate(
                    fp.read_text(errors="replace").split("\n"), 1
                ):
                    if regex.search(line):
                        matches.append({
                            "file": rel,
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(matches) >= max_results:
                            break
            except Exception:
                continue
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    return {
        "repo_path": str(root),
        "pattern": pattern,
        "file_glob": file_glob,
        "total_matches": len(matches),
        "matches": matches,
    }


# ── 11. get_graph_stats ───────────────────────────────────────────────────────

@mcp.tool()
def get_graph_stats(repo_path: str) -> dict:
    """Return summary statistics about the code graph.

    Lightweight overview suitable for high-level reporting. Will run
    extraction + graph build + clustering lazily if not already done.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        Dict with node count, edge count, density, community count,
        average cohesion, top-5 community sizes, and node-kind breakdown.
    """
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    pipeline.ensure_clusters(analysis)

    G = analysis.graph
    communities = analysis.communities or {}
    cohesion = analysis.cohesion or {}

    if G is None or G.number_of_nodes() == 0:
        return {
            "repo_path": str(root),
            "nodes": 0,
            "edges": 0,
            "density": 0.0,
            "communities": 0,
            "avg_cohesion": 0.0,
            "top_communities": [],
            "node_kinds": {},
        }

    node_kinds: dict[str, int] = {}
    for _, attrs in G.nodes(data=True):
        # graphify writes 'file_type' on most nodes (code/rationale/etc.);
        # 'kind' is also supported. Prefer 'kind' if present.
        kind = attrs.get("kind") or attrs.get("file_type") or "unknown"
        node_kinds[kind] = node_kinds.get(kind, 0) + 1

    avg_cohesion = (
        sum(cohesion.values()) / len(cohesion) if cohesion else 0.0
    )

    sorted_comms = sorted(
        communities.items(), key=lambda x: len(x[1]), reverse=True
    )

    return {
        "repo_path": str(root),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "density": round(
            (2 * G.number_of_edges())
            / max(G.number_of_nodes() * (G.number_of_nodes() - 1), 1),
            6,
        ),
        "communities": len(communities),
        "avg_cohesion": round(avg_cohesion, 4),
        "top_communities": [
            {
                "id": cid,
                "label": (analysis.community_labels or {}).get(cid, ""),
                "size": len(nodes),
            }
            for cid, nodes in sorted_comms[:5]
        ],
        "node_kinds": dict(
            sorted(node_kinds.items(), key=lambda x: x[1], reverse=True)
        ),
    }


# ── 12. generate_pdf_report ───────────────────────────────────────────────────

@mcp.tool()
def generate_pdf_report(
    repo_path: str,
    output_path: str,
    narrative: dict | None = None,
    findings: list[dict] | None = None,
    generator_name: str = "codegrapher-mcp",
    elapsed_seconds: float = 0.0,
) -> dict:
    """Render a PDF assessment report for the repository.

    Combines deterministic graph analysis (god nodes, communities,
    surprising connections, Mermaid diagrams, file tree) with optional
    narrative supplied by the caller. Sections without input render
    empty — no LLM is used to fill gaps.

    The graph-derived sections (10. God Nodes & Surprising Connections,
    13. Repository File Tree, the embedded diagrams, and the Analysis
    Trace appendix) are ALWAYS populated automatically by running the
    pipeline. Sections 1-9, 11-12 require narrative input from the caller.

    Args:
        repo_path: Absolute path to the repository root.
        output_path: Where to write the PDF. Created if parent dirs exist.
        narrative: Optional dict with any of these keys to fill prose
            sections. All keys are optional; missing keys leave the
            corresponding section empty. Schema:
              - purpose: str — one-line cover subtitle
              - summary: str — executive summary paragraph
              - architecture_style: str — e.g. "MVC", "Microservices"
              - tech_stack: list[str] — technologies/frameworks
              - key_components: list[dict] — {name, description, files,
                responsibilities}
              - data_flow: list[str] — ordered steps
              - api_endpoints: list[dict] — {method, path, description}
              - database_models: list[str] — model names
              - security_notes: list[str] — observations
              - improvement_suggestions: list[str] — recommendations
              - testing_approach: str
              - deployment_info: str
              - code_quality_notes: list[str]
        findings: Optional list of finding dicts. Schema:
              {category, title, detail, files, confidence}
            Categories: architecture, component, data_flow, api_endpoint,
            security, tech_stack, design_pattern, code_quality,
            improvement, entry_point, dependency, database_model,
            testing, deployment, file_detail.
        generator_name: Label shown in the report header (default
            "codegrapher-mcp"). Set to your tool name when calling
            from a downstream pipeline.
        elapsed_seconds: Optional analysis time to show in the trace
            appendix.

    Returns:
        Dict with output_path, file_size_bytes, sections_with_content,
        and graph_metrics summary.
    """
    from .output.pdf_generator import generate_pdf

    root = _resolve_repo(repo_path)
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    analysis = CACHE.get(root)

    # Always populate the graph-derived pieces. These are deterministic
    # and form the core value of a no-LLM report.
    pipeline.ensure_clusters(analysis)
    pipeline.ensure_god_nodes(analysis, top_n=10)
    pipeline.ensure_surprising_connections(analysis, top_n=5)

    # Generate diagrams if not already done
    if analysis.diagrams is None:
        state_for_diagrams = {
            "graph": analysis.graph,
            "communities": analysis.communities or {},
            "community_labels": analysis.community_labels or {},
            "cohesion": analysis.cohesion or {},
            "god_nodes": analysis.god_nodes or [],
        }
        analysis.diagrams = generate_all_diagrams(state_for_diagrams)

    # Compute the file tree + line count for the report header
    if analysis.scan_summary is None:
        scan_repository(repo_path=str(root), include_tree=True)
    scan = analysis.scan_summary or {}
    directory_tree = scan.get("directory_tree", "")
    total_files = scan.get("total_files", 0)

    # Quick line count over code files (read-only, bounded by detection result)
    total_lines = 0
    detection = analysis.detection or {}
    for f in detection.get("files", {}).get("code", []):
        try:
            total_lines += sum(1 for _ in Path(f).open("r", errors="replace"))
        except Exception:
            continue

    # Assemble the state_accumulator the PDF generator expects.
    # finish_data holds narrative; everything else is graph-derived.
    state_accumulator = {
        "finish_data": dict(narrative or {}),
        "findings": list(findings or []),
        "diagrams": analysis.diagrams or [],
        "god_nodes": analysis.god_nodes or [],
        "surprising_connections": analysis.surprising_connections or [],
        "communities": analysis.communities or {},
        "community_labels": analysis.community_labels or {},
        "cohesion": analysis.cohesion or {},
    }

    generate_pdf(
        output_path=out,
        repo_name=root.name,
        provider_name=generator_name,
        state_accumulator=state_accumulator,
        directory_tree=directory_tree,
        total_files=total_files,
        total_lines=total_lines,
        elapsed_seconds=elapsed_seconds,
    )

    # Report which sections actually got narrative content
    fd = state_accumulator["finish_data"]
    sections_with_content = sorted(k for k, v in fd.items() if v)

    return {
        "output_path": str(out),
        "file_size_bytes": out.stat().st_size,
        "sections_with_content": sections_with_content,
        "graph_metrics": {
            "nodes": analysis.graph.number_of_nodes() if analysis.graph else 0,
            "edges": analysis.graph.number_of_edges() if analysis.graph else 0,
            "communities": len(analysis.communities or {}),
            "god_nodes": len(analysis.god_nodes or []),
            "surprising_connections": len(analysis.surprising_connections or []),
            "diagrams": len(analysis.diagrams or []),
        },
    }


# ── 13. load_aws_transform_output ─────────────────────────────────────────────

@mcp.tool()
def load_aws_transform_output(aws_output_path: str) -> dict:
    """Load and parse an AWS Transform comprehensive-codebase-analysis output.

    Accepts either a local directory path or an s3:// URL (the tool fetches
    via boto3 — requires AWS credentials and `pip install boto3`).

    The parser is intentionally lenient because AWS does not publish a
    file-level schema for this output. It scans the directory tree,
    classifies files by name patterns (executive_summary, technical_debt,
    architecture, code_analysis, domains, components, behavior,
    dependencies, recommendations), and parses JSON or markdown content.

    Args:
        aws_output_path: Local directory or s3:// path to the AWS Transform
            output. The expected S3 path shape is
            s3://atx-custom-output-{acct}/transformations/{job}/{ts}{conv}/

    Returns:
        Dict with sections_found, sections_missing, file_counts, and the
        full parsed analysis. Use this dict's content (or just the source
        path) as input to generate_merged_report.
    """
    from .aws_transform import load_aws_transform

    aws = load_aws_transform(aws_output_path)

    return {
        "source": aws.source,
        "job_name": aws.job_name,
        "conversation_id": aws.conversation_id,
        "files_seen": len(aws.files_seen),
        "files_parsed": len(aws.files_seen) - len(aws.unparsed_files),
        "unparsed_files": aws.unparsed_files,
        "missing_sections": aws.missing_sections,
        "sections_found": {
            "executive_summary": bool(aws.executive_summary),
            "project_overview": bool(aws.project_overview),
            "purpose": bool(aws.purpose),
            "architecture_style": bool(aws.architecture_style),
            "technical_debt_items": len(aws.technical_debt),
            "architecture_strengths": len(aws.architecture_strengths),
            "architecture_weaknesses": len(aws.architecture_weaknesses),
            "code_metrics_files": len(aws.code_metrics),
            "domains": len(aws.domains),
            "components": len(aws.components),
            "tech_stack": len(aws.tech_stack),
            "dependencies": len(aws.dependencies),
            "business_rules": len(aws.business_rules),
            "data_flow_steps": len(aws.data_flow),
            "api_endpoints": len(aws.api_endpoints),
            "database_models": len(aws.database_models),
            "recommendations": len(aws.recommendations),
        },
    }


# ── 14. generate_merged_report ────────────────────────────────────────────────

@mcp.tool()
def generate_merged_report(
    repo_path: str,
    aws_output_path: str,
    output_path: str,
    extra_narrative: dict | None = None,
    verify_business_rules: bool = True,
    generator_name: str = "codegrapher-mcp + AWS Transform",
) -> dict:
    """Generate a merged PDF combining AWS Transform analysis with CodeGrapher's
    graph analysis.

    This is the integration point. The two systems contribute different
    things to the final report:

      AWS Transform supplies:
        - Executive summary, project overview, purpose
        - Architecture style and strengths/weaknesses
        - Technical debt findings (severity-ranked)
        - Tech stack and dependencies
        - Business logic rules (early access)
        - Data flow narrative, API endpoints, database models
        - Modernization recommendations

      CodeGrapher supplies:
        - NetworkX graph (nodes, edges)
        - Communities and cohesion scores
        - God nodes and surprising connections
        - Mermaid diagrams (architecture, flow, components)
        - Repository file tree
        - Business rule verification (anchoring AWS rules to actual graph paths)

    When AWS provides a section that CodeGrapher also computes, the merge
    rule is documented per-field in the implementation. Generally:
      - Narrative prose -> AWS
      - Component file lists -> AWS labels + CodeGrapher community membership
      - Centrality / surprising connections -> CodeGrapher only
      - Improvement recommendations -> AWS list + CodeGrapher's keystone-files
        cross-reference

    Args:
        repo_path: Local repository to analyze with CodeGrapher.
        aws_output_path: AWS Transform output (local dir or s3:// URL).
        output_path: Where to write the merged PDF.
        extra_narrative: Optional dict of additional narrative fields to
            override or add. Same schema as generate_pdf_report's narrative
            arg. These take precedence over AWS-derived content for any
            fields you supply.
        verify_business_rules: If True, anchor AWS-extracted business rules
            to actual nodes in the CodeGrapher graph. Unverified rules are
            flagged in the findings list.
        generator_name: Label shown in the report header.

    Returns:
        Dict with the output path, AWS sections found, CodeGrapher graph
        metrics, and (if enabled) business rule verification stats.
    """
    from .aws_transform import (
        load_aws_transform,
        to_narrative_dict,
        to_findings,
        verify_business_rules as _verify_rules,
    )
    from .output.pdf_generator import generate_pdf
    from .output.mermaid_converter import generate_all_diagrams

    # 1. Load AWS Transform output
    aws = load_aws_transform(aws_output_path)

    # 2. Run CodeGrapher's analysis (lazily; cached if already done)
    root = _resolve_repo(repo_path)
    analysis = CACHE.get(root)
    pipeline.ensure_clusters(analysis)
    pipeline.ensure_god_nodes(analysis, top_n=10)
    pipeline.ensure_surprising_connections(analysis, top_n=5)

    if analysis.diagrams is None:
        state_for_diagrams = {
            "graph": analysis.graph,
            "communities": analysis.communities or {},
            "community_labels": analysis.community_labels or {},
            "cohesion": analysis.cohesion or {},
            "god_nodes": analysis.god_nodes or [],
        }
        analysis.diagrams = generate_all_diagrams(state_for_diagrams)

    if analysis.scan_summary is None:
        scan_repository(repo_path=str(root), include_tree=True)
    scan = analysis.scan_summary or {}
    directory_tree = scan.get("directory_tree", "")
    total_files = scan.get("total_files", 0)

    total_lines = 0
    detection = analysis.detection or {}
    for f in detection.get("files", {}).get("code", []):
        try:
            total_lines += sum(1 for _ in Path(f).open("r", errors="replace"))
        except Exception:
            continue

    # 3. Convert AWS analysis into narrative + findings format
    narrative = to_narrative_dict(aws)
    findings = to_findings(aws)

    # 4. Cross-reference: enrich AWS recommendations with CodeGrapher's
    #    keystone files (high-centrality files that AWS also flagged as debt).
    #    God-node dicts don't carry source_file, so look it up via the graph.
    god_node_files: set[str] = set()
    for gn in (analysis.god_nodes or []):
        nid = gn.get("id")
        if nid and analysis.graph is not None and nid in analysis.graph.nodes:
            src = analysis.graph.nodes[nid].get("source_file", "")
            if src:
                god_node_files.add(src)

    # AWS debt files may be repo-relative; CodeGrapher source files are absolute.
    # Match on basename + suffix-of-path to bridge the format gap.
    def _path_keys(p: str) -> set[str]:
        """Generate matching keys from a path: basename, full, and suffix forms."""
        keys = {p, Path(p).name}
        parts = Path(p).parts
        # Add progressively longer suffixes (e.g. "core/extract.py", "codegrapher/core/extract.py")
        for i in range(1, len(parts) + 1):
            keys.add(str(Path(*parts[-i:])))
        return keys

    god_node_keys: set[str] = set()
    for gf in god_node_files:
        god_node_keys |= _path_keys(gf)

    aws_debt_files: set[str] = set()
    aws_debt_keys: set[str] = set()
    for debt in aws.technical_debt:
        for f in debt.get("files", []):
            aws_debt_files.add(f)
            aws_debt_keys |= _path_keys(f)

    # A "keystone priority" is any path-form match between the two key sets.
    raw_matches = god_node_keys & aws_debt_keys
    # For each match, keep only the longest-form representation. If
    # "extract.py" and "core/extract.py" and "codegrapher/core/extract.py"
    # all match, only the longest is informative.
    keystone_priorities: list[str] = []
    sorted_matches = sorted(raw_matches, key=lambda s: (s.count("/"), len(s)), reverse=True)
    seen_basenames: set[str] = set()
    for m in sorted_matches:
        bn = Path(m).name
        if bn in seen_basenames:
            continue
        seen_basenames.add(bn)
        keystone_priorities.append(m)
    keystone_priorities.sort()

    if keystone_priorities:
        narrative["improvement_suggestions"] = list(narrative["improvement_suggestions"])
        narrative["improvement_suggestions"].insert(
            0,
            "PRIORITY: " + ", ".join(keystone_priorities) +
            " — flagged by AWS as technical debt AND identified by CodeGrapher "
            "as high-centrality. Changes here ripple widely; address first."
        )

    # 5. Verify business rules against the graph
    rule_verification = None
    if verify_business_rules and aws.business_rules:
        rule_verification = _verify_rules(aws, analysis.graph)
        # Add unverified rules as findings flagged for review
        for rule in rule_verification["unverified"]:
            findings.append({
                "category": "code_quality",
                "title": f"UNVERIFIED business rule: {rule.get('rule', '')[:80]}",
                "detail": (
                    "AWS Transform extracted this business rule, but CodeGrapher "
                    "could not anchor it to any function or file in the code "
                    "graph. May be an LLM hallucination, may reference logic "
                    "our extractor missed. Manual review recommended."
                ),
                "files": rule.get("files", []),
                "confidence": "low",
            })
        # Add verified rules as findings
        for rule in rule_verification["verified"]:
            verif = rule.get("_verification", {})
            findings.append({
                "category": "design_pattern",
                "title": f"Verified business rule",
                "detail": (
                    f"{rule.get('rule', '')} "
                    f"Anchored to: {', '.join(verif.get('matched_functions', []) + verif.get('matched_files', []))}"
                ),
                "files": rule.get("files", []),
                "confidence": "high",
            })

    # 6. Apply caller's extra narrative overrides (highest priority)
    if extra_narrative:
        for k, v in extra_narrative.items():
            if v:
                narrative[k] = v

    # 7. Render the merged PDF
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    state_accumulator = {
        "finish_data": narrative,
        "findings": findings,
        "diagrams": analysis.diagrams or [],
        "god_nodes": analysis.god_nodes or [],
        "surprising_connections": analysis.surprising_connections or [],
        "communities": analysis.communities or {},
        "community_labels": analysis.community_labels or {},
        "cohesion": analysis.cohesion or {},
    }

    generate_pdf(
        output_path=out,
        repo_name=root.name,
        provider_name=generator_name,
        state_accumulator=state_accumulator,
        directory_tree=directory_tree,
        total_files=total_files,
        total_lines=total_lines,
    )

    result = {
        "output_path": str(out),
        "file_size_bytes": out.stat().st_size,
        "aws_sections_loaded": {
            "executive_summary": bool(aws.executive_summary),
            "technical_debt_items": len(aws.technical_debt),
            "components": len(aws.components),
            "tech_stack": len(aws.tech_stack),
            "business_rules": len(aws.business_rules),
            "recommendations": len(aws.recommendations),
        },
        "aws_unparsed_files": aws.unparsed_files,
        "aws_missing_sections": aws.missing_sections,
        "codegrapher_metrics": {
            "nodes": analysis.graph.number_of_nodes() if analysis.graph else 0,
            "edges": analysis.graph.number_of_edges() if analysis.graph else 0,
            "communities": len(analysis.communities or {}),
            "god_nodes": len(analysis.god_nodes or []),
        },
        "keystone_priorities": keystone_priorities,
    }

    if rule_verification is not None:
        result["business_rule_verification"] = {
            "verified": len(rule_verification["verified"]),
            "partial": len(rule_verification["partial"]),
            "unverified": len(rule_verification["unverified"]),
        }

    return result


# ── 15. reset_cache ───────────────────────────────────────────────────────────

@mcp.tool()
def reset_cache(repo_path: str | None = None) -> dict:
    """Drop cached analysis for a repository (or all repos if path is None).

    Use this after the repository contents change on disk, or to free memory.

    Args:
        repo_path: Repository to reset. If omitted, clears the entire cache.

    Returns:
        Dict confirming what was cleared.
    """
    if repo_path is None:
        CACHE.clear()
        return {"cleared": "all"}
    p = _resolve_repo(repo_path)
    CACHE.reset(p)
    return {"cleared": str(p)}


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
