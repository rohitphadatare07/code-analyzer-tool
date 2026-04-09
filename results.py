"""
Converts agent findings into a structured AnalysisResult for the PDF generator.

The agent records raw findings; this module organises them into the
structured form needed to generate the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from repoanalyzer_v2.tools import ToolExecutor
from repoanalyzer_v2.agent import AgentRun


@dataclass
class AnalysisResult:
    # From finish_analysis call
    summary: str = ""
    purpose: str = ""
    architecture_style: str = ""

    # Compiled from record_finding calls
    tech_stack: list[str] = field(default_factory=list)
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

    # From generate_diagram calls
    architecture_diagram_mermaid: str = ""
    architecture_diagram_description: str = ""
    flow_diagram_mermaid: str = ""
    flow_diagram_description: str = ""
    component_diagram_mermaid: str = ""
    component_diagram_description: str = ""

    # Agent run metadata
    total_tool_calls: int = 0
    total_steps: int = 0
    elapsed_seconds: float = 0.0
    provider_name: str = ""


def compile_results(executor: ToolExecutor, run: AgentRun, provider_name: str) -> AnalysisResult:
    """Turn raw agent findings + diagrams into a structured AnalysisResult."""
    result = AnalysisResult()

    # From finish_analysis
    result.summary = executor.finish_data.get("summary", "")
    result.purpose = executor.finish_data.get("purpose", "")
    result.architecture_style = executor.finish_data.get("architecture_style", "")

    # From diagrams
    arch = executor.diagrams.get("architecture", {})
    result.architecture_diagram_mermaid = arch.get("mermaid_code", "")
    result.architecture_diagram_description = arch.get("description", "")

    flow = executor.diagrams.get("flow", {})
    result.flow_diagram_mermaid = flow.get("mermaid_code", "")
    result.flow_diagram_description = flow.get("description", "")

    comp = executor.diagrams.get("components", {})
    result.component_diagram_mermaid = comp.get("mermaid_code", "")
    result.component_diagram_description = comp.get("description", "")

    # Group findings by category
    for finding in executor.findings:
        cat = finding["category"]
        title = finding["title"]
        detail = finding["detail"]
        files = finding.get("files", [])

        if cat == "tech_stack":
            result.tech_stack.append(title)

        elif cat == "component":
            result.key_components.append({
                "name": title,
                "description": detail,
                "files": files,
                "responsibilities": _extract_bullets(detail),
                "depends_on": [],
            })

        elif cat == "data_flow":
            # Split multi-step flows into individual steps
            steps = _extract_bullets(detail)
            if steps:
                result.data_flow.extend(steps)
            else:
                result.data_flow.append(detail)

        elif cat == "entry_point":
            result.entry_points.append(f"{title}: {detail}" if detail != title else title)

        elif cat == "dependency":
            result.dependencies.append(title)

        elif cat == "design_pattern":
            result.design_patterns.append(f"{title}: {detail}" if detail else title)

        elif cat == "api_endpoint":
            # Try to parse method/path from title like "GET /api/users"
            parts = title.split(" ", 1)
            if len(parts) == 2 and parts[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
                result.api_endpoints.append({
                    "method": parts[0].upper(),
                    "path": parts[1],
                    "description": detail,
                    "files": files,
                })
            else:
                result.api_endpoints.append({
                    "method": "",
                    "path": title,
                    "description": detail,
                    "files": files,
                })

        elif cat == "database_model":
            result.database_models.append(f"{title}: {detail}" if detail else title)

        elif cat == "security":
            result.security_notes.append(f"{title}: {detail}" if detail else title)

        elif cat == "testing":
            result.testing_approach = f"{title}: {detail}" if not result.testing_approach else result.testing_approach

        elif cat == "deployment":
            result.deployment_info = f"{title}: {detail}" if not result.deployment_info else result.deployment_info

        elif cat == "code_quality":
            result.code_quality_notes.append(f"{title}: {detail}" if detail else title)

        elif cat == "improvement":
            result.improvement_suggestions.append(f"{title}: {detail}" if detail else title)

        elif cat == "file_detail":
            result.file_details.append({
                "file": files[0] if files else title,
                "purpose": detail,
                "key_classes": [],
                "key_functions": [],
                "complexity": "medium",
                "notes": "",
            })

        elif cat == "architecture":
            # Architecture findings may contain additional context not captured in summary
            pass

    # Agent run stats
    result.total_tool_calls = run.total_tool_calls
    result.total_steps = len(run.steps)
    result.elapsed_seconds = run.elapsed_seconds
    result.provider_name = provider_name

    return result


def _extract_bullets(text: str) -> list[str]:
    """Try to extract a list from text that may use bullets, numbers, or newlines."""
    import re
    lines = text.split("\n")
    results = []
    for line in lines:
        # Strip bullet chars
        line = re.sub(r"^[\s\-•*\d\.]+", "", line).strip()
        if line and len(line) > 5:
            results.append(line)
    return results if len(results) > 1 else []
