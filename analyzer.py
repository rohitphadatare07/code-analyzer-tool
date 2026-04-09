"""LLM-powered analysis - generates structured insights about the repository."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from repoanalyzer.providers import LLMProvider
from repoanalyzer.scanner import RepoScan, FileInfo


SYSTEM_PROMPT = """You are an expert software architect and code analyst. 
Analyze code repositories and provide clear, accurate, detailed technical insights.
Always respond with valid JSON when asked. Be precise and technical."""


@dataclass
class AnalysisResult:
    summary: str = ""
    purpose: str = ""
    tech_stack: list[str] = field(default_factory=list)
    architecture_style: str = ""
    key_components: list[dict] = field(default_factory=list)
    data_flow: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    design_patterns: list[str] = field(default_factory=list)
    api_endpoints: list[dict] = field(default_factory=list)
    database_models: list[str] = field(default_factory=list)
    security_notes: list[str] = field(default_factory=list)
    testing_approach: str = ""
    deployment_info: str = ""
    code_quality_notes: list[str] = field(default_factory=list)
    improvement_suggestions: list[str] = field(default_factory=list)
    file_details: list[dict] = field(default_factory=list)
    architecture_diagram_mermaid: str = ""
    flow_diagram_mermaid: str = ""
    component_diagram_mermaid: str = ""


def _chunk_files(files: list[FileInfo], max_chars: int = 80_000) -> list[list[FileInfo]]:
    """Split files into chunks that fit within context limits."""
    chunks = []
    current_chunk = []
    current_size = 0

    for f in files:
        size = len(f.content) + len(f.relative_path) + 100
        if current_size + size > max_chars and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(f)
        current_size += size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _files_to_text(files: list[FileInfo], max_content: int = 3000) -> str:
    """Format files as a text block for the LLM."""
    parts = []
    for f in files:
        content = f.content
        if len(content) > max_content:
            content = content[:max_content] + f"\n... [truncated, {len(f.content)} total chars]"
        parts.append(f"### {f.relative_path} ({f.language})\n```\n{content}\n```")
    return "\n\n".join(parts)


def _safe_json(text: str) -> dict:
    """Parse JSON from LLM response, stripping markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return {}


def analyze_overview(llm: LLMProvider, scan: RepoScan) -> dict:
    """Generate high-level overview of the repository."""
    print("  📋 Analyzing repository overview...")

    # Prepare key files for overview
    key_files = [f for f in scan.files if any(
        kw in f.path.stem.lower()
        for kw in ["readme", "main", "app", "index", "config", "setup", "__init__", "package", "cargo", "go.mod"]
    )][:15]

    if not key_files:
        key_files = scan.files[:15]

    file_list = "\n".join(f"  - {f.relative_path} ({f.language})" for f in scan.files[:50])
    key_content = _files_to_text(key_files, max_content=2000)

    user_prompt = f"""Analyze this code repository and return a JSON object with these exact keys:

Repository structure:
{scan.directory_tree[:3000]}

File inventory ({len(scan.files)} files, {scan.total_lines:,} lines):
{file_list}

Key files content:
{key_content}

Return JSON with these exact keys:
{{
  "purpose": "one sentence describing what this repo does",
  "summary": "3-5 sentence technical summary",
  "architecture_style": "e.g. MVC, microservices, monolith, library, CLI tool, etc.",
  "tech_stack": ["list", "of", "technologies", "frameworks", "languages"],
  "entry_points": ["main.py", "src/index.ts", etc],
  "dependencies": ["list of major external dependencies"],
  "design_patterns": ["patterns used: e.g. Factory, Repository, Observer"],
  "testing_approach": "description of testing strategy",
  "deployment_info": "how this is deployed/run",
  "security_notes": ["security considerations found"],
  "code_quality_notes": ["observations about code quality"],
  "improvement_suggestions": ["concrete improvement ideas"]
}}"""

    response = llm.complete(SYSTEM_PROMPT, user_prompt, max_tokens=3000)
    return _safe_json(response)


def analyze_components(llm: LLMProvider, scan: RepoScan) -> dict:
    """Analyze key components, modules, and their relationships."""
    print("  🧩 Analyzing components and architecture...")

    # Group files by directory
    dirs: dict[str, list[FileInfo]] = {}
    for f in scan.files:
        parts = f.relative_path.split("/")
        top = parts[0] if len(parts) > 1 else "root"
        dirs.setdefault(top, []).append(f)

    dir_summary = "\n".join(
        f"  {d}/: {len(fs)} files ({', '.join(set(f.language for f in fs[:3]))})"
        for d, fs in sorted(dirs.items())
    )

    # Use first chunk of files for content
    chunks = _chunk_files(scan.files[:60], max_chars=70_000)
    first_chunk_text = _files_to_text(chunks[0] if chunks else [], max_content=1500)

    user_prompt = f"""Analyze these code files and identify key components.

Directory breakdown:
{dir_summary}

File contents:
{first_chunk_text}

Return JSON:
{{
  "key_components": [
    {{
      "name": "ComponentName",
      "description": "what it does",
      "files": ["file1.py", "file2.py"],
      "responsibilities": ["responsibility 1", "responsibility 2"],
      "depends_on": ["OtherComponent"]
    }}
  ],
  "data_flow": [
    "Step 1: User sends request to X",
    "Step 2: X calls Y",
    "Step 3: Y queries database",
    "Step 4: Results returned"
  ],
  "api_endpoints": [
    {{"method": "GET", "path": "/api/v1/users", "description": "List users"}}
  ],
  "database_models": ["User", "Order", "Product"]
}}"""

    response = llm.complete(SYSTEM_PROMPT, user_prompt, max_tokens=3000)
    return _safe_json(response)


def analyze_file_details(llm: LLMProvider, scan: RepoScan) -> list[dict]:
    """Analyze individual important files in detail."""
    print("  📄 Analyzing individual files...")

    # Pick most important files to detail
    important_files = [
        f for f in scan.files
        if f.language in ("Python", "TypeScript", "JavaScript", "Go", "Rust", "Java")
        and len(f.content) > 200
    ][:30]

    if not important_files:
        important_files = scan.files[:20]

    # Process in batches
    all_details = []
    batch_size = 8

    for i in range(0, len(important_files), batch_size):
        batch = important_files[i:i + batch_size]
        batch_text = _files_to_text(batch, max_content=1200)

        user_prompt = f"""For each file, provide a brief analysis. Return JSON array:
[
  {{
    "file": "relative/path.py",
    "purpose": "one sentence",
    "key_classes": ["ClassName"],
    "key_functions": ["function_name"],
    "complexity": "low|medium|high",
    "notes": "important observations"
  }}
]

Files:
{batch_text}"""

        response = llm.complete(SYSTEM_PROMPT, user_prompt, max_tokens=2000)
        parsed = _safe_json(response)
        if isinstance(parsed, list):
            all_details.extend(parsed)
        elif isinstance(parsed, dict) and "files" in parsed:
            all_details.extend(parsed["files"])

    return all_details


def generate_diagrams(llm: LLMProvider, scan: RepoScan, overview: dict, components: dict) -> dict:
    """Generate Mermaid diagram code for architecture, flow, and component views."""
    print("  📊 Generating diagrams...")

    tech_stack = ", ".join(overview.get("tech_stack", [])[:5])
    arch_style = overview.get("architecture_style", "unknown")
    component_names = [c.get("name", "") for c in components.get("key_components", [])[:8]]
    data_flow = components.get("data_flow", [])

    user_prompt = f"""Generate 3 Mermaid diagrams for this codebase.

Architecture: {arch_style}
Tech stack: {tech_stack}
Components: {', '.join(component_names)}
Data flow steps: {json.dumps(data_flow[:6])}

Return JSON with exactly these 3 keys containing valid Mermaid diagram syntax:

{{
  "architecture_diagram": "graph TD\\n  A[Client] --> B[API]\\n  B --> C[Database]\\n  ...",
  "flow_diagram": "sequenceDiagram\\n  participant User\\n  ...",
  "component_diagram": "graph LR\\n  subgraph Core\\n  A[ComponentA]\\n  end\\n  ..."
}}

Rules:
- architecture_diagram: use graph TD or LR, show system components and their connections
- flow_diagram: use sequenceDiagram, show main request/data flow
- component_diagram: use graph LR with subgraphs, show module dependencies
- Keep diagrams focused and readable (max 15 nodes each)
- Use descriptive node labels
- No special characters in node IDs (use only alphanumeric and underscore)"""

    response = llm.complete(SYSTEM_PROMPT, user_prompt, max_tokens=2000)
    return _safe_json(response)


def run_analysis(llm: LLMProvider, scan: RepoScan) -> AnalysisResult:
    """Run full analysis pipeline and return structured results."""
    result = AnalysisResult()

    # Step 1: Overview
    overview = analyze_overview(llm, scan)
    result.summary = overview.get("summary", "")
    result.purpose = overview.get("purpose", "")
    result.tech_stack = overview.get("tech_stack", [])
    result.architecture_style = overview.get("architecture_style", "")
    result.entry_points = overview.get("entry_points", [])
    result.dependencies = overview.get("dependencies", [])
    result.design_patterns = overview.get("design_patterns", [])
    result.testing_approach = overview.get("testing_approach", "")
    result.deployment_info = overview.get("deployment_info", "")
    result.security_notes = overview.get("security_notes", [])
    result.code_quality_notes = overview.get("code_quality_notes", [])
    result.improvement_suggestions = overview.get("improvement_suggestions", [])

    # Step 2: Components
    components = analyze_components(llm, scan)
    result.key_components = components.get("key_components", [])
    result.data_flow = components.get("data_flow", [])
    result.api_endpoints = components.get("api_endpoints", [])
    result.database_models = components.get("database_models", [])

    # Step 3: File details
    result.file_details = analyze_file_details(llm, scan)

    # Step 4: Diagrams
    diagrams = generate_diagrams(llm, scan, overview, components)
    result.architecture_diagram_mermaid = diagrams.get("architecture_diagram", "")
    result.flow_diagram_mermaid = diagrams.get("flow_diagram", "")
    result.component_diagram_mermaid = diagrams.get("component_diagram", "")

    return result
