---
type: belief
id: frag-session-bind-contract
provenance: harness#42 review remediation (R09)
ts: 2026-11-24T17:42:00Z
---

The session bind gate is a wire contract, not a callback convention.

Both bind paths — the in-process `register_session_gate` hook and the
`AGENTS_SESSION_BIND_CMD` template — normalize to one shape:
`{"bound": bool, "workdir": str, "already_bound": bool}`. A bare `False`
(or `{"bound": false}`) is a decline and always fails closed with
`SessionGateError`; collapsing it to `{}` runs the tool unbound and drops
the session worktree, which is how the first edit landed in the main
repo. `{"bound": true, "workdir": ...}` switches `AGENTS_WORKDIR` before
the triggering tool executes.

Corollaries the contract carries:

- The gate fires once per process on the first host-local call, so any
  code that spawns a child turn (delegate, persona wake) must bind
  BEFORE it captures `os.environ` or mutates inbox state — a post-hoc
  bind ships the pre-bind workdir to the child or orphans a task.
- Trust layers are asymmetric: `mcp.stdio:*` approval rules only count
  from host-trusted config (host `agents.json`, `AGENTS_APPROVAL_POLICY`,
  `AGENTS_APPROVAL_RULES`). `<ws>/config.json` is agent-writable and can
  never permit a host binary.
- `approval_ask: false` is a waiver on the arguments, not the module:
  any path that can smuggle `--verb` (a `verb` field or a raw `argv`
  token list) re-arms the prompt.
