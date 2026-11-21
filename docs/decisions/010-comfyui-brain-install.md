# 010 ComfyUI-shaped brain install

**Status:** implemented (sandbox first-launch). Product `agents-brain` bundle still later.
**Date:** 2026-08-31

## Context

agents-sandbox is the fleet test: sibling `agents-*` clones into one venv, `brain/` as the vault fixture, this harness as the loop. The product shape is the same bundle as an *install*, like Hermes Agent or ComfyUI on first launch. Empty shell. Pick a machine. Enable Cordis modules. Fill.

Decision 002 already said agents-brain is a later install bundle, not a merged store. This is how that bundle should feel.

## Decision

1. **Empty shell first.** Harness + identity + traces dir. No live `~/.agents` merge, no MCP auto-write into the host IDE. `agents-sandbox/setup.ps1` is that first launch: venv, pip -e fleet, seed brain, list tools, selfcheck.
2. **vand is the updater.** Replicate/pin sibling clones. Do not force-reset dirty trees.
3. **Modules are selectable.** Package catalog in `runner/modules/`. Machine overlay `AGENTS_MODULES_DIR`. `enabled.json` in that overlay is the pick list for tools advertised to the LLM. Enabling memory/docs/traces/skills is turning Cordis verbs on, not forking the loop.
4. **Brain is the vault + the catalog, not a new store.** Fixture today is `agents-sandbox/brain/`. Install copies that layout into `AGENTS_HOME` on the target machine.
5. **First launch is a shell.** One command: venv, pip -e fleet, seed brain, selfcheck (echo, scripted tool turn, advertised tools, optional live LMS roundtrip). Different machines get the same script with different overlays.
6. **Roundtrips go through OpenAI tools.** `as_tool` modules become functions (`mcp.memory.search` → `mcp_memory_search`). One tool round: complete → `execute_job` → complete. First live model is LM Studio `gemma-4-e4b-claude-4.6-opus-reasoning-distilled`. Host URL/token stay in the sandbox overlay, not in this clone.

Trade-off: one tool round in the loop is enough to prove Cordis+brain. A full ReAct agent would import store internals or grow an in-process MCP client. Rejected for now. Assistant still owns FastMCP adapters. Forced `tool_choice` stays off on the overlay so bench completes are not hijacked into memory search.

## Now

- Scripted smoke: `scripted.tool` → `mcp.memory.search` → canary `amber-47`.
- Live smoke (`setup.ps1 -Live`): LMS Gemma must call `mcp_memory_search`, Cordis must run, answer must quote `amber-47`.
