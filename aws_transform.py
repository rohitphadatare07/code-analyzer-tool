"""AWS Transform output loader and normalizer.

Takes an AWS Transform comprehensive-codebase-analysis output (either a
local directory or an s3:// path) and normalizes it into a stable internal
schema that the merge tool can consume.

# IMPORTANT: SCHEMA ASSUMPTIONS

AWS does NOT publish a file-level schema for comprehensive-codebase-analysis
output. This parser is built against documented categories from the blog post
(https://aws.amazon.com/blogs/migration-and-modernization/aws-transform-comprehensive-codebase-analysis-for-modernization/)
and the documented S3 path structure. When real output is available, the
matching logic in `_classify_file` and the section extractors should be
tightened.

What the blog post tells us:
  - Four output categories: technical debt, architecture, code analysis,
    project overview / executive summary
  - Hierarchy: root-level (exec summary + top debt), domain level
    (component clusters), component level (per-module detail)
  - S3 path: s3://atx-custom-output-{acct}/transformations/{job}/{ts}{conv}/
  - Behavior analysis is in early access (separate output)

What we DON'T know:
  - Exact filenames or formats (markdown vs. JSON vs. HTML)
  - Whether sections are in a single index file or split per file
  - Field names within JSON outputs

So this parser is deliberately lenient: it scans the directory tree, classifies
files by name pattern, parses JSON if it parses, falls back to markdown text,
and reports gaps clearly via `unparsed_files` and `missing_sections`.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ── Internal schema ───────────────────────────────────────────────────────────

@dataclass
class AWSTransformAnalysis:
    """Normalized view of an AWS Transform output.

    Every field is optional. The merge tool checks what's present and
    fills in CodeGrapher data for the rest.
    """
    source: str = ""                    # s3://... or local path
    job_name: str = ""                  # extracted from path if possible
    conversation_id: str = ""           # extracted from path if possible

    # Top-level narrative (Project Overview / Executive Summary)
    executive_summary: str = ""
    project_overview: str = ""
    purpose: str = ""
    architecture_style: str = ""

    # Technical debt findings (severity-ranked)
    technical_debt: list[dict] = field(default_factory=list)

    # Architecture documentation
    architecture_notes: str = ""
    architecture_strengths: list[str] = field(default_factory=list)
    architecture_weaknesses: list[str] = field(default_factory=list)

    # Code analysis metrics (file-level)
    code_metrics: list[dict] = field(default_factory=list)

    # Domain / component breakdown (the hierarchical bit)
    domains: list[dict] = field(default_factory=list)
    components: list[dict] = field(default_factory=list)

    # Tech stack and dependencies
    tech_stack: list[str] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)

    # Behavioral analysis (early access — may be absent)
    business_rules: list[dict] = field(default_factory=list)
    data_flow: list[str] = field(default_factory=list)
    api_endpoints: list[dict] = field(default_factory=list)
    database_models: list[str] = field(default_factory=list)

    # Improvement / modernization recommendations
    recommendations: list[str] = field(default_factory=list)

    # Diagnostics — what we saw vs. what we expected
    files_seen: list[str] = field(default_factory=list)
    unparsed_files: list[str] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    raw_documents: dict[str, Any] = field(default_factory=dict)


# ── Path resolution ───────────────────────────────────────────────────────────

def _is_s3_path(path: str) -> bool:
    return path.startswith("s3://")


def _materialize_s3(s3_path: str) -> Path:
    """Download an s3:// prefix to a local temp directory and return the path.

    Requires boto3 and AWS credentials. Lazily imports so users without
    AWS access aren't forced to install boto3.
    """
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "boto3 is required to read s3:// paths. Install with: pip install boto3"
        ) from e

    parsed = urlparse(s3_path)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3")
    tmp = Path(tempfile.mkdtemp(prefix="aws-transform-"))

    paginator = s3.get_paginator("list_objects_v2")
    n = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix):]
            if not rel:
                continue
            local = tmp / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local))
            n += 1

    if n == 0:
        raise FileNotFoundError(f"No objects found under {s3_path}")
    return tmp


def _resolve_aws_path(path: str) -> Path:
    """Return a local Path containing AWS Transform output.

    Accepts either:
      - s3://bucket/prefix/   (downloads via boto3)
      - /local/path           (used directly)
    """
    if _is_s3_path(path):
        return _materialize_s3(path)
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"AWS output path does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"AWS output path is not a directory: {p}")
    return p


# ── Path metadata extraction ──────────────────────────────────────────────────

# Documented S3 path:
#   s3://atx-custom-output-{acct}/transformations/{job-name}/{timestamp}{conv-id}/
# We try to extract job-name and conversation-id when those segments are present.

_PATH_JOB_RE = re.compile(r"transformations/([^/]+)/(\d{8,})([0-9a-f-]+)")


def _extract_path_metadata(source: str) -> tuple[str, str]:
    m = _PATH_JOB_RE.search(source)
    if not m:
        return ("", "")
    return (m.group(1), m.group(3))


# ── File classification ───────────────────────────────────────────────────────

# The blog post mentions section names like "executive summary",
# "technical debt report", "architecture", "code analysis", "project overview".
# We match against filenames flexibly. When we have real output, this list
# should tighten.

_SECTION_PATTERNS = {
    "executive_summary": [
        r"executive[-_]?summary",
        r"summary\.(?:json|md|html)$",
    ],
    "project_overview": [
        r"project[-_]?overview",
        r"overview\.(?:json|md|html)$",
    ],
    "technical_debt": [
        r"technical[-_]?debt",
        r"debt[-_]?report",
        r"debt\.(?:json|md|html)$",
    ],
    "architecture": [
        r"architecture",
        r"arch[-_]?docs?",
    ],
    "code_analysis": [
        r"code[-_]?analysis",
        r"code[-_]?metrics",
        r"complexity",
    ],
    "domains": [
        r"domains?",
        r"clusters?",
    ],
    "components": [
        r"components?",
        r"modules?",
    ],
    "behavior": [
        r"behavior",
        r"behavioral[-_]?analysis",
        r"business[-_]?rules?",
    ],
    "dependencies": [
        r"dependenc(?:y|ies)",
    ],
    "recommendations": [
        r"recommendations?",
        r"improvements?",
        r"migration[-_]?plan",
    ],
}


def _classify_file(name: str) -> str | None:
    """Return the section key this filename maps to, or None."""
    lower = name.lower()
    for section, patterns in _SECTION_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, lower):
                return section
    return None


# ── Per-section parsers ───────────────────────────────────────────────────────

def _safe_load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _parse_executive_summary(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, dict):
        analysis.executive_summary = (
            data.get("summary") or data.get("executive_summary") or ""
        )
        analysis.purpose = data.get("purpose") or data.get("description") or ""
        analysis.architecture_style = (
            data.get("architecture_style") or data.get("style") or ""
        )
        return bool(analysis.executive_summary or analysis.purpose)
    text = _read_text(path)
    if text:
        analysis.executive_summary = text.strip()
        return True
    return False


def _parse_project_overview(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, dict):
        analysis.project_overview = data.get("overview") or data.get("description") or ""
        analysis.purpose = analysis.purpose or data.get("purpose", "")
        if "tech_stack" in data and isinstance(data["tech_stack"], list):
            analysis.tech_stack = list(data["tech_stack"])
        return bool(analysis.project_overview or analysis.tech_stack)
    text = _read_text(path)
    if text:
        analysis.project_overview = text.strip()
        return True
    return False


def _parse_technical_debt(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        # Assume each entry is a debt item; pass through as-is plus a category tag
        for item in data:
            if isinstance(item, dict):
                norm = {
                    "title": item.get("title") or item.get("name") or "Untitled",
                    "severity": item.get("severity") or item.get("priority") or "medium",
                    "description": item.get("description") or item.get("detail") or "",
                    "files": item.get("files") or item.get("affected_files") or [],
                    "category": item.get("category", "technical_debt"),
                }
                analysis.technical_debt.append(norm)
        return bool(analysis.technical_debt)
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        return _parse_technical_debt_list(data["items"], analysis)
    text = _read_text(path)
    if text:
        # Markdown form — store raw and let consumer interpret
        analysis.raw_documents.setdefault("technical_debt_markdown", []).append(text)
        return True
    return False


def _parse_technical_debt_list(items: list, analysis: AWSTransformAnalysis) -> bool:
    for item in items:
        if isinstance(item, dict):
            analysis.technical_debt.append({
                "title": item.get("title") or item.get("name") or "Untitled",
                "severity": item.get("severity") or item.get("priority") or "medium",
                "description": item.get("description") or item.get("detail") or "",
                "files": item.get("files") or item.get("affected_files") or [],
                "category": item.get("category", "technical_debt"),
            })
    return bool(analysis.technical_debt)


def _parse_architecture(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, dict):
        analysis.architecture_notes = (
            data.get("description") or data.get("notes") or ""
        )
        analysis.architecture_style = (
            analysis.architecture_style or data.get("style") or data.get("pattern") or ""
        )
        if isinstance(data.get("strengths"), list):
            analysis.architecture_strengths = list(data["strengths"])
        if isinstance(data.get("weaknesses"), list):
            analysis.architecture_weaknesses = list(data["weaknesses"])
        return True
    text = _read_text(path)
    if text:
        analysis.architecture_notes = text.strip()
        return True
    return False


def _parse_code_analysis(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                analysis.code_metrics.append({
                    "file": item.get("file") or item.get("path") or "",
                    "lines": item.get("lines") or item.get("loc") or 0,
                    "complexity": item.get("complexity") or item.get("cyclomatic") or 0,
                    "maintainability": item.get("maintainability") or 0,
                    "issues": item.get("issues") or [],
                })
        return bool(analysis.code_metrics)
    if isinstance(data, dict) and "files" in data and isinstance(data["files"], list):
        return _parse_code_analysis_list(data["files"], analysis)
    return False


def _parse_code_analysis_list(items: list, analysis: AWSTransformAnalysis) -> bool:
    for item in items:
        if isinstance(item, dict):
            analysis.code_metrics.append({
                "file": item.get("file") or item.get("path") or "",
                "lines": item.get("lines") or item.get("loc") or 0,
                "complexity": item.get("complexity") or item.get("cyclomatic") or 0,
                "maintainability": item.get("maintainability") or 0,
                "issues": item.get("issues") or [],
            })
    return bool(analysis.code_metrics)


def _parse_domains(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                analysis.domains.append({
                    "name": item.get("name") or item.get("label") or "Unnamed",
                    "description": item.get("description") or "",
                    "components": item.get("components") or [],
                    "files": item.get("files") or [],
                })
        return bool(analysis.domains)
    return False


def _parse_components(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                analysis.components.append({
                    "name": item.get("name") or "Unnamed",
                    "description": item.get("description") or "",
                    "files": item.get("files") or [],
                    "responsibilities": item.get("responsibilities") or [],
                    "domain": item.get("domain", ""),
                })
        return bool(analysis.components)
    return False


def _parse_behavior(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("business_rules"), list):
        for rule in data["business_rules"]:
            if isinstance(rule, dict):
                analysis.business_rules.append({
                    "id": rule.get("id", ""),
                    "rule": rule.get("rule") or rule.get("description") or "",
                    "files": rule.get("files") or [],
                    "functions": rule.get("functions") or [],
                })
    if isinstance(data.get("data_flow"), list):
        analysis.data_flow = [
            str(s) for s in data["data_flow"] if isinstance(s, (str, int))
        ]
    if isinstance(data.get("api_endpoints"), list):
        for ep in data["api_endpoints"]:
            if isinstance(ep, dict):
                analysis.api_endpoints.append({
                    "method": ep.get("method", ""),
                    "path": ep.get("path", ""),
                    "description": ep.get("description", ""),
                })
    if isinstance(data.get("database_models"), list):
        analysis.database_models = [
            str(m) for m in data["database_models"]
        ]
    return True


def _parse_dependencies(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                analysis.dependencies.append({
                    "name": item.get("name") or "",
                    "version": item.get("version") or "",
                    "type": item.get("type") or "runtime",
                    "outdated": item.get("outdated", False),
                    "issues": item.get("issues") or [],
                })
                if item.get("name"):
                    analysis.tech_stack.append(item["name"])
        # De-dupe tech_stack
        analysis.tech_stack = sorted(set(analysis.tech_stack))
        return bool(analysis.dependencies)
    return False


def _parse_recommendations(path: Path, analysis: AWSTransformAnalysis) -> bool:
    data = _safe_load_json(path)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                analysis.recommendations.append(item)
            elif isinstance(item, dict):
                title = item.get("title") or item.get("recommendation") or ""
                detail = item.get("detail") or item.get("description") or ""
                analysis.recommendations.append(
                    f"{title}: {detail}".strip(": ").strip() if title else detail
                )
        return bool(analysis.recommendations)
    text = _read_text(path)
    if text:
        # Markdown bullets — extract lines starting with -, *, or numbered
        bullets = re.findall(r"^\s*[-*\d.]+\s+(.+)$", text, re.MULTILINE)
        analysis.recommendations.extend(bullets)
        return bool(bullets)
    return False


_PARSERS = {
    "executive_summary": _parse_executive_summary,
    "project_overview": _parse_project_overview,
    "technical_debt": _parse_technical_debt,
    "architecture": _parse_architecture,
    "code_analysis": _parse_code_analysis,
    "domains": _parse_domains,
    "components": _parse_components,
    "behavior": _parse_behavior,
    "dependencies": _parse_dependencies,
    "recommendations": _parse_recommendations,
}


# ── Public entrypoint ─────────────────────────────────────────────────────────

def load_aws_transform(path: str) -> AWSTransformAnalysis:
    """Load and parse an AWS Transform output directory (S3 or local).

    Walks the directory tree, classifies each file by name pattern, and
    parses recognized files into an AWSTransformAnalysis.

    Files we don't recognize are listed in `unparsed_files` so the caller
    can see what was skipped. Sections we expected but didn't find are
    listed in `missing_sections`.
    """
    local_root = _resolve_aws_path(path)
    job_name, conv_id = _extract_path_metadata(path)

    analysis = AWSTransformAnalysis(
        source=path,
        job_name=job_name,
        conversation_id=conv_id,
    )

    sections_seen: set[str] = set()

    for fp in sorted(local_root.rglob("*")):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(local_root))
        analysis.files_seen.append(rel)

        section = _classify_file(fp.name)
        if section is None:
            analysis.unparsed_files.append(rel)
            continue

        parser = _PARSERS.get(section)
        if parser is None:
            analysis.unparsed_files.append(rel)
            continue

        try:
            if parser(fp, analysis):
                sections_seen.add(section)
            else:
                analysis.unparsed_files.append(rel)
        except Exception as e:
            analysis.unparsed_files.append(f"{rel} (parse error: {e})")

    # Report which expected sections were absent
    expected = set(_PARSERS.keys())
    analysis.missing_sections = sorted(expected - sections_seen)

    return analysis


def to_narrative_dict(aws: AWSTransformAnalysis) -> dict:
    """Convert AWS analysis into the narrative dict shape that
    generate_pdf_report expects.

    This is the bridge between the two systems. Each PDF section gets
    populated from the most appropriate AWS field.
    """
    summary_parts = []
    if aws.executive_summary:
        summary_parts.append(aws.executive_summary)
    if aws.project_overview and aws.project_overview not in summary_parts:
        summary_parts.append(aws.project_overview)
    summary = "\n\n".join(summary_parts).strip()

    # Improvement suggestions: AWS recommendations + critical/high tech debt items
    improvement_suggestions = list(aws.recommendations)
    for debt in aws.technical_debt:
        if debt.get("severity", "").lower() in ("critical", "high"):
            t = debt.get("title", "")
            d = debt.get("description", "")
            improvement_suggestions.append(
                f"[{debt.get('severity', 'high').upper()}] {t}: {d}".strip(": ")
            )

    # Security notes: anything categorized as security in tech debt
    security_notes = []
    for debt in aws.technical_debt:
        cat = (debt.get("category") or "").lower()
        if "security" in cat or "vulnerability" in cat:
            security_notes.append(
                f"{debt.get('title', 'Security issue')}: {debt.get('description', '')}"
            )

    # Code quality notes from architecture weaknesses + non-security debt
    code_quality_notes = list(aws.architecture_weaknesses)

    # Key components: AWS components, falling back to AWS domains
    key_components = []
    for comp in aws.components:
        key_components.append({
            "name": comp.get("name", ""),
            "description": comp.get("description", ""),
            "files": comp.get("files", []),
            "responsibilities": comp.get("responsibilities", []),
        })
    if not key_components:
        for dom in aws.domains:
            key_components.append({
                "name": dom.get("name", ""),
                "description": dom.get("description", ""),
                "files": dom.get("files", []),
                "responsibilities": [],
            })

    return {
        "purpose": aws.purpose,
        "summary": summary,
        "architecture_style": aws.architecture_style,
        "tech_stack": aws.tech_stack,
        "key_components": key_components,
        "data_flow": aws.data_flow,
        "api_endpoints": aws.api_endpoints,
        "database_models": aws.database_models,
        "security_notes": security_notes,
        "improvement_suggestions": improvement_suggestions,
        "testing_approach": "",   # AWS doesn't produce this
        "deployment_info": "",    # AWS doesn't produce this
        "code_quality_notes": code_quality_notes,
    }


def to_findings(aws: AWSTransformAnalysis) -> list[dict]:
    """Convert AWS technical debt + dependencies into the findings shape
    that generate_pdf_report expects.
    """
    findings = []
    for debt in aws.technical_debt:
        findings.append({
            "category": "code_quality",
            "title": debt.get("title", ""),
            "detail": (
                f"Severity: {debt.get('severity', 'medium')}. "
                f"{debt.get('description', '')}"
            ),
            "files": debt.get("files", []),
            "confidence": "high",
        })
    for dep in aws.dependencies:
        if dep.get("outdated"):
            findings.append({
                "category": "dependency",
                "title": f"Outdated dependency: {dep.get('name', '')}",
                "detail": (
                    f"Version {dep.get('version', '?')}. "
                    f"{'; '.join(dep.get('issues', [])) or 'Marked outdated by AWS Transform.'}"
                ),
                "files": [],
                "confidence": "high",
            })
    return findings


def verify_business_rules(
    aws: AWSTransformAnalysis,
    graph,  # networkx.Graph
) -> dict:
    """Anchor AWS-extracted business rules to actual call paths in the graph.

    For each rule:
      - If the rule references functions, check whether those function names
        appear as nodes in the CodeGrapher graph.
      - If the rule references files, check whether those files contain any
        nodes in the graph.

    Returns a dict with three lists:
      - verified: rules with at least one function or file matched
      - partial:  rules where some references matched, others didn't
      - unverified: rules with no matched references in the graph

    Unverified rules are flagged for human review — they may be LLM
    hallucinations from AWS's behavioral analysis layer, or they may
    reference logic our tree-sitter extractor missed.
    """
    if graph is None or graph.number_of_nodes() == 0:
        return {"verified": [], "partial": [], "unverified": list(aws.business_rules)}

    # Build lookup tables from the graph
    label_to_nodes: dict[str, list[str]] = {}
    file_to_nodes: dict[str, list[str]] = {}
    for nid, attrs in graph.nodes(data=True):
        label = attrs.get("label", "").strip()
        # Normalize: strip parens for function names ("foo()" -> "foo")
        label_norm = label.rstrip("()").lower()
        if label_norm:
            label_to_nodes.setdefault(label_norm, []).append(nid)
        src = attrs.get("source_file", "")
        if src:
            file_to_nodes.setdefault(src, []).append(nid)
            # Also index by basename for loose matching
            file_to_nodes.setdefault(Path(src).name, []).append(nid)

    verified, partial, unverified = [], [], []

    for rule in aws.business_rules:
        funcs = [f.strip() for f in (rule.get("functions") or []) if f.strip()]
        files = [f.strip() for f in (rule.get("files") or []) if f.strip()]

        matched_funcs = []
        unmatched_funcs = []
        for fn in funcs:
            norm = fn.rstrip("()").lower()
            if norm in label_to_nodes:
                matched_funcs.append(fn)
            else:
                unmatched_funcs.append(fn)

        matched_files = []
        unmatched_files = []
        for f in files:
            if f in file_to_nodes or Path(f).name in file_to_nodes:
                matched_files.append(f)
            else:
                unmatched_files.append(f)

        total_refs = len(funcs) + len(files)
        total_matched = len(matched_funcs) + len(matched_files)

        annotated = {
            **rule,
            "_verification": {
                "matched_functions": matched_funcs,
                "unmatched_functions": unmatched_funcs,
                "matched_files": matched_files,
                "unmatched_files": unmatched_files,
            },
        }

        if total_refs == 0:
            # No references at all — can't verify
            unverified.append(annotated)
        elif total_matched == 0:
            unverified.append(annotated)
        elif total_matched == total_refs:
            verified.append(annotated)
        else:
            partial.append(annotated)

    return {
        "verified": verified,
        "partial": partial,
        "unverified": unverified,
    }
