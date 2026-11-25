---
type: belief
id: frag-derived-identity-fails-soft-at-consumption
provenance: agents-bench tau2race substrate hardening — approval allowlist silently scoped to ws-.agents on legacy hosts
ts: 2026-11-25
---

# Derived workspace identity fails soft at consumption

When a consumer resolves the active workspace for scoping decisions, a
derived-but-unregistered identity must degrade to the wider scope, never
narrow into a scope that cannot exist. Scoping is a partitioning operation:
partitioning by a phantom key does not merely fail — it silently moves data
to a place nothing will ever read.

The failure that planted this belief: on unmigrated legacy hosts the host
resolves as an ad-hoc workspace named `.agents`. The approval allowlist
then scoped approvals to `ws-.agents` — a workspace that will never exist —
silently dropping the host-wide allowlist users already had. No error
surfaced; the approvals simply stopped applying.

The complement lives in agents-workspace
(`frag-exported-workspace-identity-must-resolve`): producers must not export
unresolvable identities. This side is the defensive half: consumers must
not trust that what resolves is registered. A system needs both, because
each half alone leaves a silent failure mode open at the boundary.
