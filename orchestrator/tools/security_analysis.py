"""
Security & Compliance Analysis Agent (agents-as-tools)

Runs READ-ONLY dependency/CVE scanning and secrets detection on a local clone
of the target repository - static manifest/lockfile parsing and static text
scanning only, NEVER executes client code. Feeds report section 3 (Security &
Compliance Findings), the one section that previously had no v1 data source.

Methodology and tool choices are adapted from the OWASP Secure Agent Playbook
(https://github.com/OWASP/secure-agent-playbook, CC-BY-4.0) - specifically its
`sca-audit.md` and `secrets-scan.md` plays. We do not vendor or execute any of
that repo's files (it targets the Agent Skills spec / Claude Code, a different
runtime than this Strands-based orchestrator); we reuse its documented
methodology, tool recommendations, finding schema, and (for the regex
fallback) its example patterns, reimplemented natively here.

- Dependency/CVE scanning: shells out to `osv-scanner` (github.com/google/osv-
  scanner), the play's primary recommended tool - it's the play's own advice
  that "manually cross-referencing versions against CVE databases is not
  cost-effective," and osv-scanner already correctly handles manifest/lockfile
  discovery across 8+ ecosystems, which we do not attempt to reimplement.
- Secrets detection: shells out to `detect-secrets` (Yelp) rather than the
  play's top pick (trufflehog) - deliberately, since detect-secrets installs
  as a pure pip package (already a dependency-heavy platform: atx, git, uvx,
  Graviz's `dot`; avoiding a 6th separate binary was worth deviating from the
  play's own ranking here). detect-secrets stores only a hashed_secret (SHA1),
  never the raw value, which satisfies the play's rule verbatim: "Never
  include the actual secret value in findings."

Both scanners degrade gracefully (empty findings + a logged/returned warning)
if their binary/package isn't installed or a scan otherwise fails - never
raises, never blocks the rest of the report.

Verification status: `osv-scanner`'s exact command shape and JSON parsing
(including the ecosystem-filtered fixed-version lookup below) were confirmed
against a real v2.4.0 binary run against a dummy vulnerable package.json/
package-lock.json this session - the CLI requires an explicit "scan source"
subcommand ("osv-scanner scan source -r --format json <path>"), which an
earlier version of this file got wrong (bare "osv-scanner --format json -r
<path>", which is not valid syntax). `detect-secrets`' file/directory-scan
behavior was NOT successfully reproduced in a Windows sandbox test (its
underlying detectors were confirmed working via `--string` mode, but `scan
--all-files` returned no results even for an unambiguous private-key header)
- verify this one on the real Linux deployment target before trusting it.
"""

import os
import re
import time
import json
import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from strands import tool

from .assessmenttransform import _extract_repo_name, _extract_params, _clone_repo, WORKSPACE_ROOT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SCA_TIMEOUT_SECONDS = int(os.getenv("SCA_TIMEOUT_SECONDS", "600"))
SECRETS_SCAN_TIMEOUT_SECONDS = int(os.getenv("SECRETS_SCAN_TIMEOUT_SECONDS", "300"))

# Fallback-only regex patterns, used exclusively when `detect-secrets` isn't
# installed. Sourced from the OWASP Secure Agent Playbook's secrets-scan.md
# play (CC-BY-4.0), which documents these as its own manual-pattern fallback
# tier too - not our own invention.
_SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Anthropic API Key", re.compile(r"sk-ant-[a-zA-Z0-9\-]{80,}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[a-zA-Z0-9]{36}")),
    ("Private Key Header", re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Embedded Credential URL", re.compile(r"://[^/\s:]+:[^/\s@]+@")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}")),
]

# Skip these directories entirely during the regex fallback walk - version
# control internals and dependency trees are noise, not client source.
_SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv"}


def _redact(value: str) -> str:
    """Redact a matched secret before it ever leaves this process."""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _run_osv_scanner(repo_path: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Dependency/CVE scan via osv-scanner. Returns (findings, warning_or_none).
    Never raises - a missing binary or scan failure yields no findings plus a
    warning string surfaced in the report, not a crashed tool call.

    Command verified against a real v2.4.0 binary this session (downloaded and
    run against a dummy vulnerable package.json/package-lock.json): the CLI
    requires the explicit "scan source" subcommand - a bare "osv-scanner
    --format json -r <path>" (this file's original form) is NOT valid syntax
    and would silently produce no usable output.

    Deliberately NEVER pass --call-analysis: its go/rust support is documented
    as "(*) Will run build scripts" - that would execute client code, which
    violates golden rule 1. Do not add it even for better reachability data.
    """
    cmd = ["osv-scanner", "scan", "source", "-r", "--format", "json", repo_path]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SCA_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.warning("osv-scanner CLI not found on PATH - dependency/CVE scan skipped")
        return [], "osv-scanner not installed - dependency/CVE scan skipped (see README prerequisites)"
    except subprocess.TimeoutExpired:
        logger.warning(f"osv-scanner timed out after {SCA_TIMEOUT_SECONDS}s - scan skipped")
        return [], f"osv-scanner timed out after {SCA_TIMEOUT_SECONDS}s"

    # osv-scanner exits non-zero when vulnerabilities ARE found (that's the
    # normal "findings exist" signal) - only trust stdout parseability, not rc.
    if not proc.stdout.strip():
        logger.warning(f"osv-scanner produced no output (rc={proc.returncode}): {proc.stderr[-500:]}")
        return [], "osv-scanner produced no output - dependency/CVE scan skipped"

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        logger.warning(f"osv-scanner output was not valid JSON, skipping: {e}")
        return [], "osv-scanner output could not be parsed - dependency/CVE scan skipped"

    findings: List[Dict[str, Any]] = []
    for result in data.get("results", []):
        for pkg in result.get("packages", []):
            package_info = pkg.get("package", {}) or {}
            for vuln in pkg.get("vulnerabilities", []) or []:
                findings.append({
                    "package": package_info.get("name", "unknown"),
                    "version": package_info.get("version", "unknown"),
                    "ecosystem": package_info.get("ecosystem", "unknown"),
                    "vuln_id": vuln.get("id", "unknown"),
                    "aliases": vuln.get("aliases", []) or [],
                    "summary": (vuln.get("summary") or "").strip(),
                    "severity": _extract_osv_severity(vuln),
                    "fixed_version": _extract_osv_fixed_version(vuln, package_info),
                })
    return findings, None


def _extract_osv_severity(vuln: Dict[str, Any]) -> str:
    severity_list = vuln.get("severity")
    if isinstance(severity_list, list) and severity_list:
        first = severity_list[0]
        if isinstance(first, dict):
            return str(first.get("score", "UNKNOWN"))
    db_specific = vuln.get("database_specific")
    if isinstance(db_specific, dict) and db_specific.get("severity"):
        return str(db_specific["severity"])
    return "UNKNOWN"


def _extract_osv_fixed_version(vuln: Dict[str, Any], package_info: Dict[str, Any]) -> str:
    """
    A single OSV vulnerability entry's "affected" list can span MULTIPLE
    ecosystems for the same CVE (confirmed against a real scan: one lodash
    CVE listed both npm/lodash and RubyGems/lodash-rails "affected" blocks
    with different fixed versions). Filter to the scanned package's own
    ecosystem+name before reading a "fixed" event, so we never report the
    wrong ecosystem's fix version.
    """
    target_name = package_info.get("name")
    target_ecosystem = package_info.get("ecosystem")
    for affected in vuln.get("affected", []) or []:
        affected_pkg = affected.get("package", {}) or {}
        if target_name and affected_pkg.get("name") != target_name:
            continue
        if target_ecosystem and affected_pkg.get("ecosystem") != target_ecosystem:
            continue
        for rng in affected.get("ranges", []) or []:
            for event in rng.get("events", []) or []:
                if isinstance(event, dict) and "fixed" in event:
                    return str(event["fixed"])
    return "unknown"


def _run_detect_secrets(repo_path: str) -> List[Dict[str, Any]]:
    """Raises on any failure - caller decides the fallback, this never degrades silently."""
    cmd = ["detect-secrets", "scan", "--all-files", repo_path]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=SECRETS_SCAN_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"detect-secrets produced no output (rc={proc.returncode}): {proc.stderr[-500:]}")
    data = json.loads(proc.stdout)

    findings = []
    for file_path, secrets in (data.get("results") or {}).items():
        for secret in secrets or []:
            findings.append({
                "file": os.path.relpath(file_path, repo_path) if file_path.startswith(repo_path) else file_path,
                "line": secret.get("line_number", "unknown"),
                "type": secret.get("type", "unknown"),
                # detect-secrets only ever stores a SHA1 hash, never the raw value -
                # that satisfies "never include the actual secret value" by design.
                "hashed_secret": secret.get("hashed_secret", ""),
                "is_verified": bool(secret.get("is_verified", False)),
            })
    return findings


def _run_regex_secrets_scan(repo_path: str) -> List[Dict[str, Any]]:
    """Fallback-only text scan. Matched values are redacted before being returned."""
    findings = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            full_path = os.path.join(root, fn)
            try:
                if os.path.getsize(full_path) > 2_000_000:  # skip anything unusually large
                    continue
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        for label, pattern in _SECRET_PATTERNS:
                            match = pattern.search(line)
                            if match:
                                findings.append({
                                    "file": os.path.relpath(full_path, repo_path),
                                    "line": line_no,
                                    "type": label,
                                    "redacted_match": _redact(match.group(0)),
                                })
            except Exception:
                continue  # unreadable/binary file - skip, don't fail the whole scan
    return findings


def _run_secrets_scan(repo_path: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Secrets detection. Prefers `detect-secrets` (pip package) if installed;
    falls back to a small set of high-confidence regex patterns otherwise.
    Never raises - degrades to fallback or empty findings plus a warning.
    """
    try:
        return _run_detect_secrets(repo_path), None
    except FileNotFoundError:
        logger.info("detect-secrets not installed, falling back to regex patterns")
        return (
            _run_regex_secrets_scan(repo_path),
            "detect-secrets not installed - used lower-confidence regex fallback (see README prerequisites)",
        )
    except Exception as e:
        logger.warning(f"detect-secrets failed ({e}), falling back to regex patterns")
        return _run_regex_secrets_scan(repo_path), f"detect-secrets failed ({e}) - used regex fallback"


def _run_security_analysis(source: str, additional_context: str) -> Dict[str, Any]:
    repo_name = _extract_repo_name(source)
    job_name = f"security-{repo_name}-{int(time.time())}"
    workdir = os.path.join(WORKSPACE_ROOT, job_name)
    repo_path = os.path.join(workdir, "repo")
    findings_dir = os.path.join(workdir, "findings")
    os.makedirs(findings_dir, exist_ok=True)

    try:
        _clone_repo(source, repo_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch source: {e}"}

    sca_findings, sca_warning = _run_osv_scanner(repo_path)
    secrets_findings, secrets_warning = _run_secrets_scan(repo_path)

    report = {
        "context": additional_context,
        "sca_findings": sca_findings,
        "secrets_findings": secrets_findings,
        "warnings": [w for w in (sca_warning, secrets_warning) if w],
    }
    # Written OUTSIDE the repo clone (a sibling "findings" dir, never inside
    # repo_path) - we read the client's code, we never write into their tree.
    findings_file = os.path.join(findings_dir, "security-findings.json")
    with open(findings_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return {
        "status": "success",
        "transformation": "Security & Compliance Analysis (osv-scanner + detect-secrets)",
        "job_name": job_name,
        "source": source,
        "output_dir": findings_dir,
        "sca_finding_count": len(sca_findings),
        "secrets_finding_count": len(secrets_findings),
        "warnings": report["warnings"],
        "completed_at": datetime.utcnow().isoformat() + 'Z',
    }


@tool
def security_compliance_agent(query: str) -> Dict[str, Any]:
    """
    Runs dependency/CVE scanning (osv-scanner) and secrets detection
    (detect-secrets) on ONE repository. READ-ONLY - static manifest/lockfile
    parsing and static text scanning only, no client code is ever executed.
    Blocks until finished. Missing scanner binaries/packages degrade to a
    warning, not a failure - the rest of the report still generates.

    Args:
        query: e.g. "Run security and compliance scanning on https://github.com/user/repo"
    """
    logger.info("SECURITY COMPLIANCE AGENT INVOKED")
    try:
        params = _extract_params(query, """Extract fields. Return ONLY JSON:
{"source": "repo URL or local path", "context": "free-text framing, e.g. specific compliance concerns"}""")
        source = params.get('source', '')
        if not source:
            return {"status": "error", "error": "Could not extract a repository source from the request."}
        result = _run_security_analysis(source, params.get('context', ''))
        # Propagate the REAL inner status - _run_security_analysis returns "error" if
        # the repo clone failed, "success" otherwise (scanner degradation is a warning,
        # not a failure, by design - see module docstring).
        return {"status": result.get("status", "error"), "result": json.dumps(result)}
    except Exception as e:
        logger.error(f"security_compliance_agent failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}
