"""
Synthesiser — separate LLM call that produces finish_data.

This runs AFTER the ReAct exploration loop has ended.
It receives a compact, compressed context built from:
  - state_accumulator  : structured outputs from the 4 core tools
  - exploration_notes  : compressed digest of read_file / search_code
                         results extracted from the agent message history

Why separate from the agent loop
---------------------------------
During exploration the message history grows to 3-5K lines of raw
read_file / search_code results. Calling finish_analysis as a tool
inside that loop means the LLM must process the entire history on
every attempt — and if Bedrock throttles even once, the agent retries
blindly with the same giant payload, causing the loop seen in production.

Here the synthesiser:
  1. Receives only a compact summary (~500-1000 tokens), not the raw history
  2. Has its own retry loop with exponential backoff
  3. Can be retried without re-running the exploration
  4. Uses structured output (with_structured_output / JSON mode) so the
     response is parsed directly into finish_data — no tool call overhead

Flow
----
  run_agent()  ← exploration only (scan/extract/cluster/analyze/read/search)
       ↓ state_accumulator + final_state.messages
  build_synthesis_context()  ← pure Python, no LLM, compresses everything
       ↓ compact dict
  synthesise()  ← single LLM call with retry/backoff
       ↓ finish_data dict
  json_agent.build_analysis_json()
       ↓
  pdf_generator
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, ToolMessage


# ── Synthesis system prompt ───────────────────────────────────────────────────

_SYNTHESIS_SYSTEM = """### Agent Persona and Role
You are an **Expert Cloud Solutions Architect** focused on detailed **AS-IS Analysis**.
Your sole task is to generate a precise, professional AS-IS Analysis report based on
the repository data provided. Use the provided file contents and structural summaries
as your sole source. Do not invent or assume any information not present in the data.

---

### AS-IS Report Creation (Report should be detailed and professional)

### Required AS-IS Report Structure
The report must follow a structured approach covering:

#### 1. Executive Summary
A concise overview of the analysis purpose, scope, key methodology, and a summary
of the most critical findings — bottlenecks and major strengths.

#### 2. Analysis Objective, Scope, and Preparation
- **Defined Goals:** Clearly state the precise objectives of the analysis.
- **Scope & Boundaries:** Define the specific processes, systems, and organisational
  units included.

#### 3. Methodology & Data Collection
Detail the systematic methods used — File Summary Analysis, Configuration Review,
System Data Analysis.

#### 4. The AS-IS State Documentation
- **Process Description:** Provide a detailed narrative of the current workflow.
- **Resources and Technologies:** Document the existing technologies and tools used.
- **Infrastructure Configuration:** Provide detailed configuration analysis of core
  infrastructure components, specifically including instance types, regions, and
  scaling configuration where present.

#### 5. Data and API Details
- **Database Configuration:** Detail the specific database technology and its
  configuration — instance size, region, replication, backup strategy.
- **Associated Tables:** List the primary database tables/collections and briefly
  describe their function and key columns.
- **API Endpoints:** Document the major internal or external APIs that are part
  of the code, listing their purpose and core functionalities.

#### 6. System Architecture
- **Architecture Overview:** Describe the overall architecture pattern and how
  components interact.
- **Component Breakdown:** Detail each major component, its role, and its
  dependencies.
- **Data Flow:** Step-by-step description of how data moves through the system.

#### 7. Security & Compliance
- **Authentication & Authorisation:** Describe the security mechanisms in place.
- **Data Protection:** Note encryption, secrets management, and data handling.
- **Compliance Gaps:** Identify any observable security risks or missing controls.

#### 8. Code Quality & Technical Debt
- **Strengths:** What the codebase does well.
- **Weaknesses:** Areas with technical debt, missing tests, or poor patterns.
- **Improvement Recommendations:** Concrete, prioritised, actionable suggestions.

---

### Output Format
Respond ONLY with a valid JSON object — no markdown fences, no preamble, no explanation.
Output fields in EXACTLY this order (most critical first, in case output is long):

{
  "executive_summary": {
    "purpose": "one sentence — what this repo does",
    "scope": "what systems and processes are covered",
    "methodology": "how the analysis was performed",
    "critical_findings": ["key finding 1", "key finding 2"],
    "strengths": ["strength 1", "strength 2"],
    "bottlenecks": ["bottleneck 1", "bottleneck 2"]
  },
  "analysis_objective": {
    "defined_goals": ["goal 1", "goal 2"],
    "scope_boundaries": "what is included and excluded",
    "architecture_style": "e.g. MVC, microservices, CLI tool, monolith"
  },
  "methodology": {
    "data_collection_methods": ["File Summary Analysis", "Configuration Review"],
    "tools_used": ["tool 1", "tool 2"]
  },
  "as_is_state": {
    "process_description": "detailed narrative of current workflow",
    "technologies": ["Technology1", "Technology2"],
    "infrastructure": [
      {
        "component": "e.g. EKS Cluster / RDS / API Gateway",
        "type": "VM/Container/DB/Serverless/Network/Storage",
        "config": "instance type, region, replicas, ports, image — from Terraform/K8s/Docker data"
      }
    ],
    "terraform_resources": ["aws_eks_cluster.main", "aws_rds_instance.db"],
    "kubernetes_workloads": ["Deployment/api-server (3 replicas)", "Service/api-lb (LoadBalancer)"],
    "docker_images": ["node:18-alpine", "postgres:15"],
    "cicd_pipelines": ["GitHub Actions: build/test/deploy", "stages: lint → test → build → deploy"],
    "cloud_providers": ["AWS", "GCP"],
    "deployment_regions": ["us-east-1", "eu-west-1"]
  },
  "data_and_api": {
    "database_config": {
      "technology": "e.g. PostgreSQL, DynamoDB, Firestore",
      "instance_size": "",
      "region": "",
      "replication": "",
      "backup_strategy": ""
    },
    "tables": [
      {"name": "table_name", "purpose": "what it stores", "key_columns": ["col1", "col2"]}
    ],
    "api_endpoints": [
      {"method": "GET", "path": "/api/...", "purpose": "what it does", "auth_required": true}
    ]
  },
  "system_architecture": {
    "overview": "architecture pattern and component interaction",
    "components": [
      {"name": "...", "role": "...", "files": ["path/to/file"], "dependencies": ["dep1"]}
    ],
    "data_flow": ["step 1", "step 2", "step 3"]
  },
  "security_and_compliance": {
    "auth_mechanism": "description of auth/authz approach",
    "data_protection": ["encryption note", "secrets management note"],
    "compliance_gaps": ["gap 1", "gap 2"]
  },
  "code_quality": {
    "strengths": ["strength 1"],
    "weaknesses": ["weakness 1"],
    "technical_debt": ["debt item 1"],
    "test_coverage": "description of testing approach",
    "improvement_suggestions": [
      {"priority": "high", "suggestion": "what to do", "rationale": "why"}
    ]
  },
  "file_details": [
    {"file": "relative/path", "summary": "one sentence", "confidence": "high"}
  ],
  "dependency_notes": [
    {"title": "package name", "detail": "one sentence on why it matters"}
  ]
}

Strict rules:
- Use ONLY information from the provided data. Do not invent file names, endpoints, or configs.
- If a field has no data, use [] or "" or null.
- Keep string values concise — 1-2 sentences maximum.
- Lists: maximum 8 items each. components: max 6. file_details: max 8.
- The JSON must be valid and parseable by Python json.loads().
- Do NOT wrap in markdown code fences.
- Do NOT add any text before or after the JSON object.
"""


# ── Context builder ────────────────────────────────────────────────────────────

def build_synthesis_context(
    state_accumulator: dict,
    messages: list[BaseMessage],
    max_nodes: int = 50,
    max_exploration_chars: int = 4000,
) -> str:
    """
    Build a compact text payload for the synthesiser from:
      - state_accumulator  : structured core tool outputs
      - messages           : the agent message history (for read_file/search_code notes)

    Targets ~1500 tokens input so the model has enough budget for ~4096 output tokens.

    Parameters
    ----------
    max_nodes : int
        Max AST files to include in the context (keeps token count bounded).
    max_exploration_chars : int
        Max characters of compressed exploration notes to include.
    """
    parts: list[str] = []

    # ── 1. Repository overview ────────────────────────────────────────────────
    scan = state_accumulator.get("scan_result", {})
    if scan:
        ext_summary = ", ".join(
            f"{ext}:{cnt}"
            for ext, cnt in list(scan.get("by_extension", {}).items())[:10]
        )
        # Include infra presence flags so the LLM knows what was found
        infra_flags = []
        for flag in ("has_terraform", "has_kubernetes", "has_docker",
                     "has_cicd", "has_helm"):
            if scan.get(flag):
                infra_flags.append(flag.replace("has_", ""))
        infra_str = f"  Infra detected: {', '.join(infra_flags)}" if infra_flags else ""
        infra_file_count = len(scan.get("infra_files", []))

        parts.append(
            f"REPOSITORY OVERVIEW\n"
            f"Total files: {scan.get('total_files', 0)}\n"
            f"File types: {ext_summary}\n"
            f"Infra files: {infra_file_count}"
            + (f"\n{infra_str}" if infra_str else "")
        )
        tree = scan.get("directory_tree", "")
        if tree:
            # Limit tree to 40 lines
            tree_lines = tree.split("\n")[:40]
            parts.append("DIRECTORY TREE\n" + "\n".join(tree_lines))

    # ── 2. AST nodes (capped) ─────────────────────────────────────────────────
    ast_data = state_accumulator.get("graphify_ast", {})
    nodes = ast_data.get("nodes", [])
    edges = ast_data.get("edges", [])
    if nodes:
        # Group by source_file for a compact listing
        by_file: dict[str, list[str]] = {}
        for n in nodes[:max_nodes * 3]:  # sample wider, group tighter
            src = n.get("source_file", "unknown")
            label = n.get("label", n.get("id", ""))
            by_file.setdefault(src, []).append(label)

        node_lines = []
        for src, labels in list(by_file.items())[:max_nodes]:
            node_lines.append(f"  {src}: {', '.join(labels[:8])}")

        parts.append(
            f"AST NODES ({len(nodes)} total, showing {len(node_lines)} files)\n"
            + "\n".join(node_lines)
        )

    # ── 3. Communities ────────────────────────────────────────────────────────
    communities = state_accumulator.get("communities", {})
    labels = state_accumulator.get("community_labels", {})
    cohesion = state_accumulator.get("cohesion", {})
    if communities:
        com_lines = []
        for cid, node_ids in sorted(
            communities.items(), key=lambda x: len(x[1]), reverse=True
        )[:10]:
            lbl = labels.get(cid, f"Community {cid}")
            coh = cohesion.get(cid, 0.0)
            com_lines.append(f"  [{lbl}] size={len(node_ids)} cohesion={coh:.2f}")
        parts.append("COMMUNITIES\n" + "\n".join(com_lines))

    # ── 4. God nodes ──────────────────────────────────────────────────────────
    god_nodes = state_accumulator.get("god_nodes", [])
    if god_nodes:
        gn_lines = [
            f"  {g.get('label', g.get('id', ''))} ({g.get('edges', 0)} connections)"
            for g in god_nodes
        ]
        parts.append("GOD NODES (most-connected abstractions)\n" + "\n".join(gn_lines))

    # ── 5. Surprising connections ─────────────────────────────────────────────
    surprises = state_accumulator.get("surprising_connections", [])
    if surprises:
        sc_lines = [
            f"  {s.get('source','')} → {s.get('target','')} "
            f"[{s.get('confidence','')}]"
            for s in surprises
        ]
        parts.append("SURPRISING CONNECTIONS\n" + "\n".join(sc_lines))

    # ── 6. Suggested questions ────────────────────────────────────────────────
    questions = state_accumulator.get("suggested_questions", [])
    if questions:
        q_lines = [
            f"  - {q.get('question', str(q)) if isinstance(q, dict) else q}"
            for q in questions
        ]
        parts.append("SUGGESTED QUESTIONS FROM GRAPH\n" + "\n".join(q_lines))

    # ── 7. Exploration notes (compressed read_file + search_code results) ─────
    exploration = _compress_exploration(messages, max_exploration_chars)
    if exploration:
        parts.append("EXPLORATION NOTES (from file reads and code searches)\n" + exploration)

    # ── 8. Infrastructure analysis (Level 3 structured extraction) ────────────
    infra = state_accumulator.get("infra_result")
    if infra and not infra.get("error"):
        infra_lines = _format_infra_context(infra)
        if infra_lines:
            parts.append("INFRASTRUCTURE ANALYSIS (structured extraction)\n" + infra_lines)

    return "\n\n".join(parts)


def _compress_exploration(
    messages: list[BaseMessage],
    max_chars: int,
) -> str:
    """
    Extract and compress ToolMessage results from read_file and search_code calls.

    Each raw result (potentially 300 lines) is trimmed to its first 60 chars
    as a title line, giving the synthesiser the gist without the full body.
    The agent already processed these — the synthesiser just needs the names
    and surface-level content, not every line.
    """
    notes: list[str] = []
    total = 0

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        content = str(msg.content)

        # read_file results start with "=== path (N lines total) ==="
        if content.startswith("==="):
            header = content.split("\n")[0]          # "=== src/main.ts (120 lines) ==="
            # Take the first 10 meaningful lines of the body as a snippet
            body_lines = [
                ln.strip() for ln in content.split("\n")[1:20]
                if ln.strip() and not ln.strip().startswith("//")
                and not ln.strip().startswith("#")
            ][:6]
            snippet = " | ".join(body_lines)[:200]
            note = f"{header}\n  → {snippet}"

        # search_code results start with "Found N matches"
        elif content.startswith("Found ") or content.startswith("No matches"):
            # Keep the header + first 5 match lines
            lines = content.split("\n")
            note = "\n  ".join(lines[:6])

        else:
            continue

        line = note + "\n"
        if total + len(line) > max_chars:
            notes.append("... (further exploration notes truncated)")
            break
        notes.append(line)
        total += len(line)

    return "\n".join(notes)


def _format_infra_context(infra: dict) -> str:
    """
    Format the structured infra_result into a compact readable block
    for the synthesis LLM call.
    """
    lines: list[str] = []
    summary = infra.get("summary", {})

    # High-level summary
    if summary:
        cp = summary.get("cloud_providers", [])
        if cp:
            lines.append(f"Cloud providers: {', '.join(cp)}")
        rt = summary.get("resource_types", [])
        if rt:
            lines.append(f"Terraform resource types ({len(rt)}): {', '.join(rt[:12])}")
        k8s = summary.get("k8s_workload_kinds", [])
        if k8s:
            lines.append(f"K8s workload kinds: {', '.join(k8s)}")
        ns = summary.get("k8s_namespaces", [])
        if ns:
            lines.append(f"K8s namespaces: {', '.join(ns)}")
        imgs = summary.get("container_images", [])
        if imgs:
            lines.append(f"Container images: {', '.join(imgs[:8])}")
        cicd = summary.get("cicd_platforms", [])
        if cicd:
            lines.append(f"CI/CD platforms: {', '.join(cicd)}")

    # Terraform detail
    tf_list = infra.get("terraform", [])
    for tf in tf_list[:3]:
        lines.append(f"\nTerraform [{tf.get('file','')}]")
        if tf.get("providers"):
            lines.append(f"  Providers: {', '.join(tf['providers'])}")
        if tf.get("backends"):
            lines.append(f"  Backend: {', '.join(tf['backends'])}")
        if tf.get("regions"):
            lines.append(f"  Regions: {', '.join(tf['regions'])}")
        if tf.get("instance_types"):
            lines.append(f"  Instance types: {', '.join(tf['instance_types'])}")
        if tf.get("resource_names"):
            lines.append(f"  Resources ({tf.get('resource_count',0)}): "
                         f"{', '.join(tf['resource_names'][:8])}")
        if tf.get("variables"):
            lines.append(f"  Variables: {', '.join(tf['variables'][:8])}")
        if tf.get("outputs"):
            lines.append(f"  Outputs: {', '.join(tf['outputs'][:6])}")

    # Kubernetes detail
    k8s_list = infra.get("kubernetes", [])
    for m in k8s_list[:8]:
        kind = m.get("kind", "")
        name = m.get("name", "")
        ns   = m.get("namespace", "")
        ns_str = f"/{ns}" if ns else ""
        line = f"K8s {kind}: {name}{ns_str}"
        extras = []
        if m.get("replicas") is not None:
            extras.append(f"replicas={m['replicas']}")
        if m.get("images"):
            extras.append(f"image={m['images'][0]}")
        if m.get("cpu_limits"):
            extras.append(f"cpu={m['cpu_limits'][0]}")
        if m.get("memory_limits"):
            extras.append(f"mem={m['memory_limits'][0]}")
        if m.get("service_type"):
            extras.append(f"type={m['service_type']}")
        if m.get("hosts"):
            extras.append(f"host={m['hosts'][0]}")
        if extras:
            line += f"  [{', '.join(extras)}]"
        lines.append(line)

    # Dockerfile detail
    for d in infra.get("docker", [])[:2]:
        lines.append(f"\nDockerfile [{d.get('file','')}]")
        if d.get("base_images"):
            lines.append(f"  Base images: {', '.join(d['base_images'])}")
        if d.get("is_multistage"):
            lines.append(f"  Multi-stage: stages={d.get('stages', [])}")
        if d.get("exposed_ports"):
            lines.append(f"  Exposed ports: {', '.join(d['exposed_ports'])}")
        if d.get("env_vars"):
            lines.append(f"  Env vars: {', '.join(d['env_vars'][:8])}")

    # docker-compose detail
    for dc in infra.get("docker_compose", [])[:1]:
        lines.append(f"\ndocker-compose [{dc.get('file','')}]")
        for svc in dc.get("services", [])[:6]:
            svc_info = f"  Service: {svc['name']}"
            if svc.get("image"):
                svc_info += f" (image: {svc['image']})"
            elif svc.get("build"):
                svc_info += f" (build: {svc['build']})"
            if svc.get("ports"):
                svc_info += f" ports: {', '.join(svc['ports'])}"
            lines.append(svc_info)

    # Helm detail
    for h in infra.get("helm", [])[:2]:
        if h.get("type") == "helm_chart":
            lines.append(f"\nHelm Chart: {h.get('name','')} "
                         f"v{h.get('version','')} (app: {h.get('app_version','')})")
        elif h.get("type") == "helm_values":
            lines.append(f"Helm Values [{h.get('file','')}]: "
                         f"keys={', '.join(h.get('top_keys',[])[:10])}")

    # CI/CD detail
    for ci in infra.get("cicd", [])[:3]:
        lines.append(f"\nCI/CD [{ci.get('platform','')}]: {ci.get('file','')}")
        if ci.get("jobs"):
            lines.append(f"  Jobs: {', '.join(ci['jobs'][:8])}")
        if ci.get("stages"):
            lines.append(f"  Stages: {', '.join(ci['stages'])}")
        if ci.get("triggers"):
            lines.append(f"  Triggers: {', '.join(ci['triggers'][:5])}")
        if ci.get("environments"):
            lines.append(f"  Environments: {', '.join(ci['environments'])}")

    # Serverless
    for sl in infra.get("serverless", [])[:1]:
        lines.append(f"\nServerless [{sl.get('type','')}]: "
                     f"provider={sl.get('provider','')} runtime={sl.get('runtime','')} "
                     f"region={sl.get('region','')}")
        if sl.get("functions"):
            lines.append(f"  Functions: {', '.join(sl['functions'][:8])}")

    return "\n".join(lines)


# ── Synthesiser ────────────────────────────────────────────────────────────────

def synthesise(
    llm: BaseChatModel,
    context: str,
    max_retries: int = 4,
    base_delay: float = 2.0,
    verbose: bool = False,
) -> dict:
    """
    Make a single dedicated LLM call to synthesise the analysis.

    Retries with exponential backoff on throttling or parse errors.
    Returns a finish_data dict ready for json_agent.build_analysis_json().

    Parameters
    ----------
    llm : BaseChatModel
        The same LLM instance used for exploration.
    context : str
        Compact context payload from build_synthesis_context().
    max_retries : int
        Maximum attempts before giving up (default 4).
    base_delay : float
        Initial backoff delay in seconds, doubles each retry (default 2s).
    verbose : bool
        Print attempt/retry info.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    user_message = (
        "### Input Data:\n"
        "You have been provided with the following file contents and summaries from "
        "the repository. Use this information as your sole source for generating the "
        "AS-IS Analysis report.\n\n"
        "{{collected_file_data}}\n\n".replace("{{collected_file_data}}", context)
        + "Now produce the JSON AS-IS Analysis report following the required structure exactly."
    )

    messages = [
        SystemMessage(content=_SYNTHESIS_SYSTEM),
        HumanMessage(content=user_message),
    ]

    # Force high enough output token budget so the full JSON is never truncated.
    # 4096 tokens ≈ ~3000 words of JSON — more than enough for all fields.
    invoke_kwargs: dict = {"max_tokens": 4096}

    delay = base_delay
    last_error: Exception | None = None
    last_raw: str = ""

    for attempt in range(1, max_retries + 1):
        if verbose:
            print(f"   [Synthesiser attempt {attempt}/{max_retries}]")

        try:
            response = llm.invoke(messages, **invoke_kwargs)
            raw = response.content if hasattr(response, "content") else str(response)
            last_raw = raw

            # ── Check stop reason before parsing ──────────────────────────────
            # If the model hit max_tokens, the JSON is truncated — detect early.
            stop_reason = _get_stop_reason(response)
            if stop_reason in ("max_tokens", "length", "token_limit"):
                if verbose:
                    print(f"   ⚠️  Output truncated (stop_reason={stop_reason}) "
                          f"on attempt {attempt} — increasing max_tokens and retrying")
                # Double the token budget and retry immediately
                invoke_kwargs["max_tokens"] = min(invoke_kwargs["max_tokens"] * 2, 16384)
                last_error = ValueError(f"Output truncated at max_tokens={invoke_kwargs['max_tokens'] // 2}")
                if attempt < max_retries:
                    continue
                break

            # ── Strip accidental markdown fences ──────────────────────────────
            raw = raw.strip()
            if raw.startswith("```"):
                # Handle ```json ... ``` or ``` ... ```
                inner = raw.split("```", 2)
                raw = inner[2] if len(inner) > 2 else inner[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                # Strip closing fence if present
                if raw.endswith("```"):
                    raw = raw[:-3]
            raw = raw.strip()

            # ── Try full parse ────────────────────────────────────────────────
            try:
                finish_data = json.loads(raw)
                if verbose:
                    print(f"   ✅ Synthesis complete on attempt {attempt}")
                return finish_data
            except json.JSONDecodeError as parse_err:
                if verbose:
                    print(f"   ⚠️  JSON parse error on attempt {attempt}: {parse_err}")
                last_error = parse_err

                # ── Partial recovery: extract completed fields ─────────────────
                # The JSON was truncated mid-value. Extract whatever completed
                # successfully rather than returning a blank report.
                recovered = _recover_partial_json(raw, verbose=verbose)
                if recovered:
                    if verbose:
                        print(f"   ↻  Partial recovery: got {list(recovered.keys())}")
                    # Still retry — maybe next attempt gives full JSON
                    if attempt < max_retries:
                        time.sleep(base_delay)
                        continue
                    # Last attempt: use partial rather than empty
                    full = _empty_finish_data(str(parse_err))
                    full.update(recovered)
                    return full

        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            is_throttle = any(
                kw in err_str
                for kw in ("throttl", "toomanytokens", "too many", "rate limit",
                           "429", "serviceunavailable", "slow down")
            )
            if verbose:
                kind = "throttle" if is_throttle else "error"
                print(f"   ⚠️  {kind} on attempt {attempt}: {e}")

            if attempt < max_retries:
                wait = delay if is_throttle else base_delay
                if verbose:
                    print(f"   ↻  Retrying in {wait:.1f}s...")
                time.sleep(wait)
                delay *= 2

    # All retries exhausted — try one last partial recovery from the last raw response
    if verbose:
        print(f"   ❌ Synthesis failed after {max_retries} attempts: {last_error}")

    if last_raw:
        recovered = _recover_partial_json(last_raw, verbose=verbose)
        if recovered:
            if verbose:
                print(f"   ↻  Using partial recovery from last response: {list(recovered.keys())}")
            full = _empty_finish_data(str(last_error))
            full.update(recovered)
            return full

    return _empty_finish_data(str(last_error))


def _get_stop_reason(response) -> str:
    """
    Extract the stop reason from a LangChain response object.
    Different providers put this in different places.
    """
    # LangChain AIMessage response_metadata (most providers)
    meta = getattr(response, "response_metadata", {}) or {}

    # Bedrock Claude
    if "stopReason" in meta:
        return meta["stopReason"].lower()
    # OpenAI / Bedrock Converse
    if "stop_reason" in meta:
        return meta["stop_reason"].lower()
    if "finish_reason" in meta:
        return meta["finish_reason"].lower()
    # Some providers nest it under usage_metadata or additional_kwargs
    extra = getattr(response, "additional_kwargs", {}) or {}
    if "stop_reason" in extra:
        return extra["stop_reason"].lower()

    return "stop"  # default — assume normal completion


def _recover_partial_json(raw: str, verbose: bool = False) -> dict:
    """
    Attempt to salvage a truncated JSON response by extracting
    all string/list/dict fields that completed successfully.

    Strategy:
    1. Try progressively shorter substrings (closing the JSON at the last
       complete field boundary) until json.loads() succeeds.
    2. If that fails, use regex to extract individual top-level string/list
       fields that clearly completed.

    Returns a (possibly partial) dict of successfully parsed fields,
    or {} if nothing can be salvaged.
    """
    if not raw:
        return {}

    # ── Strategy 1: walk back from the end to find a valid JSON boundary ──────
    # Find the last "complete" comma or closing bracket position and try parsing
    candidate = raw.strip()
    for i in range(len(candidate) - 1, max(len(candidate) - 500, 0), -1):
        ch = candidate[i]
        if ch not in (",", "}", "]", '"', "\n", " "):
            continue
        # Try closing the object at this position
        stub = candidate[:i].rstrip().rstrip(",")
        # Count unclosed braces/brackets and close them
        closed = _close_json(stub)
        if closed is None:
            continue
        try:
            result = json.loads(closed)
            if isinstance(result, dict) and result:
                if verbose:
                    print(f"   ↻  Recovered valid JSON by closing at char {i}")
                return result
        except (json.JSONDecodeError, ValueError):
            continue

    # ── Strategy 2: regex extraction of clearly complete top-level fields ─────
    import re
    recovered: dict = {}

    # Extract string fields:  "key": "complete string value"
    for m in re.finditer(
        r'"([a-z_]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw
    ):
        key, val = m.group(1), m.group(2)
        if key in _FINISH_DATA_KEYS:
            recovered[key] = val.replace('\\"', '"')

    # Extract simple list fields: "key": ["item1", "item2"]
    # Only capture lists that are fully closed with ]
    for m in re.finditer(
        r'"([a-z_]+)"\s*:\s*(\[[^\]]*\])', raw
    ):
        key, val_str = m.group(1), m.group(2)
        if key in _FINISH_DATA_KEYS:
            try:
                recovered[key] = json.loads(val_str)
            except (json.JSONDecodeError, ValueError):
                pass

    return recovered


def _close_json(stub: str) -> str | None:
    """
    Given a truncated JSON string, count unclosed braces/brackets
    and append the correct closing characters.
    Returns the closed string or None if unbalanced beyond repair.
    """
    stack = []
    in_string = False
    escape = False

    for ch in stub:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append("}" if ch == "{" else "]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    if len(stack) > 8:
        # Too many unclosed levels — not worth attempting
        return None

    return stub + "".join(reversed(stack))


# Top-level fields we care about recovering from a truncated response
_FINISH_DATA_KEYS = {
    "executive_summary", "analysis_objective", "methodology",
    "as_is_state", "data_and_api", "system_architecture",
    "security_and_compliance", "code_quality",
    "file_details", "dependency_notes",
    # legacy flat fields (fallback if old format returned)
    "summary", "purpose", "architecture_style", "tech_stack",
    "key_components", "data_flow", "api_endpoints", "database_models",
    "security_notes", "improvement_suggestions", "testing_approach",
    "deployment_info", "code_quality_notes",
}


def _empty_finish_data(error_note: str = "") -> dict:
    """Return a safe empty finish_data when synthesis fails."""
    return {
        "summary": f"Synthesis failed — partial report. Error: {error_note}",
        "purpose": "",
        "architecture_style": "",
        "tech_stack": [],
        "key_components": [],
        "data_flow": [],
        "api_endpoints": [],
        "database_models": [],
        "security_notes": [],
        "improvement_suggestions": [],
        "file_details": [],
        "architecture_notes": [],
        "dependency_notes": [],
        "testing_approach": "",
        "deployment_info": "",
        "code_quality_notes": [],
    }
