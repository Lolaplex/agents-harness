"""Limits from the environment, then workspace config, then a default.

Env wins when it is set. ``<ws>/config.json`` is next, then ``<host>/agents.json``.
Missing files and broken JSON are ignored. A non-integer env value uses the
default and does not fall through to config.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def config_layers(workspace: Any = None) -> list[dict[str, Any]]:
    """Workspace config first, then the host agents file."""
    try:
        from agents_workspace import Workspace

        ws = workspace or Workspace.from_env()
    except Exception:
        return []
    host = getattr(ws, "host", None)
    paths = [getattr(ws, "config", None), getattr(host, "agents_json", None)]
    layers: list[dict[str, Any]] = []
    for path in paths:
        if isinstance(path, Path):
            data = _load_json(path)
            if data:
                layers.append(data)
    return layers


def config_value(key: str, workspace: Any = None) -> Any:
    for layer in config_layers(workspace):
        if key in layer and layer[key] not in (None, ""):
            return layer[key]
    return None


def int_setting(
    env_name: str,
    key: str,
    default: int,
    *,
    minimum: int = 0,
    workspace: Any = None,
) -> int:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return default
        if value < minimum:
            return default
        return value
    found = config_value(key, workspace)
    if isinstance(found, bool) or found is None:
        return default
    try:
        value = int(found)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def bool_setting(
    env_name: str,
    key: str,
    default: bool,
    *,
    workspace: Any = None,
) -> bool:
    raw = os.environ.get(env_name, "").strip().lower()
    if raw:
        if raw in _TRUTHY:
            return True
        if raw in _FALSY:
            return False
        return default
    found = config_value(key, workspace)
    if found is None:
        return default
    if isinstance(found, bool):
        return found
    if isinstance(found, int) and not isinstance(found, bool):
        return found != 0
    if isinstance(found, str):
        token = found.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
    return default
