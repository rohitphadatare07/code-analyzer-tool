# PATCHES — two small inconsistencies (handoff §6.4, §6.5)

These touch files whose full text was not in the handoff, so they are given as
exact edits rather than regenerated files.

## §6.4 — declare "Appendix F. Architecture Diagrams"

`Appendix F` is used as the diagram fallback landing zone by `diagram-placement`,
`docx-output-standards`, `atx-single` Phase 6, and the validator
(`misplaced_diagrams.actual = "Appendix F"`), but is not declared in the
canonical section tree.

**File:** `.kiro/steering/repo-component-coverage.md`
**Edit:** in the canonical section tree, immediately after the `Appendix E` line,
add:

```
Appendix F. Architecture Diagrams (all converted ASCII + fallback-placed diagrams)
```

Also add a one-line coverage rule so the appendix is governed, not just declared:

```
- Appendix F holds every diagram whose target_section could not be resolved;
  it must contain only embedded PNGs (never links), and ≤10% of all diagrams
  may land here (else placement is degraded — see diagram-placement).
```

(The validator already treats `Appendix F` as canonical — see
`CANONICAL_SECTIONS` in `tools/validate_docx_coverage.py` — so this only brings
the steering file into line with the code.)

## §6.5 — folder / skill name mismatch

Folder `.kiro/skills/spec-creation/` contains the skill whose frontmatter `name`
is `modernization-planner`. Kiro keys off the frontmatter `name`, and nothing
references the skill by folder path, so this is cosmetic.

**Preferred:** rename the folder to match the skill name.

```bash
git mv .kiro/skills/spec-creation .kiro/skills/modernization-planner
```

**If you prefer not to rename**, add this note to the top of the skill body so
the mismatch is intentional and discoverable:

```
> Folder is historically named `spec-creation`; the skill name is
> `modernization-planner`. Kiro activates on the frontmatter name, so the folder
> name is inert.
```

## File placement reference (for the generated artifacts)

| Generated file | Lives at |
|---|---|
| `modernization.md` | `.kiro/steering/modernization.md` |
| `SKILL.md` (slim) | `.kiro/skills/ascii-to-mermaid-diagrams/SKILL.md` |
| `ascii_diagram_pipeline.py` | `.kiro/skills/ascii-to-mermaid-diagrams/scripts/ascii_diagram_pipeline.py` |
| `validate_docx_coverage.py` | `tools/validate_docx_coverage.py` (workspace root) |

The `repo-completeness-validator` SKILL.md already names
`tools/validate_docx_coverage.py` in its Implementation Hint, so no edit is
needed there — just drop the file in.
