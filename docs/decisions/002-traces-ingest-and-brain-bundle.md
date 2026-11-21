# 002 Traces ingest is L0; agents-brain is a later bundle

Harness owns cadence, not stores. `traces_ingest` (`python -m agents_traces ingest`, hourly, 180s) is the L0 job that copies vendor assistant conversations into `~/.agents/traces`. `traces_stats` stays the health pulse. Memory's `ingest_catalog` stays pointers (`chats-index.md`). Cordis: the executor invokes CLIs only.

Each tool remains a separate package with one job:

| Package | Owns |
| --- | --- |
| agents-traces | live + ingested execution/conversation log |
| agents-memory | typed markdown SoT + catalog pointers |
| agents-docs | docsets |
| agents-harness | schedule + exit codes |
| agents-bench | eval |

`agents-brain` is a later install bundle of those CLIs for the full harness agent-loop. It is not a merged store. plexd `agents-runner` is the later verified scheduler; manifests migrate 1:1.

Trade-off: hourly ingest vs daily. New Cursor/Antigravity chats appear during the day; skip-if-session-exists keeps later hours cheap. 180s timeout is for a cold first ingest, not the steady state.
