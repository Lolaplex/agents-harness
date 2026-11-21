# 001 Comfyui Brain Install

- Harness+brain first launch is ComfyUI-shaped and lives in agents-sandbox (`setup.ps1`): empty cache shell, vand updates sibling clones later, `AGENTS_MODULES_DIR` + `enabled.json` pick Cordis tools, `AGENTS_PROVIDERS_DIR` holds LMS. Loop advertises `as_tool` modules as OpenAI functions and runs one execute_job round. Scripted.tool proves the canary without an LLM; `--live` is Gemma-4-e4b Claude-4.6-Opus distill via LM Studio doing a real memory-search roundtrip. Do not merge stores into agents-brain. Do not auto-merge MCP into the host IDE.
