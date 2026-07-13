# ATX Assessment Orchestrator

Bedrock AgentCore agent that runs **read-only technical due-diligence analysis** on
prospective-client repositories using the ATX CLI, then synthesizes the findings into a
consulting-grade DOCX report. This orchestrator never modifies, executes, builds, tests, or
pushes changes to client source code, and never opens pull requests — see the golden rules
in `CLAUDE.md` at the repo root.

## Architecture

The orchestrator is a Strands `Agent` with 6 tools, all agents-as-tools style (each takes a
single free-text `query` string — see "Why one string arg?" below):

```
Orchestrator (agent.py)
├── codebase_analysis_agent          → AWS/comprehensive-codebase-analysis (1 repo)
├── modernization_readiness_agent    → AWS/modernization-readiness-analysis (1 repo)
├── business_rules_agent             → AWS/business-rules-extraction (1 repo)
├── generate_assessment_report       → synthesizes all 3 into the due-diligence DOCX
├── list_output_files                → list a completed analysis's output files
└── read_output_file                 → read a specific output file's contents
```

**No AWS Batch.** Each analysis tool runs synchronously: it `git clone`s the target repo to
local disk, then runs `atx custom def exec -n <TD> -p <repo> -x -t` as a subprocess on
whatever host `agent.py` itself is running on, and returns the result directly — there's no
async job queue or status-polling step. See `tools/assessmenttransform.py`.

`generate_assessment_report` (`tools/synthesis.py`) runs as **two separate agent calls**:
1. A codebase-grounded call (report sections 1/2/4) given **zero tools** — structurally
   incapable of citing anything but the analysis findings it's handed.
2. A strategy call (sections 6-10 + executive summary) given the official **AWS
   Documentation MCP server**'s tools (`search_documentation`, `read_documentation`,
   `recommend`, launched via `uvx`), so AWS service/migration recommendations are grounded
   in real documentation, not just model training knowledge.

Every factual sentence in both calls must carry an inline citation tag
(`[[finding:...]]` / `[[doc:...]]`), which `tools/grounding.py` verifies mechanically against
that call's actual tool-call trace before the report is rendered. `build_docx.py` strips the
tags for display and lists cited AWS doc URLs as a "Sources" appendix. Report sections 3
(Security & Compliance) and 5 (Recommended To-Be Architecture) have no v1 data source and
always render as an explicit "not covered" placeholder — never fabricated.

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
| `tools/assessmenttransform.py` | The 3 per-repo analysis tools + `list_output_files`/`read_output_file` |
| `tools/synthesis.py` | `generate_assessment_report` — the two-call synthesis described above |
| `tools/build_docx.py` | Pure DOCX rendering (no AWS calls) — Executive Summary + 10 sections + Sources appendix |
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
- **AWS credentials** with at least `bedrock:InvokeModel` (env vars, `~/.aws/credentials`,
  or an instance role).

> **Known gap:** `Dockerfile` in this directory currently installs only the Python
> dependencies — it does **not** install `git` or the `atx` CLI. A container built from it
> as-is can hold a conversation but will fail with `FileNotFoundError` on any actual repo
> analysis. Fix this before relying on the Docker path (see `scaled-execution-containers/container/Dockerfile`
> for the `atx` install command to copy over).

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
    "prompt": "Run the full assessment on https://github.com/someorg/somerepo. Client: Acme Corp, industry: healthcare, compliance: HIPAA. Once all three analyses succeed, generate the due-diligence report."
  }'
```

There's no structured "repo" field — every tool's `query` is free text, and the model
extracts the repo URL / client name / context itself. The orchestrator's own prompt tells it
to run all 3 analyses per repo, then call `generate_assessment_report` once they succeed.

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
