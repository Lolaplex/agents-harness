# 005 Identity, providers, cached system prefix

Three ids: alias (`telegram:5712`) is the device handle, user (`u_…`) is the person, session (`ses_…`) is the thread. Resume from another device with `session` or `user_id`; the new alias is bound on that user. Legacy `telegram-N` traces are adopted once if no active session exists. Directory: `~/.agents/identity.json`. DID/plex-net later.

Providers are modules that are not shell verbs: `runner/providers/*.json` (`name`, `kind`, `verb=chat.completions`, `rests_on`). The loop calls `complete()`. `openai_compat` is the default; `echo` is the test double. Another LLM is another provider file.

System prompt is XML: instructions (role, workflow, guardrails, tool_rules), skill_catalog, runtime_context (user, aliases, work, project, timezone, session start_date). Wall-clock is a trailing `<clock>` message after history so prefix cache of the system block survives subsequent turns. History turns carry frozen `<ts>` from the trace event, not `now`.

- Three ids: alias (telegram:5712) is the device handle, user (u_…) is the person, session (ses_…) is the thread. Resume from another device with session or user_id. ~/.agents/identity.json. System prompt XML: instructions + skill_catalog + runtime_context with frozen start_date. Wall-clock is a trailing <clock> after history so prefix cache survives. Harness providers (openai_compat, echo) share module JSON shape; complete() not shell.
