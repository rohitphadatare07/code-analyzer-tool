"""Repository scanner - walks a directory tree and collects code files."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Extensions to analyze
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
    ".cpp", ".c", ".h", ".hpp", ".cs", ".kt", ".scala", ".php", ".swift",
    ".sh", ".bash", ".zsh", ".fish",
}
CONFIG_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env.example",
    ".dockerfile", "dockerfile",
}
DOC_EXTENSIONS = {".md", ".rst", ".txt"}

ALL_EXTENSIONS = CODE_EXTENSIONS | CONFIG_EXTENSIONS | DOC_EXTENSIONS

# Always skip these
DEFAULT_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "venv", ".venv", "env", ".env", "dist", "build",
    ".next", ".nuxt", "coverage", ".coverage", "htmlcov", ".tox",
    "eggs", ".eggs", "*.egg-info", ".idea", ".vscode", "__snapshots__",
}

DEFAULT_EXCLUDE_PATTERNS = {
    "*.min.js", "*.min.css", "*.map", "*.lock", "*.sum",
    "package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dylib",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.svg",
    "*.pdf", "*.zip", "*.tar.*", "*.whl", "*.egg",
}


@dataclass
class FileInfo:
    path: Path
    relative_path: str
    extension: str
    size: int
    content: str
    language: str


@dataclass
class RepoScan:
    root: Path
    files: list[FileInfo] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    total_lines: int = 0
    languages: dict[str, int] = field(default_factory=dict)  # lang -> file count
    directory_tree: str = ""


def _detect_language(ext: str, filename: str) -> str:
    mapping = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript/React", ".jsx": "JavaScript/React",
        ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
        ".cpp": "C++", ".c": "C", ".h": "C/C++ Header", ".hpp": "C++ Header",
        ".cs": "C#", ".kt": "Kotlin", ".scala": "Scala", ".php": "PHP",
        ".swift": "Swift", ".sh": "Shell", ".bash": "Bash",
        ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML", ".md": "Markdown", ".rst": "reStructuredText",
    }
    lower = filename.lower()
    if lower in ("dockerfile", "makefile", "gemfile", "rakefile"):
        return lower.capitalize()
    return mapping.get(ext, ext.lstrip(".").upper() or "Text")


def _build_tree(root: Path, exclude_dirs: set[str], max_depth: int = 4) -> str:
    """Build a text directory tree."""
    lines = [f"{root.name}/"]

    def _walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        entries = [e for e in entries if e.name not in exclude_dirs and not e.name.startswith(".")]
        for i, entry in enumerate(entries[:30]):  # cap at 30 per dir
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and depth < max_depth:
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


def scan_repository(
    root: Path,
    exclude_patterns: list[str] | None = None,
    max_file_size: int = 100_000,
    max_files: int = 200,
    verbose: bool = False,
) -> RepoScan:
    """Walk the repository and collect file contents."""
    root = root.resolve()
    scan = RepoScan(root=root)
    extra_patterns = set(exclude_patterns or [])
    all_exclude = DEFAULT_EXCLUDE_PATTERNS | extra_patterns

    # Build directory tree first
    scan.directory_tree = _build_tree(root, DEFAULT_EXCLUDE_DIRS)

    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in DEFAULT_EXCLUDE_DIRS
            and not d.startswith(".")
            and not any(fnmatch.fnmatch(d, pat) for pat in extra_patterns)
        ]

        for filename in filenames:
            if file_count >= max_files:
                scan.skipped_files.append(f"[limit reached: {max_files} files]")
                break

            # Skip by pattern
            if any(fnmatch.fnmatch(filename, pat) for pat in all_exclude):
                continue

            filepath = Path(dirpath) / filename
            ext = filepath.suffix.lower()

            # Check filename (e.g. Dockerfile, Makefile)
            fname_lower = filename.lower()
            is_special = fname_lower in ("dockerfile", "makefile", "gemfile", "rakefile", "procfile")

            if ext not in ALL_EXTENSIONS and not is_special:
                continue

            try:
                size = filepath.stat().st_size
            except OSError:
                continue

            if size > max_file_size:
                rel = str(filepath.relative_to(root))
                scan.skipped_files.append(f"{rel} (too large: {size:,} bytes)")
                continue

            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError) as e:
                scan.skipped_files.append(f"{filepath.relative_to(root)} ({e})")
                continue

            rel_path = str(filepath.relative_to(root))
            lang = _detect_language(ext, filename)
            lines = content.count("\n")
            scan.total_lines += lines
            scan.languages[lang] = scan.languages.get(lang, 0) + 1

            scan.files.append(FileInfo(
                path=filepath,
                relative_path=rel_path,
                extension=ext,
                size=size,
                content=content,
                language=lang,
            ))
            file_count += 1

            if verbose:
                print(f"  + {rel_path} ({lang}, {lines} lines)")

    # Sort: important files first
    priority_files = {"readme", "main", "app", "index", "setup", "config", "__init__"}
    scan.files.sort(key=lambda f: (
        0 if any(p in f.path.stem.lower() for p in priority_files) else 1,
        f.relative_path,
    ))

    return scan


def format_scan_summary(scan: RepoScan) -> str:
    """Return a human-readable summary of the scan."""
    langs = sorted(scan.languages.items(), key=lambda x: x[1], reverse=True)
    lang_str = ", ".join(f"{lang} ({n})" for lang, n in langs[:8])
    lines = [
        f"Repository: {scan.root}",
        f"Files analyzed: {len(scan.files)}",
        f"Total lines: {scan.total_lines:,}",
        f"Languages: {lang_str}",
    ]
    if scan.skipped_files:
        lines.append(f"Skipped: {len(scan.skipped_files)} files")
    return "\n".join(lines)
