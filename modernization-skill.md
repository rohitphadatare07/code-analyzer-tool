---
name: modernization-planner
description: >-
  Generate Kiro specs that modernize a codebase from one programming language to
  another, grounded in the atx comprehensive-codebase-analysis md knowledge base.
  Works for BOTH a single repository (module-by-module) and a portfolio of
  microservice repositories (service-by-service). Use this skill WHENEVER the user
  asks to modernize, migrate, port, rewrite, or convert code to another language —
  e.g. "convert this service to <lang>", "migrate these microservices to <lang>",
  "port the <lang> services to <lang>", "rewrite this codebase in <lang>",
  "modernize service A to <lang>". The skill detects whether the target is one repo
  or many, detects each unit's SOURCE language from its analysis, takes the TARGET
  language from the user (any language — no fixed list), builds the dependency
  order, pins language-neutral contracts, and writes one executable spec per unit
  plus a final integration spec. Trigger even if the user does not say "spec" — any
  language-to-language migration of analyzed code uses this skill.
compatibility: Kiro
metadata:
  category: modernization
  complexity: advanced
---

# Modernization Planner

Turns the existing atx codebase-analysis knowledge base into a set of executable
Kiro specs that the end user runs (supervised) to convert code to a target
language. It handles two topologies and **routes between them automatically**:

- **Monorepo** — one repository; the unit of conversion is a **module**; the frozen
  boundary between units is a **function/class signature**.
- **Polyrepo / microservices** — many repositories; the unit of conversion is a
  **service (repo)**; the frozen boundary between units is a **network API contract**
  (OpenAPI / protobuf / AsyncAPI). A large service is additionally exploded into
  module specs using the monorepo routine.

All polyrepo specs are written to a **single centralized control workspace**, not
scattered across the service repos. Each spec names the target repo it applies to.

**This skill plans only. It does NOT translate code.** Its output is spec files.
The end user executes those specs afterward.

---

## Core policy (self-contained)

These defaults govern every run. If `.kiro/steering/modernization.md` exists, the
choices recorded there override these defaults; if it does not exist, use these as
written (do not fail, do not ask for it).

- **Plan only.** Never translate code here. Stop at written specs.
- **Conversion model:** full rewrite, **big-bang**. Converted units are integrated
  and end-to-end tested in a single final integration spec — not wired back into the
  source-language system mid-flight.
- **Ordering:** **leaf-first**. A unit is scheduled only after every unit it depends
  on. Dependency cycles are merged into one combined spec (converted together).
- **Frozen boundary:** pinned in **language-neutral** form before any unit is
  generated. Internals may become fully idiomatic in the target language; the public
  boundary must not drift. To change a boundary, edit the contract first and flag
  dependents.
- **Language-agnostic:** the **source** language of each unit is *detected* from its
  analysis md (never assumed, never matched against a fixed list). The **target**
  language is whatever the user states (any language). All language-pair specifics
  (idioms, frameworks, libraries) are written into each spec's `design.md` at
  generation time — this skill hard-codes no language pair.
- **Definition of done (per unit):** every element of its contract implemented in the
  target language; behavioral parity proven by **ported tests**; no source-language
  references left; builds standalone in the target toolchain.
- **Execution:** the generated specs are run **supervised** — approval between
  requirements → design → tasks, and before each task.

---

## Phase 0 — Router (ALWAYS run first)

Resolve three things, in order, then dispatch. Do not generate anything in Phase 0.

### 0.1 Detect topology (monorepo vs polyrepo)
Decide from concrete signals, strongest first:
- **Filesystem:** one repo containing a single `ATXDocumentation/` (with internal
  modules) → **monorepo**. Multiple sibling repositories each owning their own
  `ATXDocumentation/` → **polyrepo**.
- **User phrasing:** "this codebase / module-by-module" leans monorepo; "these
  services / the platform / the microservices" leans polyrepo.
- **Conflict / ambiguity** (e.g. a single repo that contains several independently
  deployable services): ask exactly ONE disambiguating question and wait. Do not guess.

### 0.2 Extract the target language
Read the target from the user's request as an **opaque string** (e.g. Go, Rust,
Python, Kotlin, TypeScript, Java — anything). Never validate it against a known list.
If no target was stated, stop and ask for it before continuing.

### 0.3 Extract the scope
Determine which units are in scope from the request:
- **All** — "modernize the codebase / the platform" → every module (mono) or every
  service (poly).
- **By source language** — "convert the `<X>` services to `<target>`" → select only
  the services whose **detected** source language is `<X>`.
- **Named unit** — "convert service A to `<target>`" / "modernize the `<X>` module" →
  that single unit only.

### 0.4 Dispatch
- monorepo → **Track M**.
- polyrepo → **Track P**.

Carry `{{TARGET_LANG}}` and the resolved scope into the chosen track.

---

## Track M — Monorepo (module-by-module)

Phases end with **approval gates** where noted; wait for confirmation before
continuing. Do not generate module specs until the manifest and contracts are approved.

### M-A — Build the module inventory
From the analysis md, for every in-scope module extract: name, responsibility,
public interface (functions/classes it exposes), internal dependencies (modules it
calls), external dependencies (libraries), data models it owns, and a size/complexity
signal. Read inter-module edges directly from the md; do not re-derive from a code scan.
Detect the module's **source language** from the md here.

### M-B — Order the modules (APPROVAL GATE)
Topologically sort leaf-first. Detect cycles; merge each cycle's modules into one
combined spec. Write the order to `.kiro/specs/_modernization-plan.md` (Manifest
template). → Present the manifest; wait for approval.

### M-C — Pin interface contracts (APPROVAL GATE)
Capture each module's public interface in **language-neutral** form (signatures,
abstract parameter/return types, data shapes, error modes) in
`.kiro/specs/modernization-contracts/contracts.md` (Function-Contract template).
→ Present the contracts; wait for approval.

### M-D — Generate one spec per module
For each module in manifest order, create `.kiro/specs/modernize-<module-slug>/`
with `requirements.md`, `design.md`, `tasks.md` from the Unit templates, grounded in
that module's md section, its `contracts.md` entry, and the source→target pair.
- 5–8 requirements per spec; split very large modules into `-part1`/`-part2`.
- Every contract element maps to ≥1 requirement and ≥1 task.
- "Port the unit tests" is always an early task.

### M-E — Final integration spec
Create `.kiro/specs/modernize-_integration/` covering wiring, the target build,
dependency resolution, and the **end-to-end** tests deferred until now. Depends on
every module spec; always last.

### M-F — Hand off
Tell the user to run each spec in manifest order, supervised, marking each complete
before the next; end-to-end tests live only in `modernize-_integration`.

---

## Track P — Polyrepo / microservices (service-by-service)

atx analyzed each repo **in isolation**, so no single `ATXDocumentation/` contains
the cross-service graph. Reconstructing it is the first job.

### P-0 — Select services + detect source languages
Apply the Phase 0.3 scope to the set of service repos:
- **All** → every service repo with an analysis.
- **By source language** → for each service, **detect its source language** from its
  own analysis md, then keep only those matching the requested source.
- **Named** → just that service (still detect its source language from its md).
Confirm the resolved selection back to the user before generating, e.g.:
"Found 5 services whose detected language is `<X>`: a, b, c, d, e — converting all to
`{{TARGET_LANG}}`. Proceed?" Wait for confirmation.

### P-1 — Stitch the cross-service dependency graph (APPROVAL GATE)
Build one portfolio graph by matching, across the isolated analyses:
- each service's **outbound calls** (external endpoints/URLs/hosts it invokes, topics
  it publishes) to
- another service's **inbound surface** (routes it exposes, service name/DNS identity,
  topics it subscribes to).
Augment with any cross-service signal available: API-gateway config, service-mesh
routing, Kubernetes/Helm manifests, the OpenAPI/proto/AsyncAPI files checked into each
repo, and distributed-trace data. Output the portfolio inventory + inter-service edges.
(This mirrors the `atx-multi-repo-analysis` portfolio-stitching pattern.)
→ Present the graph (who calls whom). Wait for approval — a wrong edge mis-orders
everything downstream.

### P-2 — Build the API-contract registry (APPROVAL GATE)
For every in-scope service, capture its **inbound network API** in language-neutral
form (endpoints/methods, request/response shapes, status/error codes, event schemas)
in the **control workspace** (not inside any one service repo):
`./_modernization-control/contract-registry.md` (API-Contract template). Each service
is both a **provider** (its inbound API is frozen so callers keep working) and a
**consumer** (it may rely only on the frozen contracts of services it calls).
→ Present the registry. Wait for approval before generating service specs.

> The control workspace holds **all** modernization artifacts for the portfolio —
> the registry, the portfolio plan, every service spec, and the integration spec.
> Default location is `./_modernization-control/.kiro/` at the portfolio root; if
> `modernization.md` specifies a different location or owner, use that.

### P-3 — Order the services leaf-first
Topologically sort the service graph leaf-first; merge cyclic service pairs into one
combined spec. Write `_modernization-control/_portfolio-plan.md` (Portfolio-Manifest
template). Leaf-first here protects **contract stability** (a service's API is pinned
before its callers are rebuilt against it).

### P-4 — Generate one spec per service (centralized)
For each service in portfolio order, create
`_modernization-control/.kiro/specs/modernize-<service-slug>/` with `requirements.md`,
`design.md`, `tasks.md` from the Unit templates, grounded in that service's analysis,
its registry entry (the inbound contract it must preserve), and the contracts of the
services it calls. **Every spec is centralized — none are written into the service
repos.** Because the spec is now decoupled from the repo it modifies, each spec's
`requirements.md` MUST carry a `Target repo:` header naming the repo whose code the
spec converts, so the executor knows where the resulting code edits land. The
source→target pair drives `design.md`'s idiom/framework/library mapping. Each service
spec must:
- preserve its **inbound API contract exactly** (same routes, shapes, codes, event
  schemas) so callers — converted or not — are unaffected;
- depend only on the **frozen contracts** of the services it calls;
- include **consumer-driven contract tests** at every boundary (provider proves it
  still honors its contract; consumer proves it relies only on what the contract
  declares).

**Within-service decomposition (complexity threshold).** For each selected service,
decide whether it is large/complex enough to warrant exploding into module specs:
- **Below threshold** (small/simple service) → a **single** service spec. Stop here
  for that service.
- **At/above threshold** → additionally run the **Track M routine one level down**,
  still writing centrally: build the module inventory from that service's md, pin
  **function-signature** contracts for its internal modules in
  `_modernization-control/.kiro/specs/<service-slug>-contracts/contracts.md`, order
  modules leaf-first, and emit
  `_modernization-control/.kiro/specs/modernize-<service-slug>-<module-slug>/` specs
  (each also carrying the same `Target repo:` header). The service's own network
  contract stays the outer boundary; the module specs decompose its internals.

Threshold signal (use what the analysis provides): treat a service as "complex" when,
e.g., it has many internal modules (≈8+), high LOC, or several distinct data
models/subsystems. When borderline, prefer the single service spec and note that it
may be split later.

### P-5 — Final integration spec (only if >1 service converted)
If the scope converted **more than one** service, create
`_modernization-control/.kiro/specs/modernize-_integration/` covering cross-service
wiring, deployment/build, end-to-end and contract-conformance tests across the
portfolio. Depends on every service spec; always last.
If the scope converted **exactly one** service, **skip this** — the single service
only has to honor its existing inbound contract, verified by its own contract tests;
there is nothing portfolio-wide to integrate.

### P-6 — Hand off
Tell the user: run service specs in portfolio order (and, for an exploded service, its
module specs in their inner order), supervised, marking each complete before the next.
Note that every boundary is guarded by contract tests, and (if emitted) portfolio
end-to-end tests live only in the final integration spec.

---

## Templates

Placeholders use `{{...}}`. Fill every one; delete bracketed guidance notes. Language
placeholders are filled from the **detected source** and **user-stated target** — no
language is hard-coded.

### Portfolio manifest — `_modernization-control/_portfolio-plan.md`
```markdown
# Portfolio Modernization Plan → {{TARGET_LANG}}

Scope: {{all | source-language=<X> | service=<name>}}
Conversion model: full rewrite, big-bang, integrated and end-to-end tested at the end.
Ordering: leaf-first across services. Execute top to bottom. Supervised.
Control workspace: {{_modernization-control/}}

Control workspace: _modernization-control/.kiro/ (holds ALL specs + registry + plan)

## Service order
| # | Service | Detected source | Target repo | Spec location (centralized) | Depends on | Exploded? | Status |
|---|---------|-----------------|-------------|------------------------------|-----------|-----------|--------|
| 1 | {{svc}} | {{SOURCE_LANG}} | {{repo path}} | _modernization-control/.kiro/specs/modernize-{{slug}} | (none — leaf) | no | ☐ |
| 2 | {{svc}} | {{SOURCE_LANG}} | {{repo path}} | _modernization-control/.kiro/specs/modernize-{{slug}} | {{deps}} | yes (N modules) | ☐ |
| … | … | … | … | … | … | … | … |
| N | Integration | — | — | _modernization-control/.kiro/specs/modernize-_integration | all services | — | ☐ |

Status: ☐ not started · ◐ in progress · ☑ done
(Integration row present only when >1 service is converted.)

## Cycles / combined specs
{{Cyclic service pairs merged into one spec, or "none".}}

## Notes
{{Anything the executor should know before starting.}}
```

### Manifest (monorepo) — `.kiro/specs/_modernization-plan.md`
```markdown
# Modernization Plan: {{SOURCE_LANG}} → {{TARGET_LANG}}

Source analysis: {{ANALYSIS_MD_PATH}}
Conversion model: full rewrite, integrated and end-to-end tested at the end.
Ordering: leaf-first. Execute top to bottom. Supervised.

## Module order
| # | Module | Spec | Depends on | Status |
|---|--------|------|-----------|--------|
| 1 | {{MODULE}} | modernize-{{SLUG}} | (none — leaf) | ☐ not started |
| … | … | … | … | … |
| N | Integration | modernize-_integration | all modules | ☐ not started |

## Cycles / combined specs
{{cycles merged, or "none".}}

## Notes
{{executor notes.}}
```

### API-contract registry (polyrepo) — `_modernization-control/contract-registry.md`
```markdown
# Service API Contract Registry (language-neutral)

The frozen network boundary between services. Internals may be reshaped to be
idiomatic in {{TARGET_LANG}}; these inbound contracts must not drift. To change one,
edit it here first and flag every consuming service in the portfolio plan.

## Service: {{SERVICE}}  (detected source: {{SOURCE_LANG}})
Protocol: {{REST/OpenAPI | gRPC/proto | async/AsyncAPI}}
### Inbound operations (this service PROVIDES)
- `{{METHOD}} {{/route}}` or `{{rpc Name}}` or `{{topic}}`
  - Request: {{shape / message schema}}
  - Response: {{shape}} · Status/error codes: {{...}}
  - Purpose: {{what it does}}
### Events
- Publishes: {{topic → schema}} · Subscribes: {{topic → schema}}
### Consumes (this service CALLS — must rely only on those services' contracts)
- {{service}}: {{operations used}}
### Consumed by
- {{services that call this one}}

## Service: {{SERVICE}}
…
```

### Function-contract (monorepo / within-service) — `.../modernization-contracts/contracts.md`
```markdown
# Interface Contracts (language-neutral)

Fixed boundary between modules. Internals may be reshaped to be idiomatic in
{{TARGET_LANG}}; these public contracts must not drift.

## Module: {{MODULE}}
### Public operations
- `{{operation}}(param: {{AbstractType}}, …) -> {{AbstractType}}`
  - Purpose: {{...}} · Errors / failure modes: {{...}}
### Data shapes
- `{{TypeName}}` = { {{field}}: {{AbstractType}}, … }
### Consumed by
- {{modules that call this one}}
```

### Unit `requirements.md`  (unit = module in Track M, service in Track P)
```markdown
# Requirements: Modernize {{UNIT}} ({{SOURCE_LANG}} → {{TARGET_LANG}})

Target repo: {{repo path this spec converts — Track P only; the code edits land here}}

## Introduction
Convert {{UNIT}} to {{TARGET_LANG}} with behavioral parity. Its frozen boundary is
{{the function-signature contract in contracts.md | the network API contract in the
registry}}. Done = parity proven by ported tests + boundary preserved + no
source-language references remaining.

## Requirements

### Requirement 1 — {{capability}}
**User story:** As the system, the converted {{UNIT}} SHALL preserve {{behavior}}.
**Acceptance criteria (EARS):**
1. WHEN {{condition/event}} THE SYSTEM SHALL {{expected behavior}}.
2. WHEN {{edge case}} THE SYSTEM SHALL {{expected behavior}}.
3. IF {{error condition}} THEN THE SYSTEM SHALL {{handling}}.

### Requirement 2 — Boundary conformance
1. THE SYSTEM SHALL expose every operation in {{UNIT}}'s contract with equivalent
   inputs/outputs. [Track P: same routes/messages, request/response shapes, and
   status/error codes as the registry entry.]

### Requirement 3 — Test parity
1. WHEN the ported tests run THE SYSTEM SHALL pass every case that passed in the source.
[Track P only] 2. THE SYSTEM SHALL pass consumer-driven contract tests at every
   inbound and outbound boundary.

[5–8 requirements total. Every contract element must appear here.]
```

### Unit `design.md`
```markdown
# Design: Modernize {{UNIT}}

## Source → target mapping  (filled from the detected source + stated target)
- Language: {{SOURCE_LANG}} → {{TARGET_LANG}}
- Framework: {{src framework}} → {{target framework}}
- Library substitutions: {{src lib}} → {{target lib}}, …
- Error handling: {{src idiom}} → {{target idiom}}
- Concurrency: {{src model}} → {{target model}}
- Naming: {{src convention → target convention}}
(These are discovered per engagement — no pair is assumed by the skill.)

## Frozen boundary (verbatim from its contract)
{{Track M: the operations/data shapes from contracts.md.
  Track P: the inbound API operations/schemas from the registry.}}

## Data model translation
{{Each source type → target type; nullability, numeric precision, date/locale notes.}}

## Dependencies
{{Track M: already-converted modules this calls — use their target API directly.
  Track P: services this calls — call them over the wire via their frozen contracts
  (the source-language service may still be live; the wire protocol is language-blind).}}
- External libraries in target: {{list}}

## Components & structure
{{Target-language file/package layout for this unit.}}

## Risks
{{Runtime/precision/locale differences; protocol edge cases for services.}}
```

### Unit `tasks.md`
```markdown
# Tasks: Modernize {{UNIT}}

- [ ] 1. Scaffold {{UNIT}} in {{TARGET_LANG}} (build entry, structure).
- [ ] 2. Port tests from the source first (red state expected).  _Req 3_
- [ ] 3. Translate data models / types per design.  _Req 1, 2_
- [ ] 4. Translate {{operation/endpoint A}} to satisfy its contract.  _Req 1, 2_
- [ ] 5. Translate {{operation/endpoint B}} …
- [ ] 6. Translate internal helpers and wire the unit together.
- [ ] 7. Make all ported tests pass; confirm parity.  _Req 3_
[ Track P ] - [ ] 8. Add/verify consumer-driven contract tests at each boundary.  _Req 3_
- [ ] 9. Remove any remaining {{SOURCE_LANG}} references; build standalone.

[Each task completable in one session. Tests-first. Map tasks back to requirements.]
```

---

## Guardrails

- **Plan only** — never translate code in this skill. Stop at written specs.
- **Always run Phase 0 first** — detect topology, target, and scope before anything.
  When topology is ambiguous, ask one question; never guess.
- **Language-agnostic** — detect every source language from the analysis; accept any
  user-stated target; hard-code no language pair. Pair specifics go in `design.md`.
- **Never skip approval gates** — monorepo manifest+contracts; portfolio graph+registry.
- **Take dependency edges from the analysis md** (and, for services, the stitched
  graph); do not invent edges.
- **Freeze the boundary** — function signatures within a repo, network API contracts
  between services. Route any boundary change through the contract/registry first and
  flag dependents.
- **Final integration spec only when >1 unit** is converted; a single named unit just
  honors its existing contract.
- If `.kiro/steering/modernization.md` exists, its choices override the Core policy
  defaults; if it does not exist, proceed on the defaults — do not fail.