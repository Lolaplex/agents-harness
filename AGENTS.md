# AGENTS.md — agents-harness Developer Guidelines

## Architecture Principles
- **Per-request loop**: The agent is a reconstructed trace (`python -m runner.loop`). SKILL/MCP/A2A entries live in `runner/modules/` as Cordis verbs (`when=on_request` or `later`). Scheduled care jobs stay in `runner/schedules/`. plexd is not on this path.
- **MCP like the other agents-* packages**: `mcp.json.example` plus `python -m runner.mcp_server`. Optional extra `pip install -e ".[mcp]"`. The executor stays stdlib; FastMCP is only loaded by the server.
- **Providers**: `runner/providers/*.json` uses the same manifest shape as modules (`name`, `kind`, `verb`, `rests_on`). Package files are env-only (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). Host URLs live in `AGENTS_PROVIDERS_DIR` (agents-sandbox), not in this clone. Completions **stream**; `first_byte_sec` is prefill silence, `idle_sec` is a stall between tokens. Do not wall-clock-cap a generation. The loop calls `complete(messages, tools)`. Modules with `as_tool` become OpenAI functions (`mcp.memory.search` → `mcp_memory_search`). One tool round maps `tool_calls` onto Cordis `execute_job`; it does not import package internals. Machine picks: `AGENTS_MODULES_DIR` overlay plus optional `enabled.json`.
- **Identity**: alias (`telegram:5712`) ≠ user (`u_…`) ≠ session (`ses_…`). Resume the same thread from another device with `session` or `user_id`. Directory: `~/.agents/identity.json` (DID later).
- **Zero Heavy Dependencies**: Pure Python standard library (`subprocess`, `json`, `datetime`, `pathlib`, `logging`, `urllib`). No Celery, no Redis, no cron daemons.
- **Durable Manifests (Koru Principle)**: Every scheduled flow is declared in `runner/schedules/*.json` with the verified verb it rests on, its cadence, and its expected exit semantics (`exit 0 = healthy`).
- **Strict Layering (Cordis Principle)**: The executor owns no state, no domain store, and no semantics. It never imports module internals; it strictly invokes verified CLIs and inspects exit codes.
- **Job kernel**: Catalog JSON is the alphabet (`ITerm` view of Kernel.lean). `python -m runner.kernel` checks, mixes, reduces. Atom β is Cordis `execute_job`. Combinators are norm/konst/comp/app. NL skill bodies are not terms.

## Commands
- Validate all manifests: `python -m runner.executor --validate`
- FastMCP: `python -m runner.mcp_server` (copy `mcp.json.example` into the host MCP config; do not auto-merge `~/.cursor/mcp.json`)
- List modules: `python -m runner.loop --list-modules`
- List advertised tools: `python -m runner.loop --list-tools`
- List completion providers: `python -m runner.loop --list-providers`
- Assemble one request (no HTTP): `python -m runner.loop --channel telegram --user 123 --message "hi" --assemble-only`
- Resume a thread: `python -m runner.loop --session ses_… --message "hi" --assemble-only`
- Complete via provider: `python -m runner.loop --user fabian --message "hi" --complete --provider openai.default`
- Tool turn (no LLM): `python -m runner.loop --user probe --message canary --complete --provider scripted.tool --deliver buffered`
- Live LMS roundtrip: `python -m runner.loop --user probe --message "what is the sandbox canary" --complete --provider lmstudio.local` (host overlay; not in this clone)
- Delivery: HTTP always streams (hang detection). `--channel telegram` buffers the answer and prints `thinking...` on stderr. `--channel local` on a TTY writes tokens. `--deliver stream|buffered` overrides.
- Call a named module/schedule: `python -m runner.loop --call mcp.traces`
- Check a job term: `python -m runner.kernel --check runner/terms/skill-then-a2a.json`
- Reduce a job term: `python -m runner.kernel --reduce runner/terms/skill-then-a2a.json`
- Alphabet: `python -m runner.kernel --alphabet`
- Run all scheduled flows once: `python -m runner.executor --all`
- Run specific flow: `python -m runner.executor --run <name>`
- Run test suite: `python -m unittest discover tests`
