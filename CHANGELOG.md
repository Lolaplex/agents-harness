# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-06

### Added
- System-prefix hard rules from agents-memory (`agents-memory context` / `render_rules`) once per process+project, soft-fail when memory is missing. Disable with `AGENTS_MEMORY_RULES=0`. Cordis modules unchanged.
- Bundled Cordis modules `mcp.calendar.list|add|update|delete|calendars` so hosts without a klanker overlay still get calendar tools (needs agents-calendar 0.1.0+).
- Approval gate for mutating `call_job` tools (`AGENTS_APPROVAL_CMD`, `AGENTS_APPROVAL_MODE`, `{user}` substitution). A denial tells the model not to retry.
- Untrusted-data fence around tool results, plus a system-prompt note. Only messages the harness builds itself (approval denials, loop notices) skip the fence; they are marked by type, so tool output starting with `Denied:` or `[Notice:` is still fenced. Redaction is unchanged.
- External MCP client (`~/.agents/mcp.json`) exposing `mcp.<server>.<tool>` modules (`kind: mcp_remote`) via the optional `mcp` extra. Short-lived sessions; schema cache.
- `skill.list` / `skill.load` read `~/.agents/skills/*/SKILL.md` and the system prompt lists them. `skill.catalog` stays as an alias of `skill.list`.
- Schedule `tick()` evaluates cron in the job timezone, persists the last-run minute, applies a grace window, takes a file lock, and honors per-job timeouts (300s default for LLM jobs and routines). `kind: routine` is handed to `register_routine_handler`.
- `runner.loop --attach` (repeatable). Images are vision content parts when `AGENTS_VISION=1` or the provider advertises vision.
- Manifest `"mutates"` flags on bundled modules.
- Cordis modules `mcp.traces.audit` and `mcp.traces.seal` (`as_tool`) for autonomous integrity checks and cryptographic session sealing.
- Hourly audit schedule `runner/schedules/traces_audit.json` to monitor trace file integrity and detect execution drift.
- `--seal` flag on `runner.loop` emitting root digest in the stream trailer (`seal: <sha256>`).
- Trace recording of tool executions into `TraceStore` (`tool_call`).
- In-turn repeated read detection in `runner.loop` to avoid redundant loop cycles.
- Default `temperature` (0.2) and `max_tokens` (4096) guardrails in `OpenAICompatProvider` to prevent token degeneration.
- Module alias fallback via `find_module_for_tool` in `call_job` routing.
- Clean error handling around `_complete_once` in `runner.loop` emitting error trailer without crashing.
- `traces` extra (`agents-traces>=0.1.0`) for `--seal` and the trace audit modules. Calendar tools need agents-calendar 0.1.0 or newer.
- CLI (and MCP, when present) check PyPI at most once per day for a newer release and print one stderr / tool-response line (`uv tool upgrade …`). Disabled with `AGENTS_NO_UPDATE_CHECK=1` or when `CI` is set; offline/timeout stays silent.

### Fixed
- Cron ticks no longer treat the clock as UTC when the job has a timezone, and a second tick in the same minute no longer double-fires.
- A SKIPPED routine no longer consumes its cron slot or one-shot manifest. `tick()` does not hold the file lock while jobs run.
- `mcp.schedule.add` is not an approval prompt in `ask` mode. `strict` still gates it. `mcp.schedule.remove` stays gated in `ask`.
- `--detached-session` runs one turn in the given session and does not store it as the active session. A session id starting with `routine:` still does this without the flag.
- `tick(wait=False)` returns after claiming due jobs. The CLI still waits for them.
- A failed or timed-out one-shot is still removed (no retry). The failure is logged and `last_result` is kept in `tick-state.json`.
- `mcp.schedule.add` copies the turn's `channel` onto the job when the call omits it, the same way it copies `user`.
- `--seal` no longer drops the seal silently when agents-traces lacks the audit API (0.0.3 and older) or sealing fails; the trailer carries `seal_error` with the reason instead of `seal`.
- Cordis tool argument parser normalizes list, string, and aliased parameter structures (e.g. `file`, `path` for `file_id`) to prevent empty argv dispatch.

## [0.0.2] - 2026-09-27

### Added
- Cordis modules `mcp.docs.search` and `mcp.docs.write` (`as_tool`), parallel to `mcp.memory.search` / `mcp.memory.add`. Hard tech facts go to docs; user facts stay in memory.

### Changed
- CI runs only on pull requests to `main`.
- Dynamic `schedule.add` no longer defaults to Telegram. Text without a verb requires `--channel` and `--user`; naive ISO datetimes use an optional IANA `--timezone`.
- `on_status` emits continuous status lines to stderr during tool runs.
- Loop inserts a clean synthesis turn upon reaching `max_tool_rounds` so the answer is not cut off.

### Removed
- GitHub Release is no longer cut automatically on `v*.*.*` tags (manual `gh release create` from CHANGELOG instead).
- Bundled `plexus_verify` schedule (hardcoded foreign machine path; plexus is unreleased).
- Soft tool-round notice and the second cap (`AGENTS_MAX_TOOL_ROUNDS_HARD`, `--max-tool-rounds-hard`). One cap remains (`AGENTS_MAX_TOOL_ROUNDS` / `--max-tool-rounds`, default 12); the last round still forces a final answer.

### Fixed
- Loop clock timezone prefers the identity user zone over the global USER.md profile. `mcp.schedule.add` inherits the current turn `--user` and timezone when the model omits them.
- Loop still assembles when `agents-traces` is not installed (CI / thin hosts). Memory-search loop tests skip when `agents-memory` is missing.
- `call_job` routes built-in cordis tools (`list_catalog`, `load_schema`) when invoked by name instead of failing closed.
- `cordis_tools` strips a redundant `add` subcommand in `mcp.memory.add` and wraps shell-operator commands in `mcp.terminal`.
- `mcp.memory.add` accepts `text` as an alias for `fact` so a missing positional no longer exits 2.
- Loop emits only the last user-facing completion, not a join of mid-tool-round drafts (Telegram 4096 was cutting the real ending).

## [0.0.1] - 2026-09-05

### Added
- Agent runtime engine and scheduled task runner (`python -m runner.loop`, Cordis job kernel, MCP tool round).

[Unreleased]: https://github.com/Lolaplex/agents-harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Lolaplex/agents-harness/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/Lolaplex/agents-harness/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/Lolaplex/agents-harness/releases/tag/v0.0.1
