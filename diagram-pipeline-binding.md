---
inclusion: always
---

# Diagram pipeline binding

Whenever an atx analysis or code assessment produces a DOCX, ASCII / Unicode art
found in `ATXDocumentation/` is converted by the deterministic script — **never
by hand**. This rule is binding inside every phase of every analysis run, not
only when the `ascii-to-mermaid-diagrams` skill is explicitly named.

**Hard rules**

- You may **not** hand-convert ASCII/Unicode art to Mermaid, hand-author `.mmd`,
  or eyeball-render a PNG. The only sanctioned path is
  `.kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py`.
- Run it as a **mandatory step of the diagram phase**, in this order:
  `scan` → (convert only the `_needs_llm.json` residue, one block at a time) →
  `finalize` → `embed`.
- **Section 7.2 is excluded.** The target architecture stays on the
  `awslabs.aws-diagram-mcp-server` MCP path; the script refuses 7.2 as a target.
- If `ascii-mermaid-manifest.json` does not exist after the diagram phase, the
  run is **incomplete** — the validator will raise `ASCII_CONVERSION_NOT_INVOKED`.

**Self-trip-wire:** if you find yourself about to write Mermaid for art you read
out of a `.md` file, stop and run `scan` instead. Producing Mermaid from ASCII by
hand is a defect, not a shortcut.
