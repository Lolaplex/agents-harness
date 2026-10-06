# agents-harness

<p align="center">
  <a href="https://github.com/Lolaplex/agents-harness/releases"><img src="https://img.shields.io/badge/version-0.0.2-blue.svg?style=flat-square" alt="Version 0.0.2"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Standard-orange.svg?style=flat-square" alt="MCP"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-harness/"><img src="https://img.shields.io/pypi/v/agents-harness.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <strong>Declarative scheduled flow runner, per-request agent loop, and job kernel for local agent services.</strong><br>
  Zero heavy dependencies (Python standard library only). No Celery, no Redis, no cron daemons.
</p>

---

## Quickstart

```bash
pip install agents-harness
```

Optional FastMCP server support:

```bash
pip install "agents-harness[mcp]"
```

> [!TIP]
> **🤖 Agent-Driven Setup:**
> Give your coding agent **this repo** (clone or URL), then tell it to **"install agents-harness and register the mcp runner in your host configuration."**

---

## Architecture

| Layer | Responsibility | Components |
| :--- | :--- | :--- |
| **Agent Loop** | Per-request reconstructed trace & tool turns | `runner.loop` |
| **Job Kernel** | Declarative Cordis catalog & deterministic execution | `runner.kernel`, `runner.cordis_tools` |
| **Schedules** | Manifest-defined care flows & dynamic reminders | `runner.executor`, `runner.schedule` |
| **Providers** | Streaming completions & OpenAI-compatible tools | `runner.providers` |
| **Identity** | Decoupled alias, user, and session directory | `~/.agents/identity.json` |

- **Per-request loop (`runner.loop`)**: Reconstructs conversation traces on demand. Cordis modules (`runner/modules/*.json`) map as tools into OpenAI-compatible tool calling rounds.
- **Strict Layering (Cordis Principle)**: The executor owns no domain state or complex semantics. It strictly executes declared CLI verbs and inspects exit codes (`0 = healthy`).
- **Durable Manifests (Koru Principle)**: Scheduled tasks are declared in `runner/schedules/*.json`. Dynamic reminders are managed via `runner.schedule`.
- **Zero Bloat**: Pure Python standard library (`subprocess`, `json`, `pathlib`, `urllib`).

---

## Capabilities & Roadmap

### Core Capabilities (Implemented)

- [x] **Per-turn Execution Loop (`runner.loop`)**: Stateless reconstructed conversation trace per turn.
- [x] **Cordis CLI Tool Round**: Declared CLI verbs (`runner/modules/*.json`) mapped as OpenAI-compatible function tools.
- [x] **Job Kernel (`runner.kernel`)**: Declarative AST checking, mixing, and deterministic term reduction (`norm`, `konst`, `comp`, `app`).
- [x] **Koru Schedules (`runner.executor`)**: Declarative scheduled flow manifests (`runner/schedules/*.json`) with exit code health semantics (`exit 0 = healthy`).
- [x] **Dynamic Reminders (`runner.schedule`)**: Dynamic one-shot, cron, and routine jobs. `tick()` is in-process safe (file lock, timezone, grace window) and still callable from host cron.
- [x] **Approval gate**: `AGENTS_APPROVAL_CMD` / `AGENTS_APPROVAL_MODE` before mutating `call_job` tools.
- [x] **Untrusted tool fence**: tool results are wrapped for the model; redaction stays.
- [x] **External MCP client**: `~/.agents/mcp.json` tools show up as `mcp.<server>.<tool>`.
- [x] **Skills**: `skill.list` / `skill.load` read `~/.agents/skills/*/SKILL.md`.
- [x] **Attachments**: `--attach` sends images as vision parts when vision is enabled.
- [x] **Provider Streaming**: Streaming completions with prefill (`first_byte_sec`) and stall (`idle_sec`) hang detection.
- [x] **Decoupled Identity**: Decoupled alias (`telegram:123`), user (`u_...`), and session (`ses_...`) directory.
- [x] **FastMCP Interface (`runner.mcp_server`)**: Exposes `list_catalog`, `load_schema`, and `call_job` for MCP hosts.
- [x] **Zero Bloat Runtime**: Pure Python standard library only (no Celery, no Redis, no cron daemons).

### Planned (Roadmap)

- [ ] **Formal Kernel Verification**: Lean 4 formalization export of Cordis job combinators.
- [ ] **Decentralized Identity (DID)**: Native Ed25519 `did:key` resolution and proof exchange.
- [ ] **Multi-Agent Orchestration**: A2A peer execution mesh with cryptographically signed task mailboxes.
- [ ] **Adaptive Cadence**: Self-tuning care flow intervals based on host execution history.

---

## CLI & Modules

| Command / Entrypoint | Description |
| :--- | :--- |
| `agents-harness` (`runner.executor`) | Run and validate scheduled flows (`--all`, `--run <name>`, `--validate`) |
| `agents-harness-loop` (`runner.loop`) | Per-request agent loop (`--complete`, `--assemble-only`, `--call <verb>`) |
| `agents-harness-kernel` (`runner.kernel`) | Inspect and reduce declarative Cordis job terms (`--alphabet`, `--check`, `--reduce`) |
| `agents-harness-mcp` (`runner.mcp_server`) | FastMCP server exposing `list_catalog`, `load_schema`, and `call_job` |
| `python -m runner.schedule` | Dynamic schedule runner (`add`, `list`, `remove`, `tick`) |

### Example Commands

```bash
# Validate all scheduled manifests
python -m runner.executor --validate

# Run all scheduled tasks once
python -m runner.executor --all

# List discovered Cordis modules and tool bindings
python -m runner.loop --list-modules
python -m runner.loop --list-tools
python -m runner.loop --list-providers

# Assemble a turn (context + prompt) without calling provider
python -m runner.loop --user 123 --message "hello" --assemble-only

# Complete request with a provider
python -m runner.loop --user probe --message "status report" --complete --provider openai.default

# Tick dynamic due reminders (also safe to call in-process from klanker serve)
python -m runner.schedule tick

# Assemble a turn with an image (vision parts when AGENTS_VISION=1)
python -m runner.loop --user 123 --message "what is this?" --attach ./shot.png --assemble-only
```

---

## Approval, skills, schedules, attachments

Mutating `call_job` tools ask first when `AGENTS_APPROVAL_CMD` is set. `{user}` in that command becomes the turn's channel user. `AGENTS_APPROVAL_MODE=ask` gates mutators (the default once a command is set). Creating or listing a schedule does not ask in `ask` mode; removing one does. `strict` gates every tool that is not read-only, including schedule create. `off`, or no command, keeps the old behavior. Exit 0 approves, 1 denies, 2 is timeout/unavailable (denied). A denial tells the model not to retry.

Every tool result the model sees is wrapped in `<untrusted_data source="...">...</untrusted_data>` after redaction. Denials stay outside that fence.

Skills live in `~/.agents/skills/<name>/SKILL.md` (`AGENTS_SKILLS_DIR`, plus `AGENTS_SKILLS_EXTRA`). The system prompt lists name and description. `skill.load` returns the file. `skill.catalog` is a compatibility alias of `skill.list`.

Cron uses the job's `timezone` (else `AGENTS_TIMEZONE`, `TZ`, or `timezone` in `~/.agents/config.json`). The last fired minute is stored next to the manifests so a double tick does not double-fire, and a miss inside `grace_min` (default 5) still runs once. LLM reminders and routines default to `timeout_sec` 300. `tick()` holds its file lock only while claiming or finalizing, then runs jobs outside the lock. `tick(wait=False)` returns after the claim; `python -m runner.schedule tick` waits. A failed or timed-out one-shot is removed and not retried. The failure is logged, and `last_result` stays in `tick-state.json`. Routine jobs (`--prompt`) are not executed here: register `runner.schedule.register_routine_handler`. With no handler, a routine that has a verb runs that verb; a routine with only a prompt is skipped and its slot or one-shot file is left in place. `mcp.schedule.add` copies the turn's `channel` and `user` onto the job when the call omits them.

`python -m runner.loop --detached-session --session ses_…` runs that turn in `ses_…` and restores the previous active session afterward. A session id that starts with `routine:` is detached the same way without the flag. A host cron or a platform scheduler can call `python -m runner.schedule tick` when it runs as the same user as the service. That process does not inherit environment set only on the long-running service (for example `AGENTS_APPROVAL_CMD`).

## External MCP client

`~/.agents/mcp.json` (override `AGENTS_MCP_CONFIG`) uses the Claude/Cursor `mcpServers` shape, plus optional `allow` / `deny` globs. `${ENV_VAR}` expands in commands, args, env, urls, and headers. See `mcp.client.json.example`.

Each server tool is a catalog module `mcp.<server>.<tool>` (`kind: mcp_remote`) with the server's `inputSchema`. `readOnlyHint: true` is non-mutating; anything else is a mutator for the approval gate. Hand-written modules of the same name win.

The loop process is new every turn. Schemas are cached for `AGENTS_MCP_CACHE_SEC` (default 60). Each call connects, initializes, calls one tool, and disconnects (stdio, streamable HTTP, or SSE). Install the SDK with `pip install "agents-harness[mcp]"`. OAuth is out of scope.

## MCP Server Integration

To use the harness as an MCP server for Claude, Cursor, or Antigravity:

```json
{
  "mcpServers": {
    "agents-harness": {
      "command": "python",
      "args": ["-m", "runner.mcp_server"]
    }
  }
}
```

Exposes:
- `list_catalog`: List Cordis job modules available on this machine.
- `load_schema`: Load parameter schema for a catalog module.
- `call_job`: Safely execute a catalog module by name (unknown names fail closed).

---

## Tests

```bash
# Validate manifests
python -m runner.executor --validate

# Run test suite
python -m unittest discover tests
```

---

## License

MIT. See [LICENSE](LICENSE).
