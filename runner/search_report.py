"""Coverage line for grep, glob, and search results.

The harness does not search. The tool's own ``truncated`` flag and
``files_searched`` count win. When a finished search omits the count, a
bounded walk of the workdir fills ``files_searched``. A truncated search
without a count stays ``unknown`` so a short match list is not a complete one.

agents-terminal prints ``truncated`` on stderr and does not yet emit
``files_searched``. There is no ``glob`` command there yet. This module is
the generic report for whichever tool returns the rows.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any

from .settings import bool_setting, int_setting
from .workspace_tree import confine, is_inside, resolve_workdir, skip_dirs

SEARCH_COMMANDS = frozenset({"grep", "glob", "search"})
DEFAULT_SEARCH_MAX_FILES = 50000

SEARCH_NOTE = (
    "Search results (grep, glob, search) carry a harness line "
    '<search files_searched="..." truncated="0|1" matches="..."/> '
    "after the untrusted fence. "
    "files_searched is how many files were in scope. "
    "truncated 1 means the match list is not the full result. "
    "Do not claim a file, directory, or frontend is absent unless "
    'files_searched is an integer and truncated is 0. '
    "unknown or truncated 1 means the search is incomplete."
)

_SEARCH_LINE = re.compile(r"(?m)^[ \t]*<search\s[^>]*/>[ \t]*\n?")
_FILES_LINE = re.compile(
    r"(?i)^files_searched\s*[:=]\s*(\d+)\s*$"
)
_TRUNC_LINE = re.compile(
    r"(?i)^truncated\s*(?:[:=]\s*(true|false|1|0))?\s*$"
)


def strip_search_tags(text: str) -> str:
    """Drop tool-supplied coverage lines. The harness writes its own."""
    return _SEARCH_LINE.sub("", text or "")


def invoked_search(mod: Any, arguments: Any) -> str | None:
    """Return grep, glob, or search when this call is one of those commands."""
    from .helpjson import select_command, unwrap_arguments

    if isinstance(mod, dict):
        selected = select_command(mod, arguments)
        if selected is not None:
            name = str(selected[0] or "").strip().lower()
            if name in SEARCH_COMMANDS:
                return name
    args = unwrap_arguments(arguments)
    tokens: list[str] = []
    if isinstance(args, dict):
        for key in ("command_name", "subcommand", "command", "cmd"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                tokens = val.strip().split()
                break
        argv = args.get("argv")
        if isinstance(argv, list) and argv:
            tokens = [str(item) for item in argv]
    elif isinstance(args, list) and args:
        tokens = [str(item) for item in args]
    elif isinstance(args, str) and args.strip():
        tokens = args.strip().split()
    while tokens and tokens[0] in {"run", "--"}:
        tokens.pop(0)
    if tokens:
        head = tokens[0].strip().lower()
        if head in SEARCH_COMMANDS:
            return head
    if isinstance(mod, dict):
        tail = str(mod.get("name") or "").rsplit(".", 1)[-1].strip().lower()
        if tail in SEARCH_COMMANDS:
            return tail
    return None


def search_notice(
    mod: Any,
    arguments: Any,
    stdout: str,
    stderr: str = "",
    *,
    workspace: Any = None,
) -> str:
    """One coverage line, or empty when this call is not a search."""
    command = invoked_search(mod, arguments)
    if command is None:
        return ""
    reported = _from_streams(stdout or "", stderr or "")
    files = reported["files_searched"]
    truncated = reported["truncated"]
    matches = reported["matches"]
    if files is None and truncated is not True and _census_enabled(workspace):
        counted = _census_count(command, arguments, workspace=workspace)
        if counted is not None:
            visited, hits, hit_cap = counted
            if not hit_cap:
                files = visited
                if command == "glob" and hits > matches:
                    truncated = True
    if truncated is None:
        truncated = False
    files_text = "unknown" if files is None else str(int(files))
    flag = "1" if truncated else "0"
    return (
        f'<search files_searched="{files_text}" truncated="{flag}" '
        f'matches="{int(matches)}"/>'
    )


def _census_enabled(workspace: Any) -> bool:
    return bool_setting("AGENTS_SEARCH_CENSUS", "search_census", True, workspace=workspace)


def _max_files(workspace: Any) -> int:
    return int_setting(
        "AGENTS_SEARCH_MAX_FILES",
        "search_max_files",
        DEFAULT_SEARCH_MAX_FILES,
        minimum=1,
        workspace=workspace,
    )


def _from_streams(stdout: str, stderr: str) -> dict[str, Any]:
    files: int | None = None
    truncated: bool | None = None
    matches: int | None = None
    text = stdout.strip()
    data: Any = None
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        if "files_searched" in data:
            files = _as_int(data.get("files_searched"))
        if "truncated" in data:
            truncated = _as_bool(data.get("truncated"))
        matches = _list_len(data, ("matches", "files", "entries", "paths"))
    elif isinstance(data, list):
        matches = len(data)
    for line in stderr.splitlines():
        stripped = line.strip()
        found_files = _FILES_LINE.match(stripped)
        if found_files and files is None:
            files = int(found_files.group(1))
        found_trunc = _TRUNC_LINE.match(stripped)
        if found_trunc and truncated is None:
            token = (found_trunc.group(1) or "true").lower()
            truncated = token in {"true", "1"}
    if matches is None:
        rows = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("<page ") or stripped.startswith("<search "):
                continue
            if stripped.startswith("<ref "):
                continue
            rows.append(stripped)
        matches = len(rows)
    return {"files_searched": files, "truncated": truncated, "matches": matches}


def _list_len(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes"}:
            return True
        if token in {"0", "false", "no"}:
            return False
    return None


def _scope(command: str, arguments: Any) -> tuple[str | None, str]:
    """Return ``(glob, path)``. A grep pattern is not a path glob."""
    from .helpjson import unwrap_arguments

    args = unwrap_arguments(arguments)
    glob_pattern: str | None = None
    path = "."
    argv: list[str] = []
    if isinstance(args, dict):
        raw_glob = args.get("glob")
        if command == "glob" and not (isinstance(raw_glob, str) and raw_glob.strip()):
            raw_glob = args.get("pattern")
        if isinstance(raw_glob, str) and raw_glob.strip():
            glob_pattern = raw_glob.strip()
        raw_path = args.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            path = raw_path.strip()
        raw_argv = args.get("argv")
        if isinstance(raw_argv, list):
            argv = [str(item) for item in raw_argv]
        elif isinstance(args.get("command"), str):
            argv = str(args.get("command")).split()
    elif isinstance(args, list):
        argv = [str(item) for item in args]
    elif isinstance(args, str):
        argv = args.split()
    while argv and argv[0] in {"run", "--"}:
        argv.pop(0)
    if argv and argv[0].lower() in SEARCH_COMMANDS:
        argv = argv[1:]
    positional: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--glob" and index + 1 < len(argv):
            glob_pattern = argv[index + 1]
            index += 2
            continue
        if token.startswith("--glob="):
            glob_pattern = token.split("=", 1)[1]
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        positional.append(token)
        index += 1
    if command == "glob":
        if positional:
            glob_pattern = positional[0]
        if len(positional) > 1:
            path = positional[1]
    elif len(positional) >= 2:
        path = positional[1]
    return glob_pattern, path


def _census_count(
    command: str,
    arguments: Any,
    *,
    workspace: Any,
) -> tuple[int, int, bool] | None:
    root = resolve_workdir(workspace)
    if root is None or not root.is_dir():
        return None
    pattern, path = _scope(command, arguments)
    start = confine(root, path)
    if start is None or not start.exists():
        return None
    return _walk(root, start, pattern, _max_files(workspace))


def _glob_ok(rel: str, name: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(name, pattern)


def _walk(root: Path, start: Path, pattern: str | None, max_files: int) -> tuple[int, int, bool]:
    """Return (files_in_scope, glob_hits, hit_cap)."""
    if start.is_symlink():
        return 0, 0, False
    if start.is_file():
        rel = _rel(root, start)
        hit = _glob_ok(rel, start.name, pattern)
        return (1, 1, False) if hit else (0, 0, False)
    if not start.is_dir():
        return 0, 0, False
    skip = skip_dirs()
    considered = 0
    visited = 0
    root_real = root.resolve(strict=False)

    def _on_error(_exc: OSError) -> None:
        return None

    for dirpath, dirnames, filenames in os.walk(start, followlinks=False, onerror=_on_error):
        current = Path(dirpath)
        if not is_inside(root_real, current):
            dirnames.clear()
            continue
        dirnames.sort()
        filenames.sort()
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if name in skip or name.startswith(".") or child.is_symlink():
                continue
            if not is_inside(root_real, child):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = current / name
            if child.is_symlink() or not child.is_file():
                continue
            if not is_inside(root_real, child):
                continue
            visited += 1
            if visited > max_files:
                return considered, considered, True
            rel = _rel(root, child)
            if _glob_ok(rel, name, pattern):
                considered += 1
    return considered, considered, False


def _rel(root: Path, path: Path) -> str:
    try:
        rel = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return path.name
    text = rel.as_posix()
    return "." if text in ("", ".") else text
