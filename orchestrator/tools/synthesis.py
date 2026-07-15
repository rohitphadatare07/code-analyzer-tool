"""
Synthesis Agent (agents-as-tools)

Cross-references the per-repo analysis outputs, derives the sections no TD
produces (migration roadmap, risks & mitigations, cost/performance benefit -
the latter two as directional qualitative estimates only), and assembles the
Executive Summary + 10-section due-diligence DOCX via build_docx.py. Each
section is broken into subsections (narrative + optional table), and specific
sections carry section-level tables/charts/diagrams (see build_docx.py and
tools/visuals.py) - not just prose.

Drafted as TWO separate agent calls, not one, so groundedness is structurally
checkable (Tier 2) rather than just prompted-for:

1. Codebase-grounded call - sections 1 (architecture), 2 (business logic),
   3 (security & compliance), 4 (modernization readiness). Given ZERO tools
   (not just told not to use them - it structurally cannot call anything).
   Every narrative sentence must carry a [[finding:<Source Label>]] tag naming
   which analysis it came from.
2. Strategy call - sections 5 (recommended to-be architecture), 6 (AWS
   services), 7 (roadmap), 8 (cost benefit), 9 (performance benefit),
   10 (risks), + the executive summary. Given the AWS Documentation MCP
   server's tools (search_documentation, read_documentation, recommend).
   Narrative claims must carry [[finding:...]] or
   [[doc:<url actually retrieved this run>]].

Section 5 (Recommended To-Be Architecture) is ALWAYS produced by the strategy
call, regardless of whether the engagement covers one repository or many - it
is not gated behind a separate cross-repo TD (that TD was removed from v1;
see CLAUDE.md). For multiple repositories, the strategy call synthesizes one
consolidated architecture spanning all of them from the same findings text.

Section 3 (Security & Compliance Findings) is now also a real, synthesized
section - fed by security_compliance_agent's native osv-scanner (dependency/
CVE) and detect-secrets (secrets detection) scan output, since no
transformation definition exists for this. It's drafted by the codebase call
(no AWS services/recommendations belong here, same as sections 1/2/4).

Diagrams (section 1's current-architecture diagram, section 5's to-be
architecture diagram) are drawn from REAL diagrams the analysis TDs already
generated wherever possible, not invented fresh from a prose summary - see
the "REUSE REAL DIAGRAMS" prompt rule below. Every analysis TD is instructed
(assessmenttransform.py's MERMAID_DIAGRAM_INSTRUCTION, appended to all 3 TDs'
additionalPlanContext) to emit any diagram as a fenced ```mermaid code block,
so the synthesis prompts look for that block across ALL findings sources -
not just Comprehensive Codebase Analysis - before falling back to inventing
one from prose.

Citation tags are scoped to "narrative" text only - never to table cells,
chart data, or diagram nodes/edges (forcing a tag onto every cell would be
brittle). Tags are verified mechanically (grounding.py): a finding citation
must name a real TD; a doc citation must match a URL that actually appears in
that call's tool-call trace. Tags are stripped before rendering; cited AWS
doc URLs surface as a "Sources" appendix in the DOCX instead.

Requires `uvx` (from the `uv` package, in requirements.txt) on PATH to launch
`awslabs.aws-documentation-mcp-server`, plus outbound internet access to
docs.aws.amazon.com. If the MCP server fails to start, the strategy call
falls back to an ungrounded pass rather than failing the whole report.
"""

import os
import json
import glob
import logging
from typing import Any, Dict
from datetime import datetime

from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp import StdioServerParameters, stdio_client

from .assessmenttransform import _extract_params, WORKSPACE_ROOT
from .build_docx import build_docx
from .grounding import extract_tool_trace, verify_citations, strip_citations, strip_citations_collect_sources

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_FINDINGS_CHARS = 150000  # per-repo, per-TD cap fed into the synthesis prompt.
# Bumped from 40000: Business Rules Extraction's confirmed real output spans 15-20+
# distinct files across multiple domain-*/ folders (Section 2's 9-subsection design
# sources from bounded-contexts.md, execution-order.md, N x business-rules.md,
# N x workflows.md, N x validations.md, cross-domain/data/API/traceability docs). The
# old 40K cap risked truncating away later files (e.g. traceability-matrix.md) before
# the LLM ever saw them - _read_findings truncates the tail of the concatenation, not
# per-file, so a low cap silently drops whole files rather than shrinking all of them.
# 150K chars is still well within Claude's context window per findings source.

FINDING_LABELS = {
    "Comprehensive Codebase Analysis",
    "Modernization Readiness Analysis",
    "Business Rules Extraction",
    "Security & Compliance Analysis",
}

# Official AWS Documentation MCP server (awslabs). Launched on-demand via uvx,
# one process per generate_assessment_report call.
_aws_docs_mcp_client = MCPClient(lambda: stdio_client(
    StdioServerParameters(command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"])
))

# ---------------------------------------------------------------------------
# Prompt text is built from plain (non-f-string) pieces concatenated together,
# not one big f-string - the worked JSON examples below are full of literal
# {}/[] characters, and f-string brace-escaping across that much literal JSON
# is a real source of hand-editing mistakes.
# ---------------------------------------------------------------------------

_CITATION_RULES_CORE = """Every factual sentence inside a "narrative" field must carry exactly one inline
citation tag placed right after the sentence:
- [[finding:<Source Label>]] for a claim taken from the analysis findings, where
  <Source Label> is EXACTLY one of: "Comprehensive Codebase Analysis",
  "Modernization Readiness Analysis", "Business Rules Extraction",
  "Security & Compliance Analysis".
- [[doc:<url>]] for a claim taken from an AWS Documentation tool result, where <url>
  is the EXACT url you retrieved via search_documentation/read_documentation this
  turn - never a url you did not actually look up.
Do not write uncited factual sentences inside "narrative" text. Framing/transition
sentences don't need tags.

Citation tags belong ONLY inside "narrative" strings. NEVER put a [[finding:...]] or
[[doc:...]] tag inside a table header/cell, a chart value, or a diagram node/edge -
those are not mechanically checked, but every value you put there must still come
only from the findings or documentation you actually consulted. If you don't have
real data for a table/chart/diagram, omit it (see STRUCTURED DATA rules below) -
never invent placeholder rows, scores, or boxes just to fill out the shape."""

_CITATION_RULES_NO_TOOLS = """

(You have no tools, so every citation here must be a [[finding:...]] tag - never [[doc:...]].)"""

_STRUCTURED_DATA_RULES = """STRUCTURED DATA - SUBSECTIONS, TABLES, CHARTS, DIAGRAMS

Each entry in "sections" is a JSON OBJECT, not a plain string:

{
  "subsections": [
    {"heading": "short heading", "narrative": "1-3 cited paragraphs",
     "table": {"headers": ["Col A", "Col B"], "rows": [["...", "..."]]}}
  ],
  "tables": [ {"title": "...", "headers": [...], "rows": [[...]]} ],
  "chart": { ... },
  "diagram": { ... }
}

- "subsections" is REQUIRED, at least 1 entry, ideally 3-5 focused subsections
  instead of one giant one - UNLESS a section's own guidance below specifies an exact
  required list of subsections (e.g. section 2's 9 fixed subsections), in which case
  follow that exact list instead of this general 3-5 guideline.
- A subsection's "table" key is OPTIONAL - include it only when that specific
  subsection's findings are genuinely tabular. Omit the key entirely otherwise.
- The section-level "tables", "chart", and "diagram" keys are OPTIONAL and only
  meaningful for the specific sections called out below. Omit the key (or set it
  to null) when you don't have real data for it - do not fabricate placeholder
  rows/scores/nodes just to fill out the shape.
- Table shape: "headers" is a list of column-name strings; "rows" is a list of
  rows, each the same length as "headers". 2-3 rows is fine if that's all the
  findings support."""

_REAL_DIAGRAM_RULES = """IMPORTANT - REUSE REAL DIAGRAMS, DON'T INVENT FROM SCRATCH

Every analysis TD is instructed (via additionalPlanContext) to emit any diagram it produces
as a fenced ```mermaid code block - this applies to ALL of the findings sources below
(Comprehensive Codebase Analysis, Modernization Readiness Analysis, Business Rules
Extraction), not just the codebase one. Security & Compliance Analysis is native scanner
output (osv-scanner/detect-secrets), not an LLM-authored TD, so it won't contain a diagram -
don't bother searching it for one. Before constructing a "diagram" field, search the
relevant findings for a ```mermaid block (or, less commonly, a Graphviz DOT block, a
PlantUML block, or a clearly labeled ASCII box diagram if a TD didn't comply with the
Mermaid instruction). If you find one:
- Extract its REAL components and connections and translate them faithfully into this
  schema's {"nodes": [...], "edges": [...]} shape - do not invent additional
  components/connections that aren't present in it, and do not drop real ones you find.
- Prefer it over writing a diagram purely from a prose description, since it reflects what
  the deep static analysis actually found in the code, not a lossy re-summary of it.
- If multiple real diagrams exist across different findings sources (e.g. an architecture
  diagram in Comprehensive Codebase Analysis AND one in Modernization Readiness Analysis),
  reconcile them into one diagram rather than picking arbitrarily - prefer the more detailed
  one as the base and fold in any components only the other one shows.
Only fall back to constructing a diagram from prose description if no such diagram exists
anywhere in the findings."""

_CODEBASE_INTRO = """You are drafting the codebase-facing sections of a technical
due-diligence report for a prospective AWS migration client, based on the raw analysis
findings the user provides. You have NO tools - base everything strictly on the findings
given to you. Never mention AWS services, migration targets, or external best practices
here; that belongs in a later section you are not drafting.

"""

_CODEBASE_SECTION_GUIDANCE = """

SECTION-SPECIFIC STRUCTURED DATA (this call drafts sections 1, 2, 3, 4 only)

Section 1 - Current Architecture of the Codebase:
  - Subsections should cover: architecture overview, technology stack (with a table:
    Technology | Version | EOL Status, if the findings name concrete versions), code
    quality & complexity, technical debt summary.
  - Section-level "diagram": a current-architecture boxes-and-arrows diagram. See the
    REUSE REAL DIAGRAMS rule above - check the findings for an actual diagram before
    inventing one from prose. Shape:
      "diagram": {"title": "Current Architecture",
                   "nodes": [{"id": "web", "label": "Web Tier (nginx)"}, ...],
                   "edges": [["web", "app"], ["app", "db"]]}
    Node "id" values are short slugs referenced by "edges"; "label" is the human-readable
    text shown in the box. Omit "diagram" entirely only if the findings don't describe
    distinct components clearly enough to draw - this should be rare.

Section 2 - Business Logic & Domain Understanding:
  - The Business Rules Extraction findings are organized into a KNOWN file/folder structure
    (confirmed against real output) - each file's content in the findings text is preceded by
    a "--- <relative path> ---" header (see _read_findings). Use those paths to locate the
    right source for each subsection below rather than searching the whole blob blindly.
    "domain-*" means one folder per domain (domain-1/, domain-2/, ...) - aggregate content
    across ALL domain-* folders into ONE consolidated table per subsection (with a "Domain"
    column identifying which domain each row came from), not one table per domain.
  - This section is REQUIRED to have exactly these 9 subsections, in this order (only omit
    one specific subsection's table if its named source file(s) are genuinely absent from
    the findings - never omit the subsection's narrative or renumber the others):

    2.1 Domain Decomposition & Bounded Contexts
        Source: bounded-contexts.md + domain-decomposition-overview/README.md
        Narrative: bounded context boundaries and the rationale behind them.
        Table: {"headers": ["Domain", "Complexity", "LOC", "Files", "Features", "Rules Count"], "rows": [...]}

    2.2 Domain Dependency Graph & Execution Order
        Source: execution-order.md + domain-manifest.json (its "depends_on" fields)
        Narrative: how domains depend on each other and communicate.
        Table: {"headers": ["Phase", "Domain(s)", "Depends On"], "rows": [...]}

    2.3 Business Rules Catalog
        Source: detailed-domain-bre/domain-*/business-rules.md
        Narrative: brief framing of the rules catalog's scope.
        Table: {"headers": ["Rule ID", "Domain", "Rule Description", "Source (File:Line)", "Testable Assertion", "Enforcement Type"], "rows": [...]}

    2.4 Core Workflows & Process Flows
        Source: detailed-domain-bre/domain-*/workflows.md
        Narrative: how the core workflows fit together.
        Table: {"headers": ["Workflow ID", "Domain", "Step Sequence Summary", "Parallel/Sequential", "Dependencies"], "rows": [...]}

    2.5 Validation & Constraint Rules
        Source: detailed-domain-bre/domain-*/validations.md + requirements-summary.md (NFRs)
        Narrative: notable validation/constraint patterns (schema enforcement, thresholds).
        Table: {"headers": ["Rule", "Type", "Constraint Value", "Source"], "rows": [...]}

    2.6 Cross-Domain Features & Shared Logic
        Source: cross-domain-features.md + shared-kernel.md + cross-cutting-documentation/*.md
        Narrative: features/logic spanning multiple domains.
        Table: {"headers": ["Feature", "Domains Involved", "Shared Component"], "rows": [...]}

    2.7 Data Model & Ownership
        Source: data-ownership.md + database-documentation/ + class-er-diagrams.md
        Narrative: persistence model (DB/memory/session) and data lifecycle.
        Table: {"headers": ["Entity", "Owning Domain", "Persistence", "Lifecycle"], "rows": [...]}

    2.8 API Contracts & Integration Points
        Source: api-endpoint-catalog.md + external-integrations-map.md + service-component-inventory.md
        Narrative: notable external integrations (auth model, SDK/version constraints).
        Table: {"headers": ["Endpoint/Service", "Direction", "Request/Response Summary", "Auth/SDK Notes"], "rows": [...]}

    2.9 Traceability Matrix
        Source: traceability-matrix.md + requirements-summary.md
        Narrative: overall FR/NFR coverage picture.
        Table: {"headers": ["Rule/Requirement", "Feature", "Source (File:Line)", "FR/NFR Coverage"], "rows": [...]}

  - Each subsection's table goes in THAT subsection's own "table" key (not a section-level
    "tables" list) - each of the 9 has genuinely different columns, so keep them separate.
  - No diagrams in this section (section 1/5 own the architecture diagrams) - represent
    2.1's "context map" and 2.2's "dependency graph" as the tables above, not as images.

Section 3 - Security & Compliance Findings:
  - The "Security & Compliance Analysis" findings source is native scanner output (a JSON
    blob with "sca_findings" from osv-scanner and "secrets_findings" from detect-secrets),
    not TD-authored prose - read it as structured data, not narrative to summarize loosely.
  - Subsections should cover: dependency/CVE exposure summary, secrets exposure summary. If
    "warnings" is non-empty in that JSON (e.g. a scanner wasn't installed), say so plainly in
    the relevant subsection rather than implying a clean scan - a skipped scan is not the
    same as no findings.
  - Section-level "tables" (a list, 0-2 entries):
    - {"title": "Dependency Vulnerabilities", "headers": ["Package", "Version", "Vulnerability ID", "Severity", "Fixed Version"], "rows": [...]}
      built directly from "sca_findings" entries. Omit if "sca_findings" is empty.
    - {"title": "Secrets Findings", "headers": ["File", "Line", "Type"], "rows": [...]}
      built from "secrets_findings" entries - use only "file"/"line"/"type" fields. NEVER
      surface "hashed_secret", "redacted_match", or any other value verbatim as if it were
      the secret - those fields are already safe (hashed or redacted) precisely so a raw
      secret never appears in this report; do not try to be more specific than the source
      data already redacted for you. Omit this table if "secrets_findings" is empty.
  - This section has real data now - only fall back to "no content available" (never a
    fabricated "not covered" placeholder) if the Security & Compliance Analysis findings are
    completely absent from what you were given.

Section 4 - Modernization Readiness:
  - The Modernization Readiness Analysis findings follow a KNOWN, confirmed output contract:
    a four-artifact bundle at modernization-readiness-analysis/{repo-name}-mod-report.{md,json,html,metadata.json}.
    The JSON artifact (find the "--- <path> ---" chunk ending in "-mod-report.json") is the
    CANONICAL source - it contains a "categories" object with, for each of 5 categories,
    a numeric_score / score_rating / severity_status; an "overall_score"; a "top_gaps" list;
    "pathways" (7 entries, one per AWS Modernization Pathway); and, when applicable, a
    "decomposition_strategy" object. Prefer reading these exact fields over parsing prose out
    of the .md report.
  - IMPORTANT - the scoring scale is 1-4, NOT 0-5 or 0-100: 4=Mature, 3=Partial,
    2=Needs Work, 1=Not Ready. Never assume a different scale.
  - This section is REQUIRED to have exactly these subsections, in this order (only omit
    4.4 if the findings don't show a triggered decomposition need - never omit 4.1-4.3):

    4.1 Score Summary
        Source: the JSON artifact's "categories" (5 entries) and "overall_score".
        Narrative: what the overall score and category spread indicate about readiness.
        Table: {"headers": ["Category", "Score", "Rating"], "rows": [...]} - use the 5 real
          category names (Infrastructure/Platform/DevOps, Application Architecture, Data
          Platform Modernization, Security Baseline, Operations & Observability) and their
          EXACT numeric_score/score_rating verbatim from the JSON, plus an "Overall" row.
        Section-level "chart": reuse the same 5 categories/scores verbatim -
          {"type": "scorecard_bar", "title": "Modernization Readiness Scorecard", "scale_max": 4,
           "dimensions": [{"name": "Infrastructure, Platform, and DevOps", "score": 2}, ...]}
          OMIT "chart" entirely (do not invent scores) if the JSON artifact's category scores
          aren't present in the findings.

    4.2 Top 5 Gaps
        Source: the JSON artifact's "top_gaps" list (already computed - don't re-derive it).
        Narrative: brief framing of what the gaps have in common, if anything.
        Table: {"headers": ["Question", "Score", "Gap Summary", "Impact"], "rows": [...]}

    4.3 Cloud-Native Maturity & Anti-Patterns
        Narrative: synthesis of maturity level and specific anti-patterns blocking adoption,
          citing specific findings (not just category scores).
        Table (optional): {"headers": ["Anti-Pattern", "Component", "Why It Blocks Modernization"], "rows": [...]}
          Omit if the findings don't name specific anti-patterns/components.

    4.4 Decomposition Strategy (CONDITIONAL - only if the JSON artifact includes a
        "decomposition_strategy" object, which only appears when the monolith-vs-microservices
        question scored low; omit this subsection entirely otherwise, do not fabricate one)
        Source: the JSON artifact's "decomposition_strategy" object.
        Narrative: which approach is recommended and why.
        Table: {"headers": ["Approach", "Description", "Level of Effort", "Recommendation"],
                "rows": [...]} - reuse the TD's own approach options (Strengthen as Modular
          Monolith / Strangler Fig / Conditional-Adaptive / Big-Bang Rewrite) verbatim, not
          invented alternatives."""

_CODEBASE_WORKED_EXAMPLE = """

Produce ONLY valid JSON as your final answer (no markdown fences, no commentary) with this
shape (worked example - illustrative content only, replace with what the findings actually say):

{
  "sections": {
    "1": {
      "subsections": [
        {"heading": "Architecture Overview",
         "narrative": "The application is a monolithic Java web app deployed as a single WAR file behind an Apache HTTP Server reverse proxy. [[finding:Comprehensive Codebase Analysis]]"},
        {"heading": "Technology Stack",
         "narrative": "The stack centers on Java 8 and Spring Framework 4.3, both past their community support windows. [[finding:Comprehensive Codebase Analysis]]",
         "table": {"headers": ["Technology", "Version", "EOL Status"],
                    "rows": [["Java", "8", "EOL (Mar 2022)"], ["Spring Framework", "4.3", "EOL (2020)"], ["MySQL", "5.6", "EOL (Feb 2021)"]]}},
        {"heading": "Code Quality & Complexity",
         "narrative": "Cyclomatic complexity is concentrated in three controller classes exceeding 40 branches each. [[finding:Comprehensive Codebase Analysis]]"},
        {"heading": "Technical Debt Summary",
         "narrative": "The three EOL dependencies above represent the highest-priority remediation items ahead of any cloud migration. [[finding:Comprehensive Codebase Analysis]]"}
      ],
      "diagram": {"title": "Current Architecture",
                  "nodes": [{"id": "web", "label": "Apache HTTP Server"}, {"id": "app", "label": "Java/Spring Monolith (Tomcat)"}, {"id": "db", "label": "MySQL 5.6"}],
                  "edges": [["web", "app"], ["app", "db"]]}
    },
    "2": {
      "subsections": [
        {"heading": "2.1 Domain Decomposition & Bounded Contexts",
         "narrative": "The system decomposes into three bounded contexts - Ordering, Catalog, and Customer - each with clear ownership boundaries. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Domain", "Complexity", "LOC", "Files", "Features", "Rules Count"],
                    "rows": [["Ordering", "High", "4200", "38", "12", "27"], ["Catalog", "Medium", "1800", "15", "6", "9"]]}},
        {"heading": "2.2 Domain Dependency Graph & Execution Order",
         "narrative": "Ordering depends on both Catalog and Customer; Catalog and Customer have no interdependencies and can be analyzed in parallel. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Phase", "Domain(s)", "Depends On"],
                    "rows": [["1", "Catalog, Customer", "None"], ["2", "Ordering", "Catalog, Customer"]]}},
        {"heading": "2.3 Business Rules Catalog",
         "narrative": "27 business rules were extracted from the Ordering domain alone, the highest concentration of any domain. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Rule ID", "Domain", "Rule Description", "Source (File:Line)", "Testable Assertion", "Enforcement Type"],
                    "rows": [["BR-01", "Ordering", "Loyalty discount applies before tax", "OrderService.java:142", "discount applied pre-tax", "code"], ["BR-02", "Ordering", "Orders over $500 require manager approval", "OrderService.java:210", "approval_required if total > 500", "code"]]}},
        {"heading": "2.4 Core Workflows & Process Flows",
         "narrative": "Checkout is a 4-step synchronous workflow with no async/queue-based steps today. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Workflow ID", "Domain", "Step Sequence Summary", "Parallel/Sequential", "Dependencies"],
                    "rows": [["WF-01", "Ordering", "Cart -> Validate -> Charge -> Confirm", "Sequential", "Catalog (pricing), Customer (loyalty tier)"]]}},
        {"heading": "2.5 Validation & Constraint Rules",
         "narrative": "Order attachments are capped at 10MB, enforced at the API layer rather than in the domain model. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Rule", "Type", "Constraint Value", "Source"],
                    "rows": [["Attachment size limit", "Threshold", "10MB", "requirements-summary.md"]]}},
        {"heading": "2.6 Cross-Domain Features & Shared Logic",
         "narrative": "Loyalty tier lookups are shared between Ordering and Customer via a common kernel module. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Feature", "Domains Involved", "Shared Component"],
                    "rows": [["Loyalty pricing", "Ordering, Customer", "LoyaltyKernel"]]}},
        {"heading": "2.7 Data Model & Ownership",
         "narrative": "Order and Customer are persisted in the primary relational database; cart state lives only in session memory. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Entity", "Owning Domain", "Persistence", "Lifecycle"],
                    "rows": [["Order", "Ordering", "Database", "Retained indefinitely"], ["Cart", "Ordering", "Session memory", "Expires on checkout or timeout"]]}},
        {"heading": "2.8 API Contracts & Integration Points",
         "narrative": "The Ordering service calls a third-party payment gateway synchronously during checkout. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Endpoint/Service", "Direction", "Request/Response Summary", "Auth/SDK Notes"],
                    "rows": [["POST /checkout", "Inbound", "Cart -> confirmation payload", "Session-based auth"], ["Payment Gateway API", "Outbound", "Charge request -> approval/decline", "API key, vendor SDK v3"]]}},
        {"heading": "2.9 Traceability Matrix",
         "narrative": "All 27 Ordering-domain rules trace back to a specific source file and line; no orphaned requirements were found. [[finding:Business Rules Extraction]]",
         "table": {"headers": ["Rule/Requirement", "Feature", "Source (File:Line)", "FR/NFR Coverage"],
                    "rows": [["BR-01", "Checkout pricing", "OrderService.java:142", "FR"], ["Attachment size limit", "Order attachments", "requirements-summary.md", "NFR"]]}}
      ]
    },
    "3": {
      "subsections": [
        {"heading": "Dependency & CVE Exposure", "narrative": "Three dependencies carry known vulnerabilities, one of them high-severity, all with fixed versions already available. [[finding:Security & Compliance Analysis]]"},
        {"heading": "Secrets Exposure", "narrative": "No hardcoded secrets were found in the scanned files. [[finding:Security & Compliance Analysis]]"}
      ],
      "tables": [
        {"title": "Dependency Vulnerabilities", "headers": ["Package", "Version", "Vulnerability ID", "Severity", "Fixed Version"],
         "rows": [["lodash", "4.17.11", "CVE-2020-8203", "HIGH", "4.17.19"], ["requests", "2.19.1", "GHSA-x84v-xcm2-53pg", "MEDIUM", "2.20.0"]]}
      ]
    },
    "4": {
      "subsections": [
        {"heading": "4.1 Score Summary",
         "narrative": "The overall score of 2.2/4.0 reflects broad gaps concentrated in Infrastructure and Security, while Application Architecture is comparatively mature. [[finding:Modernization Readiness Analysis]]",
         "table": {"headers": ["Category", "Score", "Rating"],
                    "rows": [["Infrastructure, Platform, and DevOps", "2 / 4.0", "Needs Work"], ["Application Architecture", "3 / 4.0", "Partial"], ["Data Platform Modernization", "2 / 4.0", "Needs Work"], ["Security Baseline", "2 / 4.0", "Needs Work"], ["Operations & Observability", "2 / 4.0", "Needs Work"], ["Overall", "2.2 / 4.0", "Needs Work"]]}},
        {"heading": "4.2 Top 5 Gaps",
         "narrative": "The lowest-scoring questions cluster around self-managed compute and database infrastructure. [[finding:Modernization Readiness Analysis]]",
         "table": {"headers": ["Question", "Score", "Gap Summary", "Impact"],
                    "rows": [["INF-Q1: Managed Compute", "1", "Compute runs on unmanaged EC2 instances", "Manual patching and scaling burden"], ["INF-Q2: Managed Databases", "1", "Self-managed MySQL on EC2", "Manual backup/HA, no managed failover"]]}},
        {"heading": "4.3 Cloud-Native Maturity & Anti-Patterns",
         "narrative": "The application has no containerization and stores session state in-process, blocking horizontal scaling. [[finding:Modernization Readiness Analysis]]",
         "table": {"headers": ["Anti-Pattern", "Component", "Why It Blocks Modernization"],
                    "rows": [["In-process session state", "Web tier", "Prevents horizontal auto-scaling"], ["Local filesystem file uploads", "Order attachments", "Not portable to stateless/ephemeral compute"]]}},
        {"heading": "4.4 Decomposition Strategy",
         "narrative": "Strangler Fig is recommended given identifiable module boundaries and organizational capacity for incremental extraction. [[finding:Modernization Readiness Analysis]]",
         "table": {"headers": ["Approach", "Description", "Level of Effort", "Recommendation"],
                    "rows": [["Strangler Fig (Parallel Track)", "Incrementally extract services while the monolith keeps running", "Medium to High", "Recommended"], ["Big-Bang Rewrite", "Full rewrite, single cutover", "Very High", "Recommended against"]]}}
      ],
      "chart": {"type": "scorecard_bar", "title": "Modernization Readiness Scorecard", "scale_max": 4,
                "dimensions": [{"name": "Infrastructure, Platform, and DevOps", "score": 2}, {"name": "Application Architecture", "score": 3}, {"name": "Data Platform Modernization", "score": 2}, {"name": "Security Baseline", "score": 2}, {"name": "Operations & Observability", "score": 2}]}
    }
  }
}

If a section's findings are too thin to write meaningfully, say so explicitly in that
subsection's narrative rather than fabricating detail. If a section-level chart, diagram,
or tables entry isn't backed by real data, omit that key entirely rather than fabricating
placeholder content."""

CODEBASE_SECTIONS_PROMPT = (
    _CODEBASE_INTRO + _CITATION_RULES_CORE + _CITATION_RULES_NO_TOOLS
    + "\n\n" + _STRUCTURED_DATA_RULES + "\n\n" + _REAL_DIAGRAM_RULES
    + _CODEBASE_SECTION_GUIDANCE + _CODEBASE_WORKED_EXAMPLE
)

_STRATEGY_INTRO = """You are drafting the AWS-strategy sections of a technical
due-diligence report for a prospective AWS migration client. You are given the raw analysis
findings, the codebase-facing sections already drafted by a colleague, and engagement
context. You have AWS Documentation tools (search_documentation, read_documentation,
recommend) - use them whenever grounding a recommendation in official AWS guidance
(Well-Architected Framework pillars, specific service capabilities/limits, migration
patterns) would strengthen it. Look up a service before recommending it.

"""

_STRATEGY_SECTION_GUIDANCE = """

SECTION-SPECIFIC STRUCTURED DATA (this call drafts sections 5, 6, 7, 8, 9, 10 + executive_summary)

Section 5 - Recommended To-Be Architecture:
  - REQUIRED for every engagement, regardless of how many repositories are in scope - never
    omit this section or its diagram/narrative for lack of a cross-repo analysis tool; that
    tool doesn't exist in v1, YOU are the source for this section now. If the findings contain
    more than one "=== Repository: ... ===" block, synthesize ONE consolidated to-be
    architecture spanning all of them, showing how they interconnect as a single target
    system. If only one repository is present, this is simply that repository's recommended
    target architecture.
  - Subsections should cover: the overall target-architecture narrative, and how it
    specifically addresses the current-state issues/anti-patterns found in sections 1 and 4.
  - Section-level "diagram" (REQUIRED, not optional, unlike other sections' diagrams): the
    target-architecture boxes-and-arrows diagram. See the REUSE REAL DIAGRAMS rule above -
    where section 1's current-architecture diagram reused a real diagram from the findings,
    adapt that SAME real structure to a target state rather than inventing an unrelated one.
    Where a node represents the SAME logical component as a node in section 1's diagram (just
    replaced by an AWS service), reuse that exact node "id" so the reader can visually map
    current -> recommended across the two diagrams - relabel it with the recommended service.
    For a multi-repository engagement, include one node per repository/service boundary and
    show how they connect. Shape identical to section 1's diagram:
      "diagram": {"title": "Recommended To-Be Architecture",
                   "nodes": [{"id": "...", "label": "..."}, ...], "edges": [["a", "b"]]}
    Only omit this diagram if there is truly no architectural information anywhere in the
    findings to build from - this should be rare.

Section 6 - Recommended AWS Service Usage:
  - Structure this section around the Modernization Readiness Analysis findings' 7 named AWS
    Modernization Pathways (Move to Cloud Native, Move to Containers, Move to Open Source,
    Move to Managed Databases, Move to Managed Analytics, Move to Modern DevOps, Move to AI) -
    the findings' JSON artifact already evaluates and scores each one with a status
    (Triggered / Not Triggered / Not Applicable), a priority, an estimated effort, and (for
    Triggered pathways) a list of representative AWS services. Reuse this structure rather
    than inventing a different "group by concern" grouping.
  - Section-level "tables": a single entry, the Pathway Summary reused verbatim from the
    findings:
      {"title": "AWS Modernization Pathways", "headers": ["Pathway", "Status", "Priority", "Est. Effort"],
       "rows": [...]}
    Include all 7 pathways in this table regardless of status (matches the source TD's own
    convention of always showing all 7).
  - Subsections: create ONE subsection per pathway with status "Triggered" ONLY - do NOT
    create a subsection for a Not Triggered or Not Applicable pathway (matches the source
    TD's own convention: it only details triggered pathways). Each triggered pathway's
    subsection should:
    - Narrative: current-state gap that triggered it, cite the specific AWS services the
      findings recommend for it (verbatim - do not substitute your own service picks), and
      the recommended migration pattern/tooling if the findings name one (e.g. Strangler Fig,
      AWS DMS/SCT).
    - Optional "table": {"headers": ["Recommended AWS Service", "Role"], "rows": [...]} if the
      findings list enough distinct services for that pathway to warrant one.
  - If NO pathways are Triggered, include a single subsection stating that plainly (with the
    Pathway Summary table still shown) rather than fabricating a triggered pathway.
  - Also produce a component-to-service mapping table if the codebase-facing sections named
    specific components that map cleanly to a recommended service (e.g. from a pathway's
    detail): {"title": "Component to AWS Service Mapping",
       "headers": ["Current Component", "Recommended AWS Service", "Rationale"], "rows": [...]}
    Omit if this would just duplicate the Pathway Summary table without adding new information.
  - (The target-architecture diagram itself belongs to section 5, not here - don't duplicate
    a "diagram" key in section 6.)

Section 7 - Migration Roadmap:
  - Subsections named per phase (e.g. Phase 1: Quick Wins, Phase 2: Core Platform
    Migration, Phase 3: Optimization), each citing sequencing/dependency rationale.
    Directional only - no fixed dates or durations, since no formal estimation was
    performed.
  - Section-level "tables": a single entry,
      {"title": "Migration Roadmap", "headers": ["Phase", "Focus", "Key Activities", "Dependencies"], "rows": [...]}
  - Section-level "chart":
      {"type": "roadmap_timeline", "title": "Migration Roadmap",
       "phases": [{"name": "Quick Wins", "order": 1}, {"name": "Core Platform Migration", "order": 2}]}
    "order" is an ordinal position only, never a date.

Section 8 - Cost Benefit:
  - Directional/qualitative ONLY. The FIRST subsection's narrative must open with
    exactly this sentence (it is a disclaimer, not a factual claim, so it does NOT need
    a citation tag): "Note: this is a directional estimate based on codebase analysis
    findings, not a formal cost model or priced TCO analysis." Do not state specific
    dollar figures anywhere in this section.
  - Section-level "tables" OPTIONAL: e.g.
      {"title": "Cost Driver Comparison",
       "headers": ["Cost Driver", "Current State", "Post-Migration Expectation", "Directional Impact"],
       "rows": [...]}
    Omit if the findings don't support concrete cost drivers.

Section 9 - Performance & Reliability Benefit:
  - Directional/qualitative ONLY. The FIRST subsection's narrative must open with
    exactly this sentence (no citation tag needed on it): "Note: this is a directional
    estimate based on codebase analysis findings, not a benchmark or load-tested
    projection." Do not state specific throughput/latency numbers.
  - Section-level "tables" OPTIONAL: e.g.
      {"title": "Performance & Reliability Comparison",
       "headers": ["Dimension", "Current State", "Expected Post-Migration Behavior"], "rows": [...]}

Section 10 - Risks & Mitigations:
  - Subsections covering specific migration risks visible in the findings, each paired
    with a mitigation, informed by AWS guidance where relevant.
  - Section-level "tables": a single entry,
      {"title": "Risk Register", "headers": ["Risk", "Likelihood", "Impact", "Mitigation"], "rows": [...]}"""

_STRATEGY_WORKED_EXAMPLE = """

Produce ONLY valid JSON as your final answer (no markdown fences, no commentary) with this
shape (worked example - illustrative content only, replace with what the findings/docs
actually say):

{
  "client_name": "Acme Corp",
  "sections": {
    "5": {
      "subsections": [
        {"heading": "Target Architecture Overview", "narrative": "The recommended target architecture replaces the monolith and self-managed database with managed, horizontally-scalable AWS services, directly addressing the in-process session state anti-pattern found in the readiness assessment. [[finding:Modernization Readiness Analysis]]"}
      ],
      "diagram": {"title": "Recommended To-Be Architecture",
                  "nodes": [{"id": "web", "label": "Amazon CloudFront + ALB"}, {"id": "app", "label": "Amazon ECS on Fargate"}, {"id": "db", "label": "Amazon Aurora MySQL"}],
                  "edges": [["web", "app"], ["app", "db"]]}
    },
    "6": {
      "subsections": [
        {"heading": "Move to Containers",
         "narrative": "Compute currently runs on unmanaged EC2 instances with no container definitions found, triggering this pathway. Containerizing onto Amazon ECS on Fargate removes the operational burden of patching EC2 hosts. [[finding:Modernization Readiness Analysis]] [[doc:https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html]]",
         "table": {"headers": ["Recommended AWS Service", "Role"],
                    "rows": [["Amazon ECS on Fargate", "Container orchestration, no host management"], ["Amazon ECR", "Container image registry"]]}},
        {"heading": "Move to Managed Databases",
         "narrative": "MySQL is self-managed on EC2 with no managed failover, triggering this pathway. Amazon Aurora MySQL-Compatible Edition supports in-place logical replication from self-managed MySQL. [[finding:Modernization Readiness Analysis]] [[doc:https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Migrating.html]]",
         "table": {"headers": ["Recommended AWS Service", "Role"],
                    "rows": [["Amazon Aurora MySQL-Compatible Edition", "Managed, EOL-free relational database"], ["AWS DMS", "Migration tooling for near-zero-downtime cutover"]]}}
      ],
      "tables": [
        {"title": "AWS Modernization Pathways", "headers": ["Pathway", "Status", "Priority", "Est. Effort"],
         "rows": [["Move to Cloud Native", "Not Triggered", "-", "-"], ["Move to Containers", "Triggered", "Medium", "Medium"], ["Move to Open Source", "Not Applicable", "-", "-"], ["Move to Managed Databases", "Triggered", "High", "Medium"], ["Move to Managed Analytics", "Not Triggered", "-", "-"], ["Move to Modern DevOps", "Not Triggered", "-", "-"], ["Move to AI", "Not Triggered", "-", "-"]]}
      ]
    },
    "7": {
      "subsections": [
        {"heading": "Phase 1: Quick Wins", "narrative": "Containerize the monolith as-is and move MySQL to Aurora before any code changes, retiring both EOL components first. [[finding:Comprehensive Codebase Analysis]]"},
        {"heading": "Phase 2: Core Platform Migration", "narrative": "Externalize session state to Amazon ElastiCache so the app tier can scale horizontally. [[doc:https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/WhatIs.html]]"},
        {"heading": "Phase 3: Optimization", "narrative": "Right-size Fargate task definitions and introduce auto-scaling once real traffic patterns are observed on AWS. [[finding:Modernization Readiness Analysis]]"}
      ],
      "tables": [{"title": "Migration Roadmap", "headers": ["Phase", "Focus", "Key Activities", "Dependencies"],
                  "rows": [["Phase 1", "Quick Wins", "Containerize app, migrate DB to Aurora", "None"],
                            ["Phase 2", "Core Platform Migration", "Externalize session state to ElastiCache", "Phase 1 complete"],
                            ["Phase 3", "Optimization", "Auto-scaling, cost tuning", "Phase 2 complete"]]}],
      "chart": {"type": "roadmap_timeline", "title": "Migration Roadmap",
                "phases": [{"name": "Quick Wins", "order": 1}, {"name": "Core Platform Migration", "order": 2}, {"name": "Optimization", "order": 3}]}
    },
    "8": {
      "subsections": [{"heading": "Overview", "narrative": "Note: this is a directional estimate based on codebase analysis findings, not a formal cost model or priced TCO analysis. Retiring self-managed MySQL in favor of Aurora removes dedicated database-host patching effort. [[finding:Modernization Readiness Analysis]]"}],
      "tables": [{"title": "Cost Driver Comparison", "headers": ["Cost Driver", "Current State", "Post-Migration Expectation", "Directional Impact"],
                  "rows": [["Database operations", "Self-managed MySQL host + DBA patching time", "Managed Aurora, automated patching", "Lower"]]}]
    },
    "9": {
      "subsections": [{"heading": "Overview", "narrative": "Note: this is a directional estimate based on codebase analysis findings, not a benchmark or load-tested projection. Removing in-process session state is expected to improve horizontal scalability under load. [[finding:Modernization Readiness Analysis]]"}]
    },
    "10": {
      "subsections": [{"heading": "Migration Risks", "narrative": "The 4-step synchronous checkout workflow has no documented rollback behavior, which is a risk during cutover testing. [[finding:Business Rules Extraction]]"}],
      "tables": [{"title": "Risk Register", "headers": ["Risk", "Likelihood", "Impact", "Mitigation"],
                  "rows": [["Undocumented checkout rollback behavior", "Medium", "High", "Add explicit integration tests for checkout failure paths before cutover"]]}]
    }
  },
  "executive_summary": "3-5 sentences for a CTO/VP audience: current-state pain points (including any material security/compliance findings from section 3, and the target architecture from section 5), recommended direction, headline benefits. No citation tag needed on the executive summary itself."
}

Section 3 (Security & Compliance Findings) is drafted by your colleague in the OTHER agent
call (the codebase-grounded one) - do NOT include it here, it's not yours to draft, but you
MAY reference its headline findings in the executive summary since you're given the
codebase-facing sections as context. Section 5 (Recommended To-Be Architecture) IS your
responsibility and must always be included with real subsections and a real diagram per the
guidance above. Base every claim strictly on the findings or documentation you actually
looked up; do not invent detail not present in either."""

STRATEGY_SECTIONS_PROMPT = (
    _STRATEGY_INTRO + _CITATION_RULES_CORE
    + "\n\n" + _STRUCTURED_DATA_RULES + "\n\n" + _REAL_DIAGRAM_RULES
    + _STRATEGY_SECTION_GUIDANCE + _STRATEGY_WORKED_EXAMPLE
)


def _read_findings(output_dir: str) -> str:
    """Concatenate an analysis TD's output files into one capped text blob."""
    if not output_dir or not os.path.isdir(output_dir):
        return ""
    chunks = []
    for pattern in ("**/*.md", "**/*.json"):
        for path in glob.glob(os.path.join(output_dir, pattern), recursive=True):
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        rel = os.path.relpath(path, output_dir)
                        chunks.append(f"--- {rel} ---\n{f.read()}")
                except Exception:
                    continue
    return "\n\n".join(chunks)[:MAX_FINDINGS_CHARS]


def _extract_agent_text(result: Any) -> str:
    """Pull the final assistant text out of a Strands AgentResult, defensively."""
    if hasattr(result, 'message'):
        msg = result.message
        if isinstance(msg, dict) and isinstance(msg.get('content'), list):
            return "".join(
                block.get('text', '') for block in msg['content'] if isinstance(block, dict)
            ).strip()
        return str(msg).strip()
    if hasattr(result, 'content'):
        return str(result.content).strip()
    return str(result).strip()


def _parse_json_response(raw_text: str) -> Dict[str, Any]:
    raw_text = raw_text.strip()
    if '```' in raw_text:
        raw_text = raw_text.split('```')[1]
        if raw_text.startswith('json'):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    return json.loads(raw_text)


def _make_bedrock_model(max_tokens: int = 8192) -> BedrockModel:
    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    return BedrockModel(model_id=model_id, region_name=region, temperature=0.3, max_tokens=max_tokens)


def _normalize_table(table: Any):
    if not isinstance(table, dict):
        return None
    headers, rows = table.get("headers"), table.get("rows")
    if not isinstance(headers, list) or not headers:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    clean_rows = [[str(cell) for cell in row] for row in rows if isinstance(row, list)]
    if not clean_rows:
        return None
    normalized = {"headers": [str(h) for h in headers], "rows": clean_rows}
    if table.get("title"):
        normalized["title"] = str(table["title"])
    return normalized


def _normalize_section(section: Any, section_key: str) -> Dict[str, Any]:
    """
    Coerce one section's LLM-produced JSON into the shape build_docx expects,
    dropping malformed pieces (with a warning) rather than letting one bad
    field crash the whole report. A nested schema is more prone to partial
    LLM non-compliance than a flat string was.
    """
    if isinstance(section, str):
        return {"subsections": [{"heading": "", "narrative": section}]}
    if not isinstance(section, dict):
        logger.warning(f"Section {section_key}: unexpected type {type(section)}, dropping")
        return {"subsections": []}

    subsections = []
    for sub in (section.get("subsections") or []):
        if not isinstance(sub, dict) or not sub.get("narrative"):
            logger.warning(f"Section {section_key}: dropping malformed subsection {sub!r}")
            continue
        clean_sub = {"heading": str(sub.get("heading", "")), "narrative": str(sub["narrative"])}
        table = _normalize_table(sub.get("table"))
        if table:
            clean_sub["table"] = table
        subsections.append(clean_sub)
    if not subsections:
        logger.warning(f"Section {section_key}: no usable subsections")

    normalized: Dict[str, Any] = {"subsections": subsections}

    tables = [t for t in (_normalize_table(t) for t in (section.get("tables") or [])) if t]
    if tables:
        normalized["tables"] = tables

    chart = section.get("chart")
    if isinstance(chart, dict) and chart.get("type") in ("scorecard_bar", "roadmap_timeline"):
        normalized["chart"] = chart
    elif chart:
        logger.warning(f"Section {section_key}: dropping malformed/unknown chart {chart!r}")

    diagram = section.get("diagram")
    if isinstance(diagram, dict) and isinstance(diagram.get("nodes"), list) and diagram["nodes"]:
        normalized["diagram"] = diagram
    elif diagram:
        logger.warning(f"Section {section_key}: dropping malformed/empty diagram {diagram!r}")

    return normalized


def _synthesize_sections(findings_by_repo: Dict[str, Dict[str, str]], context: str) -> Dict[str, Any]:
    findings_text = ""
    for repo, findings in findings_by_repo.items():
        findings_text += f"\n\n=== Repository: {repo} ===\n"
        for label, text in findings.items():
            findings_text += f"\n[{label}]\n{text or '(no output captured)'}\n"

    # --- Call 1: codebase-grounded sections, structurally NO tools ---
    # Section 2's 9-subsection design (each with its own table) makes this call's JSON
    # output substantially larger than the strategy call's - give it more room so a long
    # response doesn't get cut off mid-JSON (which would fail _parse_json_response outright).
    codebase_agent = Agent(model=_make_bedrock_model(max_tokens=16000), system_prompt=CODEBASE_SECTIONS_PROMPT, tools=[])
    codebase_result = codebase_agent(f"Findings:{findings_text}")
    codebase_text = _extract_agent_text(codebase_result)
    codebase_json = _parse_json_response(codebase_text)
    codebase_trace = extract_tool_trace(codebase_agent)  # expected to always be empty

    # --- Call 2: AWS-strategy sections, WITH MCP tools ---
    strategy_prompt = (
        f"Engagement context: {context}\n\nFindings:{findings_text}\n\n"
        f"Already-drafted codebase-facing sections (for context, do not re-cite these as "
        f"[[doc:...]]):{json.dumps(codebase_json)}"
    )
    try:
        with _aws_docs_mcp_client:
            mcp_tools = _aws_docs_mcp_client.list_tools_sync()
            strategy_agent = Agent(model=_make_bedrock_model(), system_prompt=STRATEGY_SECTIONS_PROMPT, tools=mcp_tools)
            strategy_result = strategy_agent(strategy_prompt)
            strategy_trace = extract_tool_trace(strategy_agent)
    except Exception as e:
        logger.warning(f"AWS Documentation MCP server unavailable, falling back to ungrounded synthesis: {e}")
        strategy_agent = Agent(model=_make_bedrock_model(), system_prompt=STRATEGY_SECTIONS_PROMPT, tools=[])
        strategy_result = strategy_agent(strategy_prompt)
        strategy_trace = []

    strategy_text = _extract_agent_text(strategy_result)
    strategy_json = _parse_json_response(strategy_text)

    # --- Groundedness verification runs against RAW (un-normalized) JSON - citation
    # tags inside a "narrative" string survive JSON encoding regardless of nesting. ---
    codebase_ground = verify_citations(json.dumps(codebase_json), FINDING_LABELS, codebase_trace)
    strategy_ground = verify_citations(json.dumps(strategy_json), FINDING_LABELS, strategy_trace)

    # --- Normalize, then strip citation tags from narrative text only; tables/chart/
    # diagram data pass through untouched (citation tags never expected there). ---
    merged_sections_raw = {**codebase_json.get('sections', {}), **strategy_json.get('sections', {})}
    clean_sections = {}
    all_doc_urls = set()
    for key, raw_section in merged_sections_raw.items():
        section = _normalize_section(raw_section, key)
        for sub in section["subsections"]:
            clean_text, doc_urls = strip_citations_collect_sources(sub["narrative"])
            sub["narrative"] = clean_text
            all_doc_urls.update(doc_urls)
        clean_sections[key] = section

    return {
        "client_name": strategy_json.get("client_name", ""),
        "sections": clean_sections,
        "executive_summary": strip_citations(strategy_json.get("executive_summary", "")),
        "sources": sorted(all_doc_urls),
        "groundedness": {
            "codebase_sections_citations": codebase_ground["total"],
            "codebase_sections_unverified": codebase_ground["unverified"],
            "strategy_sections_citations": strategy_ground["total"],
            "strategy_sections_unverified": strategy_ground["unverified"],
            "aws_doc_tool_calls_made": len(strategy_trace),
        },
    }


@tool
def generate_assessment_report(query: str) -> Dict[str, Any]:
    """
    Cross-references completed analysis results for one or more repositories and
    produces the due-diligence DOCX (Executive Summary + 10 sections, each with
    subsections, and tables/charts/diagrams for sections that have supporting data).
    Call this AFTER codebase_analysis_agent, modernization_readiness_agent,
    business_rules_agent, AND security_compliance_agent have all returned success.
    Returns a groundedness summary alongside the report path - if it lists any
    unverified citations, flag the report for human review before sending it to the client.

    Args:
        query: Natural language naming each repo and the 4 output_dir paths its analyses
            returned, plus any client context, e.g. "Generate the report for repo
            'acme-api': codebase output at /tmp/atx-assessments/codebase-acme-api-.../repo,
            readiness output at .../readiness-acme-api-.../repo, business rules output at
            .../bizrules-acme-api-.../repo, security output at .../security-acme-api-.../findings.
            Client: Acme Corp, industry: healthcare."
    """
    logger.info("SYNTHESIS/REPORT AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"repos": [{"name": "repo-name", "codebase_output_dir": "", "readiness_output_dir": "", "business_rules_output_dir": "", "security_output_dir": ""}],
 "client_name": "", "context": "industry/compliance/other free-text engagement context"}""")
        repos = params.get('repos', [])
        if not repos:
            return {"status": "error", "error": "Could not extract repo/output_dir info from the request."}

        findings_by_repo = {}
        for r in repos:
            name = r.get('name', 'repo')
            findings_by_repo[name] = {
                "Comprehensive Codebase Analysis": _read_findings(r.get('codebase_output_dir', '')),
                "Modernization Readiness Analysis": _read_findings(r.get('readiness_output_dir', '')),
                "Business Rules Extraction": _read_findings(r.get('business_rules_output_dir', '')),
                "Security & Compliance Analysis": _read_findings(r.get('security_output_dir', '')),
            }

        sections_result = _synthesize_sections(findings_by_repo, params.get('context', ''))
        sections_result['repos'] = list(findings_by_repo.keys())
        if params.get('client_name'):
            sections_result['client_name'] = params['client_name']

        groundedness = sections_result.pop('groundedness')
        if groundedness['codebase_sections_unverified'] or groundedness['strategy_sections_unverified']:
            logger.warning(f"Unverified citations in generated report: {groundedness}")

        job_name = f"report-{'-'.join(findings_by_repo.keys())[:40]}-{int(datetime.utcnow().timestamp())}"
        output_path = os.path.join(WORKSPACE_ROOT, "reports", f"{job_name}.docx")
        build_docx(sections_result, output_path)

        return {"status": "success", "result": json.dumps({
            "report_path": output_path,
            "client_name": sections_result.get('client_name', ''),
            "repos": sections_result['repos'],
            "groundedness": groundedness,
        })}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Failed to parse synthesis output: {e}"}
    except Exception as e:
        logger.error(f"generate_assessment_report failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
