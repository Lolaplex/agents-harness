# 006 Context pointers, slim sandbox, AVO specialization

One host MCP: `agents-harness`. Memory, docs, traces, skills are Cordis modules behind `list_catalog` / `load_schema` / `call_job`. Installing another package must not add another `mcpServers` block to the host. That is how mcp.json avoids context explosion.

Catalog in the cached system prefix is names + one-line summaries. Schemas and skill bodies load when named (Koru request-time fill). Tool results stay as handles/paths (NVIDIA pass-by-reference). Do not serialize Coolify/trace dumps into the transcript.

Sandbox, slimmest that is real: Cordis already (argv, timeout, cwd, expected_exit, no in-process imports). Next: Windows Job Object + env allowlist + no `shell=True` with model text. MCP child processes inherit that jail. Docker/Firecracker is not the default on this machine. Untrusted compute-only skills can be WASM later. NVIDIA OpenShell is the Linux kernel cell; we do not pretend to have it on Windows 10.

Subsequent runs (AVO + Futamura): traces are the lineage. Prefix cache of the XML system stays. After a tool sequence succeeds, compile it to a Koru schedule (1st Futamura) and `--call` it next time instead of asking the model. Stall supervisor: same verb+error 3x from traces, stop that path. Session-bind only the schemas already loaded this thread.

ISAR combinators: the catalog is the alphabet. `call_job` is reduce. Unknown names do not execute (closed carrier). Independent verbs commute (confluence). A working reduction is a flow JSON.

- Host mcp.json is one server (agents-harness). Nested packages are Cordis modules: list_catalog / load_schema / call_job. Catalog rows in the cached XML prefix; schemas and skill bodies load when named (Koru fill). Tool results are handles not dumps (NVIDIA pass-by-reference). Slim sandbox: Cordis argv+timeout+cwd now; Windows Job Object + env allowlist next; Docker not default. Subsequent runs: traces lineage; successful verb sequence compiles to a Koru schedule (Futamura spec1). ISAR: catalog is the alphabet, unknown names do not execute, independent verbs commute.
