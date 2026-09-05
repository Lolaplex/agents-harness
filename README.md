# agents-harness

<p align="left">
  <a href="https://github.com/Lolaplex/agents-harness/releases"><img src="https://img.shields.io/badge/version-0.0.1-blue.svg?style=flat-square" alt="Version 0.0.1"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Standard-orange.svg?style=flat-square" alt="MCP"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-harness/"><img src="https://img.shields.io/pypi/v/agents-harness.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

**Declarative scheduled flow runner, per-request agent loop, and job kernel for local agent services.**  
Zero heavy dependencies (Python standard library only). No Celery, no Redis, no cron daemons.

---

## Architecture Principles

- **Per-request loop**: The agent is a reconstructed trace (python -m runner.loop). SKILL/MCP/A2A entries live in 
unner/modules/ as Cordis verbs (when=on_request or later). Scheduled care jobs stay in 
unner/schedules/.
- **MCP server support**: Optional extra pip install -e ".[mcp]" with FastMCP support (python -m runner.mcp_server). The executor stays stdlib-only.
- **Strict Layering (Cordis Principle)**: The executor owns no state, no domain store, and no semantics. It strictly invokes verified CLIs and inspects exit codes.
- **Job kernel**: Declarative catalog JSON is the alphabet. Checked, mixed, and reduced deterministically (python -m runner.kernel).
- **Identity directory**: Decoupled alias, user, and session identifiers (~/.agents/identity.json).

---

## Commands

`ash
# Validate manifests
python -m runner.executor --validate

# Run scheduled flows
python -m runner.executor --all
python -m runner.executor --run <name>

# Loop & execution
python -m runner.loop --list-modules
python -m runner.loop --list-tools
python -m runner.loop --list-providers
python -m runner.loop --assemble-only --user <id> --message "hello"

# FastMCP server
python -m runner.mcp_server

# Kernel
python -m runner.kernel --alphabet
`

---

## License

MIT License. Copyright (c) 2026 Lolaplex.
