---
inclusion: always
---

# Modernization Policy

This file is the policy the `modernization-planner` skill loads before doing
anything. It defines the conversion model, ordering, the contract rule, the
ML-in-Python carve-out, the FE→BE rule, the three resolved program decisions,
and the per-module definition of done. The planner's phases assume these rules;
they are not restated there.

The planner **plans only** — it never translates code. Everything here
constrains the plan it writes, not an execution step.

## 1. Conversion model

**Full rewrite, integrated at the end.** Each module is reimplemented in the
target language behind a frozen, language-neutral contract, then integrated as a
unit. There is no line-by-line transpilation and no half-converted module in the
manifest. End-to-end integration tests are deferred to the final integration
spec (`.kiro/specs/modernize-_integration/`), which is always last in the
manifest.

## 2. Ordering

**Leaf-first.** Take the dependency graph from the atx analysis Markdown
(`ATXDocumentation/`), topologically sort it, and convert dependency-free leaves
first so every module is built against already-modernized dependencies. Cycles
are collapsed into a single combined spec and converted as one unit. Never
re-derive the graph by reading source — the analysis Markdown is authoritative.

## 3. Contract rule

Before any module spec is written, pin the **language-neutral interface
contract** for every boundary the module exposes or consumes. Contracts are
frozen for the duration of the migration: the new implementation must satisfy
the same contract the legacy implementation did. A contract change mid-migration
is a re-planning event, not an in-spec edit.

- **In-process boundaries** (monorepo, library calls): the contract is the
  function/method signature set plus its documented semantics, captured in
  `.kiro/specs/modernization-contracts/contracts.md`.
- **Network boundaries** (microservices / polyrepo): the contract is the
  **network API definition — OpenAPI, protobuf, or AsyncAPI — which replaces the
  function signature as the frozen boundary.** These live in the shared registry
  (see §6).

## 4. ML-in-Python carve-out

**ML code stays in Python by default, behind a contract.** Detection: a module
that imports a recognized ML/numerical library (torch, tensorflow, sklearn,
xgboost, jax, numpy-heavy pipelines, etc.) is classified ML. Do not schedule it
for conversion; instead freeze a contract around it (a service boundary or a
typed interface) and convert only the surrounding backend that calls it. The ML
module is a **per-service target-language override** (see §5), not a special
case in the ordering — it is treated as an already-satisfied leaf.

## 5. Target language

**Uniform target by default; per-service override allowed.** Unless a service is
explicitly pinned in the plan, all services convert to the single target
language/framework supplied as planner input — uniformity keeps operational and
skills overhead down. A service may be pinned to a different target where there
is a concrete reason; the canonical reason is the ML carve-out (§4), which pins
the module to Python. Because contracts are language-neutral (§3), a mixed-target
estate is consistent by construction. If the target is not supplied, the planner
**stops and asks** — it never guesses a target.

## 6. Control plane / contract registry

There is **one source of truth** for contracts, version-pinned, with a single
owner.

- **Monorepo:** the registry is `.kiro/specs/modernization-contracts/contracts.md`,
  owned by whoever owns the spec tree.
- **Polyrepo / microservices:** the registry is a dedicated **`modernization-contracts`
  repository** holding the OpenAPI/proto/AsyncAPI definitions, owned by the
  platform / architecture team. Service teams consume it via generated
  client/server stubs and propose changes by PR. During the migration the
  registry is **additive and backward-compatible only** — no breaking change to a
  frozen contract lands until both sides of that boundary have cut over.

## 7. Rollout model

**Incremental (strangler) by default.** Frozen contracts plus consumer contract
tests (§8) make module-by-module production rollout the low-blast-radius choice:
route traffic to the modernized module behind its contract, keep the legacy
implementation dark until the module's definition of done is met, then remove it.
**Big-bang cutover is permitted only** for a leaf module with no production
consumers, or where a strangler seam is genuinely impossible — and must be called
out explicitly in that module's spec.

## 8. FE→BE and cross-repo consumers

A front-end (or any cross-repo consumer) depends on a back-end one-way. That edge
is **contract-freeze + consumer contract tests**, *not* a conversion-ordering
input. Do not place a consumer ahead of its provider in the topo sort on account
of this edge; instead freeze the provider's contract and require the consumer's
contract tests to stay green across the provider's conversion.

## 9. Per-module definition of done

A module is done when **all** of these hold:

1. Every public symbol / endpoint of the module is reimplemented in its target
   language behind the pinned contract (§3).
2. Unit tests are ported **first** and pass in the target language (the planner
   schedules "port unit tests" early in every module spec).
3. For modules with cross-repo or FE consumers, the **consumer contract tests are
   green** (§8).
4. No remaining call path reaches the legacy implementation of this module.
5. Target-language static checks pass: lint, type-check, build.
6. The module is integrated behind its contract and the legacy implementation is
   removed or dark (§7). End-to-end tests are **not** required here — they live in
   the final integration spec.

## 10. Guardrails (restated for emphasis)

- Plan only; never translate.
- Never skip the planner's approval gates (Phase B inventory/ordering, Phase C
  contracts).
- Dependency graph comes from the analysis Markdown, never from re-reading source.
- Contracts stay stable; a contract change is a re-planning event.
- If the target language is unknown, stop and ask.
