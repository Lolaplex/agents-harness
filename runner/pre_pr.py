"""Discover test and build commands from the project, then optionally run them.

Sources, and only these: ``package.json`` scripts, ``pyproject.toml``,
``Makefile`` targets, and fenced commands in ``AGENTS.md``. Nothing is
invented when those files do not name a command. ``run`` executes the
discovered argv. The model cannot pass its own command string.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .settings import int_setting
from .workspace_tree import (
    confine,
    find_git_repos,
    is_git_repo,
    relative_to_workdir,
    resolve_workdir,
)

PRE_PR_NOTE = (
    "Before opening a pull request, call_job repo.pre_pr. "
    "It discovers test and build commands from package.json scripts, "
    "pyproject.toml, the Makefile, and AGENTS.md. "
    "Pass run true to execute the discovered checks. "
    "Do not invent a test command when none was discovered. "
    "Pass path when several projects are present."
)

_KINDS = ("test", "build", "lint", "typecheck", "check")
_SCRIPT_NAMES = _KINDS
_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_HEADING_INTEREST = re.compile(
    r"\b(commands?|tests?|builds?|checks?|lint|typecheck)\b",
    re.IGNORECASE,
)
_PYTEST_TABLE = re.compile(r"(?m)^\[tool\.pytest(\.[A-Za-z0-9_.-]+)?\]\s*$")
_PYTEST_DEP = re.compile(
    r"""(?m)^[ \t]*["']?pytest["']?\s*(\[[^\]]*\])?\s*(>=|==|~=|<|>|,|$)"""
)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(\s|$)")
_SHELL_LANGS = frozenset({"", "bash", "sh", "shell", "console", "zsh", "fish"})
_SKIP_LANGS = frozenset(
    {"python", "py", "json", "yaml", "yml", "toml", "md", "text", "diff", "ts", "tsx", "js"}
)
_UNSAFE = ("|", "&&", "||", ";", ">", "<", "`", "$(", "\n", "\r")
_MAX_COMMANDS = 32
_MAX_RUN = 12
_STREAM_CAP = 100_000
_DEFAULT_TIMEOUT = 300
_MAX_TIMEOUT = 3600

_PROJECT_MARKERS = (
    "package.json",
    "pyproject.toml",
    "Makefile",
    "makefile",
    "AGENTS.md",
    "agents.md",
)


def pre_pr_report(arguments: Any, *, workspace: Any = None) -> tuple[str, bool]:
    """Return ``(json, ok)``. ``ok`` is false only when the call itself is refused."""
    args = arguments if isinstance(arguments, dict) else {}
    if set(args.keys()) == {"arguments"} and isinstance(args.get("arguments"), dict):
        args = args["arguments"]
    workdir = resolve_workdir(workspace)
    if workdir is None or not workdir.is_dir():
        return _dump({"ok": False, "error": "workdir is not a directory", "commands": []}), False
    selected, error, projects = _select_root(workdir, args.get("path"))
    if error:
        payload: dict[str, Any] = {"ok": False, "error": error, "commands": []}
        if projects:
            payload["projects"] = projects
        return _dump(payload), False
    assert selected is not None
    only = _only(args.get("only"))
    commands = _discover(selected)
    if only is not None:
        commands = [row for row in commands if row["kind"] in only]
    commands = commands[:_MAX_COMMANDS]
    rel = relative_to_workdir(workdir, selected)
    report: dict[str, Any] = {
        "ok": None,
        "root": rel,
        "ran": False,
        "commands": commands,
    }
    if projects and not commands:
        report["projects"] = projects
        report["message"] = (
            "Several projects declare checks. Pass path to list or run one."
        )
    if not commands:
        report["message"] = report.get("message") or (
            "No test or build command is declared in package.json scripts, "
            "pyproject.toml, Makefile, or AGENTS.md."
        )
    run = _flag(args.get("run"))
    if not run:
        return _dump(report), True
    runnable = [row for row in commands if row.get("runnable")][:_MAX_RUN]
    if not runnable:
        report["message"] = report.get("message") or "Nothing discovered is runnable."
        report["ok"] = False
        report["ran"] = False
        return _dump(report), True
    timeout = _timeout(workspace)
    results = [_run_one(selected, row, timeout) for row in runnable]
    report["ran"] = True
    report["results"] = results
    report["ok"] = all(item.get("exit_code") == 0 for item in results)
    return _dump(report), True


def _select_root(
    workdir: Path, raw_path: Any
) -> tuple[Path | None, str, list[dict[str, Any]]]:
    if isinstance(raw_path, str) and raw_path.strip():
        target = confine(workdir, raw_path.strip())
        if target is None:
            return None, "path escapes workspace root", []
        if not target.is_dir():
            return None, "path is not a directory", []
        return target, "", []
    if _is_project(workdir) or is_git_repo(workdir):
        return workdir, "", []
    repos = find_git_repos(workdir)
    projects = []
    for repo in repos:
        found = _discover(repo)
        if found:
            projects.append(
                {
                    "path": relative_to_workdir(workdir, repo),
                    "commands": found,
                }
            )
    if len(repos) == 1:
        return repos[0], "", projects
    if len(projects) == 1:
        only = confine(workdir, projects[0]["path"])
        if only is not None:
            return only, "", []
    if len(projects) > 1:
        return None, "multiple projects", projects
    return workdir, "", []


def _is_project(path: Path) -> bool:
    return any((path / name).is_file() for name in _PROJECT_MARKERS)


def _discover(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(_from_package_json(root))
    rows.extend(_from_pyproject(root))
    rows.extend(_from_makefile(root))
    rows.extend(_from_agents(root))
    seen: set[tuple[str, ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get("argv") or [])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _from_package_json(root: Path) -> list[dict[str, Any]]:
    path = root / "package.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    runner = _package_runner(root)
    rows: list[dict[str, Any]] = []
    for name in _SCRIPT_NAMES:
        script = scripts.get(name)
        if not isinstance(script, str) or not script.strip():
            continue
        rows.append(
            {
                "kind": name,
                "name": name,
                "source": "package.json",
                "argv": _pm_argv(runner, name),
                "script": script,
                "runnable": True,
            }
        )
    return rows


def _package_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
        return "bun"
    return "npm"


def _pm_argv(runner: str, name: str) -> list[str]:
    if runner == "yarn":
        return ["yarn", name]
    if runner == "bun":
        return ["bun", "run", name]
    if runner == "pnpm":
        return ["pnpm", "run", name]
    if name == "test":
        return ["npm", "test"]
    return ["npm", "run", name]


def _from_pyproject(root: Path) -> list[dict[str, Any]]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    if _PYTEST_TABLE.search(text) or _PYTEST_DEP.search(text):
        return [
            {
                "kind": "test",
                "name": "pytest",
                "source": "pyproject.toml",
                "argv": [sys.executable, "-m", "pytest"],
                "runnable": True,
            }
        ]
    return []


def _from_makefile(root: Path) -> list[dict[str, Any]]:
    path = root / "Makefile"
    if not path.is_file():
        path = root / "makefile"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line or line[0].isspace() or line.startswith("."):
            continue
        match = _MAKE_TARGET.match(line)
        if not match:
            continue
        name = match.group(1)
        if name not in _SCRIPT_NAMES:
            continue
        rows.append(
            {
                "kind": name,
                "name": name,
                "source": path.name,
                "argv": ["make", name],
                "runnable": True,
            }
        )
    return rows


def _from_agents(root: Path) -> list[dict[str, Any]]:
    path = root / "AGENTS.md"
    if not path.is_file():
        path = root / "agents.md"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for _title, body in _sections(text):
        for lang, block in _fences(body):
            head = lang.split()[0].lower() if lang.split() else ""
            if head in _SKIP_LANGS:
                continue
            if head not in _SHELL_LANGS:
                continue
            for line in block.splitlines():
                row = _command_line(line)
                if row is not None:
                    rows.append(row)
    return rows


def _sections(text: str) -> list[tuple[str, str]]:
    marks = list(_HEADING.finditer(text))
    sections: list[tuple[str, str]] = []
    for index, mark in enumerate(marks):
        title = mark.group(2).strip()
        if not _HEADING_INTEREST.search(title):
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        sections.append((title, text[mark.end() : end]))
    return sections


def _fences(body: str) -> list[tuple[str, str]]:
    return [(match.group(1).strip(), match.group(2)) for match in _FENCE.finditer(body)]


def _outside_quotes(text: str) -> str:
    """Drop quoted regions so a `;` inside `python -c` is not a shell operator."""
    out: list[str] = []
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        out.append(char)
    return "".join(out)


def _command_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("$ "):
        text = text[2:].strip()
    unsafe = any(token in _outside_quotes(text) for token in _UNSAFE)
    try:
        argv = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        return None
    if not argv:
        return None
    kind = _kind_of(argv)
    if kind not in _KINDS:
        return None
    return {
        "kind": kind,
        "name": " ".join(argv),
        "source": "AGENTS.md",
        "argv": argv,
        "runnable": not unsafe,
    }


def _kind_of(argv: list[str]) -> str:
    blob = " ".join(argv).lower()
    if any(token in blob for token in ("typecheck", "mypy", "tsc", "pyright")):
        return "typecheck"
    if any(token in blob for token in ("lint", "ruff", "eslint", "flake8")):
        return "lint"
    if any(token in blob for token in ("pytest", "unittest", "go test", "cargo test")) or re.search(
        r"\btest\b", blob
    ):
        return "test"
    if re.search(r"\bbuild\b", blob) or "cargo build" in blob:
        return "build"
    if any(token in blob for token in ("validate",)) or re.search(r"\bcheck\b", blob):
        return "check"
    return "other"


def _run_one(root: Path, row: dict[str, Any], timeout: int) -> dict[str, Any]:
    argv = [str(item) for item in row.get("argv") or []]
    result: dict[str, Any] = {
        "kind": row.get("kind"),
        "name": row.get("name"),
        "source": row.get("source"),
        "argv": argv,
        "exit_code": None,
        "status": "error",
        "stdout": "",
        "stderr": "",
    }
    if not argv:
        result["stderr"] = "empty argv"
        return result
    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        result["status"] = "timeout"
        result["stdout"] = _cap_stream(exc.stdout if isinstance(exc.stdout, str) else "")
        result["stderr"] = _cap_stream(exc.stderr if isinstance(exc.stderr, str) else "")
        return result
    except OSError as exc:
        result["stderr"] = str(exc)
        return result
    result["exit_code"] = int(proc.returncode)
    result["status"] = "ok" if proc.returncode == 0 else "failed"
    result["stdout"] = _cap_stream(proc.stdout or "")
    result["stderr"] = _cap_stream(proc.stderr or "")
    return result


def _only(value: Any) -> set[str] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return None
    kinds = {item.strip().lower() for item in items if item.strip()}
    return kinds or None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _timeout(workspace: Any) -> int:
    value = int_setting(
        "AGENTS_PRE_PR_TIMEOUT",
        "pre_pr_timeout_sec",
        _DEFAULT_TIMEOUT,
        minimum=1,
        workspace=workspace,
    )
    return min(value, _MAX_TIMEOUT)


def _cap_stream(text: str) -> str:
    if len(text) <= _STREAM_CAP:
        return text
    omitted = len(text) - _STREAM_CAP
    return text[:_STREAM_CAP] + f"\n[... {omitted} chars omitted ...]"


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
