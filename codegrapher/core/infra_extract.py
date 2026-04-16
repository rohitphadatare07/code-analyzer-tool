"""
Infrastructure file parser — Level 3 infra analysis.

Extracts structured data from infrastructure and deployment configuration files
and adds them to the state_accumulator under the key "infra_result".

Supported formats
-----------------
  Terraform HCL  (.tf, .tfvars)
    → resource blocks, provider blocks, variable declarations, module calls,
      backend config, output values

  Kubernetes YAML (.yaml, .yml with apiVersion:)
    → Deployment (image, replicas, resource limits, env vars)
    → Service (type, ports)
    → Ingress (host, paths, TLS)
    → ConfigMap (keys)
    → HPA (min/max replicas, target CPU)
    → Namespace
    → StatefulSet, DaemonSet, Job, CronJob

  Dockerfile
    → base image (FROM), exposed ports (EXPOSE), env vars (ENV),
      labels (LABEL), entry command (CMD/ENTRYPOINT), build stages

  docker-compose.yml
    → services (image, build, ports, volumes, environment, depends_on),
      networks, named volumes

  Helm Chart.yaml / values.yaml
    → chart name, version, appVersion, description (Chart.yaml)
    → top-level keys and their types (values.yaml)

  GitHub Actions / GitLab CI / Jenkins / Azure Pipelines / CircleCI
    → workflow triggers, job names, steps, environment names

  Serverless Framework / AWS CDK / Pulumi
    → provider, functions, resources (top-level)

Usage
-----
Called from tools.py extract_ast_graph or as a standalone pipeline step.
Results stored in state_accumulator["infra_result"].

The synthesiser picks up infra_result via build_synthesis_context() and
includes it in the context payload sent to the LLM.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# ── Public entry point ────────────────────────────────────────────────────────

def extract_infra(repo_root: Path, infra_files: list[str]) -> dict:
    """
    Parse all detected infrastructure files and return a structured dict.

    Parameters
    ----------
    repo_root   : Path  — absolute path to the repository root
    infra_files : list  — relative file paths (from scan_repository.infra_files)

    Returns
    -------
    dict with keys:
      terraform, kubernetes, docker, docker_compose,
      helm, cicd, serverless, summary
    """
    result: dict[str, Any] = {
        "terraform":      [],
        "kubernetes":     [],
        "docker":         [],
        "docker_compose": [],
        "helm":           [],
        "cicd":           [],
        "serverless":     [],
        "summary":        {},
    }

    for rel in infra_files:
        path = repo_root / rel
        if not path.is_file():
            continue
        fname = path.name.lower()
        ext   = path.suffix.lower()

        try:
            if ext in (".tf", ".tfvars", ".hcl"):
                parsed = _parse_terraform(path)
                if parsed:
                    result["terraform"].append({"file": rel, **parsed})

            elif ext in (".yaml", ".yml"):
                if fname in ("chart.yaml", "chart.yml"):
                    parsed = _parse_helm_chart(path)
                    if parsed:
                        result["helm"].append({"file": rel, **parsed})
                elif fname in ("values.yaml", "values.yml"):
                    parsed = _parse_helm_values(path)
                    if parsed:
                        result["helm"].append({"file": rel, **parsed})
                elif _is_cicd_file(rel, fname):
                    parsed = _parse_cicd(path, rel)
                    if parsed:
                        result["cicd"].append({"file": rel, **parsed})
                elif fname in ("docker-compose.yml", "docker-compose.yaml",
                               "docker-compose.override.yml",
                               "docker-compose.override.yaml"):
                    parsed = _parse_docker_compose(path)
                    if parsed:
                        result["docker_compose"].append({"file": rel, **parsed})
                elif fname in ("serverless.yml", "serverless.yaml"):
                    parsed = _parse_serverless(path)
                    if parsed:
                        result["serverless"].append({"file": rel, **parsed})
                else:
                    # Generic YAML — check for K8s
                    k8s = _parse_kubernetes(path)
                    if k8s:
                        result["kubernetes"].append({"file": rel, **k8s})

            elif fname.startswith("dockerfile") or fname == ".dockerfile":
                parsed = _parse_dockerfile(path)
                if parsed:
                    result["docker"].append({"file": rel, **parsed})

            elif fname == "cdk.json":
                parsed = _parse_cdk(path)
                if parsed:
                    result["serverless"].append({"file": rel, "type": "aws_cdk", **parsed})

        except Exception:
            # Never let a bad infra file crash the pipeline
            continue

    result["summary"] = _build_summary(result)
    return result


# ── Summary ───────────────────────────────────────────────────────────────────

def _build_summary(r: dict) -> dict:
    """Build a high-level summary of what infrastructure was found."""
    tf = r["terraform"]
    k8s = r["kubernetes"]
    docker = r["docker"]
    dc = r["docker_compose"]
    helm = r["helm"]
    cicd = r["cicd"]

    # Collect all cloud providers from Terraform
    providers: set[str] = set()
    resource_types: list[str] = []
    for t in tf:
        providers.update(t.get("providers", []))
        resource_types.extend(t.get("resource_types", []))

    # Collect K8s workload kinds
    k8s_kinds = [m.get("kind", "") for m in k8s if m.get("kind")]
    namespaces = list({m.get("namespace", "") for m in k8s if m.get("namespace")})

    # Container images across all sources
    images: set[str] = set()
    for d in docker:
        images.update(d.get("base_images", []))
    for m in k8s:
        images.update(m.get("images", []))
    for s in dc:
        for svc in s.get("services", []):
            if svc.get("image"):
                images.add(svc["image"])

    # CI/CD pipelines
    pipeline_types = list({c.get("platform", "") for c in cicd if c.get("platform")})

    return {
        "cloud_providers":   sorted(providers),
        "resource_types":    sorted(set(resource_types))[:20],
        "k8s_workload_kinds": sorted(set(k8s_kinds)),
        "k8s_namespaces":    sorted(namespaces),
        "container_images":  sorted(images)[:15],
        "cicd_platforms":    pipeline_types,
        "has_helm":          bool(helm),
        "tf_file_count":     len(tf),
        "k8s_manifest_count": len(k8s),
        "docker_file_count": len(docker),
        "dc_service_count":  sum(len(s.get("services", [])) for s in dc),
    }


# ── Terraform parser ──────────────────────────────────────────────────────────

# Simple regex-based HCL parser — handles the 80% case without a full HCL library
_TF_RESOURCE_RE = re.compile(
    r'resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_TF_PROVIDER_RE = re.compile(
    r'provider\s+"([^"]+)"', re.MULTILINE)
_TF_MODULE_RE   = re.compile(
    r'module\s+"([^"]+)"', re.MULTILINE)
_TF_VARIABLE_RE = re.compile(
    r'variable\s+"([^"]+)"', re.MULTILINE)
_TF_OUTPUT_RE   = re.compile(
    r'output\s+"([^"]+)"', re.MULTILINE)
_TF_BACKEND_RE  = re.compile(
    r'backend\s+"([^"]+)"', re.MULTILINE)
_TF_REGION_RE   = re.compile(
    r'region\s*=\s*"([^"]+)"', re.MULTILINE)
_TF_INSTANCE_RE = re.compile(
    r'instance_type\s*=\s*"([^"]+)"', re.MULTILINE)
_TF_IMAGE_RE    = re.compile(
    r'(?:ami|image_id|source_image_family|image_name)\s*=\s*"([^"]+)"',
    re.MULTILINE | re.IGNORECASE)


def _parse_terraform(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    resources    = _TF_RESOURCE_RE.findall(text)   # [(type, name), ...]
    providers    = [m for m in _TF_PROVIDER_RE.findall(text)]
    modules      = _TF_MODULE_RE.findall(text)
    variables    = _TF_VARIABLE_RE.findall(text)
    outputs      = _TF_OUTPUT_RE.findall(text)
    backends     = _TF_BACKEND_RE.findall(text)
    regions      = list(set(_TF_REGION_RE.findall(text)))
    instance_types = list(set(_TF_INSTANCE_RE.findall(text)))
    images       = list(set(_TF_IMAGE_RE.findall(text)))

    if not resources and not providers and not modules:
        return None

    return {
        "type":           "terraform",
        "providers":      providers,
        "backends":       backends,
        "regions":        regions,
        "resource_count": len(resources),
        "resource_types": sorted(set(r[0] for r in resources)),
        "resource_names": [f"{r[0]}.{r[1]}" for r in resources[:20]],
        "modules":        modules,
        "variables":      variables[:20],
        "outputs":        outputs[:10],
        "instance_types": instance_types,
        "images":         images[:5],
    }


# ── Kubernetes YAML parser ────────────────────────────────────────────────────

def _parse_kubernetes(path: Path) -> dict | None:
    """
    Parse a single Kubernetes manifest YAML file.
    Handles multi-document YAML (---) and extracts resource-specific fields.
    Uses regex so we avoid depending on PyYAML at the core level.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Split multi-document YAML
    docs = [d.strip() for d in text.split("---") if d.strip()]
    parsed_docs = []

    for doc in docs:
        info = _parse_k8s_doc(doc)
        if info:
            parsed_docs.append(info)

    if not parsed_docs:
        return None

    # Return first doc's fields at top level, list all if multi-doc
    if len(parsed_docs) == 1:
        return parsed_docs[0]
    return {
        "kind":      "multi-document",
        "manifests": parsed_docs,
        "images":    list({img for d in parsed_docs for img in d.get("images", [])}),
    }


_K8S_KIND_RE      = re.compile(r'^kind:\s*(\S+)', re.MULTILINE)
_K8S_NAME_RE      = re.compile(r'^\s{0,4}name:\s*(\S+)', re.MULTILINE)
_K8S_NS_RE        = re.compile(r'^\s{0,4}namespace:\s*(\S+)', re.MULTILINE)
_K8S_IMAGE_RE     = re.compile(r'image:\s*(\S+)', re.MULTILINE)
_K8S_REPLICAS_RE  = re.compile(r'replicas:\s*(\d+)', re.MULTILINE)
_K8S_CPU_RE       = re.compile(r'cpu:\s*(\S+)', re.MULTILINE)
_K8S_MEM_RE       = re.compile(r'memory:\s*(\S+)', re.MULTILINE)
_K8S_PORT_RE      = re.compile(r'port:\s*(\d+)|containerPort:\s*(\d+)', re.MULTILINE)
_K8S_SVC_TYPE_RE  = re.compile(r'^\s+type:\s*(LoadBalancer|NodePort|ClusterIP|ExternalName)',
                                re.MULTILINE)
_K8S_HOST_RE      = re.compile(r'host:\s*(\S+)', re.MULTILINE)
_K8S_ENV_NAME_RE  = re.compile(r'^\s+-\s+name:\s*([A-Z_][A-Z0-9_]+)', re.MULTILINE)


def _parse_k8s_doc(text: str) -> dict | None:
    if "apiVersion:" not in text:
        return None

    kind      = (_K8S_KIND_RE.search(text) or _match_empty()).group(1) if _K8S_KIND_RE.search(text) else ""
    name      = (_K8S_NAME_RE.search(text) or _match_empty()).group(1) if _K8S_NAME_RE.search(text) else ""
    namespace = (_K8S_NS_RE.search(text) or _match_empty()).group(1) if _K8S_NS_RE.search(text) else ""

    images    = list(set(_K8S_IMAGE_RE.findall(text)))
    replicas  = _K8S_REPLICAS_RE.search(text)
    cpus      = list(set(_K8S_CPU_RE.findall(text)))
    mems      = list(set(_K8S_MEM_RE.findall(text)))
    ports     = list(set(
        p[0] or p[1]
        for p in _K8S_PORT_RE.findall(text)
        if p[0] or p[1]
    ))
    svc_type  = (_K8S_SVC_TYPE_RE.search(text) or _match_empty()).group(1) \
                if _K8S_SVC_TYPE_RE.search(text) else ""
    hosts     = list(set(_K8S_HOST_RE.findall(text)))
    env_vars  = list(set(_K8S_ENV_NAME_RE.findall(text)))[:15]

    if not kind:
        return None

    doc = {
        "kind":      kind,
        "name":      name,
        "namespace": namespace,
        "images":    images,
    }
    if replicas:
        doc["replicas"] = int(replicas.group(1))
    if cpus:
        doc["cpu_limits"] = cpus
    if mems:
        doc["memory_limits"] = mems
    if ports:
        doc["ports"] = ports
    if svc_type:
        doc["service_type"] = svc_type
    if hosts:
        doc["hosts"] = hosts
    if env_vars:
        doc["env_vars"] = env_vars

    return doc


def _match_empty():
    """Return a dummy match object with empty group(1) for safe chaining."""
    class _Dummy:
        def group(self, n): return ""
    return _Dummy()


# ── Dockerfile parser ─────────────────────────────────────────────────────────

_DF_FROM_RE       = re.compile(r'^FROM\s+(\S+)(?:\s+AS\s+(\S+))?', re.MULTILINE | re.IGNORECASE)
_DF_EXPOSE_RE     = re.compile(r'^EXPOSE\s+([\d/ ]+)', re.MULTILINE | re.IGNORECASE)
_DF_ENV_RE        = re.compile(r'^ENV\s+([A-Z_][A-Z0-9_]*)', re.MULTILINE | re.IGNORECASE)
_DF_LABEL_RE      = re.compile(r'^LABEL\s+(\S+)', re.MULTILINE | re.IGNORECASE)
_DF_WORKDIR_RE    = re.compile(r'^WORKDIR\s+(\S+)', re.MULTILINE | re.IGNORECASE)
_DF_CMD_RE        = re.compile(r'^(?:CMD|ENTRYPOINT)\s+(.+)', re.MULTILINE | re.IGNORECASE)
_DF_RUN_PKG_RE    = re.compile(
    r'(?:apt-get install|apk add|yum install|pip install|npm install)\s+([\w\s\-=.<>]+)',
    re.IGNORECASE)


def _parse_dockerfile(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    froms   = _DF_FROM_RE.findall(text)   # [(image, alias), ...]
    exposes = _DF_EXPOSE_RE.findall(text)
    envs    = _DF_ENV_RE.findall(text)
    workdir = _DF_WORKDIR_RE.findall(text)
    cmds    = _DF_CMD_RE.findall(text)

    base_images = [f[0] for f in froms if f[0].lower() != "scratch"]
    stages      = [(f[1] if f[1] else f"stage_{i}") for i, f in enumerate(froms)]
    is_multistage = len(froms) > 1

    packages = []
    for m in _DF_RUN_PKG_RE.finditer(text):
        pkgs = m.group(1).strip().split()
        packages.extend(p for p in pkgs if not p.startswith("-"))

    ports = []
    for e in exposes:
        ports.extend(e.strip().split())

    return {
        "type":         "dockerfile",
        "base_images":  list(set(base_images)),
        "is_multistage": is_multistage,
        "stages":       stages if is_multistage else [],
        "exposed_ports": ports,
        "env_vars":     list(set(envs))[:15],
        "workdir":      workdir[-1] if workdir else "",
        "entrypoint":   cmds[-1].strip() if cmds else "",
        "packages":     list(set(packages))[:20],
    }


# ── docker-compose parser ─────────────────────────────────────────────────────

_DC_SERVICE_RE = re.compile(r'^  ([a-zA-Z][a-zA-Z0-9_\-]*):\s*$', re.MULTILINE)
_DC_IMAGE_RE   = re.compile(r'image:\s*(\S+)')
_DC_BUILD_RE   = re.compile(r'build:\s*(\S+)')
_DC_PORT_RE    = re.compile(r'"?(\d+):(\d+)"?')
_DC_ENV_RE     = re.compile(r'(?:^|\s+)-\s+([A-Z_][A-Z0-9_]*)=', re.MULTILINE)
_DC_DEPENDS_RE = re.compile(r'(?:^|\s+)-\s+([a-zA-Z][a-zA-Z0-9_\-]*)\s*$', re.MULTILINE)


def _parse_docker_compose(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Extract service blocks by splitting on top-level indented keys
    services_section = ""
    in_services = False
    for line in text.split("\n"):
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and line and not line.startswith(" ") and not line.startswith("\t"):
            break
        if in_services:
            services_section += line + "\n"

    service_names = _DC_SERVICE_RE.findall(services_section) if services_section \
                    else _DC_SERVICE_RE.findall(text)

    services = []
    for svc in service_names[:12]:
        # Find the service block approximately
        pat = re.compile(rf'  {re.escape(svc)}:(.*?)(?=\n  [a-zA-Z]|\Z)', re.DOTALL)
        m = pat.search(text)
        block = m.group(1) if m else ""

        img   = (_DC_IMAGE_RE.search(block) or _match_empty()).group(1)
        build = (_DC_BUILD_RE.search(block) or _match_empty()).group(1)
        ports = _DC_PORT_RE.findall(block)
        env_vars = list(set(_DC_ENV_RE.findall(block)))[:8]

        services.append({
            "name":     svc,
            "image":    img,
            "build":    build,
            "ports":    [f"{h}:{c}" for h, c in ports],
            "env_vars": env_vars,
        })

    if not services:
        return None

    # Top-level volumes / networks
    volumes  = re.findall(r'^  ([a-zA-Z][a-zA-Z0-9_\-]*):\s*$',
                          _section(text, "volumes"), re.MULTILINE)
    networks = re.findall(r'^  ([a-zA-Z][a-zA-Z0-9_\-]*):\s*$',
                          _section(text, "networks"), re.MULTILINE)

    return {
        "type":     "docker_compose",
        "services": services,
        "volumes":  volumes,
        "networks": networks,
    }


def _section(text: str, key: str) -> str:
    """Extract a top-level YAML section as raw text."""
    m = re.search(rf'^{key}:(.*?)(?=\n[a-zA-Z]|\Z)', text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


# ── Helm parser ───────────────────────────────────────────────────────────────

def _parse_helm_chart(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    def _get(key: str) -> str:
        m = re.search(rf'^{key}:\s*(.+)', text, re.MULTILINE)
        return m.group(1).strip().strip('"\'') if m else ""

    return {
        "type":        "helm_chart",
        "name":        _get("name"),
        "version":     _get("version"),
        "app_version": _get("appVersion"),
        "description": _get("description"),
        "type_field":  _get("type"),
    }


def _parse_helm_values(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Extract top-level keys and their value types
    top_keys = re.findall(r'^([a-zA-Z][a-zA-Z0-9_\-]*):', text, re.MULTILINE)
    # Extract image tags and replica counts as they're commonly important
    image_tags  = re.findall(r'tag:\s*(\S+)', text)
    replicas    = re.findall(r'replicaCount:\s*(\d+)', text)
    resources   = "resources:" in text

    return {
        "type":        "helm_values",
        "top_keys":    list(set(top_keys))[:20],
        "image_tags":  list(set(image_tags))[:5],
        "replica_counts": list(set(replicas)),
        "has_resources_block": resources,
    }


# ── CI/CD parser ──────────────────────────────────────────────────────────────

def _is_cicd_file(rel: str, fname: str) -> bool:
    cicd_patterns = [
        ".github/workflows/", ".gitlab-ci", "azure-pipelines",
        "bitbucket-pipelines", "circleci", "jenkinsfile",
        "travis.yml", "travis.yaml", "appveyor.yml",
    ]
    rel_lower = rel.lower()
    return any(p in rel_lower or p in fname for p in cicd_patterns)


def _parse_cicd(path: Path, rel: str) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Detect platform
    rel_lower = rel.lower()
    if ".github/workflows" in rel_lower:
        platform = "github_actions"
    elif ".gitlab-ci" in rel_lower:
        platform = "gitlab_ci"
    elif "azure-pipelines" in rel_lower:
        platform = "azure_devops"
    elif "jenkinsfile" in rel_lower:
        platform = "jenkins"
    elif "bitbucket-pipelines" in rel_lower:
        platform = "bitbucket_pipelines"
    elif "circleci" in rel_lower:
        platform = "circleci"
    else:
        platform = "unknown_cicd"

    # Extract jobs/stages
    jobs   = re.findall(r'^  ([a-zA-Z][a-zA-Z0-9_\-]*):\s*$', text, re.MULTILINE)
    stages = re.findall(r'stages?:\s*\n((?:\s+-\s+\S+\n)+)', text)
    stage_list = []
    for s in stages:
        stage_list.extend(re.findall(r'-\s+(\S+)', s))

    # GitHub Actions: extract trigger events and environment names
    triggers = re.findall(r'^on:\s*\n((?:\s+\S+.*\n)+)', text, re.MULTILINE)
    trigger_list = []
    for t in triggers:
        trigger_list.extend(re.findall(r'(\w+):', t))

    envs = re.findall(r'environment:\s*(\S+)', text)

    # Docker images used in CI
    images = re.findall(r'image:\s*(\S+)', text)

    return {
        "platform":   platform,
        "jobs":       jobs[:15],
        "stages":     stage_list[:10],
        "triggers":   trigger_list[:8],
        "environments": list(set(envs))[:5],
        "images":     list(set(images))[:8],
    }


# ── Serverless / CDK parser ───────────────────────────────────────────────────

def _parse_serverless(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    provider  = re.search(r'^\s+name:\s*(\S+)', text, re.MULTILINE)
    runtime   = re.search(r'runtime:\s*(\S+)', text, re.MULTILINE)
    region    = re.search(r'region:\s*(\S+)', text, re.MULTILINE)
    functions = re.findall(r'^  ([a-zA-Z][a-zA-Z0-9_\-]*):\s*$', text, re.MULTILINE)
    resources = re.findall(r'Type:\s*(AWS::\S+)', text)

    return {
        "type":      "serverless_framework",
        "provider":  provider.group(1) if provider else "",
        "runtime":   runtime.group(1) if runtime else "",
        "region":    region.group(1) if region else "",
        "functions": functions[:15],
        "aws_resources": list(set(resources))[:10],
    }


def _parse_cdk(path: Path) -> dict | None:
    try:
        import json as _json
        data = _json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return {
            "app":     data.get("app", ""),
            "context": list(data.get("context", {}).keys())[:10],
        }
    except Exception:
        return None
