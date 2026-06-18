---
name: ascii-to-mermaid-diagrams
description: >
  MANDATORY diagram step of every atx codebase analysis / code assessment that
  produces a DOCX. Invoke this skill during ANY comprehensive analysis, repo
  assessment, or atx run — right after atx writes ATXDocumentation/ and before
  DOCX assembly — to convert ASCII / Unicode art into validated Mermaid PNGs via
  its deterministic script. Do NOT convert ASCII art to Mermaid yourself: hand
  conversion is a defect and this script is the only sanctioned path. Also use it
  any time Markdown contains hand-drawn box-and-arrow art, flow diagrams, or
  non-Mermaid diagrams drawn with characters. Excludes Section 7.2, which is owned
  by the awslabs.aws-diagram-mcp-server MCP path.
---

# ASCII → Mermaid diagrams

This skill runs as a **step of the atx analysis pipeline**, not only on direct
request. Whenever you are performing a code assessment / atx run that emits a
DOCX, the diagram phase MUST route ASCII conversion through this skill's script.
See `.kiro/steering/diagram-pipeline-binding.md` (always loaded) — it forbids
hand-conversion in every turn.

The mechanical work is done by `scripts/ascii_diagram_pipeline.py`. Your role is
a **per-diagram subroutine**, not the orchestrator: run the script, then convert
only the blocks it could not parse, one at a time. Do not hand-extract,
hand-classify, or hand-render — that is what caused diagram drops.

**Scope:** every section except **7.2**. The Section 7.2 target architecture is
synthesized separately via the AWS Diagram MCP and must not be produced here. The
pipeline refuses 7.2 as a placement target; never override that.

## When this fires

- Inside `atx-single-repo-analysis` / `atx-multi-repo-analysis` diagram phase
  (the orchestrator calls the script directly — see "Wiring" below).
- Whenever you read ASCII/Unicode art out of any `.md` under `ATXDocumentation/`.
- On explicit request to convert ASCII diagrams.

If you are about to write Mermaid for art you read from a file, STOP and run
`scan` instead.

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

## Wiring (how the orchestrator invokes this)

`atx-single-repo-analysis` and `atx-multi-repo-analysis` must call the script by
path in their diagram phase rather than describing the conversion in prose. The
exact phase edit is in `ACTIVATION-FIX.md`. This removes the cross-skill
activation dependency entirely: the orchestrator runs the script, so this skill
never has to "auto-trigger" mid-run.

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
