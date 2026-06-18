---
inclusion: always
---

# Diagram step — procedure (binding)

This is **THE procedure** for the diagram step of any atx analysis / code
assessment / repo evaluation. It **overrides** any generic task-list entry such
as "discover and render diagrams", "render diagrams", "generate architecture
diagrams", or "convert diagrams". When you reach such a step, do **not** expand
it from general knowledge — execute the numbered commands below, in order, as
your first actions for that step.

> You reliably execute explicit commands but tend to improvise generic steps
> from memory. The diagram step is therefore written as commands, not as a
> description. Hand-writing Mermaid during an analysis means you skipped this
> procedure.

## Precondition (hard)

Before the diagram step you must NOT have authored any `.mmd`, Mermaid, or PNG by
hand. **The first action of the diagram step is to run `scan` (step 1).**
Authoring Mermaid before running `scan` is a defect — delete the hand-made files
and restart from step 1.

## Run these, in order

```bash
WORK=./.ascii-work

# 0. confirm the analysis cache exists (do not regenerate it here)
test -d "$REPO/ATXDocumentation" || { echo "ATXDocumentation missing"; exit 1; }

# 1. SCAN — extract + qualify + grid-parse all ASCII/Unicode art
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    scan "$REPO/ATXDocumentation" --out "$WORK"

# 2. (you) convert ONLY $WORK/_needs_llm.json, one block at a time, saving each
#    as $WORK/mmd/<id>.mmd with the same id. If the list is empty, skip.

# 3. FINALIZE — render every .mmd to PNG and resolve placement
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    finalize --out "$WORK" --placement "$WORK/placement.json"

# 4. EMBED — insert PNGs under their target_section headings
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    embed --docx "$FINAL_DOCX" --out "$WORK"
```

After the step, `$WORK/ascii-mermaid-manifest.json` **must** exist. If it does
not, the step did not run and the validator will raise
`ASCII_CONVERSION_NOT_INVOKED`.

## Origin discipline (still applies)

Only three diagram origins may be embedded: atx-native Mermaid (already in the
`.md` files), `ascii_converted` (produced by the script above), and the two
whitelisted synthesis diagrams — `kiro-aws-target-architecture` (7.2, MCP) and
`kiro-current-state-architecture` (3.1, mmdc). **No fabricated diagrams.**
Section 7.2 stays on the MCP path; the script refuses 7.2 as a target.

## Self-trip-wire

If at any point during an analysis you are about to write Mermaid for art you
read out of a file, STOP — you are mid-way through a step you should have started
with `scan`. Delete what you wrote and run step 1.