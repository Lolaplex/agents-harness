"""FastMCP server for agents-harness. Same shape as agents-memory / agents-traces."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .executor import execute_job, list_schedules
from .loop import build_payload
from .modules import find_module, list_modules
from .providers import list_providers
from .user_profile import load_user_profile

mcp = FastMCP("agents-harness")


@mcp.tool()
def list_harness_modules() -> str:
    """List Cordis job modules (mcp, skill, a2a) and scheduled care jobs."""
    modules = [
        {"name": m["name"], "kind": m["kind"], "when": m["when"], "verb": m["verb"]}
        for m in list_modules()
    ]
    schedules = [
        {"name": s["name"], "cadence": s["cadence"], "verb": s["verb"]}
        for s in list_schedules()
    ]
    providers = [
        {"name": p["name"], "kind": p["kind"], "verb": p["verb"]}
        for p in list_providers()
    ]
    return json.dumps(
        {"modules": modules, "schedules": schedules, "providers": providers}, indent=2
    )


@mcp.tool()
def assemble_session(
    channel: str = "local",
    user: str = "",
    session: str = "",
    message: str = "",
    project: str = "",
    limit: int = 24,
) -> str:
    """Rebuild chat-completions messages from the trace for this channel/user/project."""
    sid = session
    user_id = ""
    start_date = ""
    alias = ""
    profile = load_user_profile()
    if not sid:
        if not user:
            return json.dumps({"error": "session or user is required"})
        try:
            from agents_traces.identity import IdentityStore, session_has_events
        except ImportError:
            return json.dumps({"error": "agents-traces not installed"})
        resolved = IdentityStore().resolve(
            channel=channel,
            user=user,
            session=session,
            project=project.strip(),
            legacy_exists=session_has_events,
        )
        sid = resolved.session.id
        user_id = resolved.user.id
        start_date = resolved.session.start_date
        alias = resolved.alias
    try:
        payload = build_payload(
            sid,
            message,
            limit=limit,
            user_id=user_id,
            user_display=profile.get("display", ""),
            work=profile.get("work", ""),
            project=project.strip(),
            timezone_name=profile.get("timezone", ""),
            aliases=[alias] if alias else [],
            start_date=start_date,
        )
    except ImportError:
        return json.dumps({"error": "agents-traces not installed"})
    return json.dumps(
        {
            "session": sid,
            "user_id": user_id,
            "alias": alias,
            "start_date": start_date,
            "project": project.strip(),
            "messages": payload,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def call_job(name: str) -> str:
    """Run one module or schedule by name. Cordis: CLI verb + exit code only."""
    mod = find_module(name)
    if mod is not None:
        res = execute_job(mod)
        return json.dumps(res, default=str)
    match = [s for s in list_schedules() if s["name"].lower() == name.lower()]
    if not match:
        return json.dumps({"error": f"no module or schedule named '{name}'"})
    res = execute_job(match[0])
    return json.dumps(res, default=str)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
