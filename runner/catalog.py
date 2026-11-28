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


COMPACT_SUMMARY_MAX_CHARS = 4000
_COMPACT_DOMAIN_VERB_LIMIT = 12
_COMPACT_VERB_CHARS = 220
_COMPACT_HEADER = (
    "Capability index (summary only; use list_catalog for full rows and "
    "load_schema for exact parameters):"
)
_COMPACT_CLIPPED = "[catalog summary clipped: use list_catalog for the full catalog]"


def catalog_full_enabled() -> bool:
    """Escape hatch for deployments that want the raw per-module prompt dump."""
    return os.environ.get("HARNESS_CATALOG_FULL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _row_name(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("name") or "").strip()
    if isinstance(row, (list, tuple)) and row:
        return str(row[0] or "").strip()
    return str(row or "").strip()


def _server_domain(server: Any) -> str:
    token = str(server or "").strip().lower().replace("_", "-")
    for prefix in ("mcp.", "agents-"):
        if token.startswith(prefix):
            token = token[len(prefix):]
    return f"mcp.{token}" if token else "mcp"


def _domain_for(row: dict[str, Any], names: set[str]) -> str:
    name = _row_name(row)
    if row.get("mcp_server"):
        return _server_domain(row.get("mcp_server"))
    exposed_from = str(row.get("exposed_from") or "").strip()
    if exposed_from:
        return exposed_from
    if not name:
        return "catalog"
    parts = name.split(".")
    if parts[0] == "mcp":
        if len(parts) <= 2:
            return name
        second = parts[1].replace("_", "-")
        if second.startswith("agents-"):
            return "mcp." + second[len("agents-"):]
        bases = sorted(
            (
                candidate
                for candidate in names
                if candidate.startswith("mcp.")
                and candidate.count(".") == 1
                and name.startswith(candidate + ".")
            ),
            key=len,
            reverse=True,
        )
        if bases:
            return bases[0]
        return ".".join(parts[:2])
    if parts[0] in {"a2a", "persona", "skill"}:
        return parts[0]
    return parts[0]


def catalog_domain(row: dict[str, Any], names: set[str] | None = None) -> str:
    """Canonical capability-domain key for one catalog module row."""
    if not isinstance(row, dict):
        return "catalog"
    return _domain_for(row, names or {_row_name(row)})


def _row_verbs(row: dict[str, Any], domain: str) -> list[str]:
    out: list[str] = []
    commands = row.get("commands")
    prompt_commands = row.get("_prompt_commands")
    for mapping in (commands, prompt_commands):
        if isinstance(mapping, dict):
            out.extend(str(key).strip() for key in mapping if str(key).strip())
    command_pin = str(row.get("command_pin") or "").strip()
    if command_pin:
        out.append(command_pin)
    mcp_tool = str(row.get("mcp_tool") or "").strip()
    if mcp_tool:
        out.append(mcp_tool)
    name = _row_name(row)
    if domain and name.startswith(domain + "."):
        suffix = name[len(domain) + 1 :].strip()
        if suffix:
            out.append(suffix.replace(".", " "))
    seen: set[str] = set()
    verbs: list[str] = []
    for verb in out:
        if verb not in seen:
            seen.add(verb)
            verbs.append(verb)
    return verbs


def _group_catalog(catalog: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    rows = [row for row in catalog if isinstance(row, dict) and _row_name(row)]
    names = {_row_name(row) for row in rows}
    grouped: dict[str, set[str]] = {}
    for row in rows:
        domain = _domain_for(row, names)
        grouped.setdefault(domain, set()).update(_row_verbs(row, domain))
    return {
        domain: sorted(verbs) if verbs else ["call"]
        for domain, verbs in sorted(grouped.items())
    }


def _compact_verb_text(verbs: list[str]) -> str:
    shown: list[str] = []
    used = 0
    for verb in verbs[:_COMPACT_DOMAIN_VERB_LIMIT]:
        cost = len(verb) + (2 if shown else 0)
        if shown and used + cost > _COMPACT_VERB_CHARS:
            break
        shown.append(verb)
        used += cost
    if not shown and verbs:
        shown = [verbs[0][:_COMPACT_VERB_CHARS]]
    hidden = len(verbs) - len(shown)
    text = ", ".join(shown)
    if hidden > 0:
        text += f", +{hidden} more"
    return text


def compact_catalog_rows(
    catalog: Iterable[dict[str, Any]],
    *,
    max_chars: int = COMPACT_SUMMARY_MAX_CHARS,
) -> list[tuple[str, str]]:
    """One compact prompt row per capability domain, globally budgeted."""
    rows: list[tuple[str, str]] = []
    for domain, verbs in _group_catalog(catalog).items():
        verb_text = _compact_verb_text(verbs)
        rows.append(
            (
                domain,
                f'{len(verbs)} verbs: {verb_text} — call list_catalog("{domain}") for schemas',
            )
        )
    if not rows or max_chars <= 0:
        return []

    def size(items: list[tuple[str, str]]) -> int:
        return sum(len(domain) + len(summary) + 2 for domain, summary in items)

    if size(rows) <= max_chars:
        return rows
    clipped = ("catalog", _COMPACT_CLIPPED)
    kept = list(rows)
    while kept and size(kept + [clipped]) > max_chars:
        kept.pop()
    kept.append(clipped)
    if size(kept) <= max_chars:
        return kept
    return [clipped]


def render_compact_catalog_summary(
    catalog: Iterable[dict[str, Any]],
    *,
    max_chars: int = COMPACT_SUMMARY_MAX_CHARS,
) -> str:
    """Render the progressive-disclosure capability index.

    The full catalog remains available through ``list_catalog`` and every exact
    parameter schema through ``load_schema``; the prompt only carries domain
    names and representative verbs. Output is bounded to ``max_chars`` so a
    large MCP fleet cannot crowd out the reasoning prefix.
    """
    grouped = _group_catalog(catalog)
    if not grouped or max_chars <= 0:
        return ""
    lines = [_COMPACT_HEADER]
    for domain, verbs in grouped.items():
        lines.append(
            f'{domain} ({len(verbs)} verbs): {_compact_verb_text(verbs)} — '
            f'call list_catalog("{domain}") for schemas'
        )

    def build(domain_lines: list[str], clipped: bool) -> str:
        parts = [lines[0], *domain_lines]
        if clipped:
            parts.append(_COMPACT_CLIPPED)
        return "\n".join(parts)

    domain_lines = lines[1:]
    text = build(domain_lines, False)
    if len(text) <= max_chars:
        return text
    kept = list(domain_lines)
    while kept:
        kept.pop()
        text = build(kept, True)
        if len(text) <= max_chars:
            return text
    text = build([], True)
    if len(text) <= max_chars:
        return text
    room = max(0, max_chars - len(_COMPACT_CLIPPED) - 1)
    return text[:room].rstrip() + "\n" + _COMPACT_CLIPPED
