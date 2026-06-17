---
name: ascii-to-mermaid-diagrams
description: >
  Convert ASCII / Unicode art found in atx analysis Markdown into validated
  Mermaid diagrams rendered to PNG and embedded into the DOCX. ALWAYS use this
  skill whenever the analysis Markdown under ATXDocumentation/ contains hand-drawn
  box-and-arrow art, flow diagrams, or any non-Mermaid diagram drawn with
  characters. Runs a deterministic pipeline (scan → finalize → embed); you only
  intervene to convert the handful of blocks the parser cannot read. Does NOT
  touch Section 7.2 — that diagram is owned by the awslabs.aws-diagram-mcp-server
  MCP path.
---

# ASCII → Mermaid diagrams

The mechanical work is done by `scripts/ascii_diagram_pipeline.py`. Your role is
a **per-diagram subroutine**, not the orchestrator: run the script, then convert
only the blocks it could not parse, one at a time. Do not hand-extract,
hand-classify, or hand-render — that is what caused diagram drops.

**Scope:** every section except **7.2**. The Section 7.2 target architecture is
synthesized separately via the AWS Diagram MCP and must not be produced here. The
pipeline refuses 7.2 as a placement target; never override that.

## Pipeline

Run from the workspace root. `<work>` is a scratch dir (e.g. `./.ascii-work`).

### 1. scan

```bash
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    scan <REPO_PATH>/ATXDocumentation --out <work>
```

Extracts fenced/indented candidate blocks, rejects non-diagrams (markdown
tables, tree listings, logs, source code), grid-parses confidently-readable art
into `<work>/mmd/<id>.mmd`, and lists everything it could not parse in
`<work>/_needs_llm.json`. Writes `<work>/_manifest.partial.json`.

### 2. (you) convert the residue

For each entry in `<work>/_needs_llm.json`, read its `raw` block and
`suggested_type`, write correct Mermaid, and save it as
`<work>/mmd/<id>.mmd` (same `id`). Convert **one block at a time**; do not batch.
Keep node labels faithful to the art; do not invent edges. Leave alone any block
you genuinely cannot interpret — a dropped uncertain block is better than a
fabricated diagram. If `_needs_llm.json` is empty, skip this step.

### 3. finalize

```bash
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    finalize --out <work> --placement <work>/placement.json
```

Renders each `.mmd` to PNG (mmdc; width by node count; playwright fallback;
content-hash render cache), resolves each diagram's `target_section` from
`placement.json`, writes `<work>/ascii-mermaid-manifest.json`, and runs a
self-check. A non-zero exit means the self-check found problems (unrendered
diagrams, sub-5KB PNGs, or a 7.2 target) — fix them before embedding.

`placement.json` (optional) maps diagrams to sections:

```json
{
  "by_id":      {"ascii-003": "4.4"},
  "by_heading": {"data flow": "4.4", "deployment": "6.1"},
  "default":    "Appendix F"
}
```

Anything unresolved lands in **Appendix F. Architecture Diagrams** (the declared
fallback section). Keep appendix placements ≤10% of diagrams; if more than that
fall through, supply better `by_heading` hints rather than accepting the
degraded placement.

### 4. embed

```bash
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    embed --docx <FINAL_DOCX> --out <work>
```

Inserts each PNG under its `target_section` heading with a `Figure <n>` caption,
then asserts `embedded_count == manifest_count`. If that assertion fails the DOCX
is not coverage-complete — do not proceed to validation.

## Output contract

- `<work>/ascii-mermaid-manifest.json` — one row per converted diagram with
  `id, source="ascii_converted", target_section, render_method="mmdc",
  png_path, node_count, edge_count, confidence, mermaid_status, render_status,
  original_md_path, original_line_range`.
- Every row's PNG exists, is >5KB, and is embedded (not linked) in the DOCX.
- The manifest is what the `repo-completeness-validator` checks against
  (`ASCII_CONVERSION_NOT_INVOKED` if it is missing).

## Notes

- **Self-contained.** All logic is in `scripts/`; there are no `references/`
  paths (Kiro #6955). The script depends only on `python-docx` and, for
  rendering, `@mermaid-js/mermaid-cli` (auto-installed if absent).
- **Multi-repo.** Run scan/finalize per repo; the manifest `id`s are unique per
  run. Per-repo PNG naming and `[<repo_name>]` caption prefixing are applied by
  the multi-repo analysis skill, not here.
- **Never** emit "see diagrams folder" or "diagram unavailable" text into the
  DOCX — every diagram is an embedded image or it is dropped.
