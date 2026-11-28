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
``<host>/cache/mcp-tools-cache.json`` (TTL ``AGENTS_MCP_CACHE_SEC``, default 300s;
``0`` refreshes every list). Each ``call_job`` opens a short-lived session,
initializes, calls one tool, and closes it. Stdio and streamable HTTP are
supported. SSE is used when ``transport`` is ``sse`` or the URL path ends with
``/sse``. The ``mcp`` SDK is the optional ``agents-harness[mcp]`` extra; the
executor stays stdlib.

Catalog names are ``mcp.<server>.<tool>`` with kind ``mcp_remote``. Side
effects follow ``side_effects``, then ``mutates``, then ``readOnlyHint``.
A missing class means the tool mutates (approval gate). Hand-written modules
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
_WS_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CACHE_MEM: dict[str, Any] = {"key": None, "modules": []}


def _host_dir() -> Path:
    try:
        from agents_workspace import Host

        return Host.from_env().dir
    except ImportError:
        raw = os.environ.get("AGENTS_HOST_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return (Path.home() / ".agents").resolve()


def mcp_config_path() -> Path:
    """Host ``mcp.json``. ``AGENTS_MCP_CONFIG_FILE`` wins (alias
    ``AGENTS_MCP_CONFIG``, one release)."""
    raw = os.environ.get("AGENTS_MCP_CONFIG_FILE", "").strip()
    if not raw:
        raw = os.environ.get("AGENTS_MCP_CONFIG", "").strip()
        if raw:
            import warnings

            warnings.warn(
                "AGENTS_MCP_CONFIG is deprecated; use AGENTS_MCP_CONFIG_FILE "
                "(alias removed after one release)",
                DeprecationWarning,
                stacklevel=2,
            )
    if raw:
        return Path(raw).expanduser()
    try:
        from agents_workspace import Host

        return Host.from_env().mcp_json
    except ImportError:
        return _host_dir() / "mcp.json"


def mcp_cache_path() -> Path:
    """``<host>/cache/mcp-tools-cache.json``. ``AGENTS_MCP_CACHE_FILE`` wins
    (alias ``AGENTS_MCP_CACHE``, one release)."""
    raw = os.environ.get("AGENTS_MCP_CACHE_FILE", "").strip()
    if not raw:
        raw = os.environ.get("AGENTS_MCP_CACHE", "").strip()
        if raw:
            import warnings

            warnings.warn(
                "AGENTS_MCP_CACHE is deprecated; use AGENTS_MCP_CACHE_FILE "
                "(alias removed after one release)",
                DeprecationWarning,
                stacklevel=2,
            )
    if raw:
        return Path(raw).expanduser()
    try:
        from agents_workspace import Host

        host = Host.from_env()
    except ImportError:
        return _host_dir() / "cache" / "mcp-tools-cache.json"
    try:
        ws = _resolve_ws_no_create()
    except Exception:
        ws = None
    if ws is not None:
        return host.cache / f"mcp-tools-cache-{ws.id}.json"
    return host.cache / "mcp-tools-cache.json"


def _cache_ttl() -> float:
    raw = os.environ.get("AGENTS_MCP_CACHE_SEC", "").strip()
    if not raw:
        return 300.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


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


def _load_one(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] MCP config {path}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _merge_mcp(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Workspace overlay wins per key; dict sections merge deep."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def _resolve_ws_no_create():
    """Resolve the active Workspace without creating a missing workspace dir."""
    from agents_workspace import Host, Workspace

    host = Host.from_env()
    if os.environ.get("AGENTS_WORKSPACE_ROOT", "").strip() or host.has_legacy_layout():
        try:
            return Workspace.from_env()
        except ValueError:
            return None
    env_ws = os.environ.get("AGENTS_WORKSPACE", "").strip()
    if env_ws:
        return host.workspace(env_ws) if host.workspace_exists(env_ws) else None
    found = host.list_workspaces()
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    try:
        return host.select_workspace(env=os.environ)
    except ValueError:
        return None


def _workspace_overlay_targets() -> tuple[str, Path | None]:
    """Resolve ``(ws_id, <ws>/mcp.json)`` without creating a default workspace."""
    try:
        from agents_workspace import Host, Workspace  # noqa: F401
    except ImportError:
        ws_id = os.environ.get("AGENTS_WORKSPACE", "").strip()
        if not (_WS_ID_OK.match(ws_id) and ".." not in ws_id):
            return "", None
        return ws_id, _host_dir() / "workspaces" / ws_id / "mcp.json"

    ws = _resolve_ws_no_create()
    if ws is not None:
        return ws.id, ws.mcp_json
    env_ws = os.environ.get("AGENTS_WORKSPACE", "").strip()
    if env_ws and _WS_ID_OK.match(env_ws) and ".." not in env_ws:
        return env_ws, None
    return "", None


def _expand_trusted(data: dict[str, Any]) -> dict[str, Any]:
    """Expand ``${VAR}`` via host/workspace secret vault when available, else env."""
    try:
        from agents_workspace import Host, expand_secrets
    except ImportError:
        return _interpolate(data)
    env: dict[str, str] | None = None
    if not str(os.environ.get("AGENTS_KEYRING") or "").strip():
        try:
            Path.home()
        except RuntimeError:
            # secrets._keyring_enabled checks default_host_dir() -> Path.home()
            # when AGENTS_KEYRING is unset; disable keyring in homedir-less envs.
            env = {**os.environ, "AGENTS_KEYRING": "0"}
    return expand_secrets(
        data,
        ws=_resolve_ws_no_create(),
        host=Host.from_env(),
        env=env,
        trusted=True,
    )


def _config_layers() -> tuple[Path, Path | None, Path | None]:
    """Return ``(host_cfg, host_ws_overlay, ws_overlay)`` paths."""
    cfg_path = mcp_config_path()
    host_dir = _host_dir()
    ws_id, ws_mcp = _workspace_overlay_targets()
    host_ws_path = (host_dir / "mcp" / f"ws-{ws_id}.json") if ws_id else None
    return cfg_path, host_ws_path, ws_mcp


def load_mcp_config(path: Path | None = None) -> dict[str, Any] | None:
    """Host ``mcp.json``, optional host ``<host>/mcp/ws-<id>.json`` (both
    vault/env-expanded), plus unexpanded ``<ws>/mcp.json``.

    An explicit ``path`` short-circuits the merge.
    """
    if path is not None:
        data = _load_one(path)
        return _expand_trusted(data) if data is not None else None
    cfg_path, host_ws_path, ws_mcp = _config_layers()
    data: dict[str, Any] = {}
    found = False
    raw_host = _load_one(cfg_path)
    if raw_host is not None:
        data = _expand_trusted(raw_host)
        found = True
    if host_ws_path is not None and host_ws_path != cfg_path and host_ws_path.is_file():
        raw_host_ws = _load_one(host_ws_path)
        if raw_host_ws is not None:
            data = _merge_mcp(data, _expand_trusted(raw_host_ws))
            found = True
    if (
        ws_mcp is not None
        and ws_mcp != cfg_path
        and ws_mcp != host_ws_path
        and ws_mcp.is_file()
    ):
        raw_ws = _load_one(ws_mcp)
        if raw_ws is not None:
            # Agent-writable <ws>/mcp.json must never expand ${ENV} or vault
            # secrets, and stdio servers need an approval-policy allow.
            raw_ws = _strip_untrusted_stdio(raw_ws, data)
            data = _merge_mcp(data, raw_ws)
            found = True
    if not found or not data:
        return None
    return data


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


def _ws_stdio_permitted(server: str) -> bool:
    """An approval rule ``allow`` on ``mcp.stdio:<server>`` (or ``*``)
    explicitly permits a ws-overlay stdio server. ``deny``/``ask`` and
    silence refuse it. Only host-trusted layers count — a rule inside the
    agent-writable ``<ws>/config.json`` can never permit a host binary."""
    try:
        from .approval_policy import load_trusted_rules, rule_matches
    except ImportError:
        return False
    try:
        rules = load_trusted_rules()
    except Exception:
        return False
    allowed = False
    for rule in rules:
        if not rule_matches(rule.action, f"mcp.stdio:{server}"):
            continue
        if rule.decision != "allow":
            return False
        allowed = True
    return allowed


def _strip_untrusted_stdio(
    ws_doc: dict[str, Any], trusted: dict[str, Any]
) -> dict[str, Any]:
    """Drop ws-overlay stdio servers lacking an approval-policy allow.

    A ``command`` spec spawns an arbitrary host binary; the agent-writable
    ws layer may only mount one when a trusted rule allows it. An overlay
    entry that would edit a trusted stdio spec is refused for the same
    reason."""
    servers = ws_doc.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return ws_doc
    trusted_servers = server_map(trusted)
    for name, spec in list(servers.items()):
        if not isinstance(spec, dict):
            continue
        base = trusted_servers.get(name)
        stdio = bool(spec.get("command")) or (
            isinstance(base, dict) and bool(base.get("command"))
        )
        if not stdio or _ws_stdio_permitted(str(name)):
            continue
        del servers[name]
        print(
            f"[!] MCP ws server '{name}' refused: stdio needs an "
            "approval-policy allow (mcp.stdio:<server>)",
            file=sys.stderr,
        )
    return ws_doc


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


def _descriptor_effects(tool: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Effective mutates flag plus the metadata fields worth keeping.

    ``side_effects`` wins over ``mutates`` over ``readOnlyHint``. A descriptor
    with none of them stays mutating, which matches a missing readOnlyHint.
    """
    from .helpjson import READ_ONLY, classify_spec

    meta: dict[str, Any] = {}
    if "side_effects" in tool:
        meta["side_effects"] = tool.get("side_effects")
    if "mutates" in tool:
        meta["mutates"] = tool.get("mutates")
    ann = tool.get("annotations")
    if isinstance(ann, dict):
        meta["annotations"] = ann
    elif "readOnlyHint" in tool:
        meta["readOnlyHint"] = tool.get("readOnlyHint")
    effects = classify_spec(meta, default=None)
    if effects is None:
        read_only = bool(tool.get("readOnlyHint"))
    else:
        read_only = effects == READ_ONLY
    return (not read_only), meta


def modules_from_descriptors(
    server: str,
    tools: list[dict[str, Any]],
    *,
    used: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build catalog rows from plain tool dicts (name, description, inputSchema, side effects)."""
    names = used if used is not None else set()
    rows: list[dict[str, Any]] = []
    safe_server = _safe(server)
    for tool in tools:
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            continue
        mutates, meta = _descriptor_effects(tool)
        read_only = not mutates
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
                "mutates": mutates,
                "readOnlyHint": read_only,
                "parameters": schema,
                "mcp_server": server,
                "mcp_tool": tool_name,
            }
        )
        if "side_effects" in meta:
            rows[-1]["side_effects"] = meta["side_effects"]
        if isinstance(meta.get("annotations"), dict):
            rows[-1]["annotations"] = meta["annotations"]
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
    dumped: dict[str, Any] = {}
    model_dump = getattr(tool, "model_dump", None)
    if callable(model_dump):
        raw = model_dump()
        if isinstance(raw, dict):
            dumped = raw
    if not isinstance(schema, dict) and isinstance(dumped.get("inputSchema"), dict):
        schema = dumped["inputSchema"]
    row = {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        "readOnlyHint": read_only,
    }
    side = dumped.get("side_effects", getattr(tool, "side_effects", None))
    if side not in (None, ""):
        row["side_effects"] = side
    if "mutates" in dumped:
        row["mutates"] = dumped.get("mutates")
    annotations = dumped.get("annotations")
    if isinstance(annotations, dict):
        row["annotations"] = annotations
        if "readOnlyHint" in annotations:
            row["readOnlyHint"] = bool(annotations.get("readOnlyHint"))
    elif isinstance(ann, dict):
        row["annotations"] = ann
    return row


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


def _cache_key_for(paths: list[Path]) -> str:
    return "|".join(_cache_key(p) for p in paths)


def remote_modules() -> list[dict[str, Any]]:
    """Catalog rows for configured remote tools. Empty when no config or SDK."""
    cfg_path, host_ws_path, ws_mcp = _config_layers()
    existing: list[Path] = []
    for p in (cfg_path, host_ws_path, ws_mcp):
        if p is not None and p not in existing and p.is_file():
            existing.append(p)
    if not existing:
        return []
    key = _cache_key_for(existing)
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
    cfg = load_mcp_config()
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


class RemoteToolError(str):
    """Model-facing text of an MCP result whose ``isError`` flag is set.

    A plain ``str`` that happens to start with ``MCP error:`` is not this type.
    Callers use the type, not the prefix, to decide success.
    """

    __slots__ = ()


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
        return RemoteToolError("MCP error: " + (body or "tool failed"))
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
