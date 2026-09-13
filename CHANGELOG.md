# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Dynamic `schedule.add` no longer defaults to Telegram. Text without a verb requires `--channel` and `--user`; naive ISO datetimes use an optional IANA `--timezone`.
- Assistant texts across multi-round tool loops are preserved and joined in the final response.
- `on_status` emits continuous status lines to stderr during tool runs.
- Loop inserts a clean synthesis turn upon reaching `max_tool_rounds` so the answer is not cut off.

### Removed
- GitHub Release is no longer cut automatically on `v*.*.*` tags (manual `gh release create` from CHANGELOG instead).

### Fixed
- Loop clock timezone prefers the identity user zone over the global USER.md profile. `mcp.schedule.add` inherits the current turn `--user` and timezone when the model omits them.
- Loop still assembles when `agents-traces` is not installed (CI / thin hosts). Memory-search loop tests skip when `agents-memory` is missing.
- `call_job` routes built-in cordis tools (`list_catalog`, `load_schema`) when invoked by name instead of failing closed.
- `cordis_tools` strips a redundant `add` subcommand in `mcp.memory.add` and wraps shell-operator commands in `mcp.terminal`.

## [0.0.1] - 2026-09-05

### Added
- Agent runtime engine and scheduled task runner (`python -m runner.loop`, Cordis job kernel, MCP tool round).

[Unreleased]: https://github.com/Lolaplex/agents-harness/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/Lolaplex/agents-harness/releases/tag/v0.0.1
