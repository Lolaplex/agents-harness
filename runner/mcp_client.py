"""External MCP client bridge.

Config file ``~/.agents/mcp.json`` (override ``AGENTS_MCP_CONFIG``), Claude/Cursor
shape::

    {"mcpServers": {"name": {"command": "...", "args": [], "env": {}}
                             | {"url": "https://...", "headers": {}}},
     "allow": {"name": ["tool*"]},
     "deny": {"name": ["secret*"]}}

``${ENV_VAR}`` is expanded in commands, args, env values, urls, and headers.
OAuth is out of scope.

Session strategy: each ``runner.loop`` turn is a new process, so there is no
cross-turn MCP session. Tool *schemas* are cached in memory and in
``~/.agents/mcp-tools-cache.json`` (TTL ``AGENTS_MCP_CACHE_SEC``, default 60s;
``0`` refreshes every list). Each ``call_job`` opens a short-lived session,
initializes, calls one tool, and closes it. Stdio and streamable HTTP are
supported. SSE is used when ``transport`` is ``sse`` or the URL path ends with
``/sse``. The ``mcp`` SDK is the optional ``agents-harness[mcp]`` extra; the
executor stays stdlib.

Catalog names are ``mcp.<server>.<tool>`` with kind ``mcp_remote``. A missing
``readOnlyHint`` means the tool mutates (approval gate). Hand-written modules
of the same name win.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_CACHE_MEM: dict[str, Any] = {"key": None, "modules": []}


def mcp_config_path() -> Path:
    raw = os.environ.get("AGENTS_MCP_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".agents" / "mcp.json"


def mcp_cache_path() -> Path:
    raw = os.environ.get("AGENTS_MCP_CACHE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".agents" / "mcp-tools-cache.json"


def _cache_ttl() -> float:
    raw = os.environ.get("AGENTS_MCP_CACHE_SEC", "").strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _connect_timeout() -> float:
    raw = os.environ.get("AGENTS_MCP_CONNECT_TIMEOUT", "").strip()
    if not raw:
        return 10.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 10.0


def interpolate(value: str) -> str:
    return _ENV.sub(lambda match: os.environ.get(match.group(1), ""), value)


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return interpolate(value)
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _interpolate(item) for key, item in value.items()}
    return value


def load_mcp_config(path: Path | None = None) -> dict[str, Any] | None:
    cfg_path = path or mcp_config_path()
    if not cfg_path.is_file():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] MCP config {cfg_path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    return _interpolate(data)


def server_map(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = cfg.get("mcpServers")
    if not isinstance(raw, dict):
        raw = cfg.get("servers")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if isinstance(spec, dict):
            out[str(name)] = spec
    return out


def tool_allowed(server: str, tool: str, cfg: dict[str, Any]) -> bool:
    deny = cfg.get("deny") if isinstance(cfg.get("deny"), dict) else {}
    deny_pats = list(deny.get(server) or []) + list(deny.get("*") or [])
    if any(fnmatch.fnmatch(tool, str(pat)) for pat in deny_pats):
        return False
    allow = cfg.get("allow") if isinstance(cfg.get("allow"), dict) else None
    if not allow or (server not in allow and "*" not in allow):
        return True
    pats = allow.get(server, allow.get("*", []))
    if not isinstance(pats, list) or not pats:
        return False
    return any(fnmatch.fnmatch(tool, str(pat)) for pat in pats)


def _safe(part: str) -> str:
    cleaned = _SAFE.sub("_", part.strip())
    return cleaned or "tool"


def modules_from_descriptors(
    server: str,
    tools: list[dict[str, Any]],
    *,
    used: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build catalog rows from plain tool dicts (name, description, inputSchema, readOnlyHint)."""
    names = used if used is not None else set()
    rows: list[dict[str, Any]] = []
    safe_server = _safe(server)
    for tool in tools:
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            continue
        read_only = bool(tool.get("readOnlyHint"))
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {
            "type": "object",
            "properties": {},
        }
        base = f"mcp.{safe_server}.{_safe(tool_name)}"
        name = base
        n = 2
        while name in names:
            name = f"{base}_{n}"
            n += 1
        names.add(name)
        description = str(tool.get("description") or "").strip()
        rows.append(
            {
                "name": name,
                "kind": "mcp_remote",
                "when": "on_request",
                "cadence": "on_request",
                "verb": f"mcp://{server}/{tool_name}",
                "rests_on": description or f"remote MCP {server}/{tool_name}",
                "expected_exit": 0,
                "timeout_sec": 60,
                "mutates": not read_only,
                "readOnlyHint": read_only,
                "parameters": schema,
                "mcp_server": server,
                "mcp_tool": tool_name,
            }
        )
    return rows


def _cache_key(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return f"{path}:{mtime}"


def _read_disk_cache(path: Path, key: str) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    fetched = float(data.get("fetched_at") or 0)
    if _cache_ttl() == 0 or (time.time() - fetched) > _cache_ttl():
        return None
    modules = data.get("modules")
    if not isinstance(modules, list):
        return None
    return [row for row in modules if isinstance(row, dict)]


def _write_disk_cache(path: Path, key: str, modules: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"key": key, "fetched_at": time.time(), "modules": modules}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _stale_disk_cache(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    modules = data.get("modules") if isinstance(data, dict) else None
    if not isinstance(modules, list):
        return []
    return [row for row in modules if isinstance(row, dict)]


async def _open_session(spec: dict[str, Any], fn: Any) -> Any:
    from mcp import ClientSession

    if spec.get("command"):
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = spec.get("env") if isinstance(spec.get("env"), dict) else None
        params = StdioServerParameters(
            command=str(spec["command"]),
            args=[str(item) for item in (spec.get("args") or [])],
            env={str(k): str(v) for k, v in env.items()} if env else None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    url = str(spec.get("url") or "").strip()
    if not url:
        raise RuntimeError("MCP server needs command or url")
    headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else None
    header_map = {str(k): str(v) for k, v in headers.items()} if headers else None
    transport = str(spec.get("transport") or "").strip().lower()
    if transport == "sse" or url.rstrip("/").endswith("/sse"):
        from mcp.client.sse import sse_client

        async with sse_client(url, headers=header_map) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=header_map) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _tool_dict(tool: Any) -> dict[str, Any]:
    ann = getattr(tool, "annotations", None)
    read_only = bool(getattr(ann, "readOnlyHint", False)) if ann is not None else False
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        model_dump = getattr(tool, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict) and isinstance(dumped.get("inputSchema"), dict):
                schema = dumped["inputSchema"]
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        "readOnlyHint": read_only,
    }


async def _list_server_tools(spec: dict[str, Any]) -> list[dict[str, Any]]:
    async def _list(session: Any) -> list[dict[str, Any]]:
        listed = await session.list_tools()
        return [_tool_dict(tool) for tool in (getattr(listed, "tools", None) or [])]

    return await asyncio.wait_for(_open_session(spec, _list), timeout=_connect_timeout())


async def _discover(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    for name, spec in server_map(cfg).items():
        try:
            tools = await _list_server_tools(spec)
        except Exception as exc:
            print(f"[!] MCP server '{name}' list failed: {exc}", file=sys.stderr)
            continue
        kept = [tool for tool in tools if tool_allowed(name, str(tool.get("name") or ""), cfg)]
        rows.extend(modules_from_descriptors(name, kept, used=used))
    return rows


def discover_modules(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "mcp SDK not installed; pip install 'agents-harness[mcp]'"
        ) from exc
    return asyncio.run(_discover(cfg))


def remote_modules() -> list[dict[str, Any]]:
    """Catalog rows for configured remote tools. Empty when no config or SDK."""
    path = mcp_config_path()
    if not path.is_file():
        return []
    key = _cache_key(path)
    if _CACHE_MEM.get("key") == key and _cache_ttl() != 0:
        fetched = float(_CACHE_MEM.get("fetched_at") or 0)
        if (time.time() - fetched) <= _cache_ttl():
            return list(_CACHE_MEM.get("modules") or [])
    disk = _read_disk_cache(mcp_cache_path(), key)
    if disk is not None:
        _CACHE_MEM["key"] = key
        _CACHE_MEM["fetched_at"] = time.time()
        _CACHE_MEM["modules"] = disk
        return list(disk)
    cfg = load_mcp_config(path)
    if not cfg:
        return []
    try:
        modules = discover_modules(cfg)
    except Exception as exc:
        print(f"[!] MCP discovery failed: {exc}", file=sys.stderr)
        stale = _stale_disk_cache(mcp_cache_path())
        return stale
    _CACHE_MEM["key"] = key
    _CACHE_MEM["fetched_at"] = time.time()
    _CACHE_MEM["modules"] = modules
    _write_disk_cache(mcp_cache_path(), key, modules)
    return list(modules)


def _content_text(result: Any) -> str:
    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(str(text))
            continue
        if isinstance(block, dict) and block.get("text"):
            chunks.append(str(block["text"]))
            continue
        chunks.append(str(block))
    body = "\n".join(chunks).strip()
    if getattr(result, "isError", False):
        return "MCP error: " + (body or "tool failed")
    return body


async def _call(spec: dict[str, Any], tool: str, arguments: dict[str, Any]) -> str:
    async def _invoke(session: Any) -> str:
        result = await session.call_tool(tool, arguments)
        return _content_text(result)

    return await asyncio.wait_for(_open_session(spec, _invoke), timeout=_connect_timeout())


def call_remote(server: str, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """One short-lived MCP session. Raises RuntimeError when the SDK or server is missing."""
    cfg = load_mcp_config()
    if cfg is None:
        raise RuntimeError(f"no MCP config at {mcp_config_path()}")
    spec = server_map(cfg).get(server)
    if spec is None:
        raise RuntimeError(f"MCP server '{server}' is not configured")
    if not tool_allowed(server, tool, cfg):
        raise RuntimeError(f"MCP tool '{server}/{tool}' is not allowed")
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "mcp SDK not installed; pip install 'agents-harness[mcp]'"
        ) from exc
    payload = arguments if isinstance(arguments, dict) else {}
    return asyncio.run(_call(spec, tool, payload))


def reset_cache() -> None:
    """Test helper. Drops the in-process schema cache."""
    _CACHE_MEM.clear()
    _CACHE_MEM["key"] = None
    _CACHE_MEM["modules"] = []
