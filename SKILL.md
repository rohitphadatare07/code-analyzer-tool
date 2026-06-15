---
name: modernization-planner
description: >-
  Generate Kiro specs that modernize an existing codebase from one programming
  language to another, grounded in the ATX comprehensive-codebase-analysis md
  knowledge base. Use this skill WHENEVER the user asks to modernize, migrate,
  port, rewrite, or convert a codebase to another language (e.g. "modernize this
  to Java", "migrate the codebase to Go", "port this from .NET to Python",
  "rewrite in Rust"). It reads the existing analysis md files, builds the module
  dependency order, pins language-neutral interface contracts, and writes one
  executable spec (requirements/design/tasks) per module plus a final integration
  spec, so the end user can convert the codebase module by module. Trigger even if
  the user does not say the word "spec" — any language-to-language migration of
  the analyzed codebase should use this skill.
compatibility: Kiro
metadata:
  category: modernization
  complexity: advanced
---

# Modernization Planner

Turns the existing ATX codebase-analysis knowledge base into a set of executable
Kiro specs, one per module, that the end user runs (supervised) to convert the
codebase to a target language — leaf module first, integration last.

**This skill plans only. It does NOT translate code.** Its output is spec files.
The end user executes those specs afterward.

Before doing anything, load the modernization policy in
`.kiro/steering/modernization.md`. It defines the conversion model (full rewrite,
integrate at the end), the leaf-first ordering, the contract rule, and the
per-module definition of done. Everything below assumes those rules.

---

## Inputs to confirm first

1. **Target language / framework.** Required. If the user did not state it, ask.
2. **Knowledge base location.** The folder of ATX `comprehensive-codebase-analysis`
   md files. Confirm the path if it is not obvious from the workspace.
3. **Source language.** Read it from the analysis md (do not guess).

If the target language is missing, stop and ask for it before generating anything.

---

## Workflow

Run these phases in order. Phases B and C each end with an **approval gate** — show
the user the artifact and wait for confirmation before continuing. Do not generate
module specs until the manifest and contracts are approved.

### Phase A — Build the module inventory

From the analysis md files, extract for every module:
- name, responsibility/summary
- public interface (functions/classes/endpoints it exposes to other modules)
- internal dependencies (other modules it calls)
- external dependencies (libraries/frameworks)
- data models it owns
- a rough size/complexity signal (LOC or similar) if available

The inter-module dependency edges are already present in the analysis md — read them
directly, do not re-derive from a code scan.

### Phase B — Order the modules (APPROVAL GATE)

Topologically sort the dependency graph **leaf-first**: a module appears only after
all modules it depends on. Detect and report any dependency cycles — for a cycle,
group the involved modules into one combined spec (they must be converted together)
and note it. Write the order to `.kiro/specs/_modernization-plan.md` using the
Manifest template below.

→ Present the manifest. Wait for the user to approve the order before continuing.

### Phase C — Pin interface contracts (APPROVAL GATE)

For every module, capture its public interface in **language-neutral** form
(signatures, parameter/return types as abstract types, data shapes, error
conditions) in `.kiro/specs/modernization-contracts/contracts.md` using the Contracts
template below. This is the shared boundary every module spec references.

→ Present the contracts. Wait for approval before generating module specs.

### Phase D — Generate one spec per module

For each module, in manifest order, create
`.kiro/specs/modernize-<module-slug>/` containing `requirements.md`, `design.md`,
and `tasks.md`, filled from the templates below and grounded in:
- that module's section of the analysis md,
- its entry in `contracts.md`,
- the target language/framework and the steering policy.

Rules while generating:
- 5–8 requirements per module spec. If a module is large, split it into
  `modernize-<module>-part1`, `-part2`, and reflect both in the manifest.
- Every public element listed in the module's contract must map to at least one
  requirement and one task.
- Tasks must be granular enough to finish in a single Kiro session; "port the unit
  tests" is always an early task so parity is checkable as code is written.
- These files are drafts for a **supervised** run. The end user reviews and approves
  requirements → design → tasks at the gates when they open the spec; write them so
  each phase reads cleanly on its own.

### Phase E — Generate the final integration spec

Create `.kiro/specs/modernize-_integration/` (requirements/design/tasks) covering:
wiring the converted modules together, the target-language build, dependency
resolution, and the **end-to-end** tests that were deferred until now. This spec
depends on every module spec and is always last in the manifest.

### Phase F — Hand off

Tell the user the plan is ready and how to run it: open each spec listed in
`_modernization-plan.md` **in manifest order**, execute supervised, and mark the
module complete in the manifest before moving to the next. Remind them that
end-to-end testing happens only in `modernize-_integration`, after all modules
are done.

---

## Templates

Placeholders use `{{...}}`. Fill every one; delete bracketed guidance notes.

### Manifest — `.kiro/specs/_modernization-plan.md`

```markdown
# Modernization Plan: {{SOURCE_LANG}} → {{TARGET_LANG}}

Source analysis: {{ANALYSIS_MD_PATH}}
Conversion model: full rewrite, integrated and end-to-end tested at the end.
Ordering: leaf-first. Execute top to bottom. Supervised.

## Module order

| # | Module | Spec | Depends on | Status |
|---|--------|------|-----------|--------|
| 1 | {{MODULE}} | modernize-{{SLUG}} | (none — leaf) | ☐ not started |
| 2 | {{MODULE}} | modernize-{{SLUG}} | {{DEPS}} | ☐ not started |
| … | … | … | … | … |
| N | Integration | modernize-_integration | all modules | ☐ not started |

Status legend: ☐ not started · ◐ in progress · ☑ done

## Cycles / combined specs
{{Note any dependency cycles that were merged into a single spec, or "none".}}

## Notes
{{Anything the executor should know before starting.}}
```

### Contracts — `.kiro/specs/modernization-contracts/contracts.md`

```markdown
# Interface Contracts (language-neutral)

These signatures are the fixed boundary between modules. Internal implementations
may be reshaped to be idiomatic in {{TARGET_LANG}}; these public contracts must not
drift. To change one, edit it here first and flag dependents in the manifest.

## Module: {{MODULE}}
### Public operations
- `{{operation}}(param: {{AbstractType}}, …) -> {{AbstractType}}`
  - Purpose: {{what it does}}
  - Errors / failure modes: {{conditions}}
### Data shapes
- `{{TypeName}}` = { {{field}}: {{AbstractType}}, … }
### Consumed by
- {{modules that call this one}}

## Module: {{MODULE}}
…
```

### Per-module `requirements.md`

```markdown
# Requirements: Modernize {{MODULE}} ({{SOURCE_LANG}} → {{TARGET_LANG}})

## Introduction
Convert the {{MODULE}} module to {{TARGET_LANG}} with behavioral parity. Public
contract is fixed in modernization-contracts/contracts.md. Done = parity proven by
ported unit tests + no source-language references remaining.

## Requirements

### Requirement 1 — {{capability}}
**User story:** As the system, the converted {{MODULE}} SHALL preserve {{behavior}}.
**Acceptance criteria (EARS):**
1. WHEN {{condition/event}} THE SYSTEM SHALL {{expected behavior}}.
2. WHEN {{edge case}} THE SYSTEM SHALL {{expected behavior}}.
3. IF {{error condition}} THEN THE SYSTEM SHALL {{handling}}.

### Requirement 2 — Contract conformance
1. THE SYSTEM SHALL expose every operation defined for {{MODULE}} in contracts.md
   with equivalent inputs and outputs.

### Requirement 3 — Test parity
1. WHEN the ported unit tests run THE SYSTEM SHALL pass every case that passed in the
   source module.

[5–8 requirements total. Each public contract element must appear here.]
```

### Per-module `design.md`

```markdown
# Design: Modernize {{MODULE}}

## Source → target mapping
- Language: {{SOURCE_LANG}} → {{TARGET_LANG}}
- Framework: {{src framework}} → {{target framework}}
- Library substitutions: {{src lib}} → {{target lib}}, …
- Error handling: {{src idiom}} → {{target idiom}}
- Concurrency: {{src model}} → {{target model}}
- Naming: {{e.g. snake_case → camelCase}}

## Public contract (from contracts.md)
{{List the operations/data shapes this module must expose, verbatim from contracts.md.}}

## Data model translation
{{Each source type → target type, with notes on nullability, numeric precision, etc.}}

## Dependencies
- Already-converted modules this calls (use their target-language API directly): {{list}}
- External libraries in target: {{list}}
(No legacy interop shims — full-rewrite model; integration happens in the final spec.)

## Components & structure
{{Target-language file/package layout for this module.}}

## Risks
{{Anything tricky: behavioral differences between language runtimes, precision,
date/locale handling, etc.}}
```

### Per-module `tasks.md`

```markdown
# Tasks: Modernize {{MODULE}}

- [ ] 1. Scaffold the {{MODULE}} package in {{TARGET_LANG}} (build entry, structure).
- [ ] 2. Port unit tests from the source module first (red state expected).
  - _Requirements: Req 3_
- [ ] 3. Translate data models / types per design.
  - _Requirements: Req 1, 2_
- [ ] 4. Translate {{public operation A}} to satisfy its contract.
  - _Requirements: Req 1, 2_
- [ ] 5. Translate {{public operation B}} …
- [ ] 6. Translate internal helpers and wire the module together.
- [ ] 7. Make all ported tests pass; confirm parity.
  - _Requirements: Req 3_
- [ ] 8. Remove any remaining {{SOURCE_LANG}} references; build standalone.

[Each task completable in one session. Tests-first. Map tasks back to requirements.]
```

---

## Guardrails

- Plan only — never translate code in this skill. Stop at written specs.
- Never skip the manifest and contracts approval gates.
- Take the dependency graph from the analysis md; do not invent edges.
- Keep public contracts stable; route any contract change through contracts.md first.
- If the target language is unknown, ask before generating.
