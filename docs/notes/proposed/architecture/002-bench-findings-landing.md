# 002 Bench Findings Landing — what agents-bench owes the loop

Provenance: Fabian, 2026-11-10 agents-bench session ("then that should
go into agent-harness"). Three ideas folded in; a fourth (LLM
symbol-minting bridge as loop module) considered and already
superseded — the bridge belongs to the memory store, not the loop.

## A. Tool/predicate registration — steal the manifest shape

Worth stealing for the canonical-store work: predicates register the
way Cordis modules register verbs — a declarative manifest
(`name`, `kind`, `signature`, `rests_on`), catalog-resolved, nothing
baked in. Same shape as `runner/modules/*.json` and
`runner/providers/*.json`: a fact grammar's predicates are declared
capabilities resting on verified primitives, discovered by the catalog
rather than compiled in. When the canonical store (agents-bench
`stores/rel.py`, `stores/u.py`) grows learned minting, a registered
predicate is just another module manifest — the loop never special-
cases it.

## B. Quotient-tower merge/re-quotient as scheduled flow

The OptMem tower's merge policy (agents-bench `stores/optmem.py`:
budget overflow → merge oldest span → cascade upward) is a *care* loop,
not a store-internal trigger. `runner/schedules/` is its home: declare
the rebalance cadence as a flow resting on a verified verb
(`agents-memory tower.rebalance` or similar), `exit 0 = healthy`. The
store stays dumb — it exposes `merge`, `lineage`, `coverage`; the loop
owns *when* and *how deep*. This matches 003-per-request-loop:
scheduled care jobs stay in `runner/schedules`, they are not the loop.
Consequence already measured bench-side: re-quotienting is periodic
maintenance against latent drift, which is exactly a cadence decision.

## C. Dreaming passes land here

P1 retrodiction / P2 congruence sweep / P3 temporal recourse were
prototyped in agents-bench (session-close-2026-08-25 memory note:
proof-of-concept, "will land properly later inside the actual
agents-harness"). When the tower exists as an agents-memory backend,
the dreaming passes are loop-level reflective flows over it — again
scheduled flows, not store internals.
