"""Cordis job modules: MCP, skills, A2A, and named schedules.

The executor still owns no store. Each module is a verified verb. The
per-request loop lists them; scheduled ones also live under schedules/.
`when=later` means the module exists to be called, not run on every tick.
AGENTS_MODULES_DIR overlays by name (machine-local Cordis picks), same
shape as AGENTS_PROVIDERS_DIR. `enabled.json` in that overlay is the
ComfyUI-style pick list for tools advertised to the LLM.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Set

from .executor import load_manifest

if TYPE_CHECKING:
    from agents_workspace import Workspace

MODULES_DIR = Path(__file__).parent / "modules"
KINDS = ("mcp", "skill", "a2a", "schedule", "mcp_remote")
WHENS = ("on_request", "scheduled", "later")
ENABLED_FILE = "enabled.json"
_OPENAI_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _overlay_dir(workspace: "Workspace | None" = None) -> Path | None:
    if workspace is not None:
        return workspace.modules
    extra = os.environ.get("AGENTS_MODULES_DIR", "").strip()
    return Path(extra) if extra else None


def _module_dirs(
    primary: Path | None = None,
    workspace: "Workspace | None" = None,
) -> list[Path]:
    dirs = [primary or MODULES_DIR]
    overlay = _overlay_dir(workspace)
    if overlay is not None:
        if overlay.resolve() not in {d.resolve() for d in dirs}:
            dirs.append(overlay)
    return dirs


def load_module(path: Path) -> Dict[str, Any]:
    data = load_manifest(path)
    data.setdefault("kind", "schedule")
    data.setdefault("when", "on_request")
    data.setdefault("cadence", "on_request")
    if data["kind"] not in KINDS:
        raise ValueError(f"Module {path.name} kind must be one of {KINDS}")
    if data["when"] not in WHENS:
        raise ValueError(f"Module {path.name} when must be one of {WHENS}")
    return data


def list_modules(
    modules_dir: Path | None = None,
    workspace: "Workspace | None" = None,
) -> List[Dict[str, Any]]:
    dirs = (
        [modules_dir]
        if modules_dir is not None
        else _module_dirs(workspace=workspace)
    )
    if modules_dir is not None:
        return _list_modules_live(dirs, discover=False)
    try:
        from .catalog import cached_catalog_modules

        return cached_catalog_modules(
            module_dirs=dirs,
            loader=lambda: _list_modules_live(dirs, discover=True),
            workspace=workspace,
            probed_packages=_auto_package_names(),
        )
    except Exception:
        return _list_modules_live(dirs, discover=True)


def _list_modules_live(dirs: list[Path], *, discover: bool) -> List[Dict[str, Any]]:
    seen: dict[str, Dict[str, Any]] = {}
    for folder in dirs:
        if folder is None or not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            if p.name.lower() == ENABLED_FILE:
                continue
            row = load_module(p)
            seen[str(row["name"])] = row
    if discover:
        _merge_remote_modules(seen)
        _auto_mount(seen)
        _prepare_hot_commands(seen)
        _expand_exposed(seen)
    return list(seen.values())


def _prepare_hot_commands(seen: dict[str, Dict[str, Any]]) -> None:
    """Resolve pin/expose help-json rows before the catalog is snapshotted.

    The warm disk snapshot then carries the command specs needed by the system
    prompt, so a fresh loop process does not respawn every suite package just
    to render pinned verbs. A failed probe is cached as an empty command map
    for the snapshot TTL; ``pinned_entries`` renders its usual unavailable row.
    """
    from .helpjson import with_discovered_commands

    for name, row in list(seen.items()):
        source = str(row.get("commands_from") or "").strip().lower().replace("_", "-")
        if source != "help-json":
            continue
        if not (row.get("pin") or row.get("expose_commands")):
            continue
        merged = with_discovered_commands(row)
        commands = merged.get("commands") if isinstance(merged, dict) else None
        command_map = commands if isinstance(commands, dict) else {}
        prepared = dict(row)
        prepared["_prompt_commands"] = command_map
        if row.get("expose_commands"):
            prepared["commands"] = command_map
            prepared["_commands_resolved"] = True
        seen[name] = prepared


# Suite packages probed for cordis_expose auto-mount. Checked-in manifests and
# overlays always win; this only fills gaps (install a package -> its commands
# appear as Cordis tools without a module JSON in this repo).
_AUTO_PKGS = (
    "agents_browser",
    "agents_calendar",
    "agents_docs",
    "agents_keys",
    "agents_memory",
    "agents_terminal",
    "agents_traces",
)


def _auto_package_names() -> list[str]:
    pkgs = list(_AUTO_PKGS)
    extra = os.environ.get("AGENTS_CORDIS_PKGS", "").strip()
    if extra:
        pkgs.extend(p.strip() for p in extra.split(",") if p.strip())
    return pkgs


def _auto_mount(seen: dict[str, Dict[str, Any]]) -> None:
    """Synthesize manifests for installed suite packages whose ``--help-json``
    doc sets ``cordis_expose: true``. ``AGENTS_CORDIS_PKGS`` (comma list) adds
    packages; ``AGENTS_CORDIS_EXPOSE=0`` disables the probe."""
    if os.environ.get("AGENTS_CORDIS_EXPOSE", "").strip().lower() in ("0", "false", "no", "off"):
        return
    import importlib.util

    from .helpjson import load_help_json

    for pkg in _auto_package_names():
        if importlib.util.find_spec(pkg) is None:
            continue
        doc = load_help_json(f"python -m {pkg}")
        if not isinstance(doc, dict) or not doc.get("cordis_expose"):
            continue
        commands = doc.get("commands")
        if not isinstance(commands, dict) or not commands:
            continue
        short = str(doc.get("cordis_name") or pkg).strip()
        for prefix in ("agents_", "agents-", "agents."):
            if short.startswith(prefix):
                short = short[len(prefix):]
        short = short.replace("-", "_")
        name = str(doc.get("cordis_name") or f"mcp.{short}")
        if name in seen:
            continue
        seen[name] = {
            "name": name,
            "kind": "mcp",
            "when": "on_request",
            "cadence": "on_request",
            "verb": f"python -m {pkg}",
            "rests_on": f"auto-mounted via cordis_expose from {pkg} --help-json; exit 0 = action ok",
            "expected_exit": 0,
            "mutates": True,
            "as_tool": True,
            "commands": commands,
            "auto_mounted": pkg,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": f"Declared command: {' | '.join(list(commands)[:12])}",
                    },
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Full argv including the command",
                    },
                },
                "required": [],
            },
        }


def _expand_exposed(seen: dict[str, Dict[str, Any]]) -> None:
    """``expose_commands: ["add", ...]`` splits discovered commands into
    their own module rows (``<pkg>.<command>``) for tool choice and toolset
    granularity. Hand-written manifests with the same name win."""
    from .helpjson import with_discovered_commands

    for name, row in list(seen.items()):
        exposed = row.get("expose_commands")
        if not isinstance(exposed, list) or not exposed:
            continue
        commands = with_discovered_commands(row).get("commands")
        if not isinstance(commands, dict):
            continue
        for cmd in exposed:
            token = str(cmd).strip()
            spec = commands.get(token)
            if not token or not isinstance(spec, dict):
                continue
            child = dict(row)
            child["name"] = f"{name}.{token}"
            child["commands"] = {token: spec}
            child["command_pin"] = token
            child["exposed_from"] = name
            spec_params = spec.get("parameters")
            props = (
                dict(spec_params["properties"])
                if isinstance(spec_params, dict) and isinstance(spec_params.get("properties"), dict)
                else {}
            )
            props.setdefault(
                "argv",
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Full argv including the command (overrides named args)",
                },
            )
            child["parameters"] = {
                "type": "object",
                "properties": props,
                "required": list(spec_params.get("required") or [])
                if isinstance(spec_params, dict)
                else [],
            }
            if isinstance(spec_params, dict) and spec_params.get("description"):
                child["parameters"]["description"] = spec_params["description"]
            child["mutates"] = True  # per-call classification decides via the spec
            child.pop("expose_commands", None)
            seen.setdefault(child["name"], child)


def _merge_remote_modules(seen: dict[str, Dict[str, Any]]) -> None:
    """External MCP tools fill gaps. Hand-written manifests keep their names."""
    try:
        from .mcp_client import remote_modules

        for row in remote_modules():
            name = str(row.get("name") or "")
            if name and name not in seen:
                seen[name] = row
    except Exception:
        return


def find_module(
    name: str,
    modules_dir: Path | None = None,
    workspace: "Workspace | None" = None,
) -> Dict[str, Any] | None:
    needle = name.strip().lower()
    for m in list_modules(modules_dir, workspace=workspace):
        if m["name"].lower() == needle:
            return m
    return None


def openai_tool_name(module: Dict[str, Any] | str) -> str:
    """OpenAI function names are [a-zA-Z0-9_-]. Cordis names keep their dots."""
    if isinstance(module, dict):
        explicit = str(module.get("tool_name") or "").strip()
        if explicit:
            return explicit
        name = str(module.get("name") or "")
    else:
        name = str(module)
    return name.replace(".", "_")


def find_module_for_tool(
    name: str,
    modules_dir: Path | None = None,
    workspace: "Workspace | None" = None,
) -> Dict[str, Any] | None:
    needle = name.strip().lower()
    if not needle:
        return None
    rows = list_modules(modules_dir, workspace=workspace)
    for m in rows:
        if m["name"].lower() == needle:
            return m
        if openai_tool_name(m).lower() == needle:
            return m
    return None


def enabled_tool_names(
    workspace: "Workspace | None" = None,
) -> Set[str] | None:
    """None = every as_tool module. A set (even empty) is the overlay pick list."""
    overlay = _overlay_dir(workspace)
    if overlay is None:
        return None
    path = overlay / ENABLED_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("enabled")
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON list of module names")
    return {str(x).strip() for x in data if str(x).strip()}


def openai_tools(
    modules: List[Dict[str, Any]] | None = None,
    workspace: "Workspace | None" = None,
) -> list[dict[str, Any]]:
    """Always-on Cordis tools. Host verbs are call_job-only (closed carrier)."""
    from .cordis_tools import openai_cordis_tools

    _ = modules  # catalog is resolved inside call_job at execution time
    allow = enabled_tool_names(workspace)
    tools = openai_cordis_tools()
    if allow is None:
        return tools
    # Overlay enabled.json can disable all tools (empty list) for tests.
    if not allow:
        return []
    # When overlay lists specific module names, still expose the Cordis tools.
    return tools


def modules_for_system_prompt(modules: List[Dict[str, Any]] | None = None) -> str:
    """Short catalog the completions endpoint can see. No execution."""
    rows = modules if modules is not None else list_modules()
    if not rows:
        return ""
    lines = ["Available Cordis job modules (call later or via harness --call):"]
    for m in rows:
        lines.append(
            f"- {m['name']} kind={m['kind']} when={m['when']} verb={m['verb']}"
        )
    return "\n".join(lines)
