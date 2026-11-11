"""Persona manifests: system prompt fragments without tool explosion.

Workspace personas (``<ws>/personas/<id>/persona.json``) win over package
personas (``runner/personas/<name>.json``) with the same id. Personas are
equal - a role (e.g. CoS) is a persona with a role description, not a
higher layer. A persona may keep optional private notes in
``<ws>/personas/<id>/notes.md``; workspace memory stays shared.
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


def _load_file(path: Path, name: str) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
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
            row = _load_file(manifest, entry.name)
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
