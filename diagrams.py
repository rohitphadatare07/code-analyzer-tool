"""Convert Mermaid diagram code to images for embedding in PDF."""
from __future__ import annotations

import base64
import urllib.request
import urllib.parse
import urllib.error
import re
from pathlib import Path


def _sanitize_mermaid(code: str) -> str:
    """Clean up Mermaid code to avoid common syntax errors."""
    if not code:
        return ""

    # Normalize line endings
    code = code.strip().replace("\r\n", "\n").replace("\\n", "\n")

    # If it looks like a JSON-escaped string, unescape it
    code = code.replace("\\t", "  ")

    return code


def mermaid_to_png_bytes(mermaid_code: str) -> bytes | None:
    """Convert Mermaid diagram code to PNG bytes using mermaid.ink."""
    code = _sanitize_mermaid(mermaid_code)
    if not code:
        return None

    # Encode for mermaid.ink
    encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "repoanalyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        # Verify it's an image
        if data[:4] in (b"\x89PNG", b"GIF8") or data[:2] == b"\xff\xd8":
            return data
        return None
    except Exception:
        return None


def mermaid_to_svg(mermaid_code: str) -> str | None:
    """Convert Mermaid code to SVG string using mermaid.ink."""
    code = _sanitize_mermaid(mermaid_code)
    if not code:
        return None

    encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/svg/{encoded}"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "repoanalyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def generate_fallback_diagram(title: str, mermaid_code: str) -> str:
    """Generate a simple SVG diagram when mermaid.ink is unavailable."""
    lines = [l.strip() for l in mermaid_code.split("\n") if l.strip() and not l.strip().startswith("%%")]

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300">
  <rect width="600" height="300" fill="#f8f9fa" rx="8"/>
  <text x="300" y="30" font-family="Arial" font-size="14" font-weight="bold" 
        text-anchor="middle" fill="#333">{title}</text>
  <text x="300" y="55" font-family="monospace" font-size="10" 
        text-anchor="middle" fill="#666">[Diagram - network required to render]</text>
  <rect x="20" y="70" width="560" height="220" fill="white" rx="4" stroke="#ddd"/>
  <text x="30" y="90" font-family="monospace" font-size="9" fill="#444">"""

    y = 90
    for line in lines[:15]:
        line_escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:80]
        svg += f'\n  <tspan x="30" dy="14">{line_escaped}</tspan>'

    svg += """</text>
</svg>"""
    return svg
