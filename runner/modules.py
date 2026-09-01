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
from typing import Any, Dict, List, Set

from .executor import load_manifest

MODULES_DIR = Path(__file__).parent / "modules"
KINDS = ("mcp", "skill", "a2a", "schedule")
WHENS = ("on_request", "scheduled", "later")
ENABLED_FILE = "enabled.json"
_OPENAI_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _module_dirs(primary: Path | None = None) -> list[Path]:
    dirs = [primary or MODULES_DIR]
    extra = os.environ.get("AGENTS_MODULES_DIR", "").strip()
    if extra:
        overlay = Path(extra)
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


def list_modules(modules_dir: Path | None = None) -> List[Dict[str, Any]]:
    seen: dict[str, Dict[str, Any]] = {}
    dirs = [modules_dir] if modules_dir is not None else _module_dirs()
    for folder in dirs:
        if folder is None or not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            if p.name.lower() == ENABLED_FILE:
                continue
            row = load_module(p)
            seen[str(row["name"])] = row
    return list(seen.values())


def find_module(name: str, modules_dir: Path | None = None) -> Dict[str, Any] | None:
    needle = name.strip().lower()
    for m in list_modules(modules_dir):
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


def find_module_for_tool(name: str, modules_dir: Path | None = None) -> Dict[str, Any] | None:
    needle = name.strip().lower()
    if not needle:
        return None
    rows = list_modules(modules_dir)
    for m in rows:
        if m["name"].lower() == needle:
            return m
        if openai_tool_name(m).lower() == needle:
            return m
    return None


def enabled_tool_names() -> Set[str] | None:
    """None = every as_tool module. A set (even empty) is the overlay pick list."""
    extra = os.environ.get("AGENTS_MODULES_DIR", "").strip()
    if not extra:
        return None
    path = Path(extra) / ENABLED_FILE
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("enabled")
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON list of module names")
    return {str(x).strip() for x in data if str(x).strip()}


def openai_tools(modules: List[Dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Three always-on Cordis tools. Host verbs are call_job-only (closed carrier)."""
    from .cordis_tools import openai_cordis_tools

    _ = modules  # catalog is resolved inside call_job at execution time
    allow = enabled_tool_names()
    tools = openai_cordis_tools()
    if allow is None:
        return tools
    # Overlay enabled.json can disable all tools (empty list) for tests.
    if not allow:
        return []
    # When overlay lists specific module names, still expose the three Cordis tools.
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
