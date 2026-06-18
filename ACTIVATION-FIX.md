# ACTIVATION FIX — why the ASCII skill didn't auto-trigger, and how to wire it

## Diagnosis

Kiro skill **activation is description-driven**: the model reads the available
skill descriptions and decides which to load *when it's choosing what to do*.
That decision happens at the top of a task, not continuously. During a full atx
run the model is already executing `atx-single-repo-analysis` and never
re-opens the "which skills should I pull in?" question mid-flight. So when the
diagram phase says, in prose, "convert any ASCII art to Mermaid," the model just
does it by hand — exactly the nondeterministic hand-conversion the script was
built to replace. Asking explicitly works only because it forces a fresh
activation decision.

**Conclusion:** a sub-step skill should not rely on auto-activation inside a
larger run. Make the conversion a hard, scripted step of the orchestrator. The
ASCII skill then never has to "trigger" — the orchestrator runs its script
directly.

## The three changes

### 1. Orchestrator calls the script directly  (primary — do this one)

Replace the prose ASCII step in the analysis skills with a literal command
sequence. Below are drop-in replacements.

**File:** `.kiro/skills/atx-single-repo-analysis/SKILL.md`
**Phase:** 5a/5b (ASCII extract / qualify / parse / render). Replace whatever
prose currently describes "convert ASCII art" with:

````markdown
#### Phase 5a/5b — ASCII → Mermaid (deterministic, non-optional)

Do NOT convert ASCII/Unicode art by hand. Run the pipeline script:

```bash
WORK=./.ascii-work
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    scan "$REPO_PATH/ATXDocumentation" --out "$WORK"
```

Then open `$WORK/_needs_llm.json`. For each entry, write correct Mermaid for its
`raw` block and save it as `$WORK/mmd/<id>.mmd` (same id), one block at a time.
If the file is an empty list, do nothing. Then:

```bash
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    finalize --out "$WORK" --placement "$WORK/placement.json"
```

`$WORK/ascii-mermaid-manifest.json` now drives the embed step. Its rows merge
into `diagrams.json` with `source: "ascii_converted"`. The pre-assembly gate
(Phase 5d) and the validator both require this manifest to exist.
````

The embed step (Phase 5e) should call:

```bash
python .kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py \
    embed --docx "$FINAL_DOCX" --out "$WORK"
```

**File:** `.kiro/skills/atx-multi-repo-analysis/SKILL.md`
**Phase:** 3 (per-repo diagrams). Same three commands, run **once per repo**,
with a per-repo `$WORK` (e.g. `./.ascii-work/<repo_name>`) so manifests and PNGs
don't collide. The `[<repo_name>]` caption prefix is applied by the multi-repo
skill after embed, as today.

### 2. Always-on steering rule  (catches every turn)

`.kiro/steering/diagram-pipeline-binding.md` (new, `inclusion: always`) forbids
hand-conversion and names the script. Because steering loads into every agent
turn, it applies mid-run even when no skill "activated." This is the safety net
behind change #1.

### 3. Pushier, task-triggered description  (backstop)

The `ascii-to-mermaid-diagrams` SKILL.md description now triggers on the umbrella
activity ("atx codebase analysis / code assessment that produces a DOCX") and the
words you actually type, instead of on a condition the model has to notice. This
helps when ASCII art shows up outside a full atx run.

## Why all three

Change #1 alone fixes the common path (a normal atx run). #2 covers any phase or
ad-hoc flow that doesn't go through the orchestrator. #3 covers standalone use.
Together they make "Kiro hand-converts ASCII" a defect the system actively
prevents rather than something you have to ask for each time.

## Verifying it took

After a run, the diagram phase should leave `ascii-mermaid-manifest.json` on
disk. If it's missing, the validator raises `ASCII_CONVERSION_NOT_INVOKED` — that
is now the signal that the script step was skipped.
