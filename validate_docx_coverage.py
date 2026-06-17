#!/usr/bin/env python3
"""
validate_docx_coverage.py
=========================

Deterministic, mechanical half of the `repo-completeness-validator` skill.

The validator skill prescribes 23 checks. A handful of them are *mechanical*:
they are pure functions of the bytes in the DOCX, the rows in diagrams.json and
the names in components.json, and the LLM has historically gotten them wrong by
"eyeballing" the document. Those are implemented here so the verdict is
reproducible. The interpretive checks (semantic component coverage, narrative
quality, placement *reasoning*, etc.) stay in the LLM skill.

Mechanical checks implemented (numbering follows the handoff §5):
  #1  DOCX exists and is > 50 KB                      -> DOCX_TOO_SMALL / DOCX_MISSING
  #2  TOC field present (w:fldSimple / w:instrText)   -> TOC_MISSING
      + >= 10 visible entry paragraphs                -> TOC_NOT_POPULATED
      + body must NOT contain the Word placeholder    -> TOC_NOT_POPULATED
  #12 Every diagrams.json PNG is embedded in the DOCX,
      matched by SHA-256 of the image bytes           -> DIAGRAM_NOT_EMBEDDED
  #13 Embedded image count >= diagrams.json row count -> DIAGRAM_COUNT_MISMATCH
  #18 No "see diagrams folder" / "diagram unavailable"
      style forbidden phrases in the body             -> DIAGRAM_REFERENCED_BY_TEXT
  #20 Per-section image-to-row parity                 -> DIAGRAM_SECTION_PARITY
  #22 Every "Figure x.y" caption is immediately
      preceded by an inline image (a:blip)            -> CAPTION_WITHOUT_IMAGE

Plus the Implementation-Hint component sweep:
  case-insensitive, word-boundary substring match of every inventory component
  name against the normalized document text                -> missing_components

Section presence sweep against the canonical tree (1.1 .. 10.4 + A..F):
                                                            -> missing_sections

The script prints a single JSON verdict to stdout and exits 0 on pass, 1 on
fail, 2 on a usage / load error. Nothing else is written to stdout so the
caller can pipe it straight into `jq`.

Usage:
    python tools/validate_docx_coverage.py \
        --docx report.docx \
        --inventory components.json \
        --diagrams diagrams.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from docx import Document
    from docx.oxml.ns import qn
except Exception as exc:  # pragma: no cover - import guard
    print(json.dumps({
        "status": "fail",
        "issues": [{"code": "PYTHON_DOCX_MISSING", "detail": str(exc)}],
    }))
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Canonical section tree (handoff §3). Appendix F is included here on purpose:
# §6.4 notes it is used by the fallback logic but never declared. The validator
# treats it as canonical so a report that lands diagrams in Appendix F is not
# flagged as having an undeclared section.
# --------------------------------------------------------------------------- #
CANONICAL_SECTIONS: list[str] = [
    "1.1", "1.2", "1.3", "1.4", "1.5",
    "2.1", "2.2", "2.3", "2.4",
    "3.1", "3.2", "3.3", "3.4",
    "4.1", "4.2", "4.3", "4.4",
    "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7",
    "6.1", "6.2", "6.3", "6.4", "6.5", "6.6", "6.7",
    "7.1", "7.2", "7.3", "7.4",
    "8.1", "8.2", "8.3", "8.4",
    "9.1", "9.2", "9.3", "9.4",
    "10.1", "10.2", "10.3", "10.4",
    "Appendix A", "Appendix B", "Appendix C", "Appendix D", "Appendix E",
    "Appendix F",
]

TOC_PLACEHOLDER = "update field in word to populate"

FORBIDDEN_DIAGRAM_PHRASES = [
    "see diagrams folder",
    "see the diagrams folder",
    "diagram unavailable",
    "diagram not available",
    "see attached diagram",
    "refer to the diagrams directory",
    "atxdocumentation/diagrams",
]

# A "Figure 7.2" / "Figure 4.1 — title" caption opener.
CAPTION_RE = re.compile(r"^\s*figure\s+\d+(?:\.\d+)?\b", re.IGNORECASE)
# Leading dotted-decimal or "Appendix X" token in a heading.
HEADING_NUM_RE = re.compile(r"^\s*(appendix\s+[a-f]|\d+(?:\.\d+){0,2})\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to a single space, lower-cased."""
    return re.sub(r"\s+", " ", text).strip().lower()


# --------------------------------------------------------------------------- #
# Document walk: produce an ordered list of "events" so headings, captions and
# inline images can be reasoned about positionally (needed for #20 and #22).
# --------------------------------------------------------------------------- #
class Block:
    __slots__ = ("kind", "text", "section", "has_image", "style")

    def __init__(self, kind: str, text: str, style: str, has_image: bool):
        self.kind = kind          # "heading" | "para"
        self.text = text
        self.style = style
        self.has_image = has_image
        self.section: str | None = None  # dotted-decimal for headings


def _para_has_blip(p_element) -> bool:
    return p_element.find(".//" + qn("a:blip")) is not None


def _heading_section(text: str) -> str | None:
    m = HEADING_NUM_RE.match(text)
    if not m:
        return None
    token = m.group(1).strip()
    if token.lower().startswith("appendix"):
        # normalize "Appendix  C" -> "Appendix C"
        letter = token.split()[-1].upper()
        return f"Appendix {letter}"
    return token


def walk_document(doc: "Document") -> tuple[list[Block], str]:
    """Return (ordered blocks, normalized full body text)."""
    blocks: list[Block] = []
    text_parts: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag != qn("w:p"):
            continue
        # Reconstruct paragraph text from runs (python-docx Paragraph would
        # re-wrap, but we already hold the element).
        runs_text = "".join(t.text or "" for t in child.findall(".//" + qn("w:t")))
        style_el = child.find(qn("w:pPr") + "/" + qn("w:pStyle"))
        style = style_el.get(qn("w:val")) if style_el is not None else "Normal"
        has_image = _para_has_blip(child)
        is_heading = bool(style and style.lower().startswith("heading"))
        block = Block(
            kind="heading" if is_heading else "para",
            text=runs_text,
            style=style or "Normal",
            has_image=has_image,
        )
        if is_heading:
            block.section = _heading_section(runs_text)
        blocks.append(block)
        if runs_text:
            text_parts.append(runs_text)
    return blocks, _normalize(" ".join(text_parts))


# --------------------------------------------------------------------------- #
# Image / SHA helpers
# --------------------------------------------------------------------------- #
def embedded_image_shas(doc: "Document") -> tuple[set[str], int]:
    """Return (set of SHA-256 hex digests, count of embedded image parts)."""
    shas: set[str] = set()
    count = 0
    for _rid, part in doc.part.related_parts.items():
        ctype = getattr(part, "content_type", "") or ""
        if ctype.startswith("image/"):
            count += 1
            try:
                shas.add(hashlib.sha256(part.blob).hexdigest())
            except Exception:
                pass
    return shas, count


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# TOC detection (#2)
# --------------------------------------------------------------------------- #
def has_toc_field(doc: "Document") -> bool:
    root = doc.element.body
    # w:fldSimple with a TOC instruction
    for fld in root.iter(qn("w:fldSimple")):
        instr = fld.get(qn("w:instr")) or ""
        if "TOC" in instr.upper():
            return True
    # complex field: w:instrText containing TOC
    for instr in root.iter(qn("w:instrText")):
        if instr.text and "TOC" in instr.text.upper():
            return True
    return False


def toc_entry_count(blocks: list[Block]) -> int:
    """Count paragraphs styled as a TOC entry (style id starts with 'TOC')."""
    return sum(1 for b in blocks if b.style and b.style.upper().startswith("TOC"))


# --------------------------------------------------------------------------- #
# Verdict assembly
# --------------------------------------------------------------------------- #
def build_verdict(docx_path: Path,
                  inventory_path: Path | None,
                  diagrams_path: Path | None) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "status": "pass",
        "missing_components": [],
        "missing_source_files": [],
        "missing_infra_components": [],
        "missing_deployment_components": [],
        "missing_repos": [],
        "missing_sections": [],
        "missing_diagrams": [],
        "misplaced_diagrams": [],
        "duplicate_diagrams": [],
        "low_confidence_conversions": [],
        "issues": [],
    }
    issues: list[dict[str, str]] = verdict["issues"]

    def fail(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})
        verdict["status"] = "fail"

    # ---- #1 DOCX exists & > 50 KB ----------------------------------------- #
    if not docx_path.exists():
        fail("DOCX_MISSING", f"{docx_path} not found")
        return verdict
    size = docx_path.stat().st_size
    if size <= 50 * 1024:
        fail("DOCX_TOO_SMALL", f"{size} bytes (<= 50KB)")

    doc = Document(str(docx_path))
    blocks, body_text = walk_document(doc)

    # ---- #2 TOC ----------------------------------------------------------- #
    if not has_toc_field(doc):
        fail("TOC_MISSING", "no w:fldSimple/w:instrText TOC field found")
    else:
        entries = toc_entry_count(blocks)
        if entries < 10:
            fail("TOC_NOT_POPULATED", f"only {entries} TOC entry paragraphs (<10)")
        if TOC_PLACEHOLDER in body_text:
            fail("TOC_NOT_POPULATED", "body contains the unpopulated-field placeholder")

    # ---- #18 forbidden phrases ------------------------------------------- #
    for phrase in FORBIDDEN_DIAGRAM_PHRASES:
        if phrase in body_text:
            fail("DIAGRAM_REFERENCED_BY_TEXT", f"forbidden phrase present: {phrase!r}")

    # ---- section presence ------------------------------------------------- #
    present_sections = {b.section for b in blocks if b.section}
    for sec in CANONICAL_SECTIONS:
        if sec not in present_sections:
            verdict["missing_sections"].append(sec)
    if verdict["missing_sections"]:
        fail("SECTION_MISSING",
             f"{len(verdict['missing_sections'])} canonical sections absent")

    # ---- #22 caption-without-image --------------------------------------- #
    prev_had_image = False
    for b in blocks:
        if b.kind == "para" and CAPTION_RE.match(b.text):
            if not prev_had_image:
                fail("CAPTION_WITHOUT_IMAGE",
                     f"caption {b.text.strip()[:48]!r} not preceded by an image")
        # a paragraph "has image" if it itself carries a blip
        prev_had_image = b.has_image

    # ---- image SHA inventory --------------------------------------------- #
    shas, embedded_count = embedded_image_shas(doc)

    # ---- diagrams.json driven checks ------------------------------------- #
    if diagrams_path and diagrams_path.exists():
        try:
            rows = _load_json(diagrams_path)
        except Exception as exc:
            fail("DIAGRAMS_JSON_UNREADABLE", str(exc))
            rows = []
        if isinstance(rows, dict):
            rows = rows.get("diagrams", [])
        row_count = len(rows)

        # #13 count parity
        if embedded_count < row_count:
            fail("DIAGRAM_COUNT_MISMATCH",
                 f"{embedded_count} embedded images < {row_count} diagram rows")

        # #12 each PNG embedded by SHA + per-section expectation for #20
        expected_per_section: dict[str, int] = defaultdict(int)
        for row in rows:
            png = row.get("png_path")
            sec = str(row.get("target_section") or "").strip()
            rid = row.get("id", "<unknown>")
            if sec:
                expected_per_section[sec] += 1
            if not png:
                verdict["missing_diagrams"].append(rid)
                fail("DIAGRAM_NOT_EMBEDDED", f"{rid}: no png_path in diagrams.json")
                continue
            png_path = Path(png)
            if not png_path.is_absolute():
                png_path = (diagrams_path.parent / png_path)
            digest = file_sha(png_path)
            if digest is None:
                verdict["missing_diagrams"].append(rid)
                fail("DIAGRAM_NOT_EMBEDDED", f"{rid}: png missing on disk ({png})")
            elif digest not in shas:
                verdict["missing_diagrams"].append(rid)
                fail("DIAGRAM_NOT_EMBEDDED",
                     f"{rid}: png sha not found among embedded images")

        # #20 per-section parity (count images under each heading)
        actual_per_section = _images_per_section(blocks)
        for sec, want in expected_per_section.items():
            got = actual_per_section.get(sec, 0)
            if got < want:
                verdict["misplaced_diagrams"].append(
                    {"target_section": sec, "expected": want, "actual": got})
                fail("DIAGRAM_SECTION_PARITY",
                     f"section {sec}: {got} image(s) under heading, expected {want}")

    # ---- component sweep (Implementation Hint) --------------------------- #
    if inventory_path and inventory_path.exists():
        try:
            names = _component_names(_load_json(inventory_path))
        except Exception as exc:
            fail("INVENTORY_JSON_UNREADABLE", str(exc))
            names = []
        for name in names:
            if not _word_boundary_contains(body_text, name.lower()):
                verdict["missing_components"].append(name)
        if verdict["missing_components"]:
            fail("COMPONENT_NOT_DOCUMENTED",
                 f"{len(verdict['missing_components'])} component name(s) absent from prose")

    return verdict


def _images_per_section(blocks: list[Block]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    current: str | None = None
    for b in blocks:
        if b.kind == "heading" and b.section:
            current = b.section
        if b.has_image and current is not None:
            counts[current] += 1
    return counts


def _component_names(inventory: Any) -> list[str]:
    """Pull component display names out of components.json / portfolio json.

    Tolerant of a few shapes: a top-level list, {"components": [...]}, or
    {"repos": [{"components": [...]}]}. Each component may be a string or a
    dict with a 'name' key.
    """
    out: list[str] = []

    def harvest(items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict) and "name" in it:
                out.append(str(it["name"]))

    if isinstance(inventory, list):
        harvest(inventory)
    elif isinstance(inventory, dict):
        harvest(inventory.get("components"))
        for repo in inventory.get("repos", []) or []:
            if isinstance(repo, dict):
                harvest(repo.get("components"))
    # de-dup, keep order, drop trivially short names
    seen: set[str] = set()
    deduped: list[str] = []
    for n in out:
        n = n.strip()
        if len(n) >= 3 and n.lower() not in seen:
            seen.add(n.lower())
            deduped.append(n)
    return deduped


def _word_boundary_contains(haystack: str, needle: str) -> bool:
    if not needle:
        return True
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     haystack) is not None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mechanical DOCX coverage validator (prints JSON verdict).")
    ap.add_argument("--docx", required=True, type=Path)
    ap.add_argument("--inventory", type=Path,
                    help="components.json / portfolio-components.json")
    ap.add_argument("--diagrams", type=Path, help="diagrams.json")
    args = ap.parse_args(argv)

    try:
        verdict = build_verdict(args.docx, args.inventory, args.diagrams)
    except Exception as exc:  # pragma: no cover - last-resort guard
        print(json.dumps({
            "status": "fail",
            "issues": [{"code": "VALIDATOR_CRASHED", "detail": repr(exc)}],
        }))
        return 2

    print(json.dumps(verdict, indent=2))
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
