---
type: belief
id: frag-small-models-copy-visible-shapes
provenance: agents-bench tau2race — pinned type-faithful schema examples flipped nested-op calls from invented names to byte-exact golden actions
ts: 2026-11-25
---

# Small models copy visible shapes; they do not discover contracts

A small model's tool-call arguments imitate the nearest visible example.
When no correct shape is visible, the model does not look one up — it
invents friendly names confidently, and nothing in its generation process
treats invention as failure.

Measured in the tau2race cage: across an entire benchmark matrix the 9B
never once called `load_schema` — zero discovery round trips — and winged
every nested op (`cabin_class` for schema `cabin`, `payment_method_id`
for `payment_methods`). Simple ops guessed right; nested ops guessed
wrong. When type-faithful schema examples were pinned into the prompt,
the same model produced byte-exact correct nested calls — correct names,
correct array-of-object structure, correct enum values — on the first
attempt, with no other change.

Rulings that follow:

- Discovery round trips are a discipline small models do not have.
  Contracts must be visible at generation time or enforced at execution
  time; relying on the model to look them up is relying on a behavior
  that does not occur.
- Example fidelity is a correctness surface. A placeholder that echoes
  the key name (`"flights": "flights"`) is not a neutral stand-in — it
  is a wrong shape the model will copy exactly.
- Enforcement and visibility complement each other: pinned shapes teach
  the hot path, execution-time validation catches invention in the long
  tail and returns the correct names in the same turn.
