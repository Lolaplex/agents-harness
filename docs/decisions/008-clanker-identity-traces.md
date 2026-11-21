# ADR 008 — Clanker identity, traces, and session resume

**Status:** accepted  
**Date:** 2026-09-02  
**Product:** Clanker — self-assembling agent (NVIDIA AVO ReAct loop + Cordis composition)

## Context

Session pointers were duplicated across `identity.json`, `clients/repl/session.json`, and harness state. Human profile fields (`display`, `work`, `project`) were copied into `UserRecord`, overlapping `USER.md`.

## Decision

| Concern | Store | Notes |
|--------|--------|--------|
| Human profile | `~/.agents/memory/USER.md` | Injected at assemble; not machine JSON |
| Crypto identity | `~/.agents/keys/` + `did:key` | agents-keys; board registration |
| Alias bindings | `~/.agents/identity.json` | `u_…` ↔ `channel:user` only |
| Thread resume | `~/.agents/traces/` | `find_latest_session(channel, user, project)` |
| Conversation bodies | traces | `record_message` tags channel/user/project |

`identity.json` no longer stores `active_session` or person fields. Explicit `ses_…` still accepted; otherwise harness resolves from traces.

## client-repl

- No session files. Passes `channel`, `user`, `project` to gateway.
- Intent resolver: builtins → semantic (gateway/harness) last — plex-shell shape.
- Optional gateway prefs only: `<project>/.agents/repl/prefs.json`.

## Consequences

- Cross-device resume requires traces (or explicit `--session`), not a shared pointer file.
- Per-project threads need distinct `project` on each turn (already on `/v1/turn`).
- Ratatui UI via `pyratatui` (`client-repl` default; `--plain` for stdin).

## Rejects

- `~/.client/` or `clients/repl/` session files — parallel privileged pointer store.
- `UserRecord.display/work/project` — duplicates USER.md.
- `active_session` in identity — traces are source of truth.
