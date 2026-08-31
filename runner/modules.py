"""Cordis job modules: MCP, skills, A2A, and named schedules.

The executor still owns no store. Each module is a verified verb. The
per-request loop lists them; scheduled ones also live under schedules/.
`when=later` means the module exists to be called, not run on every tick.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .executor import load_manifest

MODULES_DIR = Path(__file__).parent / "modules"
KINDS = ("mcp", "skill", "a2a", "schedule")
WHENS = ("on_request", "scheduled", "later")


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


def list_modules(modules_dir: Path = MODULES_DIR) -> List[Dict[str, Any]]:
    if not modules_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(modules_dir.glob("*.json")):
        out.append(load_module(p))
    return out


def find_module(name: str, modules_dir: Path = MODULES_DIR) -> Dict[str, Any] | None:
    needle = name.strip().lower()
    for m in list_modules(modules_dir):
        if m["name"].lower() == needle:
            return m
    return None


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
