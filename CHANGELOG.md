# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Provider overlays layered package < `<host>/providers` < `AGENTS_PROVIDERS_DIR` (later wins), `<ws>/config.json` `approval.mode` resolution, approval allowlists at `Host.approval_allowlist` with legacy path fallback and workspace-path rejection, per-workspace `mcp-tools-cache-<ws>.json` via `_resolve_ws_no_create()` (#37).

### Added
- `_parse_text_tool_calls` coerces the `<tool_call><function=name><parameter=k>v</parameter></function></tool_call>` emission shape (taught by qwen-family chat templates) in addition to the JSON-body shape, including multi-call blocks. LM Studio's OpenAI-compat parser does not recognize the `<function=` variant and falls back to `content`; the app layer now catches what the endpoint misses. Terminal rounds (`tools=None`) never coerce: a coerced call cannot execute there, so coercion would silently eat the only reply text.
- `AGENTS_CLOCK=0` suppresses all real-time injection — the trailing `<clock>` message, `<session start_date>` in `<runtime_context>`, and `<ts>` stamps on replayed history — for frozen-world/replay deployments.
- System-prompt layout override resolution: `<ws>/prompts/system_prompt.txt` wins over `AGENTS_PROMPT_TEMPLATE_FILE`, then inline `AGENTS_PROMPT_TEMPLATE`; empty falls back to the byte-identical built-in layout.
- Pinned-verb examples are type-faithful: placeholders derive from the declared JSON schema — objects render their shape, arrays one representative item, `$ref`/`anyOf` resolved, enums render a valid value, integers `1` — instead of echoing the key name (which was never a valid argument). Small models copy the pinned structure literally; key-echoes taught them wrong shapes.
- The pinned block is toolset-scoped like every other prompt surface: out-of-toolset modules no longer leak in and consume the character budget. `AGENTS_PINNED_MAX_CHARS` (default 2500) sizes the budget; clipping keeps mutating verbs first.

### Fixed
- The approval allowlist no longer scopes itself to a phantom workspace on unmigrated legacy hosts. `store_path()` resolved the legacy host as an ad-hoc workspace named after its directory (`.agents`) and partitioned approvals under `ws-.agents` — a workspace that will never exist — silently dropping the host-wide allowlist. Legacy hosts keep the host-wide store.
- The chat-picked model (`--model`) is now sent on every completion of a turn. Tool follow-up rounds, the forced final summary, and the final-check revision used to drop it and fall back to `LLM_MODEL` or the manifest model, so tool-using turns failed (`Turn failed: LLM_MODEL required ...` / unknown model) or switched models mid-turn.
- OpenAI-compatible providers retry HTTP 429/502/503/504 up to 2 times before the first byte (exponential backoff 1s/2s, `Retry-After` honoured as delta-seconds or HTTP-date, capped at 10s; manifest `retries` or `LLM_RETRIES` overrides, 0 disables) instead of failing the turn on the first transient upstream error.
- Workspace persona manifests are scrubbed to an allowlist (`id`, `name`, `system_append`, `notes`) on load; unknown keys — including privileged ones like `allowed_kinds` or `caps_ref` — are ignored so a remote persona create cannot claim package-only fields.
- `a2a.peer` resolves the relay via `RELAY_URL`, then `http://127.0.0.1:$RELAY_PORT`, then `GATEWAY_URL` (deprecated) instead of hardcoding 8787 — works on-host and in-container. Transport failure exits non-zero with `{"ok": false}` (no fake `stub: true` receipt). Module text clarifies it is cross-host board DID mail; intra-workspace persona chat uses `persona.*`. Verb moved to `python -m runner.a2a_peer`.
- `tests/conftest.py` autouse fixture snapshots and restores every `AGENTS_*` env var per test, so `runner.loop.main()` (which pins `AGENTS_PERSONA`/`AGENTS_WORKSPACE` at CLI entry) cannot leak env between tests.

### Changed
- `runner.loop --limit` (history turns rebuilt from traces) honors `AGENTS_HISTORY_LIMIT` as its default, matching the `AGENTS_MAX_TOOL_ROUNDS` pattern — hosts can widen the window without a wrapper command.
- `redact` drops the `AGENTS_HOME` source-env fallback (alias removed from agents-workspace); `AGENTS_WORKSPACE_ROOT` remains the escape hatch.
- `executor._internal_roots()` and the `runner.delegate` workdir clamp now delegate to `agents_workspace.allowed_roots()` (single source for the lock allow list). Behaviour unchanged.
- Provider API keys resolve via `agents_workspace.resolve_secret()`: `<ws>/secrets.json` > `<host>/secrets.json` > env (`key_env`) > manifest `api_key`. A workspace can pin per-ws provider keys without exporting them.

### Fixed
- `pre_pr` strips wrapping quotes from Windows `shlex.split` tokens: a quoted `-c` body ran as a string literal and produced empty stdout.
- Tests pin `AGENTS_HOST_DIR` instead of the removed `AGENTS_HOME` alias; the internal-dirs fixture uses host infra (`run/`) rather than the host root; the pinned-verbs map covers the new `bg`/`proc` commands.

### Added
- `mcp.model` module (`python -m runner.model`): persistent workspace provider/model switch. `use --provider <name> [--model <id>]` writes the pick to `<ws>/config.json` under `model`; `current`, `list`, `clear` inspect or remove it. Resolution order — provider: `--provider` > `LLM_PROVIDER` > workspace pick > `openai.default`; model: `--model` > workspace pick > `LLM_MODEL` > manifest model. `runner.loop` and `get_provider` honor the pick when no explicit flag/env applies.
- Git tool `git` (`python -m runner.git_tool`). The repository's default branch is protected when git records it (`origin/HEAD`, otherwise the only local branch). No branch name is assumed. Force-push is blocked. Commit author and committer are checked only when a `Name <email>` list is configured (`allow` checks, `require` sets both for that commit and does not write git config). `AGENTS_GIT_PROTECTED=none` clears branch protection. `AGENTS_GIT_FORCE_PUSH=allow` permits force-push except onto a protected branch.
- Approval policy layered on `off` / `ask` / `strict`. Rules in host `agents.json`, workspace `config.json` (`approval.rules`), `AGENTS_APPROVAL_POLICY`, or `AGENTS_APPROVAL_RULES` match action keys such as `git.push:<branch>` and `fs.delete`. `deny` blocks. `ask` prompts when an approval command is set. `allow` does not remove a prompt the tier already requires. No rules leaves the gate unchanged. Hosts add keys with `register_action_classifier`.
- Pre-turn recall, off unless `AGENTS_RECALL=1` or `recall.enabled` in workspace config. One instruction line, plus a bounded fenced note when a provider returns hits. Providers are `register_recall_provider`, `AGENTS_RECALL_CMD`, and an optional bounded repo scan. `python -m runner.recall_eval` scores whether a reply used or searched for a known fact instead of asking for it.
- Search coverage for grep, glob, and search. The harness appends `<search files_searched="..." truncated="0|1" matches="..."/>` after the untrusted fence. A tool count or a `truncated` stderr line wins. A finished search that omits the count gets a bounded workdir census (`AGENTS_SEARCH_MAX_FILES`, default 50000; `AGENTS_SEARCH_CENSUS=0` skips it; `AGENTS_SEARCH_SKIP_DIRS` replaces the default skip list). Absence claims need an integer `files_searched` and `truncated` 0.
- Repository context. A git repo in the workdir (or `AGENTS.md` at the workdir) loads `AGENTS.md` into the system prompt, with `README` as the fallback or a short summary. `AGENTS_PROJECT_CONTEXT=0` disables it. `AGENTS_PROJECT_CONTEXT_MAX` (default 24000) bounds it. The same keys live in `<ws>/config.json` or `<host>/agents.json`.
- `repo.pre_pr` discovers test and build commands from `package.json` scripts, `pyproject.toml`, `Makefile` targets, and fenced commands in `AGENTS.md`, then runs that argv when `run` is true. It does not invent a command. `AGENTS_PRE_PR_TIMEOUT` (default 300) bounds each command.
- Help-json `subcommands`: one more command depth. A command spec may nest `subcommands` (full specs with usage/parameters/side_effects); `select_command`, `call_effects`, `declared_argv`, approval `cmd:` action keys, and pinned rendering resolve `argv[1]` or an `action`/`subcommand` argument one level down — `proc poll` can be read-only while `proc kill` is gated. No sub match falls back to the parent spec.
- Persona memory split: `persona.json` `memory: "private"` resolves the store root to `<ws>/personas/<id>/memory` (`AGENTS_MEMORY_PATH`/`AGENTS_MEMORY_DIR` set at loop start and on delegate spawns); the default `"shared"` keeps workspace memory. A private persona's system prefix merges the shared `<memory_rules>` block with a second private-store block, so only that persona sees its private rules. Unknown/missing personas stay shared.
- `vision_analyze` Cordis tool (`runner/modules/vision.analyze.json` + `runner/vision_analyze.py`): sends an image file (png/jpg/jpeg/gif/webp, ≤20MB) as an `image_url` part to a vision-capable provider and returns the answer. Requires `AGENTS_VISION=1`, manifest `vision: true`, or `capabilities: ["vision"]` — refuses cleanly otherwise. Commands discovered via `--help-json`; `analyze` pinned and classified read-only.
- `cordis_expose` auto-mount: installed suite packages whose `--help-json` sets `cordis_expose: true` are probed and mounted as `mcp.<pkg>` Cordis modules at runtime — checked-in manifests and overlays always win (`AGENTS_CORDIS_PKGS` adds packages, `AGENTS_CORDIS_EXPOSE=0` disables). Toolset members gain `<glob>:<group>` command filtering: `mcp.browser:read-only` exposes only read commands; a command spec's `groups` list beats the implicit side-effect class. Unresolvable commands fail closed.
- Package-level Cordis modules via help-json: `mcp.browser`, `mcp.calendar`, `mcp.traces` are now single modules whose commands, parameters, and side-effect classes come live from `<pkg> --help-json` (agents-calendar and agents-traces emit curated specs). `expose_commands: [...]` on a manifest synthesizes `<pkg>.<command>` rows (`command_pin`ed argv, spec parameters surfaced) so `mcp.calendar.add`/`mcp.traces.audit` keep working as separate tools — the 7 hand-written sub-manifests are deleted. `mcp.memory` stays per-command until the agents-memory rework lands.
- `persona.*` Cordis modules (`persona.list` / `persona.inbox` / `persona.ask` / `persona.handoff` on `runner.persona_tool`) riding the `agents_workspace.inbox` schema (thread_id, hop/max_hops, ask|handoff, peer policy). Sender identity is `AGENTS_PERSONA` only — never a model arg. `AGENTS_PERSONA_HOP`/`AGENTS_PERSONA_MAX_HOPS` (default 3) stop persona ping-pong; self-messages refuse without `AGENTS_PERSONA_SELF=1`. `ask` runs a synchronous delegate turn and replies into the sender's inbox; `handoff --wake` starts one async. A `persona reply` CLI verb answers tasks. Workspace persona manifests may now declare `role`, `accepts_messages`, `allow_peers`, `memory` (still no privileged fields).
- Delegate depth ledger: spawn depth is persisted at `<ws>/run/delegate.depth` before a child launches; the child counts `max(env, ledger)` so a forged `AGENTS_DELEGATE_DEPTH=0` no longer resets recursion. Advisory (same-UID) until container isolation exists.
- Natural-language scheduling (`runner.schedule.parse_natural_schedule`). `mcp.schedule.add` and `python -m runner.schedule add --natural` accept chat phrasing — 'every morning 8', 'every monday 9', 'weekdays 9', 'weekends', 'every 30 minutes', 'every 2 hours', 'in 10 minutes', 'tomorrow 9', 'at 8:30pm' (next occurrence) — translated to `at`/`cron`. Explicit `at`/`cron`/`cadence` win; unparseable phrasing errors instead of guessing.
- `clarify` tool (fourth built-in Cordis tool). The model calls it with `question` and optional `options`; the turn ends with the question rendered (numbered options as text, `clarify` payload in the loop trailer for channels that render buttons). No further provider round or tool calls run after it.
- `/compress` chat command (`runner.session_ops`, `runner.loop`): appends a compaction `frame` (`agents_traces.frames.compact_session`) covering all but the newest `keep_last` events (default 6, `/compress N` to change) — long history collapses to a compact context while the thread continues unchanged.
- `/undo` and `/retry` chat commands (`runner.session_ops`, `runner.loop`). `/undo` covers the last turn — the last user message and everything after — with an empty-content `frame`, so the projection drops it while the append-only trace keeps it. `/retry` covers everything after the last user message and re-runs the provider turn with that same question; no duplicate user message is recorded.
- Named toolsets (`runner.toolsets`). A `toolsets` section in `<ws>/config.json` or `<host>/agents.json` names groups of module-name globs; `AGENTS_TOOLSET` / `runner.loop --toolset` selects one, and config `toolset` may map channels (`{"telegram": "safe"}`) plus a `default`. The active set restricts `list_catalog`, `load_schema`, `call_job`, and the prompt module list. `all`/`off` is unrestricted; an unknown set name fails closed.
- Approval pattern allowlist (`runner.approval_allowlist`). When the approval command approves a call it may print `{"remember": true}` (persist the exact call signature) or `{"allow_pattern": "<fnmatch>"}` (persist a glob) on stdout; matching calls then skip the prompt. Patterns are matched against the canonical signature, the human summary, and flattened argument text, so `git status`-style globs work. Stored in `AGENTS_APPROVAL_ALLOWLIST` (default `<host>/approval-allowlist.json`; `off` disables). `deny` rules still win over any stored pattern.
- Loop interrupt marker: `runner.loop` polls `AGENTS_INTERRUPT_FILE` before the first completion and between tool rounds; when the file appears the turn emits the trailer with `interrupted: true` and exits cleanly instead of being killed mid-call. agents-relay drops the marker for `/stop` and interrupt-and-redirect turns.

### Changed
- The workspace tree skips `.relay-inbox`, where agents-relay copies chat attachments.
- Tool text over `AGENTS_TOOL_OUTPUT_MAX` is a prefix page plus `<page next_offset="..."/>`, not a head-and-tail clip. `read_content` continues with `next` true or `offset` set to `next_offset`. Digest is optional for the latest page in this process. `AGENTS_PAGE_CHARS` (default 32768) is the page size. History preview default is 4096 (`AGENTS_HISTORY_TOOL_PREVIEW`).
- `.mailmap` maps legacy `Klix`/`Felix Kempf` author names to `Klix927` (no history rewrite; display only).

### Added
- `mcp.delegate` module (`python -m runner.delegate`): delegate a task to an isolated subagent turn. `spawn --task "…" [--provider|--model|--persona|--workdir]` starts a detached `runner.loop --detached-session` supervisor and returns `dlg_<id>`; `status`, `result [--timeout|--tail]`, `list`, `cancel` manage it. State lives under `AGENTS_DELEGATES_DIR` (default `<ws>/delegates`): `meta.json`, `out.log`, `err.log`, `exit.json`. The subagent inherits the workspace environment but gets its own session (`ses_dlg_<id>`), so it can run tools while the parent turn continues. Guards: `AGENTS_DELEGATE_DEPTH`/`AGENTS_DELEGATE_MAX_DEPTH` (default 1) stop a delegate from spawning delegates, and `--workdir` must resolve inside the roots the child's workdir lock would honor — the jail boundary cannot be widened by a spawn argument. Declared commands come from `runner.delegate --help-json` (`commands_from`), so no cordis_tools arg shim exists.

### Added
- Personas load from `<ws>/personas/<id>/persona.json` first, package `runner/personas/*.json` as fallback; `--persona <id>` / `AGENTS_PERSONA` select the persona (default: workspace `default_persona`). Personas are equal - a role is description text, not hierarchy. Optional private notes live under `<ws>/personas/<id>/notes.md`.

### Changed
- README env table names `AGENTS_WORKDIR`/`AGENTS_WORKDIR_LOCK`/`AGENTS_WORKDIR_ALLOW` and `<ws>/schedules`; old names only as deprecated-alias notes.

### Added
- `workdir_root()` replaces `workspace_root()` (name said workspace, meant workdir). `AGENTS_WORKDIR_LOCK` (alias `AGENTS_WORKSPACE_LOCK`) and `AGENTS_WORKDIR_ALLOW` (alias `AGENTS_WORKSPACE_ALLOW`), each a deprecated one-release alias that warns.
- `runner.loop --workspace <id>` selects the data workspace explicitly (validated against the Host; default `AGENTS_WORKSPACE`). The resolved `Workspace` object is threaded into modules, skills, schedules, memory rules and the user profile.
- `runner.schedule.tick()`, `add_schedule`, `list_dynamic_schedules`, `remove_schedule`, `dynamic_schedules_dir` and `configured_timezone` accept `workspace=...` so a host process can tick several workspaces in one process without mutating `os.environ`. Claimed jobs run with `ws.env()` via `execute_job(workspace=...)`.
- `runner.modules.list_modules`/`find_module`/`find_module_for_tool`/`enabled_tool_names`, `runner.skills` dir/list/load/prompt-block helpers, `runner.user_profile.load_user_profile` and `runner.memory_rules` helpers accept `workspace=...` (default: env-selected workspace).

### Changed
- All remaining paths resolve through `agents_workspace`: `mcp_config_path()` returns `<host>/mcp.json` (`AGENTS_MCP_CONFIG_FILE`, alias `AGENTS_MCP_CONFIG` one release) and `load_mcp_config` merges an optional `<ws>/mcp.json` overlay; `mcp_cache_path()` returns `<host>/cache/mcp-tools-cache.json` (`AGENTS_MCP_CACHE_FILE`, alias `AGENTS_MCP_CACHE`); `content_dir()` returns `<ws>/content`; `configured_timezone` reads `<host>/agents.json` before `<ws>/config.json`; the workspace lock's internal roots are the active workspace plus listed host infra parts (keys, secrets, run, cache, logs, clients, agents.json, mcp.json, users.json) — no `~/.agents` domains.
- Update-check cache lives at `<host>/cache/updates/updates-<pkg>.json`.

### Removed
- Archived agents-docs wiring: `runner/modules/mcp.docs*.json` and `runner/schedules/docs_catalog.json`.
- The workspace boundary is its own switch (`AGENTS_WORKSPACE_LOCK`). `AGENTS_WORKSPACE_DIR` only names the working directory. `AGENTS_WORKSPACE_ALLOW` adds allowed roots.
- The executor checks only the working directory it hands to a verb. It no longer guesses which argument is a path, so ordinary text with a slash (a branch name, a date) is not refused. Path confinement stays in the terminal, which owns file access.

### Security
- A set workspace refuses tool paths and working directories outside that root. Skills, modules, memory, traces, schedules, and the agent home stay reachable. An unset or blank workspace leaves commands unchanged.

### Added
- Optional content retrieval. Hosts register `register_content_backend(fn)` or set `AGENTS_CONTENT_READ_CMD`. The model calls `read_content` (`call_job`) with `digest`, `offset`, and `limit` (default 8000). A page reports `text`, `digest` (SHA-256 of the full body), `total_chars`, and `truncated`. With no backend and no stored body the tool says the content is unavailable. The harness does not import agents-traces or agents-memory for this.
- Tool text the harness clips (head, `[... N chars omitted ...]`, tail) keeps a `<ref digest="..." sha256="..." chars="..." truncated="1"/>` for the full redacted body, readable through the same `read_content` path.
- Successful mutating tool calls add an `agents_harness.tool_receipt.v1` receipt to the rest of the turn: tool name, arguments digest, `success`, and any JSON object with a `schema` that the tool returned.
- `register_final_check(hook)` inspects the draft reply and this turn's receipts. There is no built-in product heuristic. Remaining findings are `response_flags` on the loop trailer after one revision turn.
- Help-json side effects. A command is classified from `side_effects`, then `mutates`, then `annotations.readOnlyHint`. Missing command metadata is mutating. The command name is not evidence. `commands_from: "help-json"` loads that verb's `--help-json` when the module does not already embed `commands`.
- Pinned verbs. A module manifest with `"pin": true` (bundled `mcp.terminal`) adds a compact `call_job` example block to the system prompt: command name, `side_effects`, and a ready call. The block is capped at 2500 characters, clipped with `[pinned block clipped]`, and reports `pinned: <module> unavailable (<reason>)` when help-json discovery fails. `AGENTS_PINNED_VERBS=0` disables it. The three Cordis tools are unchanged.

### Changed
- Recent tool context keeps up to 12 tool results (`AGENTS_HISTORY_TOOL_EVENTS`, default 12), each with at most 400 characters of preview (`AGENTS_HISTORY_TOOL_PREVIEW`). Longer bodies keep a retrievable ref instead of dropping every result except the last three.
- The system prompt tells the model to fetch a ref or omission before summarizing it, and to claim a side effect only when this turn has a successful receipt.
- `mcp.terminal` sets `commands_from` to `help-json`. A declared command is invoked as itself and approved from that command's metadata. Undeclared argv still uses `run --`. If help-json cannot be loaded, the module stays mutating.
- Shown tool text and schedule `stdout_tail` may exceed `AGENTS_TOOL_OUTPUT_MAX` by one trailing `<ref .../>` line. The clipped body before that line still fits the cap. Callers that required the whole string to stay inside the cap, or to end on the original tail, should strip a trailing ref.
- Ask and strict approval no longer classifies a tool from its name or from words in the arguments. A module with no `side_effects`, `mutates`, or `readOnlyHint` is mutating, so a host manifest that used to skip the prompt only because it was named `search` or `list` is now asked. Bundled manifests set `mutates`, and the default mode is still `off` (no command, no prompt). `ask` still does not prompt for schedule create or list. `strict` still does.

### Fixed
- `--deliver stream` writes the same loop trailer as buffered delivery (`session`, `user_id`, `alias`), so a relay SSE stream can keep the thread.
- A mutating CLI call no longer gets a success receipt when the process exits non-zero or the job status is `FAILED`, `TIMEOUT`, or `ERROR`, even if stdout contains a receipt object or a plain `error:` line. Tool output is not scanned for those words. A receipt is also withheld for harness failures (`HarnessError`: approval `Denied:`, `error: timeout`, `error: non-zero exit`, `refused:`). Exit code 0 is still success when the tool itself prints the word `error`.

- The turn clock reads `timezone` from the user profile only. `agents-traces` 0.1.2 dropped `UserRecord.timezone`; `getattr` keeps an older install working.
- `AGENTS_CONTENT_READ_CMD` splits with the platform's quoting rules, so a Windows path keeps its backslashes instead of being eaten by a posix split.

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

[Unreleased]: https://github.com/Lolaplex/agents-harness/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Lolaplex/agents-harness/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Lolaplex/agents-harness/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/Lolaplex/agents-harness/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/Lolaplex/agents-harness/releases/tag/v0.0.1
