"""Persona manifests: system prompt fragments without tool explosion.

Workspace personas (``<ws>/personas/<id>/persona.json``) win over package
personas (``runner/personas/<name>.json``) with the same id. Personas are
equal - a role (e.g. CoS) is a persona with a role description, not a
higher layer. A persona may keep optional private notes in
``<ws>/personas/<id>/notes.md``; workspace memory stays shared.

A workspace manifest may only set ``id``, ``name``, ``system_append`` and
``notes`` — unknown keys are ignored. Privileged manifest fields such as
``allowed_kinds`` or ``caps_ref`` stay package-only, so a remote persona
create cannot claim them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PERSONAS_DIR = Path(__file__).parent / "personas"


def _workspace_personas_dir(workspace) -> Path | None:
    if workspace is not None:
        return Path(workspace.personas)
    try:
        from agents_workspace import Workspace

        return Workspace.from_env().personas
    except Exception:
        return None


# Fields a workspace persona manifest may set. Package manifests may carry
# privileged keys (allowed_kinds, caps_ref); a ws manifest cannot claim them.
WS_PERSONA_FIELDS = frozenset(
    {"id", "name", "system_append", "notes", "role", "accepts_messages", "allow_peers", "memory"}
)


def _load_file(path: Path, name: str, *, allowlist: bool = False) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if allowlist:
        data = {k: v for k, v in data.items() if k in WS_PERSONA_FIELDS}
    data.setdefault("id", name)
    data.setdefault("name", data.get("id") or name)
    return data


def _iter_workspace(workspace) -> list[dict]:
    root = _workspace_personas_dir(workspace)
    if root is None or not root.is_dir():
        return []
    rows = []
    for entry in sorted(root.iterdir()):
        manifest = entry / "persona.json"
        if entry.is_dir() and manifest.is_file():
            row = _load_file(manifest, entry.name, allowlist=True)
            if row is not None:
                row["source"] = "workspace"
                rows.append(row)
    return rows


def _iter_package() -> list[dict]:
    if not PERSONAS_DIR.exists():
        return []
    rows = []
    for path in sorted(PERSONAS_DIR.glob("*.json")):
        row = _load_file(path, path.stem)
        if row is not None:
            row["source"] = "package"
            rows.append(row)
    return rows


def list_personas(workspace=None) -> list[dict]:
    """Workspace personas first; package personas fill in missing ids."""
    rows = _iter_workspace(workspace)
    seen = {str(r.get("id", "")).lower() for r in rows}
    for row in _iter_package():
        if str(row.get("id", "")).lower() not in seen:
            rows.append(row)
    return rows


def load_persona(name: str, workspace=None) -> dict | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for row in list_personas(workspace):
        if str(row.get("id", "")).lower() == needle or \
                str(row.get("name", "")).lower() == needle:
            return row
    return None


def resolve_persona_id(explicit: str = "", workspace=None) -> str:
    """--persona > AGENTS_PERSONA > workspace default_persona > 'default'."""
    pick = (explicit or "").strip() or os.environ.get("AGENTS_PERSONA", "").strip()
    if pick:
        return pick
    if workspace is not None and getattr(workspace, "default_persona", ""):
        return workspace.default_persona
    return "default"


def persona_system_append(name: str, *, base: str = "", workspace=None) -> str:
    row = load_persona(name, workspace)
    if row is None:
        return base
    parts = [p for p in (base, str(row.get("system_append") or "").strip()) if p]
    return "\n\n".join(parts)


def memory_mode(persona, workspace=None) -> str:
    """``"private"`` or ``"shared"`` (default). ``persona`` is a row or id."""
    row = persona if isinstance(persona, dict) else load_persona(str(persona or ""), workspace)
    if not isinstance(row, dict):
        return "shared"
    return "private" if str(row.get("memory") or "").strip().lower() == "private" else "shared"


def persona_memory_root(persona_id: str, workspace=None):
    """``<ws>/personas/<id>/memory`` — the private store root, or None."""
    pid = str(persona_id or "").strip()
    root = _workspace_personas_dir(workspace)
    if not pid or root is None:
        return None
    return Path(root) / pid / "memory"


def apply_memory_env(persona_id: str, env: dict, workspace=None) -> dict:
    """Private persona → ``AGENTS_MEMORY_PATH``/``AGENTS_MEMORY_DIR`` on its own root.

    Shared personas inherit the workspace memory env untouched. The private
    root is created so a later store open never fails on a missing dir.
    """
    if memory_mode(persona_id, workspace) != "private":
        return env
    root = persona_memory_root(persona_id, workspace)
    if root is None:
        return env
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return env
    env["AGENTS_MEMORY_PATH"] = str(root)
    env["AGENTS_MEMORY_DIR"] = str(root)
    return env


def private_memory_root_from_env(workspace=None):
    """Current persona's private root when ``AGENTS_PERSONA`` maps to memory=private."""
    pid = os.environ.get("AGENTS_PERSONA", "").strip()
    if not pid or memory_mode(pid, workspace) != "private":
        return None
    return persona_memory_root(pid, workspace)
