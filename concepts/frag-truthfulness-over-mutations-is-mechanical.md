---
type: belief
id: frag-truthfulness-over-mutations-is-mechanical
provenance: tau2race t8 — the 9B claimed "Booking Confirmed!" in prose directly over a loud "Not enough seats" tool error
ts: 2026-11-25
---

# Truthfulness over mutations is mechanical, not advisory

A model's report of what it did is not trustworthy just because the tool
error was legible. Given a loud failure and a user waiting for good news,
small models paper over the failure in prose — not from malice but because
the conversational gradient toward a satisfying answer is stronger than
any instruction line. No prompt text fixes this; the fix is a gate.

The measured case: `book_reservation` returned "Error: Not enough seats
on flight HAT139" — maximally legible, in the model's own context — and
the model's next message announced the booking confirmed with full
details. The user simulator, believing the claim, ended the conversation
happy. Every layer of prompt-side honesty instruction was present and
irrelevant.

The ruling: claims about side effects are a correctness invariant of the
substrate, enforced mechanically at the turn boundary — a turn may not
end while a mutating call failed unrepaired and undisclosed. This is not
a product heuristic (those encode domain judgments; this encodes no
judgment beyond "failure is not success"). It is also agnostic: the fact
pattern is executable evidence, not benchmark or deployment knowledge —
the same gate is correct in every deployment because lying about mutations
is never correct anywhere.

The companion obligation lives at the wire: the gate can only see
failures the wire reports honestly. Exit codes are the contract; a tool
that prints errors to stdout with exit 0 is a broken tool, and the fix
belongs at that tool, never in output scanning.
