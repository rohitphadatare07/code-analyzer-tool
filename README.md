# codegrapher-mcp

An MCP server that exposes [CodeGrapher's](https://github.com/anthropics) static codebase analysis as tools — tree-sitter AST extraction, NetworkX graph assembly, Leiden community detection, centrality analysis, and deterministic Mermaid diagram generation.

**Pure analysis.** No LLM calls. No agent loop. Each tool is deterministic and returns structured JSON.

## What this gives you

A stateless-looking, tool-based interface to CodeGrapher's analysis pipeline. The original CodeGrapher project ran a LangGraph agent that orchestrated these same primitives via Anthropic/OpenAI/Bedrock LLMs. This server strips all that away and lets the *consumer* (Claude Desktop, Kiro, Cursor, your own LangGraph agent, AWS Transform via `~/.aws/atx/mcp.json`, etc.) decide which tools to call and in what order.

## Tools

| Tool | What it does |
|---|---|
| `scan_repository` | File counts by extension, word counts, directory tree |
| `detect_files` | Classify files into code/docs/config/data/build buckets |
| `extract_ast_graph` | Tree-sitter AST extraction across 15+ languages |
| `cluster_communities` | Leiden/Louvain community detection with cohesion scores |
| `find_god_nodes` | Top-N most-connected nodes (centrality hubs) |
| `find_surprising_connections` | Cross-community edges that suggest hidden coupling |
| `suggest_investigation_questions` | Graph-derived prompts worth investigating |
| `generate_mermaid_diagrams` | Deterministic architecture / flow / component diagrams |
| `generate_pdf_report` | Render an assessment PDF — graph data always, narrative optional |
| `load_aws_transform_output` | Parse AWS Transform comprehensive-codebase-analysis output (S3 or local) |
| `generate_merged_report` | Fuse AWS Transform output with CodeGrapher's graph analysis into one PDF |
| `read_file` | Path-traversal-safe single-file read |
| `search_code` | Regex grep across the repo |
| `get_graph_stats` | Summary stats: nodes, edges, density, kinds, top communities |
| `reset_cache` | Drop cached analysis for a repo |

### Lazy upstream evaluation

Every tool takes a `repo_path` and is independent. You can call `cluster_communities` without first calling `extract_ast_graph` — the server runs upstream stages on demand and caches the results per repo. Subsequent calls reuse cached extraction, graph, and clusters within the same session.

When the repo changes on disk, call `reset_cache` to force re-analysis.

## Install

```bash
pip install -e .
```

For better community detection (Leiden vs. fallback Louvain):

```bash
pip install -e ".[leiden]"
```

## Run

```bash
codegrapher-mcp
```

Or:

```bash
python -m codegrapher_mcp.server
```

The server speaks MCP over stdio.

## Wire it up

### Claude Desktop / Kiro / Cursor

`~/.config/claude/claude_desktop_config.json` (or equivalent):

```json
{
  "mcpServers": {
    "codegrapher": {
      "command": "codegrapher-mcp"
    }
  }
}
```

### AWS Transform CLI

`~/.aws/atx/mcp.json`:

```json
{
  "mcpServers": {
    "codegrapher": {
      "command": "codegrapher-mcp"
    }
  }
}
```

Verify with `atx mcp tools -s codegrapher`.

## Example tool use

```jsonc
// 1. Inventory the repo
{ "tool": "scan_repository", "args": { "repo_path": "/path/to/repo" } }

// 2. Build the graph (extraction runs lazily inside)
{ "tool": "get_graph_stats", "args": { "repo_path": "/path/to/repo" } }

// 3. Find architectural hubs
{ "tool": "find_god_nodes", "args": { "repo_path": "/path/to/repo", "top_n": 15 } }

// 4. Surface hidden coupling
{ "tool": "find_surprising_connections", "args": { "repo_path": "/path/to/repo" } }

// 5. Get diagrams
{ "tool": "generate_mermaid_diagrams", "args": { "repo_path": "/path/to/repo" } }

// 6. Render the PDF — narrative is optional
{
  "tool": "generate_pdf_report",
  "args": {
    "repo_path": "/path/to/repo",
    "output_path": "/path/to/report.pdf",
    "narrative": {
      "purpose": "What this repo does in one line",
      "summary": "Three-to-five-sentence executive summary",
      "architecture_style": "MVC / microservices / library / CLI tool",
      "tech_stack": ["Python", "FastAPI", "PostgreSQL"],
      "key_components": [
        {
          "name": "auth",
          "description": "Token-based authentication",
          "files": ["src/auth/"],
          "responsibilities": ["Login", "JWT issuance"]
        }
      ]
    }
  }
}
```

## PDF report — what's deterministic vs. caller-supplied

`generate_pdf_report` produces a 17-section A4 PDF. The split:

**Always populated** (deterministic, computed from the graph):
- §10 God Nodes & Surprising Connections
- §13 Repository File Tree
- All embedded Mermaid diagrams (architecture, flow, components)
- The Analysis Trace appendix (graph metrics, community count, etc.)

**Populated from caller's `narrative` dict** (or empty if absent):
- §1 Executive Summary, §2 Tech Stack, §3 Architecture Overview
- §7 Key Components, §8 API Endpoints, §9 Code Quality & Security
- §11 File-by-File Findings, §12 Improvement Recommendations

The MCP server itself never invokes an LLM. Whoever calls the tool decides what narrative to provide — handwritten, AWS Transform's output, your own LangGraph agent's findings, or nothing at all.

## AWS Transform integration (Pattern A)

Two tools fuse AWS Transform output with CodeGrapher's graph analysis.

```jsonc
// 1. Parse AWS Transform output to see what's there
{
  "tool": "load_aws_transform_output",
  "args": {
    "aws_output_path": "s3://atx-custom-output-1234/transformations/myjob/202604010000abc-def/"
    // or a local path: "/data/aws-output/"
  }
}
// Returns: { sections_found: {...}, missing_sections: [...], unparsed_files: [...] }

// 2. Generate a merged report
{
  "tool": "generate_merged_report",
  "args": {
    "repo_path": "/path/to/repo",
    "aws_output_path": "/data/aws-output/",
    "output_path": "/path/to/merged.pdf",
    "verify_business_rules": true
  }
}
```

### What each side contributes

| Section | Source | Why |
|---|---|---|
| Executive Summary | **AWS** | LLM-summarized prose is AWS's strength |
| Tech Stack | **AWS** | Classification task |
| Architecture Style | **AWS** | LLM does this well |
| Key Components | **AWS** labels + CodeGrapher membership | AWS describes; graph grounds the file lists |
| Data Flow | **AWS** | Plain-language is AWS's strength |
| API Endpoints / DB Models | **AWS** | Behavioral analysis output |
| Business Rules | **Merged + verified** | AWS extracts; CodeGrapher anchors each rule to actual call paths. Unverifiable rules flagged for review |
| God Nodes / Surprising Connections | **CodeGrapher** | AWS doesn't compute centrality |
| Communities & Cohesion | **CodeGrapher** | Leiden output |
| Mermaid Diagrams | **CodeGrapher** | Deterministic from graph |
| Improvement Recommendations | **AWS** + CodeGrapher cross-reference | High-debt + high-centrality files surface as priority items |
| File Tree | **CodeGrapher** | Filesystem walk |

### Business rule verification

When `verify_business_rules: true`, CodeGrapher attempts to anchor each
AWS-extracted business rule to actual nodes in the code graph. Rules
referencing functions or files that don't exist in the graph are flagged
as `unverified` — they may be LLM hallucinations from AWS's behavioral
analysis layer. This is the most defensible piece of the merge: an
independent cross-check of AWS's automated-reasoning claims.

### S3 vs. local

The AWS tools accept either:
- **Local directory** — already-downloaded output. Tool reads it directly.
- **`s3://bucket/prefix/` URL** — tool downloads via boto3 to a temp directory,
  then parses. Requires `pip install boto3` and AWS credentials.

### Schema lenience

AWS does not publish a file-level schema for comprehensive-codebase-analysis
output. The parser scans the directory for known section names
(executive_summary, technical_debt_report, architecture, code_analysis,
domains, components, behavior, dependencies, recommendations) and accepts
JSON or Markdown. Files it doesn't recognize are listed in `unparsed_files`
so you can see what was skipped. Sections expected but absent are listed
in `missing_sections`. Adapt `_SECTION_PATTERNS` and the per-section parsers
in `aws_transform.py` when you have real output to match against.

## What was removed vs. CodeGrapher

| Removed | Why |
|---|---|
| `codegrapher/agent/` | Whole LangGraph agent (graph, providers, state, tools) |
| `codegrapher/__main__.py` | CLI driver for the agent |
| `codegrapher/pipeline.py` (original) | Agent orchestration glue |
| `record_finding`, `finish_analysis` tools | LLM-only state mutators |
| `langchain-*`, `langgraph`, `boto3`, `langchain-aws` | LLM provider deps |

## What was kept

- `codegrapher_mcp/core/` — full graphify analysis library:
  `detect`, `extract`, `build`, `cluster`, `analyze`, `cache`, `security`, `validate`
- `codegrapher_mcp/output/mermaid_converter.py` — deterministic diagrams
- `codegrapher_mcp/output/diagrams.py` — diagram rendering helpers
- `codegrapher_mcp/output/pdf_generator.py` — PDF rendering (now invoked
  as a tool with caller-supplied narrative; agent-specific labels relabelled)

## License

MIT
