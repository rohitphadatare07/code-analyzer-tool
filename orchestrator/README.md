# ATX Assessment Orchestrator

Bedrock AgentCore agent that runs **read-only technical due-diligence analysis** on
prospective-client repositories using the ATX CLI, then synthesizes the findings into a
consulting-grade DOCX report. This orchestrator never modifies, executes, builds, tests, or
pushes changes to client source code, and never opens pull requests — see the golden rules
in `CLAUDE.md` at the repo root.

## Architecture

The orchestrator is a Strands `Agent` with 7 tools, all agents-as-tools style (each takes a
single free-text `query` string — see "Why one string arg?" below):

```
Orchestrator (agent.py)
├── codebase_analysis_agent          → AWS/comprehensive-codebase-analysis (1 repo)
├── modernization_readiness_agent    → AWS/modernization-readiness-analysis (1 repo)
├── business_rules_agent             → AWS/business-rules-extraction (1 repo)
├── security_compliance_agent        → native osv-scanner + detect-secrets scan (1 repo, no TD)
├── generate_assessment_report       → synthesizes all 4 into the due-diligence DOCX
├── list_output_files                → list a completed analysis's output files
└── read_output_file                 → read a specific output file's contents
```

**No AWS Batch.** Each of the 3 TD-backed analysis tools runs synchronously: it `git clone`s
the target repo to local disk, then runs `atx custom def exec -n <TD> -p <repo> -x -t` as a
subprocess on whatever host `agent.py` itself is running on, and returns the result directly
— there's no async job queue or status-polling step. See `tools/assessmenttransform.py`.
`security_compliance_agent` (`tools/security_analysis.py`) follows the same clone-then-scan
shape, but has no TD to invoke — no scanning TD exists for this — so it shells out directly
to `osv-scanner` (dependency/CVE) and `detect-secrets` (secrets detection) against the clone.

`generate_assessment_report` (`tools/synthesis.py`) runs as **two separate agent calls**:
1. A codebase-grounded call (report sections 1/2/3/4) given **zero tools** — structurally
   incapable of citing anything but the analysis findings it's handed.
2. A strategy call (sections 5-10 + executive summary) given the official **AWS
   Documentation MCP server**'s tools (`search_documentation`, `read_documentation`,
   `recommend`, launched via `uvx`), so AWS service/migration recommendations are grounded
   in real documentation, not just model training knowledge.

Every factual sentence inside a section's narrative text must carry an inline citation tag
(`[[finding:...]]` / `[[doc:...]]`) — never inside a table cell, chart value, or diagram
node/edge — which `tools/grounding.py` verifies mechanically against that call's actual
tool-call trace before the report is rendered. `build_docx.py` strips the tags for display
and lists cited AWS doc URLs as a "Sources" appendix. Every section is now a real synthesized
section — section 3 (Security & Compliance) is fed by `security_compliance_agent`'s scan
output (methodology adapted from the [OWASP Secure Agent Playbook](https://github.com/OWASP/secure-agent-playbook),
CC-BY-4.0 — see `tools/security_analysis.py`'s module docstring for exactly what was reused
vs. reimplemented), and section 5 (Recommended To-Be Architecture) is **always** synthesized
regardless of whether the engagement covers one repository or many — it is no longer gated
behind the removed cross-repo portfolio TD; for multiple repositories the strategy call
produces one consolidated architecture spanning all of them.

Every analysis TD is instructed to emit any diagram it produces as a fenced ```mermaid```
code block — `assessmenttransform.py`'s `MERMAID_DIAGRAM_INSTRUCTION` is appended to all 3
TDs' `additionalPlanContext` on every run, not just `comprehensive-codebase-analysis`. The
current-architecture diagram (section 1) and to-be-architecture diagram (section 5) are then
built by extracting that real Mermaid diagram from the findings (whichever source it turns
up in) and translating it faithfully into the render schema — not invented fresh from a
prose summary. The prompt only falls back to inventing one from prose if no such diagram
exists anywhere in the findings (e.g. the installed TD version doesn't honor the instruction).

Each section is broken into subsections (heading + narrative + optional table), not one
prose blob, and specific sections carry section-level tables/charts/diagrams (`tools/visuals.py`):
a Modernization Readiness scorecard bar chart (section 4, reusing the readiness TD's own
`report.json` scores rather than inventing them), a Migration Roadmap timeline chart
(section 7), and Graphviz boxes-and-arrows architecture diagrams for the current (section 1)
and recommended to-be (section 5, always produced regardless of repo count) architecture.
Any chart/diagram/table without real supporting data in the findings is omitted, not
fabricated (section 5's diagram/narrative is the one exception — it's mandatory), and a
missing Graphviz `dot` binary just skips that one diagram rather than failing the whole report.

### Why one string arg?

Every tool function takes a single `query: str` and does its own internal Bedrock call to
extract structured parameters, instead of exposing multiple typed `@tool` parameters. This
works around a Strands streaming bug (`streaming.py` does `str += int` when a tool-call
`input` contains a non-string value) — see the monkey-patch at the top of `agent.py`. Keep
new tools consistent with this pattern.

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator: system prompt, tool wiring, AgentCore entrypoint |
| `tools/assessmenttransform.py` | The 3 TD-backed per-repo analysis tools + `list_output_files`/`read_output_file` |
| `tools/security_analysis.py` | `security_compliance_agent` — native osv-scanner/detect-secrets scan, no TD |
| `tools/synthesis.py` | `generate_assessment_report` — the two-call synthesis described above |
| `tools/build_docx.py` | Pure DOCX rendering (no AWS calls) — Executive Summary + 10 sections (each with subsections/tables/charts/diagrams) + Sources appendix |
| `tools/visuals.py` | Pure chart (matplotlib) and diagram (Graphviz) rendering to PNG — every function degrades to `None` on failure instead of raising |
| `tools/grounding.py` | Citation extraction, tool-call trace capture, mechanical groundedness verification |
| `tools/memory_client.py` | AgentCore Memory client |
| `tools/memory_hooks.py` | Short-term memory hooks |
| `requirements.txt` | Python dependencies |

## Prerequisites

Whatever host actually runs `agent.py` (local machine, EC2, or the container built from
`Dockerfile`) needs, beyond the pip packages in `requirements.txt`:

- **`git`** — analysis tools clone the target repo locally before running `atx` against it.
- **`atx` CLI** — installed and on `PATH`, with the 3 analysis transformation definitions
  (`AWS/comprehensive-codebase-analysis`, `AWS/modernization-readiness-analysis`,
  `AWS/business-rules-extraction`) registered/available.
- **`uvx`** — provided by the `uv` package (already in `requirements.txt`), used to launch
  the AWS Documentation MCP server on demand. Needs outbound internet access to
  `docs.aws.amazon.com`.
- **Graphviz (`dot` binary)** — required for the architecture diagrams (section 1's
  current-architecture diagram, section 5's recommended to-be architecture diagram). Install via
  `apt-get install graphviz` / `brew install graphviz` / the Windows installer, then confirm
  `dot -V` is on PATH. The `graphviz` pip package (in `requirements.txt`) only wraps this
  binary, it doesn't bundle it. If `dot` isn't found, that one diagram is skipped with a
  logged warning rather than failing the whole report (`tools/visuals.py`).
- **`osv-scanner` binary** — required for section 3's dependency/CVE scan
  (`security_compliance_agent`). A single static Go binary from the
  [google/osv-scanner](https://github.com/google/osv-scanner) project. Not pip-installable.
  Verified this session against a real v2.4.0 binary (downloaded and run against a dummy
  vulnerable `package.json`/`package-lock.json`) — confirms the exact command shape
  `security_analysis.py` uses: `osv-scanner scan source -r --format json <path>` (the
  `scan source` subcommand is required; a bare `osv-scanner --format json -r <path>` is
  invalid syntax). Install options, per the project's own docs:
  - **Linux/macOS/Windows, prebuilt binary** (recommended): download from the
    [releases page](https://github.com/google/osv-scanner/releases/latest), e.g.
    `osv-scanner_windows_amd64.exe` / `osv-scanner_linux_amd64` / `osv-scanner_darwin_amd64`,
    put it on `PATH` (rename to `osv-scanner`/`osv-scanner.exe`).
  - **Homebrew** (macOS/Linux): `brew install osv-scanner`
  - **Scoop** (Windows): `scoop install osv-scanner`
  - **WinGet** (Windows): `winget install Google.OSVScanner`
  - **Arch Linux**: `pacman -S osv-scanner` · **Alpine**: `apk add osv-scanner`
  - **Go install** (needs Go 1.26.2+): `go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest`
  - **Docker**: `ghcr.io/google/osv-scanner` (e.g. for ad-hoc testing outside the orchestrator's
    own subprocess call: `docker run -v ${PWD}:/src ghcr.io/google/osv-scanner scan source /src`)
  If the binary is missing, the dependency/CVE scan is skipped with a warning, not a failure.
- **`detect-secrets`** — required for section 3's secrets scan. Installed via
  `requirements.txt` (pip), which also creates the `detect-secrets` console script this tool
  shells out to — no separate binary download needed, unlike the two above. If missing (or
  the scan otherwise fails), falls back to a small set of regex patterns automatically.
  **Note**: this integration's file/directory-scan behavior (`detect-secrets scan
  --all-files <path>`) was not successfully reproduced end-to-end in a Windows sandbox test
  during development — the detector logic itself was confirmed working (`detect-secrets scan
  --string "..."` correctly flags known patterns), but the file-scanning path returned no
  results even for an unambiguous private-key header, for reasons not fully root-caused
  (`detect-secrets` is primarily built/tested for Linux CI environments, which is what this
  orchestrator actually runs on — plausibly a Windows-specific quirk, not a real bug, but
  unconfirmed). **Verify this actually detects secrets on the real Linux deployment target
  before trusting it.**
- **AWS credentials** with at least `bedrock:InvokeModel` (env vars, `~/.aws/credentials`,
  or an instance role).

> **Known gap:** `Dockerfile` in this directory currently installs only the Python
> dependencies — it does **not** install `git`, the `atx` CLI, Graphviz's `dot` binary, or
> `osv-scanner`. A container built from it as-is can hold a conversation but will fail with
> `FileNotFoundError` on any actual repo analysis, and will silently skip both architecture
> diagrams and the dependency/CVE scan. Fix this before relying on the Docker path (see
> `scaled-execution-containers/container/Dockerfile` for the `atx` install command to copy
> over; Graphviz and `osv-scanner` are a standard apt/distro package and a downloadable
> release binary, respectively).

## Local Development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export AWS_REGION=us-east-1
export BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0

python agent.py   # Runs on http://localhost:8080
```

**Different port:** set `PORT` before starting — `export PORT=9000 && python agent.py`.
Confirmed against the installed SDK: `BedrockAgentCoreApp.run(port: int = 8080, host=None,
**kwargs)`, which `agent.py`'s `__main__` block passes through from the `PORT` env var
(default `8080`) — same pattern as `AWS_REGION`/`BEDROCK_MODEL_ID`. Via Docker:
`docker run -e PORT=9000 -p 9000:9000 atx-orchestrator`.

Invoke it directly (the AgentCore local dev server follows the SageMaker-style container
contract: `POST /invocations`, `GET /ping` — confirmed from the installed SDK's route table):

```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Run the full assessment on https://github.com/someorg/somerepo. Client: Acme Corp, industry: healthcare, compliance: HIPAA. Once all four analyses succeed, generate the due-diligence report."
  }'
```

There's no structured "repo" field — every tool's `query` is free text, and the model
extracts the repo URL / client name / context itself. The orchestrator's own prompt tells it
to run all 4 analyses per repo, then call `generate_assessment_report` once they succeed.

## Deploy (Bedrock AgentCore Runtime)

```bash
pip install bedrock-agentcore strands-agents boto3 pyyaml bedrock-agentcore-starter-toolkit

agentcore configure -e agent.py -n atx_transform_orchestrator -r us-east-1 -ni \
  --deployment-type direct_code_deploy --runtime PYTHON_3_11 -rf requirements.txt
agentcore deploy --auto-update-on-conflict
```

```bash
agentcore invoke '{"prompt": "Analyze https://github.com/user/repo for tech debt and architecture."}'
```

Note: `agentcore deploy`'s direct-code-deploy path packages this directory's Python code into
a managed AgentCore Runtime container — it is a **different** build path than `Dockerfile`,
and it's not yet confirmed whether that managed runtime image includes `git`/`atx`/`uvx`
either. Verify the prerequisites above hold in whichever deploy path you actually use before
trusting analysis results from it.
