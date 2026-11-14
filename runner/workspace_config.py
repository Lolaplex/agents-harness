"""Read host and workspace JSON config without creating a workspace.

Host ``agents.json`` is the shared layer. The selected workspace's
``config.json`` overrides it field by field. Missing files are empty.
This module does not call ``Workspace.from_env`` or ``select_workspace``,
both of which can create a default workspace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _host():
    try:
        from agents_workspace import Host
    except Exception:
        return None
    try:
        return Host.from_env()
    except Exception:
        return None


def _workspace_from_env(host):
    ws_id = os.environ.get("AGENTS_WORKSPACE", "").strip()
    try:
        if ws_id:
            if not host.workspace_exists(ws_id):
                return None
            return host.workspace(ws_id)
        found = host.list_workspaces()
    except Exception:
        return None
    if len(found) == 1:
        return found[0]
    return None


def config_documents(workspace=None) -> list[dict[str, Any]]:
    """Host document, then workspace document. Later overrides earlier."""
    if workspace is not None:
        docs: list[dict[str, Any]] = []
        host = getattr(workspace, "host", None)
        agents = getattr(host, "agents_json", None) if host is not None else None
        if agents is not None:
            docs.append(read_json(Path(agents)))
        cfg = getattr(workspace, "config", None)
        if cfg is not None:
            docs.append(read_json(Path(cfg)))
        return docs
    host = _host()
    if host is None:
        return []
    docs = [read_json(host.agents_json)]
    ws = _workspace_from_env(host)
    if ws is not None:
        docs.append(read_json(ws.config))
    return docs


def merged_section(name: str, workspace=None) -> dict[str, Any]:
    """Shallow-merge one object key across host then workspace."""
    merged: dict[str, Any] = {}
    for doc in config_documents(workspace):
        section = doc.get(name)
        if isinstance(section, dict):
            merged.update(section)
    return merged


def env_flag(name: str) -> bool | None:
    """``True`` / ``False`` when the variable is set to a boolean, else ``None``."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return None


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value
