"""
Tool definitions for the agentic loop.

The agent decides WHICH tools to call, in WHAT ORDER, and WHEN to stop.
Each tool is a concrete action the agent can take against the repository.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Tool result ───────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: str
    data: dict = field(default_factory=dict)


# ── Tool registry ─────────────────────────────────────────────────────────────

# JSON schema definitions sent to the LLM so it knows what tools exist
TOOL_SCHEMAS = [
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories at a given path in the repository. "
            "Use this first to understand the repo structure before deciding which files to read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the repository (use '.' for root)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "If true, list all files recursively. Default false.",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full content of a specific file in the repository. "
            "Use this to deeply understand a file's code, classes, functions, and logic. "
            "Read files that seem most important based on your exploration so far."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the repository",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Max lines to return (default 300, max 600)",
                    "default": 300,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a pattern across all files in the repository. "
            "Use this to find where specific classes, functions, routes, or patterns are defined or used. "
            "Useful for tracing dependencies and understanding cross-file relationships."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.ts'). Default: all files.",
                    "default": "*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matching lines to return (default 30)",
                    "default": 30,
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_multiple_files",
        "description": (
            "Read several files at once for comparison or when you need context from multiple related files. "
            "More efficient than calling read_file repeatedly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative file paths to read",
                },
                "max_lines_each": {
                    "type": "integer",
                    "description": "Max lines per file (default 150)",
                    "default": 150,
                },
            },
            "required": ["paths"],
        },
    },
    {
        "name": "analyze_imports",
        "description": (
            "Analyze import/dependency relationships in a file or across the repo. "
            "Shows what each module depends on, helping map the dependency graph."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path to analyze imports for",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_file_stats",
        "description": (
            "Get statistics about the repository: file counts by language, "
            "largest files, recently modified files, directory sizes. "
            "Use this early to understand the repo's shape and scale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Root path to analyze (use '.' for whole repo)",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
    {
        "name": "record_finding",
        "description": (
            "Record an important finding or insight about the codebase. "
            "Use this to save your discoveries as you explore — "
            "these findings will be compiled into the final report. "
            "Call this whenever you discover something significant: "
            "an architectural pattern, a key component, a security concern, a design decision, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "architecture",
                        "component",
                        "data_flow",
                        "api_endpoint",
                        "security",
                        "tech_stack",
                        "design_pattern",
                        "code_quality",
                        "improvement",
                        "entry_point",
                        "dependency",
                        "database_model",
                        "testing",
                        "deployment",
                        "file_detail",
                    ],
                    "description": "Category of the finding",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for this finding",
                },
                "detail": {
                    "type": "string",
                    "description": "Detailed description of what you found",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relevant file paths for this finding",
                    "default": [],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How confident you are in this finding",
                    "default": "high",
                },
            },
            "required": ["category", "title", "detail"],
        },
    },
    {
        "name": "generate_diagram",
        "description": (
            "Generate a Mermaid diagram based on your current understanding. "
            "Call this when you have enough information to accurately represent "
            "the architecture, data flow, or component structure. "
            "You decide when you have sufficient context — don't generate diagrams too early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "diagram_type": {
                    "type": "string",
                    "enum": ["architecture", "flow", "components"],
                    "description": "Type of diagram to generate",
                },
                "mermaid_code": {
                    "type": "string",
                    "description": "The Mermaid diagram syntax",
                },
                "description": {
                    "type": "string",
                    "description": "What this diagram shows",
                },
            },
            "required": ["diagram_type", "mermaid_code", "description"],
        },
    },
    {
        "name": "finish_analysis",
        "description": (
            "Signal that you have completed the analysis and are ready to generate the report. "
            "Only call this when you have: explored the repo structure, read the key files, "
            "recorded your findings, and generated diagrams. "
            "Include a final executive summary in your call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "3-5 sentence executive summary of the repository",
                },
                "purpose": {
                    "type": "string",
                    "description": "One sentence describing what this repo does",
                },
                "architecture_style": {
                    "type": "string",
                    "description": "Architecture style (e.g. MVC, microservices, CLI tool, library)",
                },
            },
            "required": ["summary", "purpose", "architecture_style"],
        },
    },
]


# ── Tool executor ─────────────────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "venv", ".venv", "env", "dist", "build",
    ".next", ".nuxt", "coverage", ".tox", ".idea", ".vscode",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
    ".zip", ".tar", ".gz", ".whl", ".egg",
    ".min.js", ".map",
}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".cpp", ".c", ".h", ".cs", ".kt", ".scala", ".php",
    ".swift", ".sh", ".bash",
}


def _is_skippable(path: Path) -> bool:
    return (
        path.name in SKIP_DIRS
        or path.name.startswith(".")
        or any(path.name.endswith(ext) for ext in SKIP_EXTENSIONS)
    )


class ToolExecutor:
    """Executes agent tool calls against a real repository."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.findings: list[dict] = []
        self.diagrams: dict[str, dict] = {}
        self.finished = False
        self.finish_data: dict = {}
        self._tool_call_count = 0

    def _resolve(self, rel_path: str) -> Path:
        """Safely resolve a relative path within repo root."""
        path = (self.repo_root / rel_path).resolve()
        if not str(path).startswith(str(self.repo_root)):
            raise ValueError(f"Path escapes repository root: {rel_path}")
        return path

    def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        self._tool_call_count += 1
        try:
            fn = getattr(self, f"_tool_{tool_name}", None)
            if fn is None:
                return ToolResult(tool_name, False, f"Unknown tool: {tool_name}")
            return fn(**tool_input)
        except Exception as e:
            return ToolResult(tool_name, False, f"Tool error: {e}")

    def _tool_list_directory(self, path: str = ".", recursive: bool = False) -> ToolResult:
        try:
            resolved = self._resolve(path)
        except ValueError as e:
            return ToolResult("list_directory", False, str(e))

        if not resolved.exists():
            return ToolResult("list_directory", False, f"Path not found: {path}")

        lines = []
        if recursive:
            for root, dirs, files in os.walk(resolved):
                dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS and not d.startswith(".")]
                rel_root = Path(root).relative_to(self.repo_root)
                for f in sorted(files):
                    fp = Path(root) / f
                    if not _is_skippable(fp):
                        lines.append(str(rel_root / f))
                if len(lines) > 150:
                    lines.append(f"... (truncated, {len(lines)} items shown)")
                    break
        else:
            try:
                entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                return ToolResult("list_directory", False, "Permission denied")
            for entry in entries:
                if _is_skippable(entry):
                    continue
                rel = entry.relative_to(self.repo_root)
                suffix = "/" if entry.is_dir() else f"  ({entry.stat().st_size:,} bytes)"
                lines.append(f"{rel}{suffix}")

        output = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult("list_directory", True, output, {"path": path, "count": len(lines)})

    def _tool_read_file(self, path: str, max_lines: int = 300) -> ToolResult:
        max_lines = min(max_lines, 600)
        try:
            resolved = self._resolve(path)
        except ValueError as e:
            return ToolResult("read_file", False, str(e))

        if not resolved.exists():
            return ToolResult("read_file", False, f"File not found: {path}")
        if not resolved.is_file():
            return ToolResult("read_file", False, f"Not a file: {path}")

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as e:
            return ToolResult("read_file", False, str(e))

        lines = content.split("\n")
        total = len(lines)
        if total > max_lines:
            shown = lines[:max_lines]
            shown.append(f"\n... [{total - max_lines} more lines, use max_lines parameter to see more]")
            content = "\n".join(shown)

        return ToolResult(
            "read_file", True,
            f"=== {path} ({total} lines) ===\n{content}",
            {"path": path, "total_lines": total, "size": resolved.stat().st_size},
        )

    def _tool_read_multiple_files(self, paths: list[str], max_lines_each: int = 150) -> ToolResult:
        parts = []
        for path in paths[:10]:  # cap at 10
            result = self._tool_read_file(path, max_lines=max_lines_each)
            parts.append(result.output if result.success else f"=== {path} ===\nERROR: {result.output}")
        return ToolResult("read_multiple_files", True, "\n\n".join(parts))

    def _tool_search_code(self, pattern: str, file_pattern: str = "*", max_results: int = 30) -> ToolResult:
        matches = []
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        for root, dirs, files in os.walk(self.repo_root):
            dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                if not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fp = Path(root) / fname
                if _is_skippable(fp) or fp.suffix in SKIP_EXTENSIONS:
                    continue
                try:
                    rel = fp.relative_to(self.repo_root)
                    content = fp.read_text(encoding="utf-8", errors="replace")
                    for i, line in enumerate(content.split("\n"), 1):
                        if regex.search(line):
                            matches.append(f"{rel}:{i}:  {line.strip()}")
                            if len(matches) >= max_results:
                                break
                except Exception:
                    continue
                if len(matches) >= max_results:
                    break

        if not matches:
            return ToolResult("search_code", True, f"No matches found for: {pattern}")

        output = f"Found {len(matches)} matches for '{pattern}':\n\n" + "\n".join(matches)
        return ToolResult("search_code", True, output, {"match_count": len(matches)})

    def _tool_analyze_imports(self, path: str) -> ToolResult:
        try:
            resolved = self._resolve(path)
        except ValueError as e:
            return ToolResult("analyze_imports", False, str(e))

        files_to_check = []
        if resolved.is_file():
            files_to_check = [resolved]
        else:
            for root, dirs, files in os.walk(resolved):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    fp = Path(root) / f
                    if fp.suffix in CODE_EXTENSIONS:
                        files_to_check.append(fp)

        import_map = {}
        py_import_re = re.compile(r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.,\s]+))", re.MULTILINE)
        js_import_re = re.compile(r"""(?:import|require)\s*(?:\{[^}]*\}|[\w*]+)?\s*(?:from\s*)?['"]([^'"]+)['"]""")

        for fp in files_to_check[:30]:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                rel = str(fp.relative_to(self.repo_root))
                imports = []
                if fp.suffix == ".py":
                    for m in py_import_re.finditer(content):
                        imports.append(m.group(1) or m.group(2))
                elif fp.suffix in {".js", ".ts", ".tsx", ".jsx"}:
                    for m in js_import_re.finditer(content):
                        imports.append(m.group(1))
                if imports:
                    import_map[rel] = [imp.strip() for imp in imports if imp]
            except Exception:
                continue

        if not import_map:
            return ToolResult("analyze_imports", True, "No imports found")

        lines = []
        for file, imports in import_map.items():
            lines.append(f"{file}:")
            for imp in imports[:10]:
                lines.append(f"  → {imp}")

        return ToolResult("analyze_imports", True, "\n".join(lines), {"files_analyzed": len(import_map)})

    def _tool_get_file_stats(self, path: str = ".") -> ToolResult:
        try:
            resolved = self._resolve(path)
        except ValueError as e:
            return ToolResult("get_file_stats", False, str(e))

        ext_counts: dict[str, int] = {}
        ext_lines: dict[str, int] = {}
        large_files: list[tuple[int, str]] = []
        total_files = 0
        total_lines = 0

        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                fp = Path(root) / fname
                if _is_skippable(fp):
                    continue
                ext = fp.suffix.lower() or fname.lower()
                size = fp.stat().st_size
                rel = str(fp.relative_to(self.repo_root))
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                total_files += 1
                large_files.append((size, rel))
                if ext in CODE_EXTENSIONS:
                    try:
                        lc = fp.read_text(encoding="utf-8", errors="replace").count("\n")
                        ext_lines[ext] = ext_lines.get(ext, 0) + lc
                        total_lines += lc
                    except Exception:
                        pass

        large_files.sort(reverse=True)
        lines = [
            f"Total files: {total_files}",
            f"Total lines of code: {total_lines:,}",
            "",
            "Files by type:",
        ]
        for ext, count in sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:15]:
            loc = ext_lines.get(ext, 0)
            loc_str = f"  ({loc:,} lines)" if loc else ""
            lines.append(f"  {ext or 'no ext'}: {count} files{loc_str}")

        lines += ["", "Largest files:"]
        for size, name in large_files[:10]:
            lines.append(f"  {name}: {size:,} bytes")

        return ToolResult("get_file_stats", True, "\n".join(lines))

    def _tool_record_finding(
        self,
        category: str,
        title: str,
        detail: str,
        files: list[str] | None = None,
        confidence: str = "high",
    ) -> ToolResult:
        finding = {
            "category": category,
            "title": title,
            "detail": detail,
            "files": files or [],
            "confidence": confidence,
        }
        self.findings.append(finding)
        return ToolResult(
            "record_finding", True,
            f"✓ Finding recorded: [{category}] {title}",
            finding,
        )

    def _tool_generate_diagram(
        self,
        diagram_type: str,
        mermaid_code: str,
        description: str,
    ) -> ToolResult:
        self.diagrams[diagram_type] = {
            "mermaid_code": mermaid_code,
            "description": description,
        }
        return ToolResult(
            "generate_diagram", True,
            f"✓ Diagram recorded: {diagram_type} — {description}",
        )

    def _tool_finish_analysis(
        self,
        summary: str,
        purpose: str,
        architecture_style: str,
    ) -> ToolResult:
        self.finished = True
        self.finish_data = {
            "summary": summary,
            "purpose": purpose,
            "architecture_style": architecture_style,
        }
        return ToolResult(
            "finish_analysis", True,
            f"✓ Analysis complete. Summary recorded.",
        )
