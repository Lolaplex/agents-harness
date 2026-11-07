# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `turn_evidence_scope()` holds one turn's adapter evidence from before tools run through `final_check`, including when the turn raises or is cancelled. `get_current_turn_evidence()` returns an immutable snapshot of that turn's invocation records. `final_check(text, receipts)` is unchanged. Copied contexts, asyncio child tasks, and worker threads do not see the turn unless they call `adopt_turn_evidence()`. The records are not added to receipts, model text, or `public_envelope`. `skill.list`, `skill.catalog`, and `skill.load` do not open an invocation and are not turn evidence.
- Optional result validators. A host registers one callable for a `(tool, adapter, schema, version)` key. The harness copies call arguments before the tool runs. The copy accepts JSON values only: null, bool, finite numbers, strings, arrays, and objects with string keys. Tuples are stored as arrays. Sets, bytearrays, non-finite numbers, cycles, non-string keys, and other objects are rejected before the tool runs with the fixed text `unsupported arguments` (the value is not included). A successful call freezes that copy and passes it with the raw result. The argument digest is taken from the copy before the validator runs. Evidence is empty unless the validator returns one complete typed result. `public_envelope` omits arguments, targets, and digests. Stdout is not an evidence source. Legacy `receipt` objects are unchanged and are not host byte guarantees.
- Optional content retrieval. Hosts register `register_content_backend(fn)` or set `AGENTS_CONTENT_READ_CMD`. The model calls `read_content` (`call_job`) with `digest`, `offset`, and `limit` (default 8000). A page reports `text`, `digest` (SHA-256 of the full body), `total_chars`, and `truncated`. With no backend and no stored body the tool says the content is unavailable. The harness does not import agents-traces or agents-memory for this.
- Tool text the harness clips (head, `[... N chars omitted ...]`, tail) keeps a `<ref digest="..." sha256="..." chars="..." truncated="1"/>` for the full redacted body, readable through the same `read_content` path.
- Successful mutating tool calls add an `agents_harness.tool_receipt.v1` receipt to the rest of the turn: tool name, arguments digest, `success`, and any JSON object with a `schema` that the tool returned.
- `register_final_check(hook)` inspects the draft reply and this turn's receipts. There is no built-in product heuristic. Remaining findings are `response_flags` on the loop trailer after one revision turn.
- Help-json side effects. A command is classified from `side_effects`, then `mutates`, then `annotations.readOnlyHint`. Missing command metadata is mutating. The command name is not evidence. `commands_from: "help-json"` loads that verb's `--help-json` when the module does not already embed `commands`.

### Changed
- Recent tool context keeps up to 12 tool results (`AGENTS_HISTORY_TOOL_EVENTS`, default 12), each with at most 400 characters of preview (`AGENTS_HISTORY_TOOL_PREVIEW`). Longer bodies keep a retrievable ref instead of dropping every result except the last three.
- The system prompt tells the model to fetch a ref or omission before summarizing it, and to claim a side effect only when this turn has a successful receipt.
- `mcp.terminal` sets `commands_from` to `help-json`. A declared command is invoked as itself and approved from that command's metadata. Undeclared argv still uses `run --`. If help-json cannot be loaded, the module stays mutating.
- Shown tool text and schedule `stdout_tail` may exceed `AGENTS_TOOL_OUTPUT_MAX` by one trailing `<ref .../>` line. The clipped body before that line still fits the cap. Callers that required the whole string to stay inside the cap, or to end on the original tail, should strip a trailing ref.
- Ask and strict approval no longer classifies a tool from its name or from words in the arguments. A module with no `side_effects`, `mutates`, or `readOnlyHint` is mutating, so a host manifest that used to skip the prompt only because it was named `search` or `list` is now asked. Bundled manifests set `mutates`, and the default mode is still `off` (no command, no prompt). `ask` still does not prompt for schedule create or list. `strict` still does.

### Fixed
- A mutating CLI call no longer gets a success receipt when the process exits non-zero or the job status is `FAILED`, `TIMEOUT`, or `ERROR`, even if stdout contains a receipt object or a plain `error:` line. Tool output is not scanned for those words. A receipt is also withheld for harness failures (`HarnessError`: approval `Denied:`, `error: timeout`, `error: non-zero exit`, `refused:`). Exit code 0 is still success when the tool itself prints the word `error`.

- Calendar tools no longer require a UTC offset on `dtstart`, `dtend`, or list bounds. Prefer local wall time plus an optional IANA `timezone` (explicit argument, else `CALDAV_TIMEZONE` or `timezone` in the credentials file, else the calendar's CalDAV calendar-timezone, else the host zone, else UTC). `Z` and numeric offsets stay absolute instants. All-day events are `YYYY-MM-DD` or `all_day=true` on add; returned all-day fields are dates and `dtend` is exclusive. Timed `dtstart` stays the UTC instant; show `dtstart_local`, `timezone`, and `all_day`. A daylight-saving gap or ambiguous naive time is an error; retry that instant with an explicit offset.

## [0.1.1] - 2026-10-21

### Changed
- Bundled `mcp.calendar.add`, `mcp.calendar.update`, and `mcp.calendar.list` describe `dtstart` / `dtend` (and list ranges) as ISO-8601 with a UTC offset. A time without an offset is treated as UTC. List returns a JSON array sorted by start time, so the range should stay narrow.

### Fixed
- Tool output is no longer a silent 1000-character tail. Trace logs still store that tail. Callers also receive the full stdout and stderr. Text shown to the model, and schedule or MCP job results shown to a caller, keep up to `AGENTS_TOOL_OUTPUT_MAX` characters (default 32768). Output over the cap keeps the head and the tail with `[... N chars omitted ...]` in the middle.

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
