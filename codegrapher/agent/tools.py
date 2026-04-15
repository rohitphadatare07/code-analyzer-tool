"""
Agent tool definitions with call budget enforcement and quality gates.

Changes from original
----------------------
1. scan_repository uses detect() from graphify instead of its own os.walk.
   detect() already handles skip lists, .graphifyignore, sensitive file
   skipping, and incremental scanning. scan_repository now calls it once
   and stores the result in state_accumulator["detection"].

2. extract_ast_graph reuses state_accumulator["detection"] set by
   scan_repository. No second filesystem walk. detect() is never called
   more than once per run.

3. make_tools() accepts file_budget, search_budget, finding_budget.
   Budgets are enforced inside read_file, search_code, record_finding.
   When exhausted the tool returns BUDGET_EXHAUSTED — the agent cannot
   exceed the limit no matter what the system prompt says.

4. record_finding has a quality gate.
   Findings with fewer than 3 sentences are REJECTED with an explanation
   of what is missing. This forces WHAT/WHY/CONSEQUENCE/HOW.

5. finish_analysis has a completeness gate.
   Prevents premature termination before minimum exploration is done.

6. Tool docstrings are LLM instructions, not developer docs.
   They tell the LLM WHEN to call the tool and WHAT to do with the result.

7. analyze_graph return value is actionable, not just raw JSON.
   It explicitly tells the agent which files to investigate next.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool


# ── Skip lists (used by search_code and _build_tree) ──────────────────────────

_SKIP_DIRS = frozenset({
    ".git", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "venv", ".venv", "env", ".tox",
    "dist", "build", ".next", ".nuxt", "coverage", ".idea", ".vscode", ".yarn",
})

_ALLOW_HIDDEN = frozenset({".github", ".gitlab-ci", ".circleci"})

_SKIP_EXTS = frozenset({
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg",
    ".zip", ".tar", ".gz", ".whl", ".egg",
    ".lock", ".snap", ".map",
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    p = (repo_root / rel_path).resolve()
    if not str(p).startswith(str(repo_root.resolve())):
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
            if e.name not in _SKIP_DIRS and (
                not e.name.startswith(".") or e.name in _ALLOW_HIDDEN
            )
        ]
        for i, entry in enumerate(entries[:25]):
            is_last   = i == len(entries) - 1
            connector = "`-- " if is_last else "|-- "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and depth < max_depth:
                _walk(entry, prefix + ("    " if is_last else "|   "), depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


def _budget_remaining(counts: dict, key: str, budget: int) -> str:
    used = counts.get(key, 0)
    return f"[{key}: {budget - used}/{budget} remaining]"


def _budget_exhausted(tool_name: str, budget: int) -> str:
    return (
        f"BUDGET_EXHAUSTED: {tool_name} budget ({budget} calls) is used up. "
        f"Do not call {tool_name} again. "
        f"Proceed with what you have gathered — call finish_analysis."
    )


# ── Tool factory ───────────────────────────────────────────────────────────────

def make_tools(
    repo_root:         Path,
    state_accumulator: dict,
    file_budget:    int = 6,
    search_budget:  int = 4,
    finding_budget: int = 15,
) -> list:
    """
    Create all agent tools as closures over repo_root, state_accumulator,
    and budget limits. Budget counters live in state_accumulator["_call_counts"].
    """
    root   = repo_root.resolve()
    counts = state_accumulator.setdefault("_call_counts", {
        "read_file": 0, "search_code": 0, "record_finding": 0,
    })

    # ── Pipeline tool 1: scan_repository ──────────────────────────────────────
    @tool
    def scan_repository(include_tree: bool = True) -> str:
        """
        Scan the repository structure. Call this FIRST and ONLY ONCE.

        Returns file counts by language, largest files, directory tree.

        After reading the output:
          - Identify the project type (Node.js, Python, Go, Java...)
          - Note the largest files — they usually contain core logic
          - Identify entry point files (index.js, main.py, boot.js, app.ts)
          - Plan which files to spend your read_file budget on
        """
        # Use detect() from graphify — it already handles:
        #   - skip lists (node_modules, venv, .git, dist, build ...)
        #   - .graphifyignore support
        #   - sensitive file skipping (.env, .pem, credentials)
        #   - incremental scanning support
        # No need to re-walk the filesystem ourselves.
        try:
            from codegrapher.core.detect import detect
            detection = detect(root)
        except Exception as e:
            return f"ERROR running detect: {e}"

        code_files = [Path(f) for f in detection.get("files", {}).get("code", [])]
        total_files = detection.get("total_files", 0)
        total_words = detection.get("total_words", 0)
        warning     = detection.get("warning")

        # detect() groups by type (code/doc/image) not by extension.
        # Compute extension breakdown and largest files from the code file list —
        # these are the two things detect() does not provide that the agent needs.
        ext_counts: dict[str, int] = {}
        large_files: list[tuple[int, str]] = []

        for fp in code_files:
            ext = fp.suffix.lower() or "(none)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            try:
                size = fp.stat().st_size
                large_files.append((size, str(fp.relative_to(root))))
            except OSError:
                pass

        large_files.sort(reverse=True)
        top_exts = dict(sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:12])

        # Store detection result so extract_ast_graph can reuse it directly
        # — no second filesystem walk needed.
        state_accumulator["detection"] = detection
        state_accumulator["scan_result"] = {
            "total_files":   total_files,
            "total_words":   total_words,
            "by_extension":  top_exts,
            "largest_files": [{"size_bytes": s, "path": p} for s, p in large_files[:10]],
            "directory_tree": _build_tree(root) if include_tree else "",
        }

        output = f"Repository: {total_files:,} files, {total_words:,} words\n"
        if warning:
            output += f"⚠ {warning}\n"
        output += (
            "\nLanguages (code files by extension):\n"
            + "\n".join(f"  {e}: {c}" for e, c in list(top_exts.items())[:8])
            + "\n\nLargest files (candidates for read_file):\n"
            + "\n".join(f"  {p} ({s//1024}KB)" for s, p in large_files[:6])
        )
        if include_tree:
            output += f"\n\nDirectory tree:\n{state_accumulator['scan_result']['directory_tree']}"
        return output

    # ── Pipeline tool 2: extract_ast_graph ────────────────────────────────────
    @tool
    def extract_ast_graph() -> str:
        """
        Extract the dependency graph using tree-sitter AST parsing.
        Call ONCE, AFTER scan_repository, BEFORE cluster_communities.

        Parses all source files and builds a graph of classes, functions,
        modules and their import/call relationships.
        """
        try:
            from codegrapher.core.extract import collect_files, extract as gf_extract
            from codegrapher.core.build   import build_from_json
        except ImportError as e:
            return f"ERROR: graphify not installed — {e}"

        # Reuse the detection result stored by scan_repository.
        # This avoids a second full filesystem walk.
        detection = state_accumulator.get("detection")
        if detection is None:
            return "ERROR: Call scan_repository first — detection result not found."

        try:
            code_files_raw = detection.get("files", {}).get("code", [])
            code_files     = []
            for f in code_files_raw:
                p = Path(f)
                if p.is_dir():
                    code_files.extend(collect_files(p))
                elif p.exists():
                    code_files.append(p)

            if not code_files:
                state_accumulator["graphify_ast"] = {"nodes": [], "edges": []}
                return "No code files found."

            result = gf_extract(code_files)
            state_accumulator["graphify_ast"] = result

            nodes = result.get("nodes", [])
            edges = result.get("edges", [])

            # Compute top nodes by connection count for agent planning
            deg: dict[str, int] = {}
            for e in edges:
                deg[e.get("source","")] = deg.get(e.get("source",""),0) + 1
                deg[e.get("target","")] = deg.get(e.get("target",""),0) + 1
            top = sorted(deg.items(), key=lambda x: x[1], reverse=True)[:5]

            return (
                f"Graph extracted: {len(nodes):,} nodes, {len(edges):,} edges "
                f"from {len(code_files):,} files\n\n"
                f"Most-connected nodes (likely architectural bottlenecks):\n"
                + "\n".join(f"  {nid}: {d} connections" for nid, d in top)
                + "\n\nNext step: cluster_communities"
            )
        except Exception as e:
            return f"ERROR: {e}"

    # ── Pipeline tool 3: cluster_communities ──────────────────────────────────
    @tool
    def cluster_communities() -> str:
        """
        Detect semantic communities using Leiden/Louvain algorithm.
        Call ONCE, AFTER extract_ast_graph, BEFORE analyze_graph.

        Groups related nodes into communities representing natural
        architectural boundaries (layers, bounded contexts, modules).

        Communities with low cohesion (<20%) contain unrelated code
        that ended up together — investigate why.
        """
        try:
            from codegrapher.core.cluster import cluster, score_all
            from codegrapher.core.build   import build_from_json
        except ImportError as e:
            return f"ERROR: {e}"

        ast_data = state_accumulator.get("graphify_ast")
        if not ast_data:
            return "ERROR: Call extract_ast_graph first."

        try:
            G           = build_from_json(ast_data)
            communities = cluster(G)
            cohesion    = score_all(G, communities)

            labels: dict[int, str] = {}
            for cid, node_ids in communities.items():
                sources = [G.nodes[n].get("source_file","") for n in node_ids if n in G.nodes]
                sources = [s for s in sources if s]
                if sources:
                    parts = [Path(s).parts[0] if Path(s).parts else s for s in sources]
                    labels[cid] = max(set(parts), key=parts.count)
                else:
                    labels[cid] = f"Community {cid}"

            state_accumulator["graph"]            = G
            state_accumulator["communities"]      = communities
            state_accumulator["cohesion"]         = cohesion
            state_accumulator["community_labels"] = labels

            lines = [f"{len(communities)} communities detected:\n"]
            for cid, nids in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True):
                coh  = cohesion.get(cid, 0)
                lbl  = labels.get(cid, f"C{cid}")
                flag = " ← LOW COHESION — investigate" if coh < 0.2 and len(nids) >= 3 else ""
                lines.append(f"  Community {cid} [{lbl}]: {len(nids)} nodes, {coh:.0%} cohesion{flag}")

            lines.append("\nNext step: analyze_graph")
            return "\n".join(lines)

        except Exception as e:
            return f"ERROR: {e}"

    # ── Pipeline tool 4: analyze_graph ────────────────────────────────────────
    @tool
    def analyze_graph() -> str:
        """
        Find architectural patterns: god nodes and surprising connections.
        Call ONCE, AFTER cluster_communities.

        God nodes = files with unusually high connectivity.
        Surprising connections = unexpected cross-community edges.

        IMPORTANT: After reading this output, use your read_file budget
        on the god nodes listed. They are the architectural core.
        Surprising connections are architectural concerns — investigate them.
        """
        try:
            from codegrapher.core.analyze import god_nodes as gf_god_nodes, surprising_connections
        except ImportError as e:
            return f"ERROR: {e}"

        G           = state_accumulator.get("graph")
        communities = state_accumulator.get("communities")
        labels      = state_accumulator.get("community_labels", {})

        if G is None or communities is None:
            return "ERROR: Call cluster_communities first."

        try:
            god      = gf_god_nodes(G, top_n=8)
            surprise = surprising_connections(G, communities, top_n=5)

            state_accumulator["god_nodes"]              = god
            state_accumulator["surprising_connections"] = surprise

            # Make output explicitly actionable — tell agent what to do next
            lines = [f"God nodes — use your read_file budget on these:\n"]
            for g in god[:5]:
                src = G.nodes.get(g["id"], {}).get("source_file", "?")
                lines.append(
                    f"  {g.get('label', g['id'])} — {g.get('edges','?')} connections\n"
                    f"    → read_file('{src}')"
                )

            if surprise:
                lines.append("\nSurprising cross-community connections — investigate these:\n")
                for s in surprise[:4]:
                    lines.append(
                        f"  {s.get('source','')} → {s.get('target','')} "
                        f"— {s.get('why','crosses community boundaries')}\n"
                        f"    → Read both files to understand if this is accidental coupling"
                    )

            lines.append(f"\nNext: generate_diagrams_from_graph")
            lines.append(f"Then spend your read_file budget on the god nodes above.")
            return "\n".join(lines)

        except Exception as e:
            return f"ERROR: {e}"

    # ── Exploration tool 5: read_file ─────────────────────────────────────────
    _read_file_desc = (
        f"Read a file from the repository. Budget: {file_budget} calls total.\n\n"
        "SPEND THIS BUDGET ON:\n"
        "  - God nodes listed by analyze_graph (highest architectural value)\n"
        "  - Files in surprising cross-community connections\n"
        "  - Entry points (index.js, main.py, boot.js, app.ts, manage.py)\n"
        "  - Files that failed a specific assessment check\n\n"
        "DO NOT read files randomly. Every call must be motivated by a\n"
        "specific finding from the pipeline tools.\n\n"
        "AFTER READING: call record_finding immediately with what you learned.\n"
        "Do not read multiple files before recording — record as you go.\n\n"
        "Args:\n"
        "    path:      Relative path from repo root\n"
        "    max_lines: Lines to read (default 200, max 400)"
    )

    @tool(description=_read_file_desc)
    def read_file(path: str, max_lines: int = 200) -> str:
        # Budget enforcement
        if counts.get("read_file", 0) >= file_budget:
            return _budget_exhausted("read_file", file_budget)

        counts["read_file"] = counts.get("read_file", 0) + 1
        remaining = file_budget - counts["read_file"]

        max_lines = min(max_lines, 400)
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
            return f"ERROR: {e}"

        lines = content.split("\n")
        total = len(lines)
        excerpt = "\n".join(lines[:max_lines])
        note    = f"\n[{total - max_lines} more lines — use max_lines={min(total,400)} to see more]" if total > max_lines else ""

        return (
            f"=== {path} ({total} lines) ===\n\n{excerpt}{note}\n\n"
            f"[read_file: {remaining}/{file_budget} calls remaining — "
            f"record a finding about this file now]"
        )

    # ── Exploration tool 6: search_code ───────────────────────────────────────
    _search_code_desc = (
        f"Search for a pattern across source files. Budget: {search_budget} calls total.\n\n"
        "HIGH-VALUE searches (spend budget on these):\n"
        "  - All API routes: 'app\\.get|router\\.post|@GetMapping'\n"
        "  - DB model definitions: 'class.*Model|Schema\\('\n"
        "  - Config/env usage: 'process\\.env|os\\.environ'\n"
        "  - Verify a specific coupling: 'require.*knex|import.*db'\n\n"
        "LOW-VALUE searches (do NOT waste budget on these):\n"
        '  - Things scan_repository already told you (file counts, languages)\n'
        "  - The same concept twice with different patterns\n"
        '  - General exploration ("security", "test", "cache")\n\n'
        "Args:\n"
        "    pattern:     Regex or plain text\n"
        "    file_glob:   Limit to matching files (e.g. '*.ts', '*.py')\n"
        "    max_results: Max matches (default 15)"
    )

    @tool(description=_search_code_desc)
    def search_code(
        pattern:     str,
        file_glob:   str = "*",
        max_results: int = 15,
    ) -> str:
        if counts.get("search_code", 0) >= search_budget:
            return _budget_exhausted("search_code", search_budget)

        counts["search_code"] = counts.get("search_code", 0) + 1
        remaining = search_budget - counts["search_code"]

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        matches = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and (
                    not d.startswith(".") or d in _ALLOW_HIDDEN
                )
            ]
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
                            matches.append(f"{rel}:{i}  {line.strip()[:100]}")
                            if len(matches) >= max_results:
                                break
                except Exception:
                    continue
                if len(matches) >= max_results:
                    break

        result = (
            f"No matches for '{pattern}'"
            if not matches else
            f"{len(matches)} matches for '{pattern}':\n\n" + "\n".join(matches)
        )
        return result + f"\n\n[search_code: {remaining}/{search_budget} calls remaining]"

    # ── Exploration tool 7: record_finding ────────────────────────────────────
    _record_finding_desc = (
        f"Record a finding. Budget: {finding_budget} calls total.\n\n"
        "QUALITY REQUIREMENT — every finding needs all four elements:\n"
        "  WHAT:        What exactly did you find? Name the actual file/component.\n"
        "  WHY:         Why does this matter architecturally?\n"
        "  CONSEQUENCE: What breaks or cannot scale if unaddressed?\n"
        "  HOW:         Specific fix — name the exact AWS service or pattern.\n\n"
        "Findings with fewer than 3 sentences are REJECTED.\n"
        "Record IMMEDIATELY after each read_file or search_code result.\n\n"
        "category: architecture | component | data_flow | api_endpoint |\n"
        "          security | tech_stack | design_pattern | code_quality |\n"
        "          improvement | entry_point | dependency | database_model |\n"
        "          testing | deployment | file_detail |\n"
        "          architecture_gap | cloud_readiness | modernization_target |\n"
        "          security_risk"
    )

    @tool(description=_record_finding_desc)
    def record_finding(
        category:   str,
        title:      str,
        detail:     str,
        files:      Optional[list] = None,
        confidence: str = "high",
    ) -> str:
        # Budget enforcement
        if counts.get("record_finding", 0) >= finding_budget:
            return _budget_exhausted("record_finding", finding_budget)

        # Quality gate — reject thin findings
        sentences = [s.strip() for s in re.split(r"[.!?]+", detail) if len(s.strip()) > 20]
        if len(sentences) < 3:
            return (
                f"REJECTED: '{title}' — detail is too thin ({len(sentences)} sentence(s)).\n"
                f"Rewrite with all four elements:\n"
                f"  WHAT: what exactly did you find (name the file)?\n"
                f"  WHY: why does this matter architecturally?\n"
                f"  CONSEQUENCE: what breaks if not fixed?\n"
                f"  HOW: specific fix with AWS service or design pattern name."
            )

        counts["record_finding"] = counts.get("record_finding", 0) + 1
        remaining = finding_budget - counts["record_finding"]

        state_accumulator.setdefault("findings", []).append({
            "category":   category,
            "title":      title,
            "detail":     detail.strip(),
            "files":      files or [],
            "confidence": confidence,
        })

        total = len(state_accumulator["findings"])
        return (
            f"✓ Finding #{total} [{category}]: {title}\n"
            f"[record_finding: {remaining}/{finding_budget} calls remaining]"
        )

    # ── Pipeline tool 8: generate_diagrams_from_graph ─────────────────────────
    @tool
    def generate_diagrams_from_graph() -> str:
        """
        Generate architecture, flow, and component diagrams from the real graph.
        Call ONCE, AFTER analyze_graph.

        Diagrams are deterministic — same graph always produces the same result.
        NEVER write Mermaid manually. Only call this once.
        """
        G = state_accumulator.get("graph")
        if G is None:
            return "ERROR: Call extract_ast_graph → cluster_communities → analyze_graph first."

        try:
            from codegrapher.output.mermaid_converter import generate_all_diagrams
            diagrams = generate_all_diagrams(state_accumulator)
            state_accumulator["diagrams"] = diagrams

            return (
                f"✓ {len(diagrams)} diagrams generated:\n"
                + "\n".join(f"  [{d['diagram_type']}] {d['description'][:70]}" for d in diagrams)
                + f"\n\nNow use your remaining exploration budget:\n"
                f"  read_file:    {file_budget - counts.get('read_file',0)}/{file_budget} remaining\n"
                f"  search_code:  {search_budget - counts.get('search_code',0)}/{search_budget} remaining\n"
                f"Spend it on the god nodes from analyze_graph."
            )
        except Exception as e:
            return f"ERROR: {e}"

    # ── Final tool 9: finish_analysis ─────────────────────────────────────────
    @tool
    def finish_analysis(
        summary:                    str,
        purpose:                    str,
        architecture_style:         str,
        tech_stack:                 list,
        key_components:             list,
        data_flow:                  list,
        api_endpoints:              list,
        database_models:            list,
        security_notes:             list,
        improvement_suggestions:    list,
        testing_approach:           str  = "",
        deployment_info:            str  = "",
        code_quality_notes:         Optional[list] = None,
        modernization_urgency:      str  = "",
        target_architecture:        str  = "",
        re_architecture_priorities: Optional[list] = None,
    ) -> str:
        """
        Complete the analysis. Call when you have genuine understanding.
        Call earlier if your exploration budget is exhausted.

        Minimum requirements before calling:
          ✓ All pipeline tools have run (graph, communities, diagrams)
          ✓ At least 5 findings recorded
          ✓ summary references specific files you actually read
          ✓ key_components lists actual components you investigated

        summary must be SPECIFIC — reference actual filenames and components.
        Generic descriptions that could apply to any project are not acceptable.
        """
        # Completeness gate
        findings_count = len(state_accumulator.get("findings", []))
        missing = []
        if state_accumulator.get("graph") is None:
            missing.append("graph not built (run extract_ast_graph → cluster_communities)")
        if not state_accumulator.get("diagrams"):
            missing.append("diagrams not generated (call generate_diagrams_from_graph)")
        if findings_count < 5:
            missing.append(f"only {findings_count} findings — need at least 5")
        if not summary or len(summary.split()) < 20:
            missing.append("summary too short — write at least 3 specific sentences")
        if not key_components:
            missing.append("key_components is empty — list actual components")

        if missing:
            return (
                "CANNOT FINISH — missing:\n"
                + "\n".join(f"  • {m}" for m in missing)
            )

        state_accumulator["finished"]    = True
        state_accumulator["finish_data"] = {
            "summary":                   summary.strip(),
            "purpose":                   purpose.strip(),
            "architecture_style":        architecture_style,
            "tech_stack":                tech_stack,
            "key_components":            key_components,
            "data_flow":                 data_flow,
            "api_endpoints":             api_endpoints,
            "database_models":           database_models,
            "security_notes":            security_notes,
            "improvement_suggestions":   improvement_suggestions,
            "testing_approach":          testing_approach,
            "deployment_info":           deployment_info,
            "code_quality_notes":        code_quality_notes or [],
            "modernization_urgency":     modernization_urgency,
            "target_architecture":       target_architecture,
            "re_architecture_priorities": re_architecture_priorities or [],
        }

        total_calls = sum(counts.values()) + 5  # +5 pipeline tools
        return (
            f"✓ Analysis complete\n"
            f"  Findings    : {findings_count}\n"
            f"  Components  : {len(key_components)}\n"
            f"  Total calls : ~{total_calls}"
        )

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