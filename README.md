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

```
                  +---------------------------+
                  |   Inbound Request / Turn  |
                  +-------------+-------------+
                                |
                                v
                   +-------------------------+
                   |  runner.loop (Loop API) |
                   +------------+------------+
                                |
        +-----------------------+-----------------------+
        |                                               |
        v                                               v
+---------------+                               +---------------+
| Providers     |                               | Cordis Kernel |
| (LLM Stream)  |                               | (Job Catalog) |
+-------+-------+                               +-------+-------+
        |                                               |
        v                                               v
+---------------+                               +---------------+
|  Synthesize   |<====== Tool Calls / Results ==| runner.modules|
|    Answer     |                               | (CLI Verbs)   |
+---------------+                               +---------------+
```

- **Per-request loop (`runner.loop`)**: Reconstructed conversation trace per turn. Cordis verbs (`runner/modules/*.json`) mapped as tools into OpenAI-compatible tool calling rounds.
- **Strict Layering (Cordis Principle)**: The executor owns no domain state or complex semantics. It strictly executes declared CLI verbs and inspects exit codes (`0 = healthy`).
- **Durable Manifests**: Scheduled jobs declared in `runner/schedules/*.json`. Dynamic reminders managed via `runner.schedule`.
- **Identity Directory**: Decoupled alias (`telegram:123`), user (`u_...`), and session (`ses_...`) mapped via `~/.agents/identity.json`.
- **Zero Bloat**: Pure Python standard library (`subprocess`, `json`, `pathlib`, `urllib`).

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

# Tick dynamic due reminders
python -m runner.schedule tick
```

---

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
