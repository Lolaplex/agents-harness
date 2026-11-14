"""Workdir resolution and a shallow repo scan. No extra packages."""

from __future__ import annotations

import os
from pathlib import Path

# Dependency, VCS, and cache directories. Not a product layout.
DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def skip_dirs() -> set[str]:
    """``AGENTS_SEARCH_SKIP_DIRS`` replaces the default, including when empty."""
    if "AGENTS_SEARCH_SKIP_DIRS" in os.environ:
        raw = os.environ.get("AGENTS_SEARCH_SKIP_DIRS") or ""
        return {part.strip() for part in raw.split(os.pathsep) if part.strip()}
    from .settings import config_value

    found = config_value("search_skip_dirs")
    if isinstance(found, list):
        return {str(part).strip() for part in found if str(part).strip()}
    if isinstance(found, str) and found.strip():
        return {part.strip() for part in found.split(os.pathsep) if part.strip()}
    return set(DEFAULT_SKIP_DIRS)


def resolve_workdir(workspace: object | None = None) -> Path | None:
    """Active workdir. ``AGENTS_WORKDIR`` wins, else the workspace object."""
    if workspace is not None and getattr(workspace, "workdir", None):
        raw = str(getattr(workspace, "workdir"))
    else:
        raw = os.environ.get("AGENTS_WORKDIR", "").strip()
        if not raw:
            try:
                from agents_workspace import Workspace

                raw = str(Workspace.from_env().workdir)
            except Exception:
                return None
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def is_git_repo(path: Path) -> bool:
    git = path / ".git"
    try:
        return git.exists()
    except OSError:
        return False


def is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def confine(root: Path, user_path: str) -> Path | None:
    """Resolve ``user_path`` inside ``root``. ``None`` when it escapes."""
    text = (user_path or "").strip() or "."
    raw = Path(text).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        real = candidate.resolve(strict=False)
        root_real = root.resolve(strict=False)
        real.relative_to(root_real)
    except (OSError, RuntimeError, ValueError):
        return None
    return real


def relative_to_workdir(root: Path, path: Path) -> str:
    try:
        rel = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return path.name or "."
    text = rel.as_posix()
    return "." if text in ("", ".") else text


def find_git_repos(root: Path, *, max_depth: int = 2, max_repos: int = 8) -> list[Path]:
    """Git repos at ``root`` or within ``max_depth``. The root repo wins alone."""
    if not root.is_dir():
        return []
    if is_git_repo(root):
        return [root]
    skip = skip_dirs()
    found: list[Path] = []
    root_real = root.resolve(strict=False)
    for dirpath, dirnames, _names in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if not is_inside(root_real, current):
            dirnames.clear()
            continue
        try:
            rel = current.resolve(strict=False).relative_to(root_real)
        except (OSError, RuntimeError, ValueError):
            dirnames.clear()
            continue
        depth = 0 if rel.as_posix() in ("", ".") else len(rel.parts)
        if depth > max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name not in skip and not name.startswith(".") and not (current / name).is_symlink()
        ]
        if depth == 0:
            continue
        if is_git_repo(current):
            found.append(current)
            dirnames.clear()
            if len(found) >= max_repos:
                break
    return found
