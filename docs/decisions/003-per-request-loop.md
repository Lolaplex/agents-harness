# 003 Per-request loop: the agent is a trace

plexd stays out. The interpreter is a request, not a daemon.

Each chat rebuilds chat-completions messages from `~/.agents/traces` for a **session** (`ses_…`), owned by a **user**, addressed via a channel **alias**. Context is a cached XML system prefix (instructions + skill catalog + runtime with frozen `start_date`) plus history plus a trailing clock. The reply is appended to the same session. Binaries are `binary_refs` (path + media type), not inline bytes. Duplex / TTS / STT later.

Cordis modules live in `runner/modules/` (`mcp`, `skill`, `a2a`, `schedule`). `when=on_request` can run now. `when=later` (A2A) exists so the loop can name it. Care jobs stay in `runner/schedules/` and are `--call`'d, not the loop itself.

agents-assistant is the API gateway. Telegram is one channel adapter (`POST /webhook/telegram`). Any client uses `POST /v1/turn` with `{channel, user, text}`. History is assembled from traces; SQLite `messages` remains a cache if traces is missing.

Trade-off: the harness loop does not run MCP tools in-process. Assistant still uses FastMCP adapters for tool turns. Harness `--call` / MCP `call_job` invokes a module verb. Merging those would import store internals into the executor. Harness MCP is optional (`mcp.json.example`); it is not auto-merged into host IDE configs.
