---
type: belief
id: frag-context-metadata-is-behaviorally-load-bearing
provenance: agents-bench tau2race — frozen-world tasks failed on injected wall-clock metadata, not on task content
ts: 2026-11-25
---

# Context metadata is behaviorally load-bearing

Every token the substrate injects into context shapes model behavior,
including metadata that reads as inert bookkeeping. There is no neutral
channel: the model conditions on all of it.

Measured in the tau2race cage: a benchmark world frozen at 2024-05 was
sabotaged by three independent real-time leaks — the trailing `<clock>`
message, `<session start_date>` inside `<runtime_context>`, and `<ts>`
stamps on replayed history. The model searched 2026 flight dates, judged
May-2024 flights "already flown", and quoted the injected timestamps back
as facts. Suppressing all three (`AGENTS_CLOCK=0`) flipped the behavior
with zero prompt-content change.

The ruling for substrate design: time, identity, and session metadata are
part of the behavioral contract, not decoration around it. Any deployment
whose world is not the wall clock — replay, simulation, frozen benchmark —
must be able to suppress or rewrite every such surface from configuration,
never by patching code. An injection surface that cannot be turned off is
a correctness bug waiting for a frozen world.
