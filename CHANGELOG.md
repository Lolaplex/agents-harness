# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Cordis modules `mcp.traces.audit` and `mcp.traces.seal` (`as_tool`) for autonomous integrity checks and cryptographic session sealing.
- Hourly audit schedule `runner/schedules/traces_audit.json` to monitor trace file integrity and detect execution drift.
- `--seal` flag on `runner.loop` emitting root digest in the stream trailer (`seal: <sha256>`).
- Trace recording of tool executions into `TraceStore` (`tool_call`).
- In-turn repeated read detection in `runner.loop` to avoid redundant loop cycles.

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

[Unreleased]: https://github.com/Lolaplex/agents-harness/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/Lolaplex/agents-harness/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/Lolaplex/agents-harness/releases/tag/v0.0.1
