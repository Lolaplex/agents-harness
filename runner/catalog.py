"""Persistent module-catalog snapshots for per-request loop processes.

Every chat turn starts a fresh ``runner.loop`` process. Module discovery is
therefore repeated unless the resolved catalog is stored on disk. This module
keeps a small workspace-scoped snapshot under ``<host>/cache`` and keys it by
the inputs that can change discovery: module manifests, MCP config layers,
active-skill state, probed suite packages, and the relevant env switches.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable

CACHE_VERSION = 1
DEFAULT_TTL_SEC = 300.0
_CACHE_MEM: dict[str, Any] = {"key": None, "fetched_at": 0.0, "modules": []}


def _host_dir() -> Path:
    try:
        from agents_workspace import Host

        return Host.from_env().dir
    except ImportError:
        raw = os.environ.get("AGENTS_HOST_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return (Path.home() / ".agents").resolve()


def _resolve_workspace(workspace: Any = None) -> Any:
    if workspace is not None:
        return workspace
    try:
        from .mcp_client import _resolve_ws_no_create

        return _resolve_ws_no_create()
    except Exception:
        return None


def catalog_cache_path(workspace: Any = None) -> Path:
    """Workspace-scoped catalog snapshot path under the host cache."""
    try:
        from agents_workspace import Host

        host = Host.from_env()
        cache_dir = host.cache
    except ImportError:
        cache_dir = _host_dir() / "cache"
    ws_id = str(getattr(workspace, "id", "") or "").strip() if workspace is not None else ""
    if not ws_id:
        raw = os.environ.get("AGENTS_WORKSPACE", "").strip()
        if raw and ".." not in raw:
            ws_id = raw
    if ws_id:
        return cache_dir / f"catalog-cache-{ws_id}.json"
    return cache_dir / "catalog-cache.json"


def cache_ttl() -> float:
    raw = os.environ.get("AGENTS_CATALOG_CACHE_SEC", "").strip()
    if not raw:
        return DEFAULT_TTL_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_SEC


def reset_cache() -> None:
    """Drop only the in-process snapshot; the disk snapshot stays intact."""
    _CACHE_MEM.clear()
    _CACHE_MEM["key"] = None
    _CACHE_MEM["fetched_at"] = 0.0
    _CACHE_MEM["modules"] = []


def _canon(path: Path) -> str:
    try:
        return os.path.normcase(str(path.expanduser().resolve(strict=False)))
    except (OSError, RuntimeError):
        return os.path.normcase(str(path))


def _stat_token(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": _canon(path), "mtime_ns": 0, "size": -1}
    return {"path": _canon(path), "mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _unique_paths(paths: Iterable[Path | None]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        key = _canon(Path(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(path))
    return out


def _module_inputs(module_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for folder in module_dirs:
        folder = Path(folder)
        rows.append({"module_dir": _canon(folder), "exists": folder.is_dir()})
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
            rows.append(_stat_token(path))
    return rows


def _mcp_inputs() -> list[dict[str, Any]]:
    try:
        from .mcp_client import _config_layers

        paths = _config_layers()
    except Exception:
        paths = ()
    return [_stat_token(path) for path in _unique_paths(paths)]


def _active_skill_inputs(workspace: Any = None) -> list[dict[str, Any]]:
    candidates: list[Path | None] = []
    ws = _resolve_workspace(workspace)
    if ws is not None:
        ws_dir = getattr(ws, "dir", None)
        skills_dir = getattr(ws, "skills", None)
        if ws_dir is not None:
            candidates.append(Path(ws_dir) / "active_skills.json")
        if skills_dir is not None:
            candidates.append(Path(skills_dir) / "active_skills.json")
    raw = os.environ.get("AGENTS_SKILLS_DIR", "").strip()
    if raw:
        candidates.append(Path(raw).expanduser() / "active_skills.json")
    extra = os.environ.get("AGENTS_SKILLS_EXTRA", "").strip()
    if extra:
        for part in extra.split(os.pathsep):
            part = part.strip()
            if part:
                candidates.append(Path(part).expanduser() / "active_skills.json")
    candidates.append(_host_dir() / "active_skills.json")
    return [_stat_token(path) for path in _unique_paths(candidates)]


def _package_inputs(packages: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in packages:
        pkg = str(raw).strip()
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        row: dict[str, Any] = {"package": pkg, "present": False}
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            row["present"] = True
            origin = getattr(spec, "origin", None)
            if origin and origin not in ("built-in", "frozen"):
                row["origin"] = _stat_token(Path(origin))
        rows.append(row)
    return rows


def catalog_fingerprint(
    module_dirs: Iterable[Path],
    *,
    workspace: Any = None,
    probed_packages: Iterable[str] = (),
) -> str:
    """Content-address the inputs whose changes invalidate the catalog."""
    ws = _resolve_workspace(workspace)
    payload = {
        "version": CACHE_VERSION,
        "workspace": str(getattr(ws, "id", "") or ""),
        "modules": _module_inputs(module_dirs),
        "mcp": _mcp_inputs(),
        "active_skills": _active_skill_inputs(workspace),
        "packages": _package_inputs(probed_packages),
        "env": {
            "AGENTS_CORDIS_EXPOSE": os.environ.get("AGENTS_CORDIS_EXPOSE", ""),
            "AGENTS_CORDIS_PKGS": os.environ.get("AGENTS_CORDIS_PKGS", ""),
            "AGENTS_MODULES_DIR": os.environ.get("AGENTS_MODULES_DIR", ""),
            "AGENTS_WORKSPACE": os.environ.get("AGENTS_WORKSPACE", ""),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_disk_cache(path: Path, key: str) -> list[dict[str, Any]] | None:
    ttl = cache_ttl()
    if ttl == 0 or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version") or 0) != CACHE_VERSION or data.get("key") != key:
        return None
    try:
        fetched = float(data.get("fetched_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if time.time() - fetched > ttl:
        return None
    modules = data.get("modules")
    if not isinstance(modules, list):
        return None
    return [copy.deepcopy(row) for row in modules if isinstance(row, dict)]


def _write_disk_cache(path: Path, key: str, modules: list[dict[str, Any]]) -> None:
    if cache_ttl() == 0:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "key": key,
            "fetched_at": time.time(),
            "modules": modules,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass


def cached_catalog_modules(
    *,
    module_dirs: Iterable[Path],
    loader: Callable[[], list[dict[str, Any]]],
    workspace: Any = None,
    probed_packages: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return a fresh-enough catalog snapshot, loading live only on a miss.

    A hit returns before ``loader`` runs, so no help-json subprocesses or MCP
    server sessions are started during prompt assembly.
    """
    dirs = list(module_dirs)
    path = catalog_cache_path(workspace)
    key = catalog_fingerprint(dirs, workspace=workspace, probed_packages=probed_packages)
    ttl = cache_ttl()
    mem_key = f"{_canon(path)}|{key}"
    if ttl != 0 and _CACHE_MEM.get("key") == mem_key:
        fetched = float(_CACHE_MEM.get("fetched_at") or 0.0)
        if time.time() - fetched <= ttl:
            return copy.deepcopy(list(_CACHE_MEM.get("modules") or []))

    disk = _read_disk_cache(path, key)
    if disk is not None:
        _CACHE_MEM["key"] = mem_key
        _CACHE_MEM["fetched_at"] = time.time()
        _CACHE_MEM["modules"] = copy.deepcopy(disk)
        return disk

    modules = loader()
    if ttl != 0:
        snapshot = copy.deepcopy(modules)
        _CACHE_MEM["key"] = mem_key
        _CACHE_MEM["fetched_at"] = time.time()
        _CACHE_MEM["modules"] = snapshot
        _write_disk_cache(path, key, snapshot)
    return modules
