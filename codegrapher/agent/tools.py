"""
LangGraph tool definitions.

Tools available to the agent (6 total — exploration only):
  1. scan_repository     - file counts, sizes, directory tree
  2. extract_ast_graph   - tree-sitter AST extraction
  3. cluster_communities - Leiden/Louvain community detection
  4. analyze_graph       - god nodes, surprising connections
  5. read_file           - read any file in the repo (LLM eyes)
  6. search_code         - grep across the repo (LLM eyes)

finish_analysis is NOT a tool anymore.
After the exploration loop ends naturally (agent returns no tool calls),
the pipeline calls synthesiser.synthesise() as a separate dedicated LLM
call with a compressed context and its own retry/backoff logic.

This eliminates the throttle loop seen in production: the exploration loop
is cheap (small per-call context), and the synthesis call is isolated,
retryable, and receives only a compact payload — not the full 26-step history.
"""
from __future__ import annotations

import fnmatch
import heapq
import json
import os
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool


# ── Shared constants ──────────────────────────────────────────────────────────

_SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "venv", ".venv", "env", "dist", "build",
    ".next", ".nuxt", "coverage", ".tox", ".idea", ".vscode",
    ".terraform",                    # Terraform provider cache — large, not useful
}

_SKIP_EXTS = {
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".zip", ".tar", ".gz", ".whl", ".egg",
    ".tfstate", ".tfstate.backup",   # Terraform state — may contain secrets
    ".lock",                         # lock files (package-lock, poetry.lock etc.)
}

# Application code extensions — used for word-count in scan
_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".cpp", ".c", ".h", ".cs", ".kt", ".swift", ".sh",
}

# Infrastructure / config extensions — Level 1: counted in scan_repository stats
_INFRA_EXTS = {
    ".tf",           # Terraform HCL
    ".tfvars",       # Terraform variable files
    ".hcl",          # Generic HCL (Packer, Vault, Consul, Nomad)
    ".yaml", ".yml", # Kubernetes manifests, Helm, GitHub Actions, docker-compose, Ansible
    ".toml",         # Rust Cargo, Python pyproject, general config
    ".json",         # package.json, serverless.json, CDK configs
    ".dockerfile",   # alternative Dockerfile extension
    ".env.example",  # environment variable templates (not .env itself)
    ".ini", ".cfg",  # legacy config files
    ".conf",         # nginx, haproxy, etc.
    ".sh", ".bash",  # shell/deploy scripts (already in _CODE_EXTS, listed for clarity)
}

# Canonical infra filenames regardless of extension
_INFRA_FILENAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "makefile", "jenkinsfile", "procfile",
    "serverless.yml", "serverless.yaml",
    "cdk.json", "pulumi.yaml",
    ".helmignore", "chart.yaml", "values.yaml",
}

# All extensions that scan_repository should count (code + infra)
_ALL_COUNTED_EXTS = _CODE_EXTS | _INFRA_EXTS


# ── Private helpers ────────────────────────────────────────────────────────────

def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """Resolve a relative path and guard against path-traversal escapes."""
    p = (repo_root / rel_path).resolve()
    if not p.is_relative_to(repo_root):
        raise ValueError(f"Path escapes repo root: {rel_path}")
    return p


def _build_tree(root: Path, max_depth: int = 4) -> str:
    """Render a Unicode directory tree string, skipping noise dirs."""
    lines = [f"{root.name}/"]

    def _walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
        except PermissionError:
            return
        entries = [e for e in entries
                   if e.name not in _SKIP_DIRS and not e.name.startswith(".")]
        for i, entry in enumerate(entries[:25]):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and depth < max_depth:
                _walk(entry, prefix + ("    " if is_last else "│   "), depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


def _is_skipped(fname: str) -> bool:
    """Return True for binary/generated files that should never be read."""
    ext = Path(fname).suffix.lower()
    if ext in _SKIP_EXTS:
        return True
    # .tfstate files even without the exact suffix match
    if fname.endswith(".tfstate") or fname.endswith(".tfstate.backup"):
        return True
    return False


def _looks_like_k8s(path: Path) -> bool:
    """Quick heuristic: does this YAML look like a Kubernetes manifest?"""
    if path.suffix.lower() not in (".yaml", ".yml"):
        return False
    try:
        head = path.read_text(errors="ignore")[:800]
        return any(kw in head for kw in (
            "apiVersion:", "kind: Deployment", "kind: Service",
            "kind: Pod", "kind: ConfigMap", "kind: Ingress",
            "kind: StatefulSet", "kind: DaemonSet", "kind: Job",
            "kind: CronJob", "kind: Namespace", "kind: HorizontalPodAutoscaler",
        ))
    except Exception:
        return False


# ── Tool factory ───────────────────────────────────────────────────────────────

def make_tools(repo_root: Path, state_accumulator: dict) -> list:
    """
    Build the exploration tool list bound to a specific repo_root.

    state_accumulator keys written by these tools:
      scan_result    : dict  — from scan_repository
      graphify_ast   : dict  — from extract_ast_graph
      detection      : dict  — from extract_ast_graph
      graph          : nx.DiGraph — from cluster_communities
      communities    : dict  — from cluster_communities
      cohesion       : dict  — from cluster_communities
      community_labels: dict — from cluster_communities
      god_nodes      : list  — from analyze_graph
      surprising_connections: list — from analyze_graph
      suggested_questions   : list — from analyze_graph

    Note: finish_analysis is NOT in this list.
    Synthesis happens outside the agent loop via synthesiser.synthesise().
    """
    root = repo_root.resolve()

    # ── 1. scan_repository ────────────────────────────────────────────────────
    @tool
    def scan_repository(include_tree: bool = True) -> str:
        """
        Scan the repository: count files by extension (code + infra), find
        the largest files, build a directory tree, and detect infra presence.
        Call this FIRST — it orientates all subsequent decisions.

        Returns JSON with:
          total_files, by_extension (top 20), largest_files (top 10),
          infra_files (list of detected infrastructure files),
          has_terraform, has_kubernetes, has_docker, has_cicd,
          directory_tree (optional).
        """
        ext_counts: dict[str, int] = {}
        total_files = 0
        largest: list[tuple[int, str]] = []
        infra_files: list[str] = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if _is_skipped(fname):
                    continue
                fp = Path(dirpath) / fname
                rel = str(fp.relative_to(root))
                ext = fp.suffix.lower() or "none"
                fname_lower = fname.lower()

                # Count everything that isn't binary noise
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                total_files += 1

                size = fp.stat().st_size
                if len(largest) < 10:
                    heapq.heappush(largest, (size, rel))
                else:
                    heapq.heappushpop(largest, (size, rel))

                # Flag infrastructure files
                if (ext in _INFRA_EXTS or fname_lower in _INFRA_FILENAMES
                        or fname_lower.startswith("dockerfile")):
                    infra_files.append(rel)

        largest_sorted = sorted(largest, reverse=True)

        # Quick presence flags for the agent and synthesiser
        has_terraform  = any(".tf" in f or ".tfvars" in f or ".hcl" in f
                             for f in infra_files)
        has_kubernetes = any(f for f in infra_files
                             if _looks_like_k8s(root / f))
        has_docker     = any("dockerfile" in f.lower() or
                             "docker-compose" in f.lower()
                             for f in infra_files)
        has_cicd       = any(
                             ".github/workflows" in f or
                             "jenkinsfile" in f.lower() or
                             ".gitlab-ci" in f.lower() or
                             "bitbucket-pipelines" in f.lower() or
                             "azure-pipelines" in f.lower() or
                             "circleci" in f.lower()
                             for f in infra_files)
        has_helm       = any("chart.yaml" in f.lower() or
                             "values.yaml" in f.lower() or
                             "helmfile" in f.lower()
                             for f in infra_files)

        result = {
            "total_files": total_files,
            "by_extension": dict(
                sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:20]
            ),
            "largest_files": [{"size": s, "path": p} for s, p in largest_sorted],
            "infra_files": sorted(infra_files)[:40],  # cap to avoid huge output
            "has_terraform":  has_terraform,
            "has_kubernetes": has_kubernetes,
            "has_docker":     has_docker,
            "has_cicd":       has_cicd,
            "has_helm":       has_helm,
        }
        if include_tree:
            result["directory_tree"] = _build_tree(root)

        state_accumulator["scan_result"] = result
        return json.dumps(result, indent=2)

    # ── 2. extract_ast_graph ──────────────────────────────────────────────────
    @tool
    def extract_ast_graph() -> str:
        """
        Run graphify's AST extraction (tree-sitter) on all code files.
        Extracts classes, functions, imports, and call relationships.
        Call this AFTER scan_repository.

        Returns a summary: node count, edge count, files processed, sample nodes.
        """
        try:
            from codegrapher.core.detect import detect
            from codegrapher.core.extract import collect_files, extract as gf_extract
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

            # ── Level 3: infra extraction ─────────────────────────────────
            infra_files = state_accumulator.get("scan_result", {}).get("infra_files", [])
            if infra_files:
                try:
                    from codegrapher.core.infra_extract import extract_infra
                    infra_result = extract_infra(root, infra_files)
                    state_accumulator["infra_result"] = infra_result
                    infra_summary = infra_result.get("summary", {})
                    print(f"   Infra: tf={infra_summary.get('tf_file_count',0)} "
                          f"k8s={infra_summary.get('k8s_manifest_count',0)} "
                          f"docker={infra_summary.get('docker_file_count',0)}")
                except Exception as e:
                    state_accumulator["infra_result"] = {"error": str(e)}

            return json.dumps({
                "nodes_extracted": len(result.get("nodes", [])),
                "edges_extracted": len(result.get("edges", [])),
                "files_processed": len(code_files),
                "sample_nodes": [
                    {
                        "id": n["id"],
                        "label": n.get("label", ""),
                        "file": n.get("source_file", ""),
                    }
                    for n in result.get("nodes", [])[:8]
                ],
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e), "nodes": 0, "edges": 0})

    # ── 3. cluster_communities ────────────────────────────────────────────────
    @tool
    def cluster_communities() -> str:
        """
        Run Leiden/Louvain community detection on the extracted AST graph.
        Groups related code entities into semantic clusters with cohesion scores.
        Call this AFTER extract_ast_graph.

        Returns: total nodes/edges, community count, top-10 communities by size.
        """
        try:
            from codegrapher.core.cluster import cluster, score_all
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

            labels: dict[int, str] = {}
            for cid, node_ids in communities.items():
                sources = [
                    G.nodes[n].get("source_file", "")
                    for n in node_ids if n in G.nodes
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
                    for cid, node_ids in sorted(
                        communities.items(), key=lambda x: len(x[1]), reverse=True
                    )[:10]
                ],
            }, indent=2)

        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── 4. analyze_graph ──────────────────────────────────────────────────────
    @tool
    def analyze_graph() -> str:
        """
        Find god nodes (highest-degree core abstractions), surprising
        cross-community connections, and suggested investigation questions.
        Call this AFTER cluster_communities.

        Returns: god_nodes, surprising_connections, suggested_questions.
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
    def read_file(path: str, max_lines: int = 100) -> str:
        """
        Read a file inside the repository. Output goes to your context —
        the synthesiser will use what you learned here.

        Args:
            path: Relative path from the repo root.
            max_lines: Lines to return (default 100, hard cap 300).
                       100 lines is enough to understand what a module does.
        """
        max_lines = min(max_lines, 300)
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
            content += f"\n\n... [{total - max_lines} more lines truncated]"

        return f"=== {path} ({total} lines total) ===\n{content}"

    # ── 6. search_code ────────────────────────────────────────────────────────
    @tool
    def search_code(
        pattern: str,
        file_glob: str = "*",
        max_results: int = 20,
    ) -> str:
        """
        Grep a pattern across all repo files. Output goes to your context —
        the synthesiser will use what you learned here.

        Args:
            pattern: Text or regex to search for (case-insensitive).
            file_glob: Filename glob filter, e.g. '*.py', '*.ts' (default: all).
            max_results: Max matching lines to return (default 20).
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        matches: list[str] = []
        done = False

        for dirpath, dirnames, filenames in os.walk(root):
            if done:
                break
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if done:
                    break
                if not fnmatch.fnmatch(fname, file_glob):
                    continue
                if _is_skipped(fname):
                    continue
                fp = Path(dirpath) / fname
                try:
                    rel = str(fp.relative_to(root))
                    for i, line in enumerate(
                        fp.read_text(errors="replace").split("\n"), 1
                    ):
                        if regex.search(line):
                            matches.append(f"{rel}:{i}:  {line.strip()[:120]}")
                            if len(matches) >= max_results:
                                done = True
                                break
                except Exception:
                    continue

        if not matches:
            return f"No matches found for: {pattern}"
        return f"Found {len(matches)} matches for '{pattern}':\n\n" + "\n".join(matches)

    return [
        scan_repository,
        extract_ast_graph,
        cluster_communities,
        analyze_graph,
        read_file,
        search_code,
    ]
